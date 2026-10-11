"""线索库与在跟踪的商机同步到自托管的 Teable(多维表格),同事在表格里筛选、改状态、写跟进;表格里改的人管字段再拉回系统。

- 三张主表与系统里的商机一一对应(core.opportunity):「商机」表一个正式商机(在跟踪的,设了进展的,含已结束的)一行,
  与系统「商机」页是同一批;台账里其余的项目是 AI 推荐商机,不进表——原来的「项目台账」表第一次推送时改名为「商机」,
  不在跟踪的行删掉,优先级、负责人、内部备注在内部叠加里都还在;「线索」表的「所属商机」关联到它属于的那个商机,「跟踪进展」就是所属商机的进展(在线索上改,
  改的是商机的进展;还没有商机的,跟踪即入账建一个);「跟进记录」表一次跟进一行,关联商机,带对商机的判断与依据、
  跟进后的进展与下一步——它与系统「商机」页的「跟进与判断」是同一份(core.opportunity.work_log):表格里记的拉回
  (core.watch.events.add_follow),系统里记的推上去;表格里改了、删了的,系统里换成改后的、撤回(_pull_follows、push_follows)。
- 推送(sync,系统 → Teable,每天监测跑完、自动入账之后):第一次运行建库(profile 的 teable.base,缺省「水务项目机会」)、
  表和视图;以后按「编号」增量新增/更新。人管的字段(HUMAN_FIELDS:线索的处理状态、跟踪进展、负责人、下一步、下一步到期、
  备注、结果原因;商机的进展、优先级、负责人、下一步、下一步到期、内部备注)建记录时写初值(同时记进 teable_state.jsonl);
  之后按上次同步三方比较(_write_back):本机改了、表格自上次同步没人改过的写回表格(线索的处理状态、跟踪进展、备注,
  商机的进展、优先级、内部备注;负责人、下一步只在表格里设),表格改过的不覆盖。
- 拉回(pull,Teable → 系统,每天监测之前;profile 的 teable.pull 打开时):读两张表的人管字段,与 watch/teable_state.jsonl
  (每条记录上次同步时的值)比较,同事改了的写成操作记录——处理状态与备注(core.watch.query.set_status)、跟踪进展(set_stage:
  线索已属于商机的记在商机上;还没有的设成跟踪中等时跟踪即入账,core.ledger_auto.enter_ledger)、负责人(owner)、
  下一步与到期(next);商机表的进展、下一步记在商机上;跟进记录表新加的行记成商机的跟进;操作人记 teable:<显示名>
  (岗位代号表 private/team.yaml 里有的换成代号),操作人与时间按格取自记录历史(查不了时取记录的最后修改人与时间)。
  商机的优先级、负责人、内部备注写进
  private/overlay/<国别>.yaml 的 items.<编号>.{priority, owner, note},提交推送 main。还没记下过同步状态的记录:建好后只有
  系统动过的,只记下现值(本机此后改的,推送时写回);有同事改过的,按系统现值比,改了的拉回。同一字段本机与表格都改了:
  以表格为准,操作记录注明本机原来的值。

接口:REST,`Authorization: Bearer <个人访问令牌>`,路径都在 <TEABLE_URL>/api 下;字段按名字写(fieldKeyType=name),开 typecast,
日期传 YYYY-MM-DD、单选传选项名、关联传 [{"id": 记录ID}];成员字段读回来是 {id, title, email}(邮箱不存)。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import yaml
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.scalarstring import LiteralScalarString, SingleQuotedScalarString

from core import opportunity as opp
from core import sync as git
from core.ledger import (
    ELIG, MILESTONES, OVERLAY_SCHEMA, SEGMENTS, STAGES, VERIFY_STATUS, LedgerError, _refs, derive, load_ledger, load_overlay, load_rules,
    parse_pdate,
)
from core.ledger_auto import ACTIVE, enter_ledger, should_enter
from core.ledger_edit import _yaml
from core.markets import load_markets
from core.profile import TEABLE_DEFAULTS, Profile
from core.watch.events import STAGES as LEAD_STAGES
from core.watch.events import (
    JUDGMENTS, add_follow, add_note, current_next, current_owners, current_stages, set_next, set_owner, set_stage,
)
from core.watch.outputs import CAT_ZH, DEADLINE_ZH, SECTOR_ZH, deadlines
from core.watch.publish import read_summary
from core.watch.query import STATUS_ZH, load_rows, set_status, set_triage_note
from core.watch.store import SYSTEM, Store

BASE_NAME = TEABLE_DEFAULTS["base"]  # profile 的 teable.base 没写时用它
T_LEADS = "线索"
T_PROJECTS = "商机"  # 在跟踪的商机一行(core.opportunity)
T_PROJECTS_OLD = "项目台账"  # 原来的表名:第一次推送时改名为「商机」
T_FOLLOW = "跟进记录"  # 同事记的跟进:拉回写成商机的跟进(带判断)
T_DAILY = "每日摘要"  # 每次监测一行(推送时写)
T_REPORTS = "报告"  # 周报、月报等 Word 文件(推送时上传)
REPORT_KINDS = ("周报", "月报", "国别动态", "线索简报")
LINKS_FILE = "teable_links.json"  # 数据目录 watch/ 下:表格的网址、库与表的 ID、各条记录的 ID(本机界面"在表格里打开"用)
TZ = "Asia/Shanghai"
LEAD_HUMAN = ("处理状态", "跟踪进展", "负责人", "下一步", "下一步到期", "备注", "结果原因")
PROJECT_HUMAN = ("进展", "优先级", "负责人", "下一步", "下一步到期", "内部备注")
# 已有的表、字段改名(第一次推送新版时改;改不成就新建,并提示):表名 → {旧字段名: 新字段名}
RENAMES = {T_PROJECTS: {"阶段": "台账阶段", "阶段说明": "台账阶段说明"}, T_LEADS: {"台账项目": "所属商机"}, T_FOLLOW: {"项目": "商机"}}
HUMAN_FIELDS = set(LEAD_HUMAN) | set(PROJECT_HUMAN)  # 人管的字段:建记录时写入初值,之后由同事在表里维护,拉回系统
PRIORITIES = ("A", "B", "C")  # 商机表的优先级(拉回 overlay 的 priority)
STATE_FILE = "teable_state.jsonl"  # 数据目录 watch/ 下,只追加:每条记录每次同步时的人管字段值(见"同步状态")
BATCH = 200
_DATE = {"formatting": {"date": "YYYY-MM-DD", "time": "None", "timeZone": TZ}}
_USER = {"type": "user", "options": {"isMultiple": False, "shouldNotify": False}}  # 被设为负责人的站内通知缺省关;profile 的 teable.notify_owner 打开时推送里打开
_INT = {"formatting": {"type": "decimal", "precision": 0}}


class TeableError(RuntimeError):
    pass


def _select(choices, multi: bool = False) -> dict:
    return {"type": "multipleSelect" if multi else "singleSelect",
            "options": {"choices": [{"name": c} for c in choices], "preventAutoNewOptions": False}}


def project_fields(seed: dict[str, list[str]] | None = None) -> list[dict]:
    """商机表:一个在跟踪的商机一行。人管的列(进展、优先级、负责人、下一步、下一步到期、内部备注)同事在表里改,拉回系统;
    当前判断、最近跟进由跟进记录算出;其余是台账里的公开事实(系统写)。"""
    seed = seed or {}
    return [
        {"name": "名称", "type": "singleLineText"},  # 第一个字段是主字段,关联单元格里显示它
        {"name": "编号", "type": "singleLineText"},
        {"name": "国别", **_select(seed.get("国别", []))},
        {"name": "进展", **_select(LEAD_STAGES.values())},  # 人管:商机的跟踪进展
        {"name": "当前判断", "type": "longText"},  # 系统写:最近一次跟进里的判断与依据
        {"name": "最近跟进", "type": "longText"},  # 系统写:最近一次跟进(日期、内容)
        {"name": "台账阶段", **_select(STAGES.values())},  # 台账里的项目阶段(公开事实,与"进展"不同)
        {"name": "台账阶段说明", "type": "singleLineText"},
        {"name": "领域", **_select(SEGMENTS.values(), multi=True)},
        {"name": "资金方", "type": "singleLineText"},
        {"name": "规模(万美元)", "type": "number", "options": {"formatting": {"type": "decimal", "precision": 0}}},
        {"name": "最近节点", "type": "singleLineText"},
        {"name": "最近节点日期", "type": "date", "options": _DATE},
        {"name": "最晚启动", "type": "date", "options": _DATE},
        {"name": "距最晚启动(天)", "type": "number", "options": {"formatting": {"type": "decimal", "precision": 0}}},
        {"name": "资格", **_select(ELIG.values())},
        {"name": "资格依据", "type": "longText"},
        {"name": "核实状态", **_select(VERIFY_STATUS.values())},
        {"name": "核实日期", "type": "date", "options": _DATE},
        {"name": "参考项", "type": "checkbox"},
        {"name": "简介", "type": "longText"},
        {"name": "来源数", "type": "number", "options": {"formatting": {"type": "decimal", "precision": 0}}},
        {"name": "台账更新提醒", "type": "longText"},
        {"name": "中文名", "type": "singleLineText"},
        {"name": "英文名", "type": "singleLineText"},
        {"name": "所属线索", "type": "number", "options": _INT},
        {"name": "最近截止", "type": "date", "options": _DATE},  # 所属线索与采购标段里最近的未过截止日
        # 人管的列(同事在表里填;优先级、负责人、内部备注拉回 private/overlay 的 items.<编号>,下一步记在商机上)
        {"name": "优先级", **_select(PRIORITIES)},
        {"name": "负责人", **_USER},
        {"name": "下一步", "type": "singleLineText"},
        {"name": "下一步到期", "type": "date", "options": _DATE},
        {"name": "内部备注", "type": "longText"},
    ]


def lead_fields(projects_table_id: str, seed: dict[str, list[str]] | None = None) -> list[dict]:
    seed = seed or {}
    return [
        {"name": "标题", "type": "singleLineText"},
        {"name": "编号", "type": "singleLineText"},
        {"name": "处理状态", **_select(STATUS_ZH.values())},
        {"name": "跟踪进展", **_select(LEAD_STAGES.values())},
        {"name": "负责人", **_USER},
        {"name": "下一步", "type": "singleLineText"},
        {"name": "下一步到期", "type": "date", "options": _DATE},
        {"name": "备注", "type": "longText"},
        {"name": "结果原因", "type": "longText"},  # 人管:中标、未中标、放弃时填,拉回记进操作记录
        {"name": "国别", **_select(seed.get("国别", []))},
        {"name": "市场区域", **_select(seed.get("市场区域", []))},
        {"name": "领域", **_select(SECTOR_ZH.values(), multi=True)},
        {"name": "类别", **_select(CAT_ZH.values())},
        {"name": "相关", "type": "checkbox"},
        {"name": "截止日期", "type": "date", "options": _DATE},
        {"name": "截止类型", **_select(DEADLINE_ZH.values())},
        {"name": "发布日期", "type": "date", "options": _DATE},
        {"name": "发现日期", "type": "date", "options": _DATE},
        {"name": "所属商机", "type": "link", "options": {"relationship": "manyMany", "foreignTableId": projects_table_id}},
        {"name": "挂接", "type": "singleLineText"},
        {"name": "信源", **_select(seed.get("信源", []))},
        {"name": "原文", "type": "singleLineText", "options": {"showAs": {"type": "url"}}},
        {"name": "摘要", "type": "longText"},
        {"name": "资金方", "type": "singleLineText"},
        {"name": "项目名称", "type": "singleLineText"},
        {"name": "金额", "type": "singleLineText"},
        {"name": "判定", **_select(["模型", "规则"])},
        {"name": "表格链接", "type": "singleLineText", "options": {"showAs": {"type": "url"}}},  # 这条记录在表格里的网址(系统写)
    ]


def follow_fields(leads_table_id: str, projects_table_id: str) -> list[dict]:
    """跟进记录:同事每次跟进记一行,关联商机(没有商机的可以只关联线索),写做了什么、对商机的判断与依据、跟进后的进展与下一步。"""
    return [
        {"name": "内容", "type": "longText"},  # 主字段:做了什么、了解到什么
        {"name": "日期", "type": "date", "options": _DATE},
        {"name": "人", **_USER},
        {"name": "商机", "type": "link", "options": {"relationship": "manyOne", "foreignTableId": projects_table_id}},
        {"name": "线索", "type": "link", "options": {"relationship": "manyOne", "foreignTableId": leads_table_id}},
        {"name": "判断", **_select(JUDGMENTS.values())},
        {"name": "判断依据", "type": "longText"},
        {"name": "进展", **_select(LEAD_STAGES.values())},  # 跟进后商机的进展(不变可不填)
        {"name": "下一步", "type": "singleLineText"},
        {"name": "下一步到期", "type": "date", "options": _DATE},
    ]


def report_fields() -> list[dict]:
    return [
        {"name": "名称", "type": "singleLineText"},  # 主字段:文件名(不含扩展名),按它增量
        {"name": "日期", "type": "date", "options": _DATE},
        {"name": "类型", **_select(REPORT_KINDS)},
        {"name": "文件", "type": "attachment"},
        {"name": "指纹", "type": "singleLineText"},  # 系统写:传上去的文件内容的指纹,报告重新生成过就换新文件
    ]


def daily_fields(reports_table_id: str) -> list[dict]:
    return [
        {"name": "运行", "type": "singleLineText"},  # 主字段:监测运行的北京时间 YYYY-MM-DD HH:MM,每次运行一行,按它增量
        {"name": "日期", "type": "date", "options": _DATE},
        {"name": "新增相关线索", "type": "number", "options": _INT},
        {"name": "14天内截止", "type": "number", "options": _INT},
        {"name": "自动入账", "type": "number", "options": _INT},
        {"name": "信源异常", "type": "longText"},
        {"name": "报告", "type": "link", "options": {"relationship": "manyMany", "foreignTableId": reports_table_id}},
    ]


# ---------- REST 客户端 ----------

class Teable:
    def __init__(self, url: str, token: str, timeout: float = 60.0, client: httpx.Client | None = None):
        self.base = url.rstrip("/") + "/api"
        self.client = client or httpx.Client(timeout=timeout, headers={"Authorization": f"Bearer {token}"})

    def req(self, method: str, path: str, **kw):
        for attempt in range(4):
            r = self.client.request(method, self.base + path, **kw)
            if r.status_code in (429, 502, 503, 504) and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            if r.status_code >= 400:
                raise TeableError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
            return r.json() if r.content else None
        raise TeableError(f"{method} {path}: 重试后仍失败")

    # 空间、库、表
    def spaces(self) -> list[dict]:
        return self.req("GET", "/space") or []

    def bases(self, space_id: str) -> list[dict]:
        return self.req("GET", f"/space/{space_id}/base") or []

    def create_base(self, space_id: str, name: str) -> dict:
        return self.req("POST", "/base", json={"spaceId": space_id, "name": name})

    def tables(self, base_id: str) -> list[dict]:
        return self.req("GET", f"/base/{base_id}/table") or []

    def create_table(self, base_id: str, name: str, fields: list[dict], views: list[dict] | None = None) -> dict:
        body = {"name": name, "fields": fields, "views": views or [{"name": "全部", "type": "grid"}]}
        return self.req("POST", f"/base/{base_id}/table/", json=body)

    def rename_table(self, base_id: str, table_id: str, name: str) -> None:
        """表改名;令牌要有「表·更新」权限(安装时给了)。"""
        self.req("PUT", f"/base/{base_id}/table/{table_id}/name", json={"name": name})

    def fields(self, table_id: str) -> list[dict]:
        return self.req("GET", f"/table/{table_id}/field") or []

    def rename_field(self, table_id: str, field_id: str, name: str) -> None:
        """字段改名(类型、设置不变);令牌要有「字段·更新」权限(安装时给了)。"""
        self.req("PATCH", f"/table/{table_id}/field/{field_id}", json={"name": name})

    def create_field(self, table_id: str, spec: dict) -> dict:
        return self.req("POST", f"/table/{table_id}/field", json=spec)

    # 记录
    def records(self, table_id: str, take: int = 500, **params) -> list[dict]:
        out, skip = [], 0
        while True:
            page = self.req("GET", f"/table/{table_id}/record",
                            params={"fieldKeyType": "name", "take": take, "skip": skip, **params}) or {}
            recs = page.get("records") or []
            out.extend(recs)
            if len(recs) < take:
                return out
            skip += take

    def create_records(self, table_id: str, records: list[dict]) -> list[dict]:
        out = []
        for i in range(0, len(records), BATCH):
            body = {"fieldKeyType": "name", "typecast": True, "records": [{"fields": f} for f in records[i:i + BATCH]]}
            out.extend((self.req("POST", f"/table/{table_id}/record", json=body) or {}).get("records") or [])
        return out

    def delete_record(self, table_id: str, record_id: str) -> None:
        """删一条记录;令牌要有「记录·删除」权限(安装时给了)。"""
        self.req("DELETE", f"/table/{table_id}/record/{record_id}")

    def update_records(self, table_id: str, records: list[tuple[str, dict]]) -> None:
        for i in range(0, len(records), BATCH):
            body = {"fieldKeyType": "name", "typecast": True,
                    "records": [{"id": rid, "fields": f} for rid, f in records[i:i + BATCH]]}
            self.req("PATCH", f"/table/{table_id}/record", json=body)

    def convert_field(self, table_id: str, field_id: str, spec: dict) -> dict:
        """改字段的设置(类型不变时只改 options,如成员字段的"用户被选择后发送通知");令牌要有「字段·更新」权限。"""
        return self.req("PUT", f"/table/{table_id}/field/{field_id}/convert", json=spec)

    def upload_attachment(self, table_id: str, record_id: str, field_id: str, path: Path) -> dict:
        """把本机文件传到记录的附件字段(追加)。先读进内存,重试时可以重发。"""
        data = Path(path).read_bytes()
        return self.req("POST", f"/table/{table_id}/record/{record_id}/{field_id}/uploadAttachment",
                        files={"file": (Path(path).name, data, "application/octet-stream")})

    def record_history(self, table_id: str, record_id: str, field_ids: list[str] | None = None, cursor: str | None = None) -> dict:
        """一条记录的修改历史,新的在前、每页 20 条:{historyList: [{fieldId, createdBy, createdTime, …}], userMap, nextCursor}。
        令牌要有「记录·更新」权限(同步本来就要);整张表的历史要另一项权限,不用。"""
        params = [("fieldIds", f) for f in field_ids or []] + ([("cursor", cursor)] if cursor else [])
        return self.req("GET", f"/table/{table_id}/record/{record_id}/history", params=params) or {}

    def collaborators(self, base_id: str) -> list[dict]:
        """库的协作者 [{userId, userName, …}](把记录里的"最后修改人"账号 ID 换成显示名用)。"""
        r = self.req("GET", f"/base/{base_id}/collaborators", params={"take": 1000}) or []
        return r.get("collaborators") or [] if isinstance(r, dict) else r

    # 视图
    def views(self, table_id: str) -> list[dict]:
        return self.req("GET", f"/table/{table_id}/view") or []

    def create_view(self, table_id: str, spec: dict) -> dict:
        return self.req("POST", f"/table/{table_id}/view", json=spec)

    def set_filter(self, table_id: str, view_id: str, flt: dict) -> None:
        self.req("PUT", f"/table/{table_id}/view/{view_id}/filter", json={"filter": flt})

    def set_sort(self, table_id: str, view_id: str, sort_objs: list[dict]) -> None:
        self.req("PUT", f"/table/{table_id}/view/{view_id}/sort", json={"sort": {"sortObjs": sort_objs, "manualSort": False}})

    def set_group(self, table_id: str, view_id: str, group: list[dict]) -> None:
        self.req("PUT", f"/table/{table_id}/view/{view_id}/group", json={"group": group})


# ---------- 行的取法 ----------

def book_of(profile: Profile, store: Store) -> opp.Book:
    """商机全貌(与系统界面同一口径,core.opportunity.build)。"""
    return opp.book_for(profile, store)


def lead_stages(book: opp.Book, events: list[dict]) -> dict[str, dict]:
    """线索号 → 线索表「跟踪进展」对应的那条进展记录:属于在跟踪商机的取商机的进展(在哪儿设的都算),其余取线索自己的。"""
    out = dict(current_stages(events))
    tracked = book.tracked(events)
    for no, m in book.members.items():
        if m.item in tracked:
            out[no] = tracked[m.item]
    return out


def _next_deadline(book: opp.Book, iid: str, sigs: dict[str, dict], today: date) -> str | None:
    """商机最近的未过截止日:所属线索的截止日、台账里采购标段的截止日。"""
    days = [d for no in book.leads.get(iid, []) for d, _ in deadlines(sigs.get(no) or {})]
    for pk in (book.items[iid][1].get("procurement") or {}).get("packages") or []:
        if pk.get("deadline"):
            try:
                days.append(parse_pdate(pk["deadline"]).end.isoformat())
            except ValueError:
                pass
    days = sorted(d for d in days if d and d >= today.isoformat())
    return days[0] if days else None


def project_rows(profile: Profile, today: date, store: Store | None = None, book: opp.Book | None = None) -> list[dict]:
    """在跟踪的商机 → 商机表的行(字段名 → 值;进展、下一步这些人管的列放系统现值,只在建记录时写入)。
    store 为 None 时(只看台账,测试用)列出全部台账项目,不带跟踪的列。"""
    rules = load_rules(profile.rules) if profile.rules else load_rules()
    zh = {c.iso3: (c.name_zh or c.name_en) for c in profile.countries}
    book = (book or book_of(profile, store)) if store is not None else None
    events = store.events() if store is not None else []
    tracked = book.tracked(events) if book else None
    sigs = {s["id"][:8]: s for s in store.load_signals()} if store is not None else {}
    judg, nxt = opp.judgments(events, book.leads if book else None), current_next(events)
    out = []
    for c in profile.countries:
        if not c.ledger:
            continue
        led = load_ledger(c.ledger, rules)
        for d in derive(led, rules, profile.bidder, profile.lead_times, today):
            it, t, v = d.item, d.timing, (d.item.get("verification") or {})
            if tracked is not None and it["id"] not in tracked:
                continue
            node = t.node
            extra = {}
            if tracked is not None:
                iid = it["id"]
                last = opp.work_log(iid, events, book.leads.get(iid, []))
                f = last[-1] if last else None
                n = nxt.get(iid) or {}
                extra = {"进展": LEAD_STAGES.get(tracked[iid]["value"]), "当前判断": opp.judgment_text(judg.get(iid)) or None,
                         "最近跟进": (f"{f.get('day') or str(f.get('at') or '')[:10]}:"
                                      + (f"进展改为{LEAD_STAGES.get(f['value'], f['value'])}," if f.get("kind") == "stage" else "")
                                      + str(f.get("note") or ""))[:300] if f else None,
                         "所属线索": len(book.leads.get(iid, [])), "最近截止": _next_deadline(book, iid, sigs, today),
                         "下一步": n.get("value") or None, "下一步到期": n.get("due") or None}
            out.append({
                "名称": f"{it['id']} {it.get('name_zh') or it.get('name_en') or ''}".strip(),
                "编号": it["id"],
                "国别": zh.get(led.country, led.country),
                **extra,
                "台账阶段": STAGES.get(it.get("stage"), it.get("stage")),
                "台账阶段说明": it.get("stage_note") or None,
                "领域": [SEGMENTS.get(s, s) for s in it.get("segments") or []],
                "资金方": "、".join(sorted({str(f.get("funder")) for f in it.get("financing") or [] if f.get("funder")})) or None,
                "规模(万美元)": round(d.size_usd / 1e4) if d.size_usd else None,
                "最近节点": f"{MILESTONES.get(node.type, node.type)} {node.when.raw}" if node else None,
                "最近节点日期": node.when.start.isoformat() if node else None,
                "最晚启动": t.latest_start.isoformat() if t.latest_start else None,
                "距最晚启动(天)": t.days_left,
                "资格": ELIG.get(d.elig.status, d.elig.status),
                "资格依据": d.elig.basis or None,
                "核实状态": VERIFY_STATUS.get(v.get("status"), v.get("status")),
                "核实日期": str(v.get("checked_at")) if v.get("checked_at") else None,
                "参考项": not it.get("in_scope", True),
                "简介": it.get("brief") or None,
                "来源数": len(set(_refs(it))),
                "台账更新提醒": "\n".join(d.stale) or None,
                "中文名": it.get("name_zh") or None,
                "英文名": it.get("name_en") or None,
            })
    return out


def lead_rows(profile: Profile, store: Store, today: date, book: opp.Book | None = None) -> list[dict]:
    """线索库 → 线索表的行;「所属商机」先放商机编号(属于在跟踪的商机才有,商机表里只有在跟踪的),写入前换成记录 ID;
    「跟踪进展」是所属商机的进展(还没有商机的,线索自己的);「挂接」写系统认出的全部台账项目(●可靠 ○名称相近)。"""
    m = load_markets(profile.markets) if profile.markets and Path(profile.markets).exists() else None
    rows = load_rows(store, today, markets=m.region_map() if m else None)
    events = store.events()
    book = book or book_of(profile, store)
    tracked, stages = book.tracked(events), lead_stages(book, events)
    out = []
    for r in rows:
        mem = book.members.get(r["no"])
        stage = (stages.get(r["no"]) or {}).get("value", "")
        out.append({
            "标题": r["title"],
            "编号": r["id"],
            "处理状态": STATUS_ZH.get(r["status"], r["status"]),
            "跟踪进展": LEAD_STAGES.get(stage) or None,
            "备注": r.get("note") or None,
            "国别": r.get("country_zh") or r.get("country") or None,
            "市场区域": r.get("market") or None,
            "领域": [SECTOR_ZH.get(s, s) for s in r.get("sectors") or []],
            "类别": r.get("category_zh") or None,
            "相关": bool(r.get("relevant")),
            "截止日期": r.get("deadline") or None,
            "截止类型": DEADLINE_ZH.get(r.get("deadline_type"), r.get("deadline_type")) or None,
            "发布日期": r.get("published") or None,
            "发现日期": r.get("first_seen") or None,
            "所属商机": [mem.item] if mem and mem.item in tracked else [],
            "挂接": r.get("links") or None,
            "信源": r.get("source_name") or None,
            "原文": r.get("url") or None,
            "摘要": r.get("summary") or None,
            "资金方": r.get("funder") or None,
            "项目名称": r.get("project") or None,
            "金额": r.get("amount") or None,
            "判定": r.get("judged_by") or None,
        })
    return out


# ---------- 比较与增量 ----------

def _norm(v):
    """把 Teable 返回的单元格值和我们要写的值归一到可比较的形式。"""
    if v is None or v == "" or v is False or v == []:
        return None
    if isinstance(v, bool):
        return True
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, list):
        if v and isinstance(v[0], dict):  # 关联:[{id, title}]
            return frozenset(x.get("id") for x in v)
        return frozenset(str(x) for x in v)
    if isinstance(v, dict):  # 成员字段等
        return v.get("id") or str(v)
    s = str(v).strip()
    if len(s) >= 20 and s[4] == "-" and s[10] == "T":  # ISO 时间 → 上海日期(也接受 UTC 日期,见 _same)
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return ("date", dt.astimezone(ZoneInfo(TZ)).date().isoformat(), dt.date().isoformat())
        except ValueError:
            pass
    return s or None


def _same(old, new) -> bool:
    a, b = _norm(old), _norm(new)
    if isinstance(a, tuple) and isinstance(b, str):
        return b in a[1:]
    if isinstance(b, tuple) and isinstance(a, str):
        return a in b[1:]
    return a == b


def diff(existing: dict, wanted: dict, protect: set[str] = frozenset()) -> dict:
    """要改的字段(不含人管的字段)。"""
    return {k: v for k, v in wanted.items() if k not in protect and not _same(existing.get(k), v)}


def upsert(api: Teable, table_id: str, rows: list[dict], key: str = "编号", protect: set[str] = frozenset(),
           log=print, on_create=None, human=None, on_update=None, records: list[dict] | None = None) -> dict[str, str]:
    """按 key 增量新增/更新 → {key 值: 记录 ID}。同一编号有几条记录的,按最近改过的那条比(与拉回一致)。
    on_create(行列表):每建好一批就调一次(记人管字段的初值用,后面的批次或更新出错时,已建的也记下了)。
    human(key 值, 记录) → {人管字段: 值}:要写回的人管字段(见 _write_back),与其余字段的改动一起更新;
    on_update(key 值列表):每更新好一批就调一次(记下写回后的同步状态)。records:已经取回的表内记录(不再取一次)。"""
    existing: dict[str, dict] = {}
    for rec in api.records(table_id) if records is None else records:
        k = (rec.get("fields") or {}).get(key)
        if k and (str(k) not in existing
                  or str(rec.get("lastModifiedTime") or "") > str(existing[str(k)].get("lastModifiedTime") or "")):
            existing[str(k)] = rec
    creates, updates, n_back = [], [], 0
    for row in rows:
        k = str(row[key])
        rec = existing.get(k)
        if rec is None:
            creates.append({f: v for f, v in row.items() if v not in (None, [], "")})
            continue
        ch = diff(rec.get("fields") or {}, row, protect)
        back = human(k, rec) if human else {}
        n_back += bool(back)
        if ch or back:
            updates.append((k, rec["id"], {**ch, **back}))
    ids = {k: rec["id"] for k, rec in existing.items()}
    for i in range(0, len(creates), BATCH):
        batch = creates[i:i + BATCH]
        for rec in api.create_records(table_id, batch):
            ids[str((rec.get("fields") or {}).get(key))] = rec["id"]
        if on_create:
            on_create(batch)
    for i in range(0, len(updates), BATCH):
        batch = updates[i:i + BATCH]
        api.update_records(table_id, [(rid, ch) for _, rid, ch in batch])
        if on_update:
            on_update([k for k, _, _ in batch])
    log(f"  新增 {len(creates)},更新 {len(updates)}" + (f"(其中写回本机改的人管字段 {n_back} 条)" if n_back else "")
        + f",未变 {len(rows) - len(creates) - len(updates)}")
    return ids


# ---------- 建库、建表、建视图 ----------

def find_base(api: Teable, name: str = BASE_NAME) -> dict | None:
    """令牌能看到的空间里按名字找库;没有时 None。"""
    spaces = api.spaces()
    if not spaces:
        raise TeableError("令牌看不到任何空间:创建令牌时要勾选空间")
    return next((b for sp in spaces for b in api.bases(sp["id"]) if b.get("name") == name), None)


def ensure_base(api: Teable, name: str = BASE_NAME) -> dict:
    return find_base(api, name) or api.create_base(api.spaces()[0]["id"], name)


def ensure_table(api: Teable, base_id: str, name: str, fields: list[dict], log=print,
                 newest_first: str | None = None) -> tuple[dict, dict[str, dict]]:
    """表不存在就建(第一个字段为主字段;newest_first:新建时默认视图按这个字段倒序);存在就补缺的字段。→ (表, 字段名 → 字段)。"""
    tables = {t["name"]: t for t in api.tables(base_id)}
    created = name not in tables
    if created:
        log(f"  建表 {name}")
        tables[name] = api.create_table(base_id, name, fields)
    t = tables[name]
    have = {f["name"]: f for f in api.fields(t["id"])}
    for spec in fields:
        if spec["name"] not in have:
            log(f"  {name} 加字段 {spec['name']}")
            have[spec["name"]] = api.create_field(t["id"], spec)
    if created and newest_first and newest_first in have:
        try:
            views = api.views(t["id"])
            if views:
                api.set_sort(t["id"], views[0]["id"], [{"fieldId": have[newest_first]["id"], "order": "desc"}])
        except TeableError as e:
            log(f"  ! {name} 的默认排序没设上(可在界面里手工设):{e}")
    return t, have


def _seed(project_rows_: list[dict], lead_rows_: list[dict]) -> dict[str, list[str]]:
    def vals(rows, k):
        return sorted({r[k] for r in rows if r.get(k)})
    return {"国别": sorted(set(vals(project_rows_, "国别")) | set(vals(lead_rows_, "国别"))),
            "市场区域": vals(lead_rows_, "市场区域"), "信源": vals(lead_rows_, "信源")}


def _view(api: Teable, table_id: str, have: dict[str, dict], name: str, vtype: str = "grid", options: dict | None = None,
          flt: dict | None = None, sort: list[dict] | None = None, group: list[dict] | None = None) -> list[str]:
    """视图不存在就建并配好筛选/排序/分组;已有的不动。→ 警告列表。"""
    warns = []
    if name in have:
        return warns
    spec = {"name": name, "type": vtype}
    if options:
        spec["options"] = options
    v = api.create_view(table_id, spec)
    for what, fn in (("筛选", lambda: flt and api.set_filter(table_id, v["id"], flt)),
                     ("排序", lambda: sort and api.set_sort(table_id, v["id"], sort)),
                     ("分组", lambda: group and api.set_group(table_id, v["id"], group))):
        try:
            fn()
        except TeableError as e:
            warns.append(f"视图「{name}」的{what}没设上(可在界面里手工设):{e}")
    have[name] = v
    return warns


def ensure_views(api: Teable, leads: tuple[dict, dict], projects: tuple[dict, dict], follows: tuple[dict, dict] | None = None) -> list[str]:
    warns = []
    lt, lf = leads
    pt, pf = projects
    fid = lambda fields, n: fields[n]["id"]  # noqa: E731
    have = {v["name"]: v for v in api.views(lt["id"])}
    warns += _view(api, lt["id"], have, "待处理", flt={"conjunction": "and", "filterSet": [
        {"fieldId": fid(lf, "处理状态"), "operator": "is", "value": "待处理"},
        {"fieldId": fid(lf, "相关"), "operator": "is", "value": True}]},
        sort=[{"fieldId": fid(lf, "截止日期"), "order": "asc"}])
    warns += _view(api, lt["id"], have, "在跟踪", flt={"conjunction": "and", "filterSet": [
        {"fieldId": fid(lf, "跟踪进展"), "operator": "isNotEmpty", "value": None}]},
        sort=[{"fieldId": fid(lf, "下一步到期"), "order": "asc"}])
    warns += _view(api, lt["id"], have, "截止日历", vtype="calendar",
                   options={"startDateFieldId": fid(lf, "截止日期"), "endDateFieldId": fid(lf, "截止日期"), "titleFieldId": fid(lf, "标题")})
    warns += _view(api, lt["id"], have, "已处理", flt={"conjunction": "and", "filterSet": [
        {"fieldId": fid(lf, "处理状态"), "operator": "isAnyOf", "value": ["已处理", "已忽略"]}]},
        sort=[{"fieldId": fid(lf, "发现日期"), "order": "desc"}])
    warns += _view(api, lt["id"], have, "待分配", flt={"conjunction": "and", "filterSet": [
        {"fieldId": fid(lf, "相关"), "operator": "is", "value": True},
        {"fieldId": fid(lf, "负责人"), "operator": "isEmpty", "value": None}]},
        sort=[{"fieldId": fid(lf, "截止日期"), "order": "asc"}])
    week = lambda name: {"fieldId": fid(lf, name), "operator": "isWithIn",  # noqa: E731
                         "value": {"mode": "nextNumberOfDays", "numberOfDays": 7, "timeZone": TZ}}
    warns += _view(api, lt["id"], have, "本周到期", flt={"conjunction": "or", "filterSet": [week("下一步到期"), week("截止日期")]},
                   sort=[{"fieldId": fid(lf, "截止日期"), "order": "asc"}])
    warns += _view(api, lt["id"], have, "看板", vtype="kanban", options={"stackFieldId": fid(lf, "跟踪进展")},
                   flt={"conjunction": "and", "filterSet": [{"fieldId": fid(lf, "跟踪进展"), "operator": "isNotEmpty", "value": None}]})
    have = {v["name"]: v for v in api.views(pt["id"])}
    warns += _view(api, pt["id"], have, "按国别", group=[{"fieldId": fid(pf, "国别"), "order": "asc"}],
                   sort=[{"fieldId": fid(pf, "最晚启动"), "order": "asc"}])
    warns += _view(api, pt["id"], have, "节点日历", vtype="calendar",
                   options={"startDateFieldId": fid(pf, "最近节点日期"), "endDateFieldId": fid(pf, "最近节点日期"), "titleFieldId": fid(pf, "名称")})
    warns += _view(api, pt["id"], have, "优先项目", flt={"conjunction": "and", "filterSet": [
        {"fieldId": fid(pf, "优先级"), "operator": "isAnyOf", "value": ["A", "B"]}]},
        sort=[{"fieldId": fid(pf, "优先级"), "order": "asc"}, {"fieldId": fid(pf, "最晚启动"), "order": "asc"}])
    active = [LEAD_STAGES[k] for k in opp.ACTIVE]
    warns += _view(api, pt["id"], have, "在跟踪", flt={"conjunction": "and", "filterSet": [
        {"fieldId": fid(pf, "进展"), "operator": "isAnyOf", "value": active}]},
        sort=[{"fieldId": fid(pf, "优先级"), "order": "asc"}, {"fieldId": fid(pf, "下一步到期"), "order": "asc"}])
    warns += _view(api, pt["id"], have, "已结束", flt={"conjunction": "and", "filterSet": [
        {"fieldId": fid(pf, "进展"), "operator": "isAnyOf", "value": [LEAD_STAGES[k] for k in opp.CLOSED]}]})
    warns += _view(api, pt["id"], have, "看板", vtype="kanban", options={"stackFieldId": fid(pf, "进展")})
    warns += _view(api, pt["id"], have, "下一步日历", vtype="calendar",
                   options={"startDateFieldId": fid(pf, "下一步到期"), "endDateFieldId": fid(pf, "下一步到期"), "titleFieldId": fid(pf, "名称")})
    if follows:
        ft, ff = follows
        have = {v["name"]: v for v in api.views(ft["id"])}
        warns += _view(api, ft["id"], have, "按商机", group=[{"fieldId": fid(ff, "商机"), "order": "asc"}],
                       sort=[{"fieldId": fid(ff, "日期"), "order": "desc"}])
        warns += _view(api, ft["id"], have, "跟进日历", vtype="calendar",
                       options={"startDateFieldId": fid(ff, "日期"), "endDateFieldId": fid(ff, "日期"), "titleFieldId": fid(ff, "内容")})
    return warns


def _migrate(api: Teable, base_id: str, log=print) -> list[str]:
    """第一次推送新版时:「项目台账」表改名为「商机」,各表的旧字段名改成新的(RENAMES:台账阶段、所属商机、跟进记录的商机)。
    已经改过的跳过;改不成的(权限、接口)提示——之后照常按新名字建表、加字段,旧的请在表格里删掉。→ 警告"""
    warns: list[str] = []
    tables = {t["name"]: t for t in api.tables(base_id)}
    if T_PROJECTS not in tables and T_PROJECTS_OLD in tables:
        try:
            api.rename_table(base_id, tables[T_PROJECTS_OLD]["id"], T_PROJECTS)
            tables[T_PROJECTS] = tables.pop(T_PROJECTS_OLD)
            log(f"  表「{T_PROJECTS_OLD}」改名为「{T_PROJECTS}」(只留在跟踪的商机)")
        except (TeableError, httpx.HTTPError) as e:
            warns.append(f"表「{T_PROJECTS_OLD}」没能改名为「{T_PROJECTS}」,另建了「{T_PROJECTS}」表;旧表不再同步,请在表格里删掉:{str(e)[:120]}")
    for name, ren in RENAMES.items():
        if name not in tables:
            continue
        tid = tables[name]["id"]
        have = {str(f["name"]): f for f in api.fields(tid)}
        for old, new in ren.items():
            if old in have and new not in have:
                try:
                    api.rename_field(tid, have[old]["id"], new)
                    log(f"  {name} 的字段「{old}」改名为「{new}」")
                except (TeableError, httpx.HTTPError) as e:
                    warns.append(f"{name} 的字段「{old}」没能改名为「{new}」,另加了「{new}」;「{old}」不再同步,请在表格里删掉:{str(e)[:120]}")
    return warns


def _prune(api: Teable, table_id: str, keep: set[str], log=print) -> tuple[set[str], list[str]]:
    """商机表里不是正式商机的行(台账里 AI 推荐、没列入的项目,原项目台账表留下的)删掉;没填编号的行不动(可能是同事正在填的)。
    优先级、负责人、内部备注拉回后在内部叠加里,以后列入正式商机时随新行写回。→ (删掉的编号, 警告)"""
    removed: set[str] = set()
    warns: list[str] = []
    for rec in api.records(table_id):
        k = str((rec.get("fields") or {}).get("编号") or "")
        if not k or k in keep:
            continue
        try:
            api.delete_record(table_id, rec["id"])
            removed.add(k)
        except (TeableError, httpx.HTTPError) as e:
            warns.append(f"商机表里的 {k} 不是正式商机,没能删掉(可在表格里删,或在系统「商机」页把它列入正式商机):{str(e)[:120]}")
    if removed:
        log(f"  商机表:删掉不是正式商机的 {len(removed)} 行(台账里 AI 推荐、没列入的项目)")
    return removed, warns


def sync(profile: Profile, store: Store, today: date, url: str, token: str, base_name: str | None = None, log=print,
         api: Teable | None = None) -> dict:
    """一次完整同步(推送)→ 摘要。库名默认取 profile 的 teable.base;api 可换成测试替身。
    新建记录时写进去的人管字段初值,每建好一批就记进 watch/teable_state.jsonl(拉回时只把之后同事改的当作改动);
    已有的记录,本机自上次同步改了、表格没人改过的人管字段写回表格(_write_back),写成一批记一批。"""
    base_name = base_name or (profile.teable or {}).get("base") or BASE_NAME
    api = api or Teable(url, token)
    state = load_state(store)
    events = store.events()
    book = book_of(profile, store)
    tri, stages, tracked = store.triage(), lead_stages(book, events), book.tracked(events)
    ov = _overlay_view(profile)
    psys = lambda k: {**(ov.get(k) or _NO_OVERLAY), "stage": (tracked.get(k) or {}).get("value", "")}  # noqa: E731
    prow = project_rows(profile, today, store, book)
    for r in prow:  # 建商机记录时写进内部叠加里的优先级(A/B/C)与内部备注;负责人只在表格里设
        o = ov.get(r["编号"]) or _NO_OVERLAY
        r["优先级"] = o["priority"] if o["priority"] in PRIORITIES else None
        r["内部备注"] = o["note"] or None
    lrow = lead_rows(profile, store, today, book)
    seed = _seed(prow, lrow)
    base = ensure_base(api, base_name)
    log(f"库「{base_name}」({base['id']}):在跟踪的商机 {len(prow)},线索 {len(lrow)}")
    odd = [f"内部叠加里 {k} 的优先级「{o['priority']}」不是 A/B/C,表格里不显示" for k, o in sorted(ov.items())
           if k in tracked and o["priority"] and o["priority"] not in PRIORITIES]
    odd += _migrate(api, base["id"], log)
    pt, pf = ensure_table(api, base["id"], T_PROJECTS, project_fields(seed), log)
    p_back = _Back(store, "project", state["projects"], psys, PROJECT_WB, PROJECT_HUMAN)
    pids = upsert(api, pt["id"], prow, protect=HUMAN_FIELDS, log=log, human=p_back.human, on_update=p_back.written,
                  on_create=lambda rows: append_state(store, [_project_line(str(r["编号"]), _human(r, PROJECT_HUMAN), psys(str(r["编号"])))
                                                              for r in rows]))
    p_back.flush()
    removed, w = _prune(api, pt["id"], {r["编号"] for r in prow}, log)
    odd += w
    pids = {k: v for k, v in pids.items() if k not in removed}
    lt, lf = ensure_table(api, base["id"], T_LEADS, lead_fields(pt["id"], seed), log)
    for r in lrow:
        r["所属商机"] = [{"id": pids[i]} for i in r["所属商机"] if i in pids]
    lrecs = api.records(lt["id"])
    known = _latest_per_key(lrecs)
    for r in lrow:  # 表格链接:按统一写法比(各处填的地址大小写、末尾斜杠不同也不来回改;换了域名的跟着改);新建的建好后补
        rec = known.get(r["编号"])
        cur = str((rec or {}).get("fields", {}).get("表格链接") or "")
        want = record_url(_canon_url(url), base["id"], lt["id"], rec["id"]) if rec else None
        r["表格链接"] = (cur if _canon_url(cur) == want else want) if rec else None
    l_back = _Back(store, "lead", state["leads"], lambda k: _sys_view(tri, stages, k), LEAD_WB, LEAD_HUMAN)
    lids = upsert(api, lt["id"], lrow, protect=HUMAN_FIELDS, log=log, human=l_back.human, on_update=l_back.written, records=lrecs,
                  on_create=lambda rows: append_state(store, [_lead_line(str(r["编号"]), _human(r, LEAD_HUMAN), _pushed_sys(r))
                                                              for r in rows]))
    l_back.flush()
    tables = {T_LEADS: lt["id"], T_PROJECTS: pt["id"]}
    save_links(store, url, base["id"], tables, lids, pids)  # 先记下:后面哪一步出错,本机界面的链接照样有
    fresh = [(rid, {"表格链接": record_url(_canon_url(url), base["id"], lt["id"], rid)}) for k, rid in lids.items() if k not in known]
    if fresh:
        api.update_records(lt["id"], fresh)
    ft, ff = ensure_table(api, base["id"], T_FOLLOW, follow_fields(lt["id"], pt["id"]), log, newest_first="日期")
    tables[T_FOLLOW] = ft["id"]  # 系统里嵌入"跟进记录"要用它的 ID
    warns = ensure_views(api, (lt, lf), (pt, pf), (ft, ff)) + odd
    if (profile.teable or {}).get("notify_owner"):
        warns += owner_notify(api, ((lt, lf), (pt, pf)), log)
    save_links(store, url, base["id"], tables, lids, pids)
    try:  # 跟进记录与系统是同一份:系统里记的推上去(出了问题只记警告,下次再推,不影响线索与商机)
        fsum = push_follows(api, ft, store, book, pids, lids, state, load_team(profile), log)
    except (TeableError, httpx.HTTPError) as e:
        fsum = {}
        warns.append(f"跟进记录没推成(下次再试):{str(e)[:200]}")
    uploaded: dict[str, str] = {}
    try:  # 报告与每日摘要出问题不影响线索、台账的同步:记成警告,下次再试
        rt, rf = ensure_table(api, base["id"], T_REPORTS, report_fields(), log, newest_first="日期")
        uploaded = push_reports(api, rt, rf, store.root.parent, log, warns, internal=bool((profile.teable or {}).get("internal_reports")))
        dt, _ = ensure_table(api, base["id"], T_DAILY, daily_fields(rt["id"]), log, newest_first="运行")
        push_daily(api, dt, read_summary(store.root.parent), uploaded, log)
        save_links(store, url, base["id"], {**tables, T_REPORTS: rt["id"], T_DAILY: dt["id"]}, lids, pids)
    except Exception as e:  # noqa: BLE001 — 这一步可有可无,任何问题都只记警告
        warns.append(f"报告或每日摘要没写成(下次再试):{type(e).__name__} {str(e)[:200]}")
    for w in warns:
        log("  ! " + w)
    return {"base": base["id"], "projects": len(pids), "leads": len(lids), "written_back": p_back.n + l_back.n,
            "follows": fsum.get("created", 0), "reports": sorted(uploaded), "warnings": warns,
            "url": f"{url.rstrip('/')}/base/{base['id']}"}


def owner_notify(api: Teable, tables: tuple[tuple[dict, dict[str, dict]], ...], log=print) -> list[str]:
    """profile 的 teable.notify_owner 打开时:线索表、项目台账「负责人」字段的"用户被选择后发送通知"打开——同事被设为负责人时,
    表格右上角的铃铛里收到提醒(自己设自己不提醒;主机配了 SMTP 时同时发邮件)。已打开的不动;没打开成的提示,可在表格里手动打开。
    → 警告"""
    warns: list[str] = []
    for t, fields in tables:
        f = fields.get("负责人")
        opts = dict((f or {}).get("options") or {})
        if not f or opts.get("shouldNotify"):
            continue
        try:
            api.convert_field(t["id"], f["id"], {"type": "user", "options": {**opts, "shouldNotify": True}})
            log(f"  {t['name']} 的负责人:打开被设为负责人时的站内通知")
        except (TeableError, httpx.HTTPError) as e:
            warns.append(f"{t['name']} 的负责人通知没打开(可在表格里手动打开:列头 → 编辑字段 → 用户被选择后发送通知):{str(e)[:120]}")
    return warns


def _latest_per_key(records: list[dict], key: str = "编号") -> dict[str, dict]:
    """key 值 → 记录;同一 key 有几条的取最近改过的(与 upsert、拉回一致)。"""
    out: dict[str, dict] = {}
    for rec in records:
        k = str((rec.get("fields") or {}).get(key) or "")
        if k and (k not in out or str(rec.get("lastModifiedTime") or "") > str(out[k].get("lastModifiedTime") or "")):
            out[k] = rec
    return out


def record_url(url: str, base_id: str, table_id: str, record_id: str) -> str:
    """表格里一条记录的网址(与 Teable 界面"复制记录链接"同一格式),打开即展开这条记录。"""
    return f"{url.rstrip('/')}/base/{base_id}/table/{table_id}?recordId={record_id}"


def _canon_url(url: str) -> str:
    """表格地址的统一写法(去掉首尾空白与末尾斜杠,协议与主机小写),各处填的写法略有不同也不来回改。"""
    u = url.strip().rstrip("/")
    m = re.match(r"^([A-Za-z]+://[^/]+)(.*)$", u)
    return (m.group(1).lower() + m.group(2)) if m else u


def save_links(store: Store, url: str, base_id: str, tables: dict[str, str], leads: dict[str, str], projects: dict[str, str]) -> None:
    """表格的网址、库与表的 ID、各条记录的 ID 写进 watch/teable_links.json(没变就不写,免得每天多一次提交)。"""
    data = {"url": _canon_url(url), "base": base_id, "tables": tables, "leads": dict(sorted(leads.items())),
            "projects": dict(sorted(projects.items()))}
    fp = store.root / LINKS_FILE
    text = json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    if not fp.exists() or fp.read_text(encoding="utf-8") != text:
        tmp = fp.with_suffix(".json.tmp")  # 先写临时文件再换上:系统页面同时在读,读不到半截
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(fp)


def load_links(store: Store) -> dict:
    """watch/teable_links.json → {url, base, tables, leads, projects};没有或读不出时空。"""
    fp = store.root / LINKS_FILE
    try:
        d = json.loads(fp.read_text(encoding="utf-8")) if fp.exists() else {}
    except (OSError, json.JSONDecodeError):
        return {}
    return d if isinstance(d, dict) else {}


def lead_link(links: dict, sid: str) -> str | None:
    """线索在表格里的网址(推送过的才有)。"""
    rid = (links.get("leads") or {}).get(sid)
    t = (links.get("tables") or {}).get(T_LEADS)
    return record_url(links["url"], links["base"], t, rid) if rid and t and links.get("url") and links.get("base") else None


# ---------- 报告与每日摘要(推送时写) ----------

REPORT_DIRS = ("weekly", "monthly")


def _report_kind(stem: str) -> str | None:
    if "_周报_" in stem:
        return "周报"
    if "_项目机会分析_" in stem:
        return "月报"
    if stem.startswith("国别报告_"):
        return "国别动态"
    if stem.startswith("线索简报_"):
        return "线索简报"
    return None


def _report_date(stem: str) -> str | None:
    """文件名里的日期;周报(2026-W39)取那周的星期日;认不出的为 None。"""
    try:
        if m := re.search(r"(\d{4}-\d{2}-\d{2})", stem):
            return date.fromisoformat(m.group(1)).isoformat()
        if m := re.search(r"(\d{4})-W(\d{2})", stem):
            return date.fromisocalendar(int(m.group(1)), int(m.group(2)), 7).isoformat()
    except ValueError:
        return None
    return None


INTERNAL_KINDS = ("国别动态", "线索简报")  # 含市场分布表里的内容(经营区域、使用方在该国)


def report_files(root: Path, internal: bool = False) -> list[tuple[str, str, str | None, Path]]:
    """数据目录 reports/weekly、reports/monthly 下的 Word 报告 → [(名称, 类型, 日期, 路径)](认不出类型的、Word 的临时文件不传)。
    含内部信息的缺省不传:月报内部版(内部叠加的判断与非公开信息)、国别动态与线索简报(市场分布表里的区域与我方情况);
    缺省只传周报与月报公开版(…_公开版)。internal=True(profile 的 teable.internal_reports)全部都传。"""
    out = []
    for d in REPORT_DIRS:
        folder = Path(root) / "reports" / d
        for fp in sorted(folder.glob("*.docx")) if folder.exists() else []:
            kind = _report_kind(fp.stem)
            if not kind or fp.name.startswith("~$"):
                continue
            if not internal and (kind in INTERNAL_KINDS or (kind == "月报" and not fp.stem.endswith("_公开版"))):
                continue
            out.append((fp.stem, kind, _report_date(fp.stem), fp))
    return out


def _digest(fp: Path) -> str:
    return hashlib.sha1(Path(fp).read_bytes()).hexdigest()[:16]


def push_reports(api: Teable, table: dict, fields: dict[str, dict], root: Path, log=print, warns: list[str] | None = None,
                 internal: bool = False) -> dict[str, str]:
    """报告表:每份 Word 报告一行(按名称增量),文件传到「文件」附件,「指纹」记下传的是哪一版;内容没变的不再传,
    重新生成过的(同名、内容变了,如周中手动出的周报被周一的完整版替换)换成新文件,上次没传成的这次再传。
    internal:含内部信息的报告也传(见 report_files)。→ {名称: 记录 ID}(这次传上去的)。"""
    files = report_files(root, internal)
    if not files:
        return {}
    existing = _latest_per_key(api.records(table["id"]), "名称")
    ids = upsert(api, table["id"], [{"名称": n, "类型": k, "日期": d} for n, k, d, _ in files], key="名称", log=lambda *_: None,
                 records=list(existing.values()))
    out: dict[str, str] = {}
    for name, _, _, fp in files:
        f = (existing.get(name) or {}).get("fields") or {}
        try:
            h = _digest(fp)
            if f.get("文件") and f.get("指纹") == h:
                continue
            if f.get("文件"):  # 旧版换掉:先清空附件再传(上传是追加)
                api.update_records(table["id"], [(ids[name], {"文件": None})])
            api.upload_attachment(table["id"], ids[name], fields["文件"]["id"], fp)
            api.update_records(table["id"], [(ids[name], {"指纹": h})])
            out[name] = ids[name]
        except (TeableError, httpx.HTTPError, OSError) as e:
            (warns if warns is not None else []).append(f"报告 {name} 没传上去(下次再传):{str(e)[:160]}")
    if out:
        log(f"  报告:传上去 {len(out)} 份({'、'.join(out)})")
    return out


def _run_label(summary: dict) -> str:
    """监测运行的北京时间 YYYY-MM-DD HH:MM(每日摘要一行的键);没有运行时间的取日期。"""
    t = _ts(summary.get("at"))
    return t.astimezone(ZoneInfo(TZ)).strftime("%Y-%m-%d %H:%M") if t else str(summary.get("date") or "")


def push_daily(api: Teable, table: dict, summary: dict, reports: dict[str, str], log=print) -> None:
    """每日摘要:监测运行摘要(watch/summary.json)每次运行写一行(键为运行时间,同一次运行再推送不另加);
    这次传上去的报告挂在这行的「报告」里(已挂的保留)。"""
    if not summary.get("date"):
        return
    key = _run_label(summary)
    recs = api.records(table["id"])
    old = _latest_per_key(recs, "运行").get(key)
    row = {"运行": key, "日期": summary["date"], "新增相关线索": summary.get("new_relevant"), "14天内截止": summary.get("due_14d"),
           "自动入账": summary.get("autoledger"), "信源异常": "\n".join(summary.get("source_errors") or []) or None}
    if reports:
        have = [x.get("id") for x in (old or {}).get("fields", {}).get("报告") or [] if isinstance(x, dict)]
        row["报告"] = [{"id": i} for i in dict.fromkeys([*have, *reports.values()]) if i]
    upsert(api, table["id"], [row], key="运行", log=lambda *_: None, records=recs)
    log(f"  每日摘要:{key} 新增相关 {row['新增相关线索'] or 0} 条")


# ---------- 同步状态:watch/teable_state.jsonl ----------
# 只追加(data 分支 union 合并,定时任务与本机同时写也不冲突;中途出错,已写的行照样有效):
#   {at, t: lead, k: 编号, v: {人管字段: 值}, s: {status, stage, note}, enter?}  这条线索这次同步后表格里的值,以及与之对应的
#       系统值(处理状态、进展、处理备注);推送与拉回都按它三方比较——表格自此改了的拉回(本机也改了即冲突,以表格为准),
#       本机自此改了、表格没改的写回。enter=pending 为跟踪即入账推送失败、下次再试
#   {at, t: project, k: 编号, v: {…}, s: {priority, note}}  项目的人管列与内部叠加里的优先级、内部备注
#   {at, t: follow, k: 记录 ID}  这条跟进记录已拉回(写成了操作记录)
#   {at, t: pull, events, conflicts}  一次拉回的摘要(本机界面据此提示冲突)
# 同一记录取 at 最晚的一行。早期的行 s 里没有 note(项目行没有 s)的,那几项按"本机没改"补记。

_STATUS_CODE = {v: k for k, v in STATUS_ZH.items()}  # 待处理 → new
_STAGE_CODE = {v: k for k, v in LEAD_STAGES.items()}  # 跟踪中 → tracking
_SKIP = object()  # 系统值在表格里写不了(如不是 A/B/C 的优先级):不写回
# 写回的字段:表格字段 → (系统值的键, 系统值 → 表格里写的值)。负责人、下一步只在表格里设,不写回
LEAD_WB = {"处理状态": ("status", lambda c: STATUS_ZH.get(c) or _SKIP),
           "跟踪进展": ("stage", lambda c: (LEAD_STAGES.get(c) or _SKIP) if c else None),
           "备注": ("note", lambda n: n or None)}
PROJECT_WB = {"进展": ("stage", lambda c: (LEAD_STAGES.get(c) or _SKIP) if c else None),
              "优先级": ("priority", lambda p: (p if p in PRIORITIES else _SKIP) if p else None),
              "内部备注": ("note", lambda n: n or None)}
_NO_OVERLAY = {"priority": "", "note": ""}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _lead_line(key: str, v: dict, s: dict, enter: str | None = None) -> dict:
    return {"at": _now(), "t": "lead", "k": key, "v": v, "s": s, **({"enter": enter} if enter else {})}


def _project_line(key: str, v: dict, s: dict) -> dict:
    return {"at": _now(), "t": "project", "k": key, "v": v, "s": s}


def append_state(store: Store, lines: list[dict]) -> None:
    if lines:
        with (store.root / STATE_FILE).open("a", encoding="utf-8") as f:
            for r in lines:
                f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")


def load_state(store: Store, until: datetime | None = None) -> dict:
    """→ {leads: {编号: 最新一行}, projects: {…}, follows: {已拉回的跟进记录 ID}, fpush: {推上去的跟进的键: 记录 ID},
    fgone: {表格里删掉、系统里已撤回的跟进记录 ID}, flost: {表格恢复备份后没了、下次推送重建的跟进记录 ID},
    last_pull: 最近一次拉回的摘要}。
    同一记录取 at 最晚的一行(两处写的行合并后先后不定);坏行(同步合并留下的等)跳过。
    until:只看这个时刻以前(含)写的行,即那时的同步状态(恢复后对齐用)。"""
    out: dict = {"leads": {}, "projects": {}, "follows": set(), "fpush": {}, "fgone": set(), "flost": set(), "last_pull": {}}
    fp = store.root / STATE_FILE
    for line in fp.read_text(encoding="utf-8").splitlines() if fp.exists() else []:
        try:
            r = json.loads(line) if line.strip() else None
        except json.JSONDecodeError:
            continue
        if not isinstance(r, dict):
            continue
        if until is not None and (_ts(r.get("at")) is None or _ts(r.get("at")) > until):
            continue
        at = str(r.get("at") or "")
        if r.get("t") == "pull":
            if at >= str(out["last_pull"].get("at") or ""):
                out["last_pull"] = r
        elif r.get("t") == "follow" and r.get("k"):
            out["follows"].add(str(r["k"]))
        elif r.get("t") == "fpush" and r.get("k") and r.get("rid"):
            out["fpush"][str(r["k"])] = str(r["rid"])
        elif r.get("t") in ("fgone", "flost") and r.get("k"):
            out[r["t"]].add(str(r["k"]))
        elif r.get("t") in ("lead", "project") and r.get("k") and isinstance(r.get("v"), dict):
            tab = out["leads" if r["t"] == "lead" else "projects"]
            if r["k"] not in tab or at >= str(tab[r["k"]].get("at") or ""):
                tab[r["k"]] = r
    return out


def _hv(v) -> str | None:
    """人管字段的单元格 → 记进 state、比较用的值:成员 → 账号 ID(不存显示名与邮箱);日期 → 上海日期;文字去掉首尾空白;空为 None。"""
    if isinstance(v, list):
        v = v[0] if v else None
    if isinstance(v, dict):
        return v.get("id") or v.get("title") or None
    if v is None or v is False:
        return None
    s = str(v).strip()
    if len(s) >= 20 and s[4] == "-" and s[10] == "T":
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(ZoneInfo(TZ)).date().isoformat()
        except ValueError:
            pass
    return s or None


def _human(fields: dict, names: tuple[str, ...]) -> dict[str, str | None]:
    return {k: _hv(fields.get(k)) for k in names}


def _pushed_sys(row: dict) -> dict:
    """推送新建线索记录时,系统里的处理状态、进展与处理备注(行里写的就是它们)。"""
    return {"status": _STATUS_CODE.get(row.get("处理状态") or "待处理", "new"), "stage": _STAGE_CODE.get(row.get("跟踪进展") or "", ""),
            "note": str(row.get("备注") or "").strip()}


def _sys_view(tri: dict, stages: dict, key: str) -> dict:
    """线索在系统里的处理状态、进展、处理备注(与表格的处理状态、跟踪进展、备注对应)。"""
    t = tri.get(key) or tri.get(key[:8]) or {}
    return {"status": t.get("status") or "new", "stage": (stages.get(key[:8]) or {}).get("value", ""),
            "note": str(t.get("note") or "").strip()}


def _table_sys(v: dict) -> dict:
    """表格里线索的人管字段值所对应的系统值(只有系统动过的记录,表格里就是系统写的值)。认不出的选项记 None。"""
    return {"status": _STATUS_CODE.get(v.get("处理状态") or "待处理"),
            "stage": _STAGE_CODE.get(v["跟踪进展"]) if v.get("跟踪进展") else "", "note": v.get("备注") or ""}


def _table_psys(v: dict) -> dict:
    """表格里商机的进展、优先级、内部备注所对应的系统值(进展的代码,内部叠加的优先级与备注);认不出的进展记 None。"""
    return {"stage": _STAGE_CODE.get(v["进展"]) if v.get("进展") else "", "priority": v.get("优先级") or "", "note": v.get("内部备注") or ""}


def _txt(v) -> str:
    return "" if v is None else str(v).strip()


def _overlay_view(profile: Profile) -> dict[str, dict]:
    """各国内部叠加 → {项目编号: {priority, note}}(与项目表的优先级、内部备注对应)。读不出的国别跳过(校验另有 CI)。"""
    out: dict[str, dict] = {}
    for c in profile.countries:
        if not c.overlay:
            continue
        try:
            items = load_overlay(c.overlay).get("items") or {}
        except (LedgerError, yaml.YAMLError, OSError):
            continue
        for iid, it in items.items():
            if isinstance(it, dict):
                out[str(iid)] = {"priority": _txt(it.get("priority")), "note": _txt(it.get("note"))}
    return out


def _write_back(st: dict, now: dict, sys_now: dict, spec: dict) -> tuple[dict, dict | None]:
    """推送时的三方比较,基准是这条记录最近一行同步状态(v 表格值,s 与之对应的系统值)→ (要写进表格的 {字段: 值}, 新的 {v, s};
    没有要记的为 None)。本机自上次同步改了、表格没改的写回;表格也改了的不写(以表格为准,拉回时处理并注明本机的值)。
    早期的状态行没记某项系统值的,不知道本机改没改——不写,补记现值作基准。"""
    v, s = dict(st.get("v") or {}), dict(st.get("s") or {})
    s_new, writes = dict(s), {}
    for f, (k, to_table) in spec.items():
        if k not in s:
            s_new[k] = sys_now[k]
            want = to_table(sys_now[k])
            if f not in v and now.get(f) is None and want is not None and want is not _SKIP:  # 后来加的列(如商机的进展):表格里还空着,写系统现值
                writes[f] = want
                v[f] = _hv(want)
            continue
        if sys_now[k] == s[k] or now.get(f) != v.get(f):
            continue
        want = to_table(sys_now[k])
        if want is _SKIP:  # 表格里没有这个选项(内部叠加的优先级不是 A/B/C,推送时另有提示)
            continue
        if _hv(want) != now.get(f):
            writes[f] = want
            v[f] = _hv(want)
        s_new[k] = sys_now[k]
    if not writes and s_new == s:
        return {}, None
    return writes, {"v": v, "s": s_new}


class _Back:
    """推送时一张表的写回:human 交给 upsert 逐条算要写回的字段;written 在一批更新成功后记下这些记录的新同步状态;
    flush 记下不用写、只补记基准的。"""

    def __init__(self, store: Store, kind: str, states: dict, sys_of, spec: dict, names: tuple[str, ...]):
        self.store, self.kind, self.states, self.sys_of, self.spec, self.names = store, kind, states, sys_of, spec, names
        self.pending: dict[str, dict] = {}
        self.quiet: list[dict] = []
        self.n = 0

    def _line(self, key: str, r: dict) -> dict:
        enter = (self.states.get(key) or {}).get("enter")
        return {"at": _now(), "t": self.kind, "k": key, "v": r["v"], "s": r["s"], **({"enter": enter} if enter else {})}

    def human(self, key: str, rec: dict) -> dict:
        st = self.states.get(key)
        if st is None:  # 还没有同步基准(拉回时记下):先不写
            return {}
        writes, r = _write_back(st, _human(rec.get("fields") or {}, self.names), self.sys_of(key), self.spec)
        if r is not None:
            if writes:
                self.pending[key] = r
            else:
                self.quiet.append(self._line(key, r))
        return writes

    def written(self, keys: list[str]) -> None:
        lines = [self._line(k, self.pending.pop(k)) for k in keys if k in self.pending]
        self.n += len(lines)
        append_state(self.store, lines)

    def flush(self) -> None:
        append_state(self.store, self.quiet)
        self.quiet = []


def _pushed_values(tri: dict, stages: dict, key: str) -> dict:
    """推送新建这条线索记录时会写进表格的人管字段(按系统现值;负责人、下一步不推送)。"""
    t, s = tri.get(key) or tri.get(key[:8]) or {}, _sys_view(tri, stages, key)
    return {k: _hv(v) for k, v in {"处理状态": STATUS_ZH.get(s["status"], "待处理"), "跟踪进展": LEAD_STAGES.get(s["stage"]),
                                    "负责人": None, "下一步": None, "下一步到期": None, "备注": t.get("note"), "结果原因": None}.items()}


def _edited_by_others(rec: dict) -> bool:
    """记录建好以后有别的账号改过(最后修改人不是建记录的账号,即不是系统用的令牌账号)。"""
    by, cb = rec.get("lastModifiedBy"), rec.get("createdBy")
    return bool(by and cb and by != cb)


def _ts(s) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(s).replace("Z", "+00:00")) if s else None
    except ValueError:
        return None
    return t.replace(tzinfo=timezone.utc) if t and t.tzinfo is None else t


def _sys_at_creation(tri: dict, stages: dict, events: list[dict], key: str, rec: dict) -> dict:
    """没记下过同步状态的记录,推送建它时对应的系统值:取系统现值;建好后本机又改过的项(操作记录里建记录之后有本机的
    处理标记或进展)记为不明(None)——表格里这些项也不同时,拉回以表格为准并注明本机的值。"""
    s = _sys_view(tri, stages, key)
    t0 = _ts(rec.get("createdTime"))
    for e in events if t0 else []:
        if str(e.get("id", ""))[:8] != key[:8] or str(e.get("by", "")).startswith("teable:"):
            continue
        t = _ts(e.get("at"))
        if t and t > t0:
            if e.get("kind") == "status":
                s["status"] = s["note"] = None
            elif e.get("kind") == "stage":
                s["stage"] = None
    return s


def _pulled_sys(old: dict, new: dict, keep: set[str], after: dict) -> dict:
    """拉回一条线索后的系统基准 s:表格改了、拉进来了的项取拉回后的系统值;表格没改(或没拉进来)的项保留原基准——
    本机自上次同步的改动还要在推送时写回。"""
    s_old = old.get("s") or {}
    out = {}
    for f, (k, _) in LEAD_WB.items():
        pulled = f not in keep and new.get(f) != old["v"].get(f)
        out[k] = after[k] if pulled or s_old.get(k) is None else s_old[k]
    return out


def _one_per_key(records: list[dict], warns: list[str], table: str) -> dict[str, dict]:
    """编号 → 记录;同一编号有几条的(表里复制了行),取最近改过的那条并提示。"""
    out: dict[str, dict] = {}
    dup: set[str] = set()
    for rec in records:
        key = str((rec.get("fields") or {}).get("编号") or "")
        if not key:
            continue
        if key in out:
            dup.add(key)
            if str(rec.get("lastModifiedTime") or "") <= str(out[key].get("lastModifiedTime") or ""):
                continue
        out[key] = rec
    warns += [f"「{table}」表里编号 {k} 有不止一条记录,只看最近改过的那条;多出来的请在表里删掉" for k in sorted(dup)]
    return out


# ---------- 拉回:表格里改的人管字段 → 操作记录、处理状态、内部叠加 ----------

def load_team(profile: Profile) -> dict[str, str]:
    """岗位代号表(与 profile 同目录的 team.yaml,members: [{code, teable}]):表格账号的显示名 → 岗位代号。没有时空,直接用显示名。"""
    fp = Path(profile.path).parent / "team.yaml" if getattr(profile, "path", None) else None
    raw = (yaml.safe_load(fp.read_text(encoding="utf-8")) or {}) if fp and fp.exists() else {}
    return {str(m["teable"]).strip(): str(m["code"]).strip() for m in raw.get("members") or []
            if isinstance(m, dict) and m.get("teable") and m.get("code")}




def _user_names(api: Teable, base_id: str, records: list[dict]) -> dict[str, str]:
    """账号 ID → 显示名:库的协作者(令牌没有权限时跳过)+ 记录里成员字段带的显示名。"""
    out: dict[str, str] = {}
    try:
        out.update({str(c["userId"]): str(c.get("userName") or c["userId"]) for c in api.collaborators(base_id) if c.get("userId")})
    except (TeableError, AttributeError, KeyError, TypeError, httpx.HTTPError):
        pass
    for rec in records:
        for v in (rec.get("fields") or {}).values():
            for u in v if isinstance(v, list) else [v]:
                if isinstance(u, dict) and u.get("id") and u.get("title") and str(u["id"]).startswith("usr"):
                    out.setdefault(str(u["id"]), str(u["title"]))
    return out


def _name(v, users: dict[str, str], team: dict[str, str]) -> str:
    """成员单元格或账号 → 岗位代号(代号表里有的)或显示名。"""
    if isinstance(v, list):
        v = v[0] if v else None
    if isinstance(v, dict):
        name = str(v.get("title") or users.get(str(v.get("id")), v.get("id")) or "")
    else:
        name = users.get(str(v), str(v)) if v else ""
    return team.get(name, name)


def _iso(s) -> str | None:
    """Teable 的时间(…Z)→ ISO(到秒,带时区);没有或认不出时 None(记为现在)。"""
    t = _ts(s)
    return t.astimezone(timezone.utc).isoformat(timespec="seconds") if t else None


def _when(rec: dict) -> str | None:
    """记录的最后修改时间 → ISO(到秒,带时区);没有时 None(记为现在)。"""
    return _iso(rec.get("lastModifiedTime"))


def _field_changes(api: Teable, table_id: str, rec: dict, fids: dict[str, str], users: dict[str, str], team: dict[str, str],
                   hctx: dict, pages: int = 5) -> dict[str, tuple[str, str | None]]:
    """记录历史 → 这几个字段各自最近一次是谁、什么时候改的 {字段名: (代号或显示名, ISO 时间)}(历史新的在前,每个字段取第一条)。
    历史里没有的字段不在结果里(按整条记录的最后修改人与时间记);查不了(老版本、没权限、出错)时这次拉回不再查,
    原因记在 hctx["why"]。"""
    names = {fid: n for n, fid in fids.items() if fid}
    out: dict[str, tuple[str, str | None]] = {}
    cursor = None
    for _ in range(pages if names and not hctx.get("why") else 0):
        try:
            r = api.record_history(table_id, rec["id"], list(names), cursor)
            umap = r.get("userMap") or {}
            for h in r.get("historyList") or []:
                n = names.get(str(h.get("fieldId")))
                if n and n not in out:
                    uid = str(h.get("createdBy") or "")
                    disp = str((umap.get(uid) or {}).get("name") or users.get(uid) or uid)
                    out[n] = (team.get(disp, disp), _iso(h.get("createdTime")))
            cursor = r.get("nextCursor")
        except (TeableError, httpx.HTTPError, AttributeError, KeyError, TypeError) as e:
            hctx["why"] = str(e)[:120] or type(e).__name__
            return out
        if not cursor or len(out) == len(names):
            break
    return out


def pull(profile: Profile, store: Store, today: date, url: str, token: str, base_name: str | None = None, log=print,
         api: Teable | None = None, identity: tuple[str, ...] = git.BOT_ID, keep_unpushed: bool = False) -> dict:
    """反向同步:同事在表格里改的人管字段拉回系统(见本文件开头)→ 摘要
    {events, conflicts: [线索号], entered: [说明], overlay: [说明], warnings, baseline, leads, projects}。
    identity、keep_unpushed:跟踪即入账与内部叠加提交程序仓库时的身份、推送失败时留不留提交
    (定时任务 BOT_ID、不留,下次再试;本机界面 APP_ID、留)。每处理完一条就把它的新状态追加进 state,中途出错重跑不重复记。"""
    api = api or Teable(url, token)
    base_name = base_name or (profile.teable or {}).get("base") or BASE_NAME
    out: dict = {"events": 0, "conflicts": [], "entered": [], "overlay": [], "warnings": [], "baseline": 0, "leads": 0, "projects": 0}
    base = find_base(api, base_name)
    if base is None:
        out["warnings"].append(f"表格里还没有库「{base_name}」,先推送一次")
        log("拉回:" + out["warnings"][-1])
        return out
    tables = {t["name"]: t for t in api.tables(base["id"])}
    if T_PROJECTS not in tables and T_PROJECTS_OLD in tables:  # 还没推送过新版(表没改名):照旧从原来的表拉
        tables[T_PROJECTS] = tables[T_PROJECTS_OLD]
    lraw = api.records(tables[T_LEADS]["id"]) if T_LEADS in tables else []
    praw = api.records(tables[T_PROJECTS]["id"]) if T_PROJECTS in tables else []
    lrecs, precs = _one_per_key(lraw, out["warnings"], T_LEADS), _one_per_key(praw, out["warnings"], T_PROJECTS)
    users, team = _user_names(api, base["id"], [*lrecs.values(), *precs.values()]), load_team(profile)
    state = load_state(store)
    n0 = len(store.events())
    sigs = {s["id"]: s for s in store.load_signals()}
    events = store.events()
    book = book_of(profile, store)
    tri, stages = store.triage(), lead_stages(book, events)
    baseline: list[dict] = []
    hctx: dict = {}  # 记录历史查不了时的原因(之后不再查)
    try:
        lfid = {str(x["name"]): str(x["id"]) for x in api.fields(tables[T_LEADS]["id"])} if T_LEADS in tables else {}
    except (TeableError, httpx.HTTPError, AttributeError, KeyError, TypeError) as e:
        lfid, hctx["why"] = {}, f"读不到线索表的字段:{str(e)[:100] or type(e).__name__}"
    for key, rec in lrecs.items():
        f = rec.get("fields") or {}
        new, st = _human(f, LEAD_HUMAN), state["leads"].get(key)
        if st is not None:  # 早期的行少记的字段(后来加的)按空值
            st = {**st, "v": {**dict.fromkeys(LEAD_HUMAN), **st["v"]}}
        out["leads"] += 1
        if st is None and not _edited_by_others(rec):  # 没记下过、建好后只有系统动过:表格里是系统写的值,记下作基准
            baseline.append(_lead_line(key, new, _table_sys(new)))  # (本机此后改的,推送时写回)
            continue
        if key not in sigs:
            if st is None or new != st["v"]:
                out["warnings"].append(f"表格里的线索 {key[:8]} 在系统里找不到,改动没拉回")
            continue
        # 没记下过、建好后有同事改过:按推送建它时写进表格的系统值比(建好后本机又改过的项不明,拉回时注明冲突)
        old = st or {"v": _pushed_values(tri, stages, key), "s": _sys_at_creation(tri, stages, events, key, rec)}
        if new == old["v"] and old.get("enter") != "pending":
            if st is None:
                baseline.append(_lead_line(key, new, _table_sys(new)))
            continue
        changed = {k: lfid.get(k, "") for k in LEAD_HUMAN  # 谁改的哪一格:查记录历史(跟踪即入账重试时要知道谁设的进展)
                   if new[k] != old["v"].get(k) or (k == "跟踪进展" and old.get("enter") == "pending")}
        hist = _field_changes(api, tables[T_LEADS]["id"], rec, changed, users, team, hctx)
        keep, enter = _pull_lead(profile, store, today, sigs[key], new, old, f, _name(rec.get("lastModifiedBy"), users, team),
                                 _when(rec), users, team, identity, keep_unpushed, out, hist, book)
        tri, stages = store.triage(), lead_stages(book, store.events())
        v = {k: (old["v"].get(k) if k in keep else new[k]) for k in LEAD_HUMAN}  # 没拉进来的字段留旧值,下次再拉
        append_state(store, [_lead_line(key, v, _pulled_sys(old, new, keep, _sys_view(tri, stages, key)), enter)])
    if hctx.get("why"):
        out["warnings"].append(f"表格的记录历史查不了,这次的操作人按整条记录的最后修改人记({hctx['why']})")
    changes: dict[str, dict] = {}
    later: dict[str, dict] = {}
    ov = _overlay_view(profile)
    tracked = book.tracked(store.events())
    try:
        pfid = {str(x["name"]): str(x["id"]) for x in api.fields(tables[T_PROJECTS]["id"])} if T_PROJECTS in tables else {}
    except (TeableError, httpx.HTTPError, AttributeError, KeyError, TypeError):
        pfid = {}
    for key, rec in precs.items():
        f = rec.get("fields") or {}
        new, st = _human(f, PROJECT_HUMAN), state["projects"].get(key)
        out["projects"] += 1
        if st is None and not _edited_by_others(rec):  # 表格里是系统写的值(建记录时的初值或空):记下作基准,内部叠加里的推送时写回
            baseline.append(_project_line(key, new, _table_psys(new)))
            continue
        old = {**dict.fromkeys(PROJECT_HUMAN), **(st["v"] if st else {})}  # 没记下过的、后来加的列按空值比
        s_old = (st.get("s") or {}) if st else _table_psys(old)
        if new == old:
            if st is None:
                baseline.append(_project_line(key, new, _table_psys(new)))
            continue
        cur = {**(ov.get(key) or _NO_OVERLAY), "stage": (tracked.get(key) or {}).get("value", "")}
        kept = _pull_opp(store, key, new, old, s_old, rec, book, sigs, cur, (users, team, hctx), (api, tables[T_PROJECTS]["id"], pfid), out)
        ch: dict = {}
        for fname, k in (("优先级", "priority"), ("负责人", "owner"), ("内部备注", "note")):
            if new[fname] == old.get(fname):
                continue
            ch[k] = (_name(f.get("负责人"), users, team) if new["负责人"] else None) if k == "owner" else new[fname]
            if k in s_old and k in cur and cur[k] != s_old[k] and cur[k] != (new[fname] or ""):  # 内部叠加自上次同步也改了:以表格为准
                out["warnings"].append(f"商机 {key} 的{fname}本机也改过" + (f"(本机为「{cur[k]}」)" if k == "priority" else "")
                                       + ",以表格为准")
        v = {k: (old.get(k) if k in kept else new[k]) for k in PROJECT_HUMAN}  # 没拉进来的列留旧值,下次再拉
        s_new = {k: (_table_psys(new)[k] if new[fn] != old.get(fn) and fn not in kept else s_old.get(k, cur.get(k, "")))
                 for fn, (k, _) in PROJECT_WB.items()}
        if ch:  # 内部叠加写成了才记下(没写成、没推上去的下次再拉)
            changes[key] = ch
            later[key] = (v, s_new)
        else:
            baseline.append(_project_line(key, v, s_new))
    append_state(store, baseline)
    out["baseline"] = len(baseline)
    if T_FOLLOW in tables:
        _pull_follows(store, api.records(tables[T_FOLLOW]["id"]), lraw, praw, state, users, team, out, book, sigs)
    if changes:
        out["overlay"], written = write_overlay(profile, changes, today, identity, out["warnings"])
        append_state(store, [_project_line(k, v, s) for k, (v, s) in later.items() if k in written])  # 没写成、没推上去的下次再拉
    out["events"] = len(store.events()) - n0
    if out["events"] or out["conflicts"]:  # 没拉到改动的不记(服务器上每 30 秒拉一次,免得每次都多一行、多一次提交)
        append_state(store, [{"at": _now(), "t": "pull", "events": out["events"], "conflicts": out["conflicts"]}])
    log(f"拉回:线索 {out['leads']} 条、商机 {out['projects']} 个;操作记录 {out['events']} 条"
        + (f",与本机冲突以表格为准 {len(out['conflicts'])} 条" if out["conflicts"] else "")
        + (f";第一次见到、没人改过的 {out['baseline']} 条只记下现值" if out["baseline"] else ""))
    for x in out["entered"] + out["overlay"]:
        log("  " + x)
    for w in out["warnings"]:
        log("  ! " + w)
    return out


def _src(hist: dict | None, who: str, at: str | None, *names: str) -> tuple[str, str | None]:
    """这几个字段在表格里最近一次是谁、什么时候改的 → (teable:<名>, 时间);记录历史里没有的按整条记录的最后修改人与时间。"""
    hits = [hist[n] for n in names if n in (hist or {})]
    w, a = max(hits, key=lambda x: x[1] or "") if hits else (who, at)
    return f"teable:{w or '表格'}", a


def _pull_opp(store: Store, key: str, new: dict, old: dict, s_old: dict, rec: dict, book: opp.Book, sigs: dict[str, dict],
              cur: dict, people: tuple, where: tuple, out: dict) -> set[str]:
    """商机表一行的进展、下一步在表格里改了 → 记在商机上(进展清除即不再跟踪,core.opportunity.set_progress)。
    本机自上次同步也改了进展的,以表格为准、注明本机的值。→ 这次没拉进来、下次再拉的列"""
    users, team, hctx = people
    api, table_id, fids = where
    changed = {k: fids.get(k, "") for k in ("进展", "下一步", "下一步到期") if new[k] != old.get(k)}
    if not changed:
        return set()
    if key not in book.items:
        out["warnings"].append(f"商机表里的 {key} 在台账里找不到,进展、下一步没拉回")
        return set(changed)
    hist = _field_changes(api, table_id, rec, changed, users, team, hctx)
    who, at = _name(rec.get("lastModifiedBy"), users, team), _when(rec)
    kept: set[str] = set()
    if "进展" in changed:
        g = _STAGE_CODE.get(new["进展"]) if new["进展"] else ""
        local = cur.get("stage", "")
        if g is None:
            out["warnings"].append(f"商机 {key} 的进展「{new['进展']}」认不出,没拉回(改成跟踪中、准备投标等以后下次再拉)")
            kept.add("进展")
        elif g != local:
            mine = local != s_old.get("stage", local)
            by, when = _src(hist, who, at, "进展")
            ids = [next((k for k in sigs if k.startswith(no)), no) for no in book.leads.get(key, [])]
            opp.set_progress(store, key, ids, g or None, by=by, at=None if mine else when,
                             conflict=f"本机为「{LEAD_STAGES.get(local, '未设置')}」" if mine else "")
            if mine and key not in out["conflicts"]:
                out["conflicts"].append(key)
    if "下一步" in changed or "下一步到期" in changed:
        c = current_next(store.events()).get(key) or {}
        if (new["下一步"] or "", new["下一步到期"] or "") != (c.get("value", ""), c.get("due", "")):
            by, when = _src(hist, who, at, "下一步", "下一步到期")
            set_next(store, key, new["下一步"] or "", new["下一步到期"], by=by, at=when)
    return kept


def _ids(v) -> list[str]:
    """关联单元格(多对一为 {id, title},多对多为 [{id, title}])→ 记录 ID 列表。"""
    return [str(x["id"]) for x in (v if isinstance(v, list) else [v]) if isinstance(x, dict) and x.get("id")]


def _txt_cmp(v) -> str | None:
    """跟进记录一格 → 比较用的值(_hv 之外,换行统一成 \\n)。"""
    h = _hv(v)
    return h.replace("\r\n", "\n") if isinstance(h, str) else h


def _day_of(e: dict) -> str | None:
    """一条留痕的日期:人填的跟进日期,没有时取记下的时间(上海日期)。"""
    if e.get("day"):
        return str(e["day"])[:10]
    t = _ts(e.get("at"))
    return t.astimezone(ZoneInfo(TZ)).date().isoformat() if t else None


FOLLOW_VALUES = ("内容", "日期", "判断", "判断依据", "进展", "下一步", "下一步到期")  # 跟进记录里系统与表格都写、要一致的列


def follow_row(e: dict) -> dict:
    """商机的一条工作留痕(core.opportunity.work_log)→ 跟进记录表一行的值(不含关联与人):内容、日期、判断与依据、
    跟进后的进展、下一步与到期。带说明的进展变化:内容是说明,进展是改成的进展。"""
    kind = e.get("kind")
    stage = (e.get("value") if kind == "stage" else e.get("stage") if kind == "follow" else "") or ""
    return {"内容": str(e.get("note") or "").strip() or None, "日期": _day_of(e),
            "判断": JUDGMENTS.get(e.get("value")) if kind == "follow" and e.get("value") else None,
            "判断依据": str(e.get("reason") or "").strip() or None,
            "进展": LEAD_STAGES.get(stage) if stage else None,
            "下一步": str(e.get("step") or "").strip() or None, "下一步到期": e.get("due") or None}


def _same_follow(fields: dict, e: dict) -> bool:
    """表格里这一行与系统里那条一致(没被改过)。表格里没填日期、系统里那条也没记跟进日期的,算一致。"""
    want = follow_row(e)
    for k in FOLLOW_VALUES:
        have = _txt_cmp(fields.get(k))
        if have != _txt_cmp(want[k]) and not (k == "日期" and have is None and not e.get("day")):
            return False
    return True


def _follow_index(events: list[dict], state: dict) -> dict[str, dict]:
    """跟进记录的行 → 系统里那条(撤回的不算):推上去的按同步状态(fpush,含表格恢复备份、清空以后重建的),
    其余表格拉回的按 ref。"""
    gone, pushed = opp.retracted(events), state.get("fpush") or {}
    out: dict[str, dict] = {}
    for e in events:
        if opp.is_work(e) and (key := opp.entry_key(e)) not in gone and (rid := pushed.get(key) or e.get("ref")):
            out[str(rid)] = e
    return out


def _retract_row(store: Store, events: list[dict], rid: str, e: dict, note: str, by: str, at: str | None = None) -> None:
    """撤回表格里这一行对应的系统里的跟进:那一条,连同同一行拉回过几次留下的(带同一 ref、还没撤回的)。"""
    gone = opp.retracted(events)
    for x in [e, *(x for x in events if x is not e and str(x.get("ref") or "") == rid and opp.entry_key(x) not in gone)]:
        if opp.entry_key(x) not in gone:
            opp.retract(store, x, note, by=by, at=at)
            gone.add(opp.entry_key(x))


def _person(by: str | None, team: dict[str, str]) -> str | None:
    """操作人 → 表格账号的显示名(写「人」用:表格按显示名认人,认不出的留空)。系统、本机记的没有人。"""
    if not by or by in (SYSTEM, "本机"):
        return None
    name = by[len("teable:"):] if by.startswith("teable:") else by
    return {code: disp for disp, code in team.items()}.get(name, name) or None


_JUDGE_CODE = {v: k for k, v in JUDGMENTS.items()}  # 继续跟进 → continue


def _follow_fields(f: dict, rid: str, out: dict) -> tuple[str, str, str, str | None, str, str, str | None]:
    """跟进记录一行 → (内容, 判断代码, 判断依据, 日期, 进展代码, 下一步, 下一步到期);认不出的判断、进展提示后当作没填。"""
    text, judge = str(f.get("内容") or "").strip(), str(f.get("判断") or "").strip()
    j = _JUDGE_CODE.get(judge, "") if judge else ""
    if judge and not j:
        out["warnings"].append(f"跟进记录 {rid} 的判断「{judge}」认不出,只记了内容")
    g = _STAGE_CODE.get(str(f.get("进展") or "").strip()) if f.get("进展") else ""
    if g is None:
        out["warnings"].append(f"跟进记录 {rid} 的进展「{f.get('进展')}」认不出,没改进展")
        g = ""
    return (text, j, str(f.get("判断依据") or "").strip(), _hv(f.get("日期")), g, str(f.get("下一步") or "").strip(),
            _hv(f.get("下一步到期")))


def _apply_follow(store: Store, k: str, g: str, nxt: str, due: str | None, who: str, at: str | None, book: opp.Book | None,
                  sigs: dict[str, dict]) -> None:
    """跟进记录里填的进展、下一步 → 商机(或还没有商机的线索)的进展与下一步;与现值相同的不重复记。"""
    is_item = book is not None and k in book.items
    cur = (book.stage(k, store.events()) if is_item else current_stages(store.events()).get(k[:8])) or {}
    if g and g != cur.get("value", ""):
        if is_item:
            ids = [next((s for s in sigs if s.startswith(no)), no) for no in book.leads.get(k, [])]
            opp.set_progress(store, k, ids, g, by=who, at=at)
        else:
            set_stage(store, k, g, by=who, at=at)
    if nxt or due:
        c = current_next(store.events()).get(k[:8]) or {}
        if (nxt, due or "") != (c.get("value", ""), c.get("due", "")):
            set_next(store, k, nxt, due, by=who, at=at)


def _pull_follows(store: Store, records: list[dict], lraw: list[dict], praw: list[dict], state: dict,
                  users: dict[str, str], team: dict[str, str], out: dict, book: opp.Book | None = None,
                  sigs: dict[str, dict] | None = None) -> None:
    """跟进记录表 → 商机的跟进,与系统里的工作留痕是同一份(core.opportunity.work_log):
    - 新加的行 → 商机的跟进(kind follow,带 ref = 这一行的记录 ID:内容、判断与依据、跟进日期、跟进后的进展与下一步),
      记在关联的商机上;只关联了线索的,记在线索所属的商机上,线索还没有商机的记在线索上。填了「进展」的同时改商机的进展,
      判断为「放弃」又没填进展的进展记为放弃;填了「下一步」的记下一步与到期。操作人记「人」(没填记建这行的账号),
      时间取建这行的时间。内容与判断都空的、没关联的先不拉,补上以后下次再拉。
    - 已经对上的行(拉回过的、系统推上去的)在表格里改了 → 撤回原来那条,换成改后的一条(带 ref;进展、下一步改了的照样改);
      内容与判断都清空了的只撤回。操作人、时间取这一行的最后修改人与时间。
    - 已经对上的行在表格里删掉了 → 撤回系统里那条(表格整张空着时不当作删除;表格恢复备份后没了的不算,见 after_restore)。
    关联到表里复制出来的重复行也认(按编号)。提示里不写跟进的内容。"""
    lead_of = {rec["id"]: str((rec.get("fields") or {}).get("编号") or "") for rec in lraw}
    proj_of = {rec["id"]: str((rec.get("fields") or {}).get("编号") or "") for rec in praw}
    members = book.members if book else {}
    sigs = sigs or {}
    done, gone, lost = state.get("follows") or set(), state.get("fgone") or set(), state.get("flost") or set()
    pushed = set((state.get("fpush") or {}).values())
    known = _follow_index(store.events(), state)
    lines = []
    for rec in records:
        f, rid = rec.get("fields") or {}, str(rec.get("id"))
        if rid in gone:
            continue
        if rid in known:  # 拉回过的、推上去的:改了就换成改后的
            e = known[rid]
            if _same_follow(f, e):
                continue
            who = f"teable:{_name(rec.get('lastModifiedBy') or f.get('人') or rec.get('createdBy'), users, team) or '表格'}"
            at = _when(rec)
            text, j, reason, day, g, nxt, due = _follow_fields(f, rid, out)
            _retract_row(store, store.events(), rid, e, "表格「跟进记录」里改了这一行,换成改后的", who, at)
            if text or j:
                add_follow(store, str(e.get("id", "")), text, j, reason, day, by=who, at=at, stage=g, step=nxt, due=due, ref=rid)
                old = follow_row(e)
                _apply_follow(store, str(e.get("id", "")), g if LEAD_STAGES.get(g) != old["进展"] else "",
                              nxt if (nxt or None, due) != (old["下一步"], old["下一步到期"]) else "",
                              due if (nxt or None, due) != (old["下一步"], old["下一步到期"]) else None, who, at, book, sigs)
            out["follows_edited"] = out.get("follows_edited", 0) + 1
            continue
        if rid in done or rid in pushed:  # 对上过、系统里那条已撤回(或很早以前拉回、没记 ref 的):不再拉
            continue
        text, judge = str(f.get("内容") or "").strip(), str(f.get("判断") or "").strip()
        if not (text or judge):
            continue
        items = list(dict.fromkeys(proj_of[i] for i in _ids(f.get("商机") or f.get("项目")) if proj_of.get(i)))
        leads = list(dict.fromkeys(lead_of[i] for i in _ids(f.get("线索")) if lead_of.get(i)))
        targets = items or list(dict.fromkeys((members[k[:8]].item if k[:8] in members else k) for k in leads))
        if not targets:
            out["warnings"].append(f"跟进记录 {rid} 没有关联商机或线索,先不拉回(关联上以后下次再拉)")
            continue
        who = f"teable:{_name(f.get('人') or rec.get('createdBy'), users, team) or '表格'}"
        at = _iso(rec.get("createdTime"))
        text, j, reason, day, g, nxt, due = _follow_fields(f, rid, out)
        for k in targets:  # 跟进上记表格里填的进展;判断为放弃、没填进展的,商机的进展记为放弃
            add_follow(store, k, text, j, reason, day, by=who, at=at, stage=g, step=nxt, due=due, ref=rid)
            _apply_follow(store, k, g or ("dropped" if j == "drop" else ""), nxt, due, who, at, book, sigs)
        lines.append({"at": _now(), "t": "follow", "k": rid})
    present = {str(rec.get("id")) for rec in records}
    missing = [rid for rid in known if rid not in present and rid not in gone and rid not in lost]
    if missing and not records:
        out["warnings"].append(f"跟进记录表是空的(系统里对得上的有 {len(known)} 行),没有当作删除;如果确实删光了,请告诉管理员")
        missing = []
    for rid in missing:  # 表格里删掉了:系统里那条撤回
        _retract_row(store, store.events(), rid, known[rid], "表格「跟进记录」里删掉了这一行", "teable:表格")
        lines.append({"at": _now(), "t": "fgone", "k": rid})
    out["follows_deleted"] = len(missing)
    append_state(store, lines)


def push_follows(api: Teable, table: dict, store: Store, book: opp.Book, pids: dict[str, str], lids: dict[str, str],
                 state: dict, team: dict[str, str], log=print, records: list[dict] | None = None) -> dict:
    """商机的工作留痕(core.opportunity.work_log:跟进、人记的一笔、带说明的进展,含所属线索上的)→ 跟进记录表,与系统是同一份:
    表格里还没有的建一行(关联商机;记在所属线索上的同时关联线索;「人」写操作人的表格显示名),建好的记进同步状态
    (fpush:这条的键 → 记录 ID),拉回时不再当新行拉;表格里已有一模一样的行(同一商机、内容、日期、判断)的认下,不再建。
    已有的行关联不对的改过来(线索换了商机、商机表的行重建过)。表格里删掉的不重建(拉回时系统里撤回);
    表格恢复备份后没了的(flost)、整张表空着的重建。商机表里没有的(不在跟踪的)商机不推。→ {created, adopted, relinked}"""
    events = store.events()
    records = api.records(table["id"]) if records is None else records
    present = {str(r["id"]): r for r in records}
    known = _follow_index(events, state)
    pushed, lost = state.get("fpush") or {}, set(state.get("flost") or ())
    if not present:  # 整张表空着(新建的表、清空了):按系统里的全部重建(拉回时也不当作删除)
        lost |= set(known)
    free: dict[tuple, list[str]] = {}  # 还没对上的行:(商机, 内容, 日期, 判断) → 记录 ID(认下一模一样的用)
    for rid, r in present.items():
        if rid not in known:
            f = r.get("fields") or {}
            sig = (tuple(_ids(f.get("商机"))), _txt_cmp(f.get("内容")), _txt_cmp(f.get("日期")), _txt_cmp(f.get("判断")))
            free.setdefault(sig, []).append(rid)
    creates: list[tuple[str, dict]] = []
    relink: list[tuple[str, dict]] = []
    adopted: list[dict] = []
    for iid, prid in pids.items():
        for e in opp.work_log(iid, events, book.leads.get(iid, [])):
            key, eid = opp.entry_key(e), str(e.get("id", ""))
            lrid = None if eid == iid else (lids.get(eid) or next((v for k, v in lids.items() if k[:8] == eid[:8]), None))
            links = {"商机": {"id": prid}, **({"线索": {"id": lrid}} if lrid else {})}
            rid = str(pushed.get(key) or e.get("ref") or "")  # 恢复备份后重建过的按新行
            if rid and rid in present:
                f = present[rid].get("fields") or {}
                if _ids(f.get("商机")) != [prid] or (lrid and _ids(f.get("线索")) != [lrid]):
                    relink.append((rid, links))
                continue
            if rid and rid not in lost:  # 表格里删掉了:不重建(拉回时系统里撤回)
                continue
            row = {k: v for k, v in follow_row(e).items() if v is not None}
            sig = ((prid,), _txt_cmp(row.get("内容")), _txt_cmp(row.get("日期")), _txt_cmp(row.get("判断")))
            if free.get(sig):
                adopted.append({"at": _now(), "t": "fpush", "k": key, "rid": free[sig].pop(0)})
                continue
            if who := _person(e.get("by"), team):
                row["人"] = who
            creates.append((key, {**row, **links}))
    append_state(store, adopted)
    n = 0
    for i in range(0, len(creates), BATCH):
        batch = creates[i:i + BATCH]
        made = api.create_records(table["id"], [r for _, r in batch])
        append_state(store, [{"at": _now(), "t": "fpush", "k": k, "rid": str(rec["id"])} for (k, _), rec in zip(batch, made)])
        n += len(made)
    if relink:
        api.update_records(table["id"], relink)
    log(f"  跟进记录:新写 {n} 条(系统里记的)" + (f",认下表格里已有的 {len(adopted)} 条" if adopted else "")
        + (f",改关联 {len(relink)} 条" if relink else ""))
    return {"created": n, "adopted": len(adopted), "relinked": len(relink)}


def _pull_lead(profile: Profile, store: Store, today: date, sig: dict, new: dict, old: dict, f: dict, who: str, at: str | None,
               users: dict[str, str], team: dict[str, str], identity: tuple[str, ...], keep_unpushed: bool,
               out: dict, hist: dict[str, tuple[str, str | None]] | None = None,
               book: opp.Book | None = None) -> tuple[set[str], str | None]:
    """一条线索的人管字段在表格里改了 → 写成操作记录。操作人 teable:<名>、时间按字段取自记录历史(hist,谁、什么时候改的这一格),
    历史里没有的按整条记录的最后修改人与时间(who、at);本机上次同步后也改过、以表格为准的,时间记为这次拉回,时间线上排在本机那次之后。
    表格里改了、系统现值又确实不同才记(本机早已是这个值的不重复记)。→ (这次没拉进来、下次再拉的字段, 跟踪即入账要不要下次再试)"""
    sid, no = sig["id"], sig["id"][:8]

    def src(*names: str) -> tuple[str, str | None]:
        """这几个字段在表格里最近一次是谁、什么时候改的 → (teable:<名>, 时间)。"""
        hits = [hist[n] for n in names if n in (hist or {})]
        w, a = max(hits, key=lambda x: x[1] or "") if hits else (who, at)
        return f"teable:{w or '表格'}", a

    ov, sysold = old["v"], old.get("s") or {}
    tri, events = store.triage(), store.events()
    t = tri.get(sid) or tri.get(no) or {}
    local_status = t.get("status") or "new"
    mem = book.members.get(no) if book else None  # 已属于商机:表格里的「跟踪进展」就是商机的进展
    local_stage = ((lead_stages(book, events) if book else current_stages(events)).get(no) or {}).get("value", "")
    keep: set[str] = set()

    def conflict(local_zh: str) -> str:
        if no not in out["conflicts"]:
            out["conflicts"].append(no)
        return f"本机为「{local_zh}」"

    note, note_old = new["备注"] or "", ov.get("备注") or ""
    s_new, s_old = _STATUS_CODE.get(new["处理状态"] or "待处理"), _STATUS_CODE.get(ov.get("处理状态") or "待处理")
    status_done = False
    if s_new is None:
        out["warnings"].append(f"线索 {no} 的处理状态「{new['处理状态']}」认不出,没拉回(改成待处理、已处理或已忽略后下次再拉)")
        keep.add("处理状态")
    elif s_new != s_old and s_new != local_status:
        mine = local_status != sysold.get("status", local_status)
        by, when = src("处理状态")
        set_status(store, [sid], s_new, note, by=by, at=None if mine else when,
                   conflict=conflict(STATUS_ZH.get(local_status, local_status)) if mine else "")
        status_done = True
    if not status_done and note != note_old and note != (t.get("note") or ""):
        set_triage_note(store, sid, note)  # 已处理、已忽略的处理备注跟着改;待处理的只记一笔
        by, when = src("备注")
        add_note(store, sid, f"表格里的备注改为:{note}" if note else "表格里的备注清空了", by=by, at=when, auto=True)
    g_new = _STAGE_CODE.get(new["跟踪进展"]) if new["跟踪进展"] else ""
    g_old = _STAGE_CODE.get(ov.get("跟踪进展") or "", "")
    enter = old.get("enter") == "pending"  # 上次跟踪即入账推送失败,这次再试
    if g_new is None:
        out["warnings"].append(f"线索 {no} 的跟踪进展「{new['跟踪进展']}」认不出,没拉回")
        keep.add("跟踪进展")
    elif g_new != g_old and g_new != local_stage:
        mine = local_stage != sysold.get("stage", local_stage)
        by, when = src("跟踪进展")
        cf = conflict(LEAD_STAGES.get(local_stage, "未设置")) if mine else ""
        if mem:  # 改的是所属商机的进展(记在商机上)
            opp.set_progress(store, mem.item, [sid if x == no else x for x in book.leads.get(mem.item, [])], g_new or None,
                             f"在线索 {no} 上改的", by=by, at=None if mine else when, conflict=cf, auto=True)
        else:
            set_stage(store, sid, g_new or None, "", by=by, at=None if mine else when, conflict=cf)
            enter = enter or should_enter(local_stage, g_new)
    reason = new.get("结果原因") or ""  # 中标、未中标、放弃的原因:记在进展之后
    if reason != (ov.get("结果原因") or ""):
        by, when = src("结果原因")
        add_note(store, sid, f"结果原因:{reason}" if reason else "表格里的结果原因清空了", by=by, at=when, auto=True)
    pending = None
    if enter and (current_stages(store.events()).get(no) or {}).get("value", "") in ACTIVE:  # 跟踪即入账,与本机界面同一处规则
        e = enter_ledger(profile, store, sig, today, identity, by=src("跟踪进展")[0], keep_unpushed=keep_unpushed)
        out["entered"].append(f"线索 {no}:{e.msg}")
        if not e.settled:
            pending = "pending"
        elif not e.created:
            add_note(store, sid, f"跟踪即入账:{e.msg}", by=SYSTEM)
    if new["负责人"] != ov.get("负责人"):
        name = _name(f.get("负责人"), users, team) if new["负责人"] else ""
        if name != (current_owners(events).get(no) or {}).get("value", ""):
            by, when = src("负责人")
            set_owner(store, sid, name, by=by, at=when)
    if (new["下一步"], new["下一步到期"]) != (ov.get("下一步"), ov.get("下一步到期")):
        cur = current_next(events).get(no) or {}
        if (new["下一步"] or "", new["下一步到期"] or "") != (cur.get("value", ""), cur.get("due", "")):
            by, when = src("下一步", "下一步到期")
            set_next(store, sid, new["下一步"] or "", new["下一步到期"], by=by, at=when)
    return keep, pending


# ---------- 表格从备份恢复以后:对齐到上次同步 ----------

def after_restore(profile: Profile, store: Store, restored_from: datetime, url: str, token: str, base_name: str | None = None,
                  log=print, api: Teable | None = None) -> dict:
    """表格从备份恢复以后、下一次拉回之前用(restored_from:备份的时间,带时区)。表格里是备份时的旧值,不对齐的话下次拉回会当成
    同事改的,把系统也改回去、记下没发生过的操作。这里逐格改回上次同步时的值(同步状态里的 v),不写操作记录;只改同时满足的格:
    - 备份以后系统对这一格知道了新值:最新的同步状态晚于备份,且这一格与备份时刻那时的同步状态不同(推送写回时没碰的格、
      认不出的选项沿用旧值,不算);
    - 表格里仍是别的值,且恢复以后没人再改过(记录历史里这一格没有晚于备份的修改;改过的留给拉回照常拉)。
    原负责人已不在库里、写不回的,同步状态改成表格现在的值并列出来。读不到库成员、字段或记录历史时不改,报错(稍后重跑)。
    备份以后才建的线索下次推送时重建,负责人、下一步等要在表格里重填,列出来。重跑、时间填早了都不会覆盖恢复以后的改动。
    → {leads, projects, fields, kept: [说明], refill: [编号], follows_lost, warnings}"""
    if restored_from.tzinfo is None:
        raise ValueError("备份时间要带时区,如 2026-10-05T03:30+08:00")
    api = api or Teable(url, token)
    base_name = base_name or (profile.teable or {}).get("base") or BASE_NAME
    out: dict = {"leads": 0, "projects": 0, "fields": 0, "kept": [], "refill": [], "follows_lost": 0, "warnings": []}
    base = find_base(api, base_name)
    if base is None:
        raise TeableError(f"表格里没有库「{base_name}」:恢复得不对,或 profile 的 teable.base 写错了")
    tables = {t["name"]: t for t in api.tables(base["id"])}
    if T_PROJECTS not in tables and T_PROJECTS_OLD in tables:  # 还没推送过新版:原来的表
        tables[T_PROJECTS] = tables[T_PROJECTS_OLD]
    try:
        members = {str(c["userId"]) for c in api.collaborators(base["id"]) if c.get("userId")}
    except (TeableError, AttributeError, KeyError, TypeError, httpx.HTTPError) as e:  # 带类型转换写成员字段时,不在库里的账号会被悄悄清空
        raise TeableError(f"读不到库的成员,没法核对负责人,这次没对齐(稍后重跑):{e}") from e
    last, then = load_state(store), load_state(store, until=restored_from)
    hctx: dict = {}
    lines: list[dict] = []
    for tname, kind, names, line in ((T_LEADS, "leads", LEAD_HUMAN, _lead_line), (T_PROJECTS, "projects", PROJECT_HUMAN, _project_line)):
        if tname not in tables:
            continue
        tid, updates = tables[tname]["id"], []
        fids = {str(x["name"]): str(x["id"]) for x in api.fields(tid)}
        recs = _one_per_key(api.records(tid), out["warnings"], tname)
        for key, rec in recs.items():
            st = last[kind].get(key)
            at = _ts((st or {}).get("at"))
            if st is None or at is None or at <= restored_from:
                continue
            label = f"{tname} {key[:8] if kind == 'leads' else key}"
            now, want = _human(rec.get("fields") or {}, names), {**dict.fromkeys(names), **st["v"]}
            was = {**dict.fromkeys(names), **((then[kind].get(key) or {}).get("v") or {})}
            diff = {f: want[f] for f in names if now.get(f) != want[f] and want[f] != was[f]}
            if not diff:
                continue
            hist = _field_changes(api, tid, rec, {f: fids.get(f, "") for f in diff}, {}, {}, hctx)
            if hctx.get("why"):
                raise TeableError(f"读不到记录历史,没法判断恢复以后有没有人改过,这次没对齐(稍后重跑):{hctx['why']}")
            for f in [f for f in diff if (_ts((hist.get(f) or ("", None))[1]) or restored_from) > restored_from]:
                diff.pop(f)
                out["kept"].append(f"{label}:{f}恢复以后在表格里改过,不改回(下次拉回照常拉)")
            if diff.get("负责人") and diff["负责人"] not in members:
                diff.pop("负责人")
                lines.append({**line(key, {**want, "负责人": now.get("负责人")}, st.get("s") or {}),
                              **({"enter": st["enter"]} if st.get("enter") else {})})
                out["kept"].append(f"{label}:原负责人已不在库里,表格里保留现在的负责人,请核对")
            if diff:
                updates.append((rec["id"], diff))
                out[kind] += 1
                out["fields"] += len(diff)
        if updates:
            api.update_records(tid, updates)
        if kind == "leads":  # 备份以后才有的线索:表格里没了,下次推送重建,人填的负责人、下一步等不会跟着回来
            out["refill"] = sorted(k[:8] for k, st in last[kind].items() if k not in recs and k not in then[kind]
                                   and any((st.get("v") or {}).get(f) for f in ("负责人", "下一步", "下一步到期", "结果原因")))
    append_state(store, lines)
    if T_FOLLOW in tables:  # 备份以后拉回过、推上去的跟进,表格里没了:记下(flost),拉回时不当作删除,下次推送按系统里的重建
        now_ids = {str(r.get("id")) for r in api.records(tables[T_FOLLOW]["id"])}
        before, after = then["follows"] | set(then["fpush"].values()), last["follows"] | set(last["fpush"].values())
        lost = sorted(after - before - now_ids - last["fgone"])
        append_state(store, [{"at": _now(), "t": "flost", "k": rid} for rid in lost])
        out["follows_lost"] = len(lost)
    log(f"对齐:备份以后同步过、表格里是旧值的 线索 {out['leads']} 条、项目 {out['projects']} 个,改回上次同步时的值 {out['fields']} 项"
        + (f";备份以后记的跟进 {out['follows_lost']} 条表格里没了,下次推送按系统里的重建" if out["follows_lost"] else ""))
    if out["refill"]:
        log(f"  ! 备份以后新出现的线索 {len(out['refill'])} 条,下次推送重建,负责人、下一步、结果原因要在表格里重填(操作记录里有):"
            + "、".join(out["refill"][:10]) + (" 等" if len(out["refill"]) > 10 else ""))
    for x in out["kept"] + out["warnings"]:
        log("  ! " + x)
    return out


# ---------- 项目表的人管列 → 内部叠加(private/overlay/<国别>.yaml) ----------

OVERLAY_KEYS = {"priority": "优先级", "owner": "负责人", "note": "内部备注"}


def _yaml_text(v: object):
    """写进 overlay 的文字:多行的用块写法;PyYAML(YAML 1.1)会读成别的值的(no、Yes、ON、1:20…)加引号。"""
    s = str(v)
    if "\n" in s:
        return LiteralScalarString(s)
    try:
        same = yaml.safe_load(s) == s
    except yaml.YAMLError:
        same = False
    return s if same else SingleQuotedScalarString(s)


def write_overlay(profile: Profile, changes: dict[str, dict], today: date, identity: tuple[str, ...] = git.BOT_ID,
                  warns: list[str] | None = None) -> tuple[list[str], set[str]]:
    """项目表里改的优先级、负责人、内部备注写进内部叠加 items.<编号>.{priority, owner, note}(值为空即删去该项):
    按国别往返读写 overlay(保留注释与写法)、校验,提交推送 main → (说明列表, 写成并推上去了的项目编号)。
    国别没配 overlay 文件、写坏了、推送失败的不算写成,提示原因(拉回时这些项目下次再拉)。
    说明与提交说明里只写改了哪一项(优先级带值),不写负责人与内部备注的内容。"""
    warns = warns if warns is not None else []
    out: list[str] = []
    written: set[str] = set()
    by_country: dict[str, dict[str, dict]] = {}
    for iid, ch in changes.items():
        c = next((c for c in profile.countries if iid.startswith(f"{c.iso3}-")), None)
        if c is None or not c.overlay:
            warns.append(f"项目 {iid} 的国别没有配置内部叠加文件(profile 的 overlay),表格里改的{'、'.join(OVERLAY_KEYS[k] for k in ch)}没写进去")
            continue
        by_country.setdefault(c.iso3, {})[iid] = ch
    for iso, chs in by_country.items():
        fp = Path(next(c.overlay for c in profile.countries if c.iso3 == iso))
        text = fp.read_text(encoding="utf-8") if fp.exists() else f"schema: {OVERLAY_SCHEMA}\ncountry: {iso}\n"
        y = _yaml(text)
        doc = y.load(text)
        items = doc.get("items")
        if items is None:
            items = doc["items"] = CommentedMap()
        done = []
        for iid, ch in chs.items():
            it = items.get(iid)
            if it is None:
                it = items[iid] = CommentedMap()
            for k, v in ch.items():
                if v in (None, ""):
                    it.pop(k, None)
                else:
                    it[k] = _yaml_text(v)
            if not it:
                del items[iid]
            done.append(f"{iid} " + "、".join(f"{OVERLAY_KEYS[k]}清空" if v in (None, "") else f"优先级 {v}" if k == "priority"
                                              else OVERLAY_KEYS[k] for k, v in ch.items()))
        doc["as_of"] = today
        buf = io.StringIO()
        y.dump(doc, buf)
        tmp = fp.with_suffix(".tmp.yaml")
        tmp.write_text(buf.getvalue(), encoding="utf-8")
        try:
            load_overlay(tmp)
        except (LedgerError, yaml.YAMLError) as e:  # 写坏了就不写,提示原因
            tmp.unlink(missing_ok=True)
            warns.append(f"内部叠加 {fp.name} 写入后校验不通过,没写:{e}")
            continue
        tmp.replace(fp)
        root = git.toplevel(fp)
        msg = f"内部叠加:{iso} 表格里改的项目优先级、负责人、内部备注({';'.join(done)[:150]})"
        r = git.commit_program(root, [fp], msg, identity) if root else None
        if r is not None and not r.ok:
            warns.append(f"内部叠加 {fp.name} 已写入,提交或推送失败(下次拉回再试):{r.output[:200]}")
            continue
        written |= set(chs)
        out.append(f"内部叠加 {iso}:" + ";".join(done))
    return out, written


def settings() -> tuple[str | None, str | None]:
    """TEABLE_URL / TEABLE_TOKEN:环境变量优先,其次程序目录的 .env(OSINT_WATER_ENV 可指到别处)。"""
    env = Path(os.environ.get("OSINT_WATER_ENV") or Path(__file__).resolve().parent.parent / ".env")
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return os.environ.get("TEABLE_URL"), os.environ.get("TEABLE_TOKEN")
