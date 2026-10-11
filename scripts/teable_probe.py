"""阶段 A 能力核验(架构方案第 8 节):在新库里建几张试验表,逐项核验本实例的 Teable 能不能做方案要用的事,出一张核验表;
核验完删掉试验表(--keep 留着,好在界面里人工确认看板拖动与评论通知)。只动新库(库名以「新版」结尾,没有就建)。

    TEABLE_URL=https://<域名> python scripts/teable_probe.py                     # 提示输入管理员令牌(不回显)
    python scripts/teable_probe.py --keep --mention 张三                          # 留下试验表;评论 @张三,看他收没收到通知
    python scripts/teable_probe.py --invite-bot                                    # 另外生成新库的邀请链接(编辑者),给机器账号注册用
    python scripts/teable_probe.py --out docs/阶段A能力核验.md                     # 核验表另存一份
    python scripts/teable_probe.py --recheck                                       # 第二天:看含 TODAY 的公式隔天重算了没有,然后删试验表

令牌用管理员的(TEABLE_ADMIN_TOKEN;建库、建表要所有者或创建者),只在本机或执行会话里用,不进服务器。核验表里没有令牌。
"""
from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import ontology as onto_mod  # noqa: E402
from core import tstore  # noqa: E402

TMAP = onto_mod.load_map()
TZ = TMAP["timezone"]  # 时区与库名只在 teable_map.yaml 写一份(审查二-6)
TRIAL_TODAY, TRIAL_TOMORROW = "今日(试)", "明日(试)"  # 给第二天复核用的两条跟进
DATE = {"formatting": {"date": "YYYY-MM-DD", "time": "None", "timeZone": TZ}}


@dataclass
class Check:
    name: str
    result: str  # ✓ 可以 / ✗ 不行 / ? 待人工确认
    note: str
    fallback: str


def _day(v) -> str | None:
    """Teable 的日期值 → 上海日期 YYYY-MM-DD(实现在 core.tstore.day)。"""
    return tstore.day(v, TZ)


def today_sh() -> date:
    return datetime.now(ZoneInfo(TZ)).date()


class Probe:
    def __init__(self, api: tstore.Client, base_name: str, log=print, sleep=time.sleep):
        self.api, self.log, self.sleep = api, log, sleep
        self.base_name = tstore.require_new_base(base_name)
        self.checks: list[Check] = []
        self.made: list[str] = []  # 本次建的试验表(只删这些)
        self.base: dict = {}
        self.version = ""
        self.title = "阶段 A 能力核验"

    def add(self, name: str, ok: bool | None, note: str, fallback: str = "") -> None:
        self.checks.append(Check(name, "✓ 可以" if ok else "? 待人工确认" if ok is None else "✗ 不行", note, fallback))
        self.log(f"  {self.checks[-1].result} {name}:{note}")

    def run(self, step: str, fallback: str, fn, needs: tuple[str, ...] = ()) -> None:
        """跑一项核验;出错记 ✗(接口的原话);needs 里的前置结果(关联字段、试验记录)没做成的记"没测",不中断。"""
        if any(getattr(self, a, None) is None for a in needs):
            self.add(step, None, "没测:前置项(关联字段或写记录)没通过", fallback)
            return
        try:
            fn()
        except (tstore.TStoreError, KeyError, StopIteration, TypeError, ValueError, AttributeError) as e:
            self.add(step, False, f"{type(e).__name__}:{str(e)[:220]}", fallback)

    # ---------- 接口说明 ----------
    def discover(self) -> None:
        try:
            r = self.api.get_raw("/docs-json")
            doc = r.json() if r.status_code == 200 else {}
        except Exception as e:  # noqa: BLE001 — 接口说明拿不到不影响后面的实测
            doc, r = {}, None
            self.add("接口说明 /docs-json", False, f"拿不到:{type(e).__name__}", "按下面的实测为准")
            return
        paths = doc.get("paths") or {}
        self.version = str((doc.get("info") or {}).get("version") or "")
        text = str(doc)
        want = {"/api/table/{tableId}/aggregation/row-count": "按条件计数", "/api/table/{tableId}/record/{recordId}/history": "记录历史",
                "/api/comment/{tableId}/{recordId}/create": "评论", "/api/base/{baseId}/invitation/link": "库级邀请链接",
                "/api/table/{tableId}/record/{recordId}/{fieldId}/uploadAttachment": "附件上传"}
        missing = [v for k, v in want.items() if k not in paths]
        self.add("接口说明 /docs-json", bool(paths), f"{len(paths)} 个接口" + (f",版本 {self.version}" if self.version else "")
                 + (f";没找到:{'、'.join(missing)}" if missing else ";方案要用的接口都在"), "按下面的实测为准")
        self.add("说明里有 trackedFieldIds", "trackedFieldIds" in text, "最后修改人/时间可只跟踪指定字段" if "trackedFieldIds" in text
                 else "接口说明里没有", "A18 改读记录历史接口")
        lo = [k for k in ("filter", "sort", "limit") if f"'{k}'" in text]
        self.add("说明里的查找选项", None, "lookupOptions 可能有:" + ("、".join(lo) or "(没认出)") + ";以下面的实测为准", "")

    # ---------- 实测 ----------
    def setup(self) -> None:
        self.base = self.api.ensure_new_base(self.base_name)
        self.prefix = "_核验_" + datetime.now(ZoneInfo(TZ)).strftime("%m%d%H%M%S") + "_"  # 上海日期:--recheck 由它算建表那天
        self.rev = self.o1 = self.o2 = self.fs = None  # 前置结果:关联的反向字段、试验记录;没做成时依赖它们的项记"没测"
        mk = self._mk
        self.opp = mk("商机", [{"name": "名称", "type": "singleLineText"},
                               {"name": "阶段", "type": "singleSelect", "options": {"choices": [{"name": x} for x in ("建议", "跟踪中", "准备投标")]}},
                               {"name": "备注", "type": "singleLineText"}])
        self.fol = mk("跟进", [{"name": "摘要", "type": "singleLineText"}, {"name": "内容", "type": "longText"},
                               {"name": "日期", "type": "date", "options": DATE},
                               {"name": "判断", "type": "singleSelect", "options": {"choices": [{"name": x} for x in ("继续跟进", "观望")]}},
                               {"name": "记录人", "type": "user", "options": {"isMultiple": False, "shouldNotify": False}},
                               {"name": "附件", "type": "attachment"}])

    def _mk(self, name: str, fields: list[dict]) -> dict:
        t = self.api.create_table(self.base["id"], self.prefix + name, fields)
        self.made.append(t["id"])
        return t

    def f(self, t: dict) -> dict[str, dict]:
        return {x["name"]: x for x in self.api.fields(t["id"])}

    def checks_all(self, mention: str | None) -> None:
        self.run("关联与反向字段改名", "关联不能改名时,反向字段保留 Teable 起的名字", self.c_link)
        self.run("单向关联(isOneWay)", "改用双向关联,反向字段放在最后", self.c_oneway)
        self.run("写记录(按字段编号)与按条件计数", "页面按条件计数不行时:只取需要的字段分页数", self.c_records, needs=("rev",))
        self.run("经关联的查找带 sort/limit", "(按 Teable 源码,关联查找只收 filter:预期收不下,所以用下面的条件查找)",
                 self.c_lookup_link, needs=("rev", "o1"))
        self.run("条件查找(isConditionalLookup,按日期倒序取第一条)", "最近跟进、当前判断、下一步由后台算(标注\"后台算\")",
                 self.c_lookup_cond, needs=("rev", "o1"))
        self.run("汇总 max / countall", "后台算", self.c_rollup, needs=("rev", "o1"))
        self.run("最后修改人/时间只跟踪一个字段", "A18 改读记录历史接口", self.c_tracked, needs=("o2",))
        self.run("记录历史接口", "A18 只靠阶段修改人/时间", self.c_history, needs=("o2",))
        self.run("公式做主字段", "主字段用单行文本,由人或机器填", self.c_primary_formula, needs=("fs",))
        self.run("公式 DATE_ADD", "最晚启动由后台算", self.c_date_formula, needs=("fs",))
        self.run("单选的缺省值", "上传的线索由后台补处理状态与来源方式", self.c_default)
        self.run("看板视图(按单选分列)", "用按阶段分组的表格视图", self.c_kanban)
        self.run("日历视图", "只读页面的日历", self.c_calendar)
        self.run("附件上传", "报告文件放主机,表里写链接", self.c_attachment, needs=("fs",))
        self.run("评论 @成员", "到期提醒不用评论,改看日历", lambda: self.c_comment(mention), needs=("o1",))
        self.run("单次读 1000 行与翻页", "每页 500 行", self.c_paging)
        self.run("含 TODAY 的公式与 today 筛选(隔天重算要第二天 --recheck)", "后台每天把过期的时间节点改为「已发生」(实施审查三-A 的 (b))",
                 self.c_today, needs=("rev", "o1"))
        self.add("看板拖动改阶段", None, "接口测不了:用 --keep 留下试验表,在看板里拖一张卡片,看「阶段」变没变", "用按阶段分组的表格视图")
        self.add("静态页面带会话读接口", None, "阶段 E 部署只读页面时在浏览器里确认(同域 cookie)", "只读页面经 Caddy 转发带令牌读(只读令牌)")

    def c_link(self) -> None:
        link = self.api.create_field(self.fol["id"], {"name": "商机", "type": "link",
                                                      "options": {"relationship": "manyOne", "foreignTableId": self.opp["id"]}})
        sym = (link.get("options") or {}).get("symmetricFieldId")
        of = {x["id"]: x for x in self.api.fields(self.opp["id"])}
        if not sym or sym not in of:
            self.add("关联与反向字段改名", False, "没有自动生成反向字段", "反向字段手工建")
            return
        self.rev = sym
        self.api.update_field(self.opp["id"], sym, name="跟进记录")
        ok = {x["id"]: x for x in self.api.fields(self.opp["id"])}[sym]["name"] == "跟进记录"
        self.add("关联与反向字段改名", ok, f"反向字段原名「{of[sym]['name']}」,改名{'成功' if ok else '没生效'}")

    def c_oneway(self) -> None:
        before = len(self.api.fields(self.opp["id"]))
        self.api.create_field(self.fol["id"], {"name": "建议商机", "type": "link",
                                               "options": {"relationship": "manyOne", "foreignTableId": self.opp["id"], "isOneWay": True}})
        ok = len(self.api.fields(self.opp["id"])) == before
        self.add("单向关联(isOneWay)", ok, "对方表没有多出反向字段" if ok else "对方表还是多了反向字段")

    def c_records(self) -> None:
        of, ff = self.f(self.opp), self.f(self.fol)
        self.o1, self.o2 = self.api.create_records(self.opp["id"], [{of["名称"]["id"]: "试验商机甲", of["阶段"]["id"]: "跟踪中"},
                                                                    {of["名称"]["id"]: "试验商机乙", of["阶段"]["id"]: "建议"}])
        rows = [{ff["内容"]["id"]: c, ff["日期"]["id"]: d, ff["判断"]["id"]: j, ff["商机"]["id"]: {"id": self.o1["id"]}}
                for c, d, j in (("第一次", "2026-09-01", "观望"), ("第二次", "2026-09-20", "继续跟进"), ("第三次", "2026-09-10", "观望"))]
        self.fs = self.api.create_records(self.fol["id"], rows)
        flt = {"conjunction": "and", "filterSet": [{"fieldId": of["阶段"]["id"], "operator": "is", "value": "建议"}]}
        n = self.api.count(self.opp["id"], flt)
        got = self.api.records(self.fol["id"], projection=[ff["内容"]["id"]])
        proj = all(set(r["fields"]) <= {ff["内容"]["id"]} for r in got)
        d = _day(next(r for r in self.api.records(self.fol["id"]) if r["id"] == self.fs[0]["id"])["fields"].get(ff["日期"]["id"]))
        self.add("写记录(按字段编号)与按条件计数", n == 1 and d == "2026-09-01",
                 f"按条件计数 {n}(应为 1);projection {'只回指定字段' if proj else '不起作用(仍回全部字段)'};日期写 2026-09-01 读回 {d}")

    def c_lookup_link(self) -> None:
        """经关联字段的查找(isLookup + linkFieldId)带上 sort、limit:按 Teable 源码,接口只收 filter、静默丢掉 sort 与 limit——
        实测回显,说明为什么"最近一条"不能这样做。"""
        ff = self.f(self.fol)
        fld = self.api.create_field(self.opp["id"], {"name": "判断(经关联)", "type": "singleSelect", "isLookup": True,
                                                     "options": ff["判断"].get("options"),
                                                     "lookupOptions": {"foreignTableId": self.fol["id"], "linkFieldId": self.rev,
                                                                       "lookupFieldId": ff["判断"]["id"],
                                                                       "sort": {"fieldId": ff["日期"]["id"], "order": "desc"}, "limit": 1}})
        lo = fld.get("lookupOptions") or {}
        kept = [k for k in ("sort", "limit") if lo.get(k)]
        self.sleep(1)
        v = next(r for r in self.api.records(self.opp["id"]) if r["id"] == self.o1["id"])["fields"].get(fld["id"])
        vals = v if isinstance(v, list) else [v]
        self.add("经关联的查找带 sort/limit", len(kept) == 2 and vals == ["继续跟进"],
                 f"接口收下了 {('、'.join(kept) or '无')};结果 {vals}(收下了才会是 [继续跟进],否则是三条全部)")

    def c_lookup_cond(self) -> None:
        """条件查找(isConditionalLookup):不经关联字段,filter 里写"对方表的商机 = 本行"(value 写 {type: field, fieldId: 本表主字段}),
        按日期倒序取 1 条——core/schema.py 建「当前判断」等字段就是这个写法。"""
        of, ff = self.f(self.opp), self.f(self.fol)
        lo = {"foreignTableId": self.fol["id"], "lookupFieldId": ff["判断"]["id"],
              "filter": {"conjunction": "and", "filterSet": [
                  {"fieldId": ff["商机"]["id"], "operator": "is", "value": {"type": "field", "fieldId": of["名称"]["id"]}},
                  {"fieldId": ff["判断"]["id"], "operator": "isNotEmpty", "value": None}]},
              "sort": {"fieldId": ff["日期"]["id"], "order": "desc"}, "limit": 1}
        fld = self.api.create_field(self.opp["id"], {"name": "当前判断", "type": "singleSelect", "isLookup": True, "isConditionalLookup": True,
                                                     "options": ff["判断"].get("options"), "lookupOptions": lo})
        got = fld.get("lookupOptions") or {}
        kept = [k for k in ("filter", "sort", "limit") if got.get(k)] + (["isConditionalLookup"] if fld.get("isConditionalLookup") else [])
        self.sleep(1)
        rows = {r["id"]: r for r in self.api.records(self.opp["id"])}
        v1, v2 = rows[self.o1["id"]]["fields"].get(fld["id"]), rows[self.o2["id"]]["fields"].get(fld["id"])
        vals = v1 if isinstance(v1, list) else [v1]
        self.add("条件查找(isConditionalLookup,按日期倒序取第一条)", (vals == ["继续跟进"] and not v2) if len(kept) == 4 else False,
                 f"接口收下了 {('、'.join(kept) or '无')};甲 {vals}(应为 [继续跟进],即 9 月 20 日那条),乙 {v2}(应为空:乙没有跟进)")

    def c_rollup(self) -> None:
        ff = self.f(self.fol)
        lo = {"foreignTableId": self.fol["id"], "linkFieldId": self.rev, "lookupFieldId": ff["日期"]["id"]}
        mx = self.api.create_field(self.opp["id"], {"name": "最近跟进", "type": "rollup", "lookupOptions": lo,
                                                    "options": {"expression": "max({values})", **DATE}})
        ct = self.api.create_field(self.opp["id"], {"name": "跟进数", "type": "rollup", "lookupOptions": lo,
                                                    "options": {"expression": "countall({values})"}})
        rec = next(r for r in self.api.records(self.opp["id"]) if r["id"] == self.o1["id"])["fields"]
        m, c = _day(rec.get(mx["id"])), rec.get(ct["id"])
        self.add("汇总 max / countall", m == "2026-09-20" and c == 3, f"max 日期 {m}(应为 2026-09-20),countall {c}(应为 3)")

    def c_tracked(self) -> None:
        of = self.f(self.opp)
        by = self.api.create_field(self.opp["id"], {"name": "阶段修改人", "type": "lastModifiedBy",
                                                    "options": {"trackedFieldIds": [of["阶段"]["id"]]}})
        at = self.api.create_field(self.opp["id"], {"name": "阶段修改时间", "type": "lastModifiedTime",
                                                    "options": {"trackedFieldIds": [of["阶段"]["id"]], **DATE}})
        kept = (by.get("options") or {}).get("trackedFieldIds") == [of["阶段"]["id"]]

        def now():
            return next(r for r in self.api.records(self.opp["id"]) if r["id"] == self.o2["id"])["fields"].get(at["id"])
        self.api.update_records(self.opp["id"], [(self.o2["id"], {of["阶段"]["id"]: "跟踪中"})])
        t1 = now()
        self.sleep(1.2)
        self.api.update_records(self.opp["id"], [(self.o2["id"], {of["备注"]["id"]: "只改备注"})])
        t2 = now()
        self.sleep(1.2)
        self.api.update_records(self.opp["id"], [(self.o2["id"], {of["阶段"]["id"]: "准备投标"})])
        t3 = now()
        ok = kept and t1 is not None and t1 == t2 and t3 != t2
        self.add("最后修改人/时间只跟踪一个字段", ok, f"选项{'收下了' if kept else '没收下'};改阶段 {t1} → 只改备注 {t2} → 再改阶段 {t3}"
                 "(只改备注时应不变)")

    def c_history(self) -> None:
        h = self.api.record_history(self.opp["id"], self.o2["id"])
        n = len(h.get("historyList") or [])
        self.add("记录历史接口", n > 0, f"{n} 条修改历史")

    def c_primary_formula(self) -> None:
        ff = self.f(self.fol)
        expr = (f'DATETIME_FORMAT({{{ff["日期"]["id"]}}}, "YYYY-MM-DD") & " · " & {{{ff["记录人"]["id"]}}} & " · " & '
                f'LEFT({{{ff["内容"]["id"]}}}, 40)')
        self.api.convert_field(self.fol["id"], ff["摘要"]["id"], {"type": "formula", "options": {"expression": expr, "timeZone": TZ}})
        nf = self.f(self.fol)["摘要"]
        v = next(r for r in self.api.records(self.fol["id"]) if r["id"] == self.fs[0]["id"])["fields"].get(nf["id"])
        ok = nf["type"] == "formula" and nf.get("isPrimary")
        self.add("公式做主字段", ok if v else None, f"主字段类型 {nf['type']};值「{v}」(应为 2026-09-01 · · 第一次)")

    def c_date_formula(self) -> None:
        ff = self.f(self.fol)
        d = self.api.create_field(self.fol["id"], {"name": "最晚启动(试)", "type": "formula",
                                                   "options": {"expression": f'DATE_ADD({{{ff["日期"]["id"]}}}, -84, "day")', "timeZone": TZ, **DATE}})
        rec = next(r for r in self.api.records(self.fol["id"]) if r["id"] == self.fs[0]["id"])["fields"]
        v = _day(rec.get(d["id"]))
        self.add("公式 DATE_ADD", v == "2026-06-09" if v else None, f"2026-09-01 − 84 天 = {v}(应为 2026-06-09)")

    def c_today(self) -> None:
        """含 TODAY() 的公式、today 筛选的条件查找(线索「最近截止」、商机「下一时间节点」、「最晚启动」的写法):当天的值应该对;
        隔天会不会重算,Teable 源码里没有按天重算的任务,要第二天跑 --recheck 看。留两条跟进(今日、明日)给第二天比。"""
        of, ff = self.f(self.opp), self.f(self.fol)
        d0 = today_sh()
        self.api.create_records(self.fol["id"], [
            {ff["内容"]["id"]: TRIAL_TODAY, ff["日期"]["id"]: d0.isoformat(), ff["商机"]["id"]: {"id": self.o1["id"]}},
            {ff["内容"]["id"]: TRIAL_TOMORROW, ff["日期"]["id"]: (d0 + timedelta(days=1)).isoformat(), ff["商机"]["id"]: {"id": self.o1["id"]}}])
        self.api.create_field(self.fol["id"], {"name": "今天(试)", "type": "formula", "options": {"expression": "TODAY()", "timeZone": TZ, **DATE}})
        self.api.create_field(self.fol["id"], {"name": "未过(试)", "type": "formula", "options": {
            "expression": f'IF(IS_BEFORE({{{ff["日期"]["id"]}}}, TODAY()), BLANK(), {{{ff["日期"]["id"]}}})', "timeZone": TZ, **DATE}})
        self.api.create_field(self.opp["id"], {"name": "下一跟进日(试)", "type": "date", "isLookup": True, "isConditionalLookup": True, "options": DATE,
                                               "lookupOptions": {"foreignTableId": self.fol["id"], "lookupFieldId": ff["日期"]["id"],
                                                                 "filter": {"conjunction": "and", "filterSet": [
                                                                     {"fieldId": ff["商机"]["id"], "operator": "is",
                                                                      "value": {"type": "field", "fieldId": of["名称"]["id"]}},
                                                                     {"fieldId": ff["日期"]["id"], "operator": "isOnOrAfter",
                                                                      "value": {"mode": "today", "timeZone": TZ}}]},
                                                                 "sort": {"fieldId": ff["日期"]["id"], "order": "asc"}, "limit": 1}})
        self.sleep(1)
        got = self._today_values()
        d = d0.isoformat()
        ok = got["今天"] == d and got["未过"].get(TRIAL_TODAY) == d and got["下一"] == d
        self.add("含 TODAY 的公式与 today 筛选(隔天重算要第二天 --recheck)", ok,
                 f"TODAY()={got['今天']},未过(试)={got['未过'].get(TRIAL_TODAY)},甲的下一跟进日={got['下一']}(都应为 {d});"
                 "明天跑 --recheck 看它们有没有跟着变")

    def _today_values(self) -> dict:
        """第二天复核要看的几个值:TODAY() 公式、今日/明日两条的「未过(试)」与日期、甲的「下一跟进日(试)」。"""
        of, ff = self.f(self.opp), self.f(self.fol)
        rows = self.api.records(self.fol["id"])
        by = {r["fields"].get(ff["内容"]["id"]): r["fields"] for r in rows}
        first = rows[0]["fields"] if rows else {}
        jia = next((r for r in self.api.records(self.opp["id"]) if r["fields"].get(of["名称"]["id"]) == "试验商机甲"), {"fields": {}})["fields"]
        nxt = jia.get(of["下一跟进日(试)"]["id"]) if "下一跟进日(试)" in of else None
        nxt = nxt[0] if isinstance(nxt, list) and nxt else nxt
        trial = {k: v for k, v in by.items() if k in (TRIAL_TODAY, TRIAL_TOMORROW)}
        return {"今天": _day(first.get(ff["今天(试)"]["id"])) if "今天(试)" in ff else None,
                "未过": {k: _day(v.get(ff["未过(试)"]["id"])) for k, v in trial.items()} if "未过(试)" in ff else {},
                "日期": {k: _day(v.get(ff["日期"]["id"])) for k, v in trial.items()}, "下一": _day(nxt)}

    def recheck(self, keep: bool = False) -> None:
        """第二天:找上次 --keep 留下的试验表,看含 TODAY() 的公式、today 筛选的条件查找有没有按新的一天重算;看完删试验表(keep 不删)。"""
        self.title = "阶段 A 第二天复核(含 TODAY 的公式与 today 筛选隔天重算了没有)"
        self.base = self.api.ensure_new_base(self.base_name, create=False) or {}
        if not self.base:
            raise tstore.TStoreError(f"没有「{self.base_name}」库")
        tables = [t for t in self.api.tables(self.base["id"]) if t["name"].startswith("_核验_") and t["name"].count("_") >= 3]
        stamps = sorted({t["name"].split("_")[2] for t in tables})
        if not stamps:
            raise tstore.TStoreError("新库里没有 _核验_ 开头的试验表:先跑一次 --keep,第二天再 --recheck")
        self.prefix = f"_核验_{stamps[-1]}_"
        mine = [t for t in tables if t["name"].startswith(self.prefix)]
        self.made = [t["id"] for t in mine]
        self.opp = next(t for t in mine if t["name"] == self.prefix + "商机")
        self.fol = next(t for t in mine if t["name"] == self.prefix + "跟进")
        d1 = today_sh()
        d0 = datetime.strptime(stamps[-1][:4], "%m%d").date().replace(year=d1.year)
        if d0 > d1:
            d0 = d0.replace(year=d1.year - 1)
        got = self._today_values()
        if d1 == d0:
            self.add("含 TODAY 的公式隔天重算", None, f"试验表是今天({d0})建的,同一天看不出;明天再跑 --recheck", "")
        else:
            expect = min((d for d in got["日期"].values() if d and d >= d1.isoformat()), default=None)
            self.add("含 TODAY 的公式隔天重算", got["今天"] == d1.isoformat(), f"TODAY() 读回 {got['今天']}(应为 {d1};建表那天是 {d0})",
                     "后台每天把过期的时间节点改为「已发生」(实施审查三-A 的 (b)):改状态会触发重算,公式照样对")
            self.add("含 TODAY 的公式:过期的行隔天清空", got["未过"].get(TRIAL_TODAY) is None,
                     f"{d0} 那条的未过(试)读回 {got['未过'].get(TRIAL_TODAY)}(应为空)", "同上")
            self.add("today 筛选的条件查找隔天重算", got["下一"] == expect, f"甲的下一跟进日读回 {got['下一']}(应为 {expect or '空'})",
                     "商机的下一时间节点由后台算;或同上,改状态触发重算")
        if keep:
            self.log("试验表留着(--keep)。")
        else:
            self.cleanup()

    def c_default(self) -> None:
        of = self.f(self.opp)
        f = of["阶段"]
        self.api.convert_field(self.opp["id"], f["id"], {"type": f["type"], "options": {**(f.get("options") or {}), "defaultValue": "建议"}})
        rec = self.api.create_records(self.opp["id"], [{of["名称"]["id"]: "不填阶段"}])[0]
        v = next(r for r in self.api.records(self.opp["id"]) if r["id"] == rec["id"])["fields"].get(f["id"])
        self.add("单选的缺省值", v == "建议", f"新建不填阶段,读回「{v}」(应为 建议)")

    def c_kanban(self) -> None:
        of = self.f(self.opp)
        v = self.api.create_view(self.opp["id"], {"name": "看板", "type": "kanban", "options": {"stackFieldId": of["阶段"]["id"]}})
        ok = (v.get("options") or {}).get("stackFieldId") == of["阶段"]["id"] or v.get("type") == "kanban"
        self.add("看板视图(按单选分列)", ok, f"视图类型 {v.get('type')},分列字段 {(v.get('options') or {}).get('stackFieldId')}")

    def c_calendar(self) -> None:
        ff = self.f(self.fol)
        v = self.api.create_view(self.fol["id"], {"name": "日历", "type": "calendar",
                                                  "options": {"startDateFieldId": ff["日期"]["id"], "endDateFieldId": ff["日期"]["id"]}})
        self.add("日历视图", v.get("type") == "calendar", f"视图类型 {v.get('type')}")

    def c_attachment(self) -> None:
        ff = self.f(self.fol)
        self.api.upload_attachment(self.fol["id"], self.fs[0]["id"], ff["附件"]["id"], "核验.txt", "能力核验".encode())
        v = next(r for r in self.api.records(self.fol["id"]) if r["id"] == self.fs[0]["id"])["fields"].get(ff["附件"]["id"]) or []
        self.add("附件上传", bool(v), f"附件 {[a.get('name') for a in v]}")

    def c_comment(self, mention: str | None) -> None:
        users = self.api.collaborators(self.base["id"])
        uid = None
        if mention:
            u = next((u for u in users if mention in (u.get("email"), u.get("userName"))), None)
            uid = (u or {}).get("userId")
            if not uid:
                self.add("评论 @成员", False, f"「{mention}」不是这个库的协作者(显示名或邮箱不对),没 @ 到人;改对名字再跑",
                         "到期提醒不用评论,改看日历")
                return
        self.api.add_comment(self.opp["id"], self.o1["id"], "能力核验:请看这条", mention_user_id=uid)
        self.add("评论 @成员", None if uid else True, "评论发出" + (f",@了 {mention}:请他看站内通知收到没有" if uid else
                 "(没给 --mention,没 @ 人;要验通知用 --keep --mention <同事>)"))

    def c_paging(self) -> None:
        t = self._mk("翻页", [{"name": "序号", "type": "singleLineText"}])
        fid = self.f(t)["序号"]["id"]
        self.api.create_records(t["id"], [{fid: str(i)} for i in range(1005)])
        first = self.api.req("GET", f"/table/{t['id']}/record", params={"fieldKeyType": "id", "take": 1000, "skip": 0})
        n1 = len((first or {}).get("records") or [])
        total = len(self.api.records(t["id"]))
        self.add("单次读 1000 行与翻页", n1 == 1000 and total == 1005, f"第一页 {n1} 行(应为 1000),翻页共 {total} 行(应为 1005)")

    def invite(self) -> dict:
        return self.api.base_invitation_link(self.base["id"], "editor")

    def cleanup(self) -> None:
        for tid in reversed(self.made):
            try:
                self.api.delete_table(self.base["id"], tid)
            except tstore.TStoreError as e:
                self.log(f"  ! 试验表 {tid} 没删掉,请在表格里删:{str(e)[:120]}")
        self.made = []

    def table(self) -> str:
        head = (f"# {self.title}({today_sh().isoformat()},新库「{self.base_name}」"
                + (f",Teable {self.version}" if self.version else "") + ")\n\n")
        rows = ["| 能力 | 结果 | 说明 | 后备做法 |", "|---|---|---|---|"]
        rows += [f"| {c.name} | {c.result} | {c.note.replace('|', '/')} | {c.fallback} |" for c in self.checks]
        return head + "\n".join(rows) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="阶段 A:在新库里核验 Teable 的能力(用管理员令牌)")
    ap.add_argument("--base", default=TMAP["base"], help="新库名(缺省见 teable_map.yaml,要以「新版」结尾)")
    ap.add_argument("--keep", action="store_true", help="留下试验表(人工确认看板拖动、评论通知后在表格里删)")
    ap.add_argument("--mention", help="评论里 @ 的同事(显示名或邮箱),看他收没收到站内通知")
    ap.add_argument("--invite-bot", action="store_true", help="生成新库的邀请链接(编辑者),给机器账号注册用")
    ap.add_argument("--out", help="核验表另存到这个文件(Markdown)")
    ap.add_argument("--recheck", action="store_true", help="第二天:复核上次 --keep 留下的试验表里含 TODAY 的公式有没有重算,然后删试验表(--keep 不删)")
    a = ap.parse_args(argv)
    url, token = tstore.credentials(admin=True)
    p = Probe(tstore.Client(url, token), a.base)
    print(f"能力核验:{url} 新库「{a.base}」")
    if a.recheck:
        p.recheck(keep=a.keep)
        return _finish(p, a.out)
    p.discover()
    try:
        p.setup()
        p.checks_all(a.mention)
        if a.invite_bot:
            inv = p.invite()
            print("\n新库的邀请(编辑者;只给机器账号注册用,用完在库的协作者设置里作废;也可以在表格界面里库的「邀请」生成):")
            print("  " + str(inv))
    finally:
        if a.keep:
            print("\n试验表留着(名字以 _核验_ 开头):明天跑 --recheck 复核隔天重算并删掉它们(还要留着就再加 --keep)。")
        else:
            p.cleanup()
    return _finish(p, a.out)


def _finish(p: Probe, out_path: str | None) -> int:
    out = p.table()
    print("\n" + out)
    if out_path:
        Path(out_path).write_text(out, encoding="utf-8")
        print(f"已存:{out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
