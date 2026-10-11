"""阶段 C:一次性把现有数据迁进新库「水务项目机会 新版」(架构方案第 6 节)。先 --dry-run 出对账报告,再写。

    python scripts/migrate_to_teable.py --profile private/profile.yaml --dry-run --report 对账报告.md   # 不连表格
    python scripts/migrate_to_teable.py --profile private/profile.yaml --report 对账报告.md             # 写进新库(管理员令牌)

从哪来:data 分支(profile 的 data_dir:watch/ 下的线索、处理状态、操作记录、信源状态,reports/,snapshots/)与 main 分支(各国台账
ledger.yaml 与 sources.yaml、watch.yaml、内部叠加 private/overlay、市场分布、reference 的国别资料、private/reports)。
旧库只用来对账(data 分支 watch/teable_links.json 记着旧库里有哪些线索、商机),不读也不写旧库。

可重复运行:各表按键增量(线索号、商机编号、ISO3、信源编号;时间节点按"所属 + 类型 + 日期原文";跟进按"商机 + 日期 + 内容、阶段变更、
判断、下一步的指纹";报告按文件名),已有的行只补空着的字段(--sync 时按迁移数据覆盖)。行靠内容识别,同事改过的行再跑会重复建:
重复运行(含 --sync)只在切换前、同事还没用新库时;阶段 F 的增量迁移在冻结旧系统之后、通知同事之前做一次(审查二-4)。
只动新库;派生字段不写(R10)。
"""
from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import geo, schema, tstore  # noqa: E402
from core import ontology as onto_mod  # noqa: E402
from core import opportunity as opp  # noqa: E402
from core.ledger import (  # noqa: E402
    ELIG, FUNDING_STATUS, INSTRUMENTS, MILESTONES, MS_STATUS, PKG_STATUS, PKG_TYPES, SEGMENTS, _refs, eligibility,
    load_ledger, load_overlay, load_rules, parse_pdate, size_usd,
)
from core.watch import flow  # noqa: E402
from core.watch.events import JUDGMENTS, STAGES, current_next, current_owners  # noqa: E402
from core.watch.outputs import CAT_ZH, LEDGER, MANUAL, SECTOR_ZH, _llm, category, links_of, relevant, sectors_of  # noqa: E402
from core.watch.query import STATUS_ZH, row as lead_row  # noqa: E402
from core.watch.store import SYSTEM, Store, signal_id  # noqa: E402

TMAP = onto_mod.load_map()  # 表名、建表顺序、时区、库名只在 teable_map.yaml 写一份(审查二-6)
TZ = ZoneInfo(TMAP["timezone"])
ORDER = list(TMAP["order"])
NAMES = {o: TMAP["tables"][o]["table"] for o in ORDER}
CAT = {**CAT_ZH, "award": "授标或短名单", "plan": "立项或规划", "page": "其他"}  # 旧类别 → 本体的类别
SEG = {k: v.replace("/", "") for k, v in SEGMENTS.items()}  # 水库/调蓄 → 水库调蓄
PUBLIC_STAGE = {"plan": "规划", "preparation": "前期", "financing_signed": "融资已落实", "procurement": "采购进行中", "awarded": "已定标",
                "construction": "在建", "completed": "已完工", "on_hold": "暂停", "cancelled": "取消"}
ELIG_ZH = {k: ("推定可投" if k == "presumed" else v) for k, v in ELIG.items()}
VERIFY = {"verified": "已核实", "partial": "部分核实", "unverified": "未核实", "conflicting": "来源冲突"}
MS_KIND = {"board_approval": "董事会批准", "loan_signing": "贷款签署", "tender_launch": "招标发布", "eoi_deadline": "意向书截止",
           "pq_deadline": "资格预审截止", "bid_deadline": "投标截止", "rfp_deadline": "PPP标书截止", "award": "授标",
           "contract_signing": "合同签署", "construction_start": "开工", "completion": "完工"}  # 其余为「其他」
DEADLINE_KIND = {"bid_deadline": "投标截止", "eoi_deadline": "意向书截止", "pq_deadline": "资格预审截止", "rfp_deadline": "PPP标书截止"}
SOURCE_KIND = {"html_list": "网页列表", "page": "网页列表", "links": "网页列表", "rss": "RSS", "atexo_search": "采购门户",
               "wb_procnotices": "接口", "wb_projects": "接口", "aiib": "接口", "ckan": "接口", "gdelt": "接口"}
SOURCE_STATUS = {"ok": "正常", "empty": "无条目", "error": "出错", "blocked": "被拒"}
REPORT_KIND = {"国别报告": "国别报告", "项目报告": "项目报告", "周报": "周报", "月度台账分析": "月报", "线索简报": "月报"}
EXCERPT_MAX = 20000
NO_STAGE = "建议"


def _key(u: object) -> str:
    return str(u or "").strip().rstrip("/")


def _day(v: object) -> str | None:
    s = str(v or "").strip()[:10]
    return s if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s) else None


def _sha(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:10]


def _lines(items: list[str]) -> str | None:
    out = "\n".join(x for x in items if x and x.strip())
    return out or None


def followup_key(iid: str, day: str, content: str, stage=None, judgment=None, step=None) -> str:
    """跟进的键:商机 + 日期 + (内容、阶段变更、判断、下一步)的指纹——同日同内容但阶段翻转或判断不同的不并掉(审查二-3)。"""
    return f"{iid}|{day}|{_sha('|'.join(str(x or '') for x in (content, stage, judgment, step)))}"


@dataclass
class Row:
    key: str
    fields: dict[str, Any] = field(default_factory=dict)  # 字段名 → 值(不含关联与成员)
    links: dict[str, list[str]] = field(default_factory=dict)  # 关联字段名 → 对方表的键
    people: dict[str, str] = field(default_factory=dict)  # 成员字段名 → 人(岗位代号或表格显示名)
    files: list[Path] = field(default_factory=list)


@dataclass
class Plan:
    rows: dict[str, dict[str, Row]] = field(default_factory=lambda: {o: {} for o in ORDER})
    recon: list[str] = field(default_factory=list)  # 对账报告(Markdown 行)
    warnings: list[str] = field(default_factory=list)

    def add(self, oid: str, r: Row) -> Row:
        have = self.rows[oid].get(r.key)
        if have is None:
            self.rows[oid][r.key] = r
            return r
        for k, v in r.fields.items():  # 同键再来一次:只补空的
            if have.fields.get(k) in (None, "", []):
                have.fields[k] = v
        for k, v in r.links.items():
            have.links[k] = list(dict.fromkeys([*have.links.get(k, []), *v]))
        return have


# ---------- 读旧数据,拼成新库的行 ----------

class Builder:
    def __init__(self, profile, data_dir: Path, today: date):
        self.profile, self.data_dir, self.today = profile, Path(data_dir), today
        self.plan = Plan()
        self.store = Store(self.data_dir)
        self.rules = load_rules(profile.rules) if profile.rules else load_rules()
        self.ledgers = [load_ledger(c.ledger, self.rules) for c in profile.countries if c.ledger]
        self.overlays = {c.iso3: load_overlay(c.overlay) for c in profile.countries if c.overlay}
        self.sigs = {s["id"]: s for s in self.store.load_signals()}
        self.by_no = {sid[:8]: s for sid, s in self.sigs.items()}
        self.events = self.store.events()
        self.triage = self.store.triage()
        self.book = opp.book_for(profile, self.store)
        self.tracked = self.book.tracked(self.events)
        self.declined = opp.declined(self.events)
        self.url_lead = {_key(s.get("url")): sid[:8] for sid, s in self.sigs.items() if s.get("url")}
        self.items = {it["id"]: (led, it) for led in self.ledgers for it in led.items}
        self.src_lead: dict[tuple[str, str], str] = {}  # (国别, 出处编号) → 线索号
        from core.markets import load_markets
        self.markets = load_markets(profile.markets) if profile.markets and Path(profile.markets).exists() else None

    def w(self, msg: str) -> None:
        self.plan.warnings.append(msg)

    # 线索号:已有线索按原文网址认;台账出处新建的按 10.21 的规则(信源 ledger + 网址)
    def lead_for_source(self, led, ref: str) -> str | None:
        k = (led.country, ref)
        if k in self.src_lead:
            return self.src_lead[k]
        sv = led.sources.get(ref) or {}
        u = _key(sv.get("url"))
        if not u or sv.get("grade") == "D" or "内部" in str(sv.get("publisher") or ""):
            self.src_lead[k] = None
            return None
        no = self.url_lead.get(u) or signal_id(LEDGER, u)[:8]
        self.src_lead[k] = no
        return no

    def refs_text(self, led, refs: list[str]) -> str:
        """事实句末的出处:有线索的写线索号,没有的写来源编号(如内部成稿 R033)。"""
        out = []
        for r in dict.fromkeys(refs or []):
            no = self.lead_for_source(led, r)
            out.append(f"[{no}]" if no else f"〔{r}〕")
        return (" " + "".join(out)) if out else ""

    def build(self) -> Plan:
        self.sources()
        self.opportunities()
        self.leads()
        self.ledger_source_leads()
        self.countries()
        self.milestones()
        self.followups()
        self.reports()
        return self.plan

    # ---------- 信源 ----------
    def sources(self) -> None:
        from core.watch.run import global_paths, load_user_sources, load_watch
        user = load_user_sources(self.data_dir)
        disabled = set(user["disabled"])
        state = self.store.state.get("sources") or {}
        defs: list[tuple[dict, str]] = []
        for c in self.profile.countries:
            if c.watch:
                defs += [(s, c.iso3) for s in load_watch(c.watch)["sources"]]
        for fp in global_paths(self.profile):
            defs += [(s, "全球") for s in load_watch(fp)["sources"]]
        defs += [(s, "全球") for s in user["sources"]]
        self.state = state
        for s, scope in defs:
            st = state.get(s["id"]) or {}
            self.plan.add("source", Row(s["id"], {
                "名称": s.get("name") or s["id"], "信源编号": s["id"], "网址": s.get("url"), "类型": SOURCE_KIND.get(s.get("kind")),
                "范围": s.get("country") or scope, "启用": not (s["id"] in disabled or s.get("disabled")),
                "最近采集": st.get("last_run"), "最近状态": SOURCE_STATUS.get(st.get("status"))}))
        self.plan.add("source", Row(MANUAL, {"名称": "人工上传", "信源编号": MANUAL, "类型": "人工上传", "范围": "全球", "启用": True}))
        self.plan.add("source", Row(LEDGER, {"名称": "台账出处", "信源编号": LEDGER, "范围": "全球", "启用": False}))

    # ---------- 商机 ----------
    def stage_of(self, iid: str, it: dict) -> str:
        if iid in self.tracked:
            return STAGES[self.tracked[iid]["value"]]
        if iid in self.declined or it.get("in_scope") is False:
            return "不跟进"
        return NO_STAGE

    def opportunities(self) -> None:
        nat = (self.profile.bidder or {}).get("nationality")
        for led in self.ledgers:
            ov = (self.overlays.get(led.country) or {}).get("items") or {}
            has_entity = bool(((self.profile.bidder or {}).get("local_entity") or {}).get(led.country))
            for it in led.items:
                iid = it["id"]
                rt = lambda obj: self.refs_text(led, _refs(obj))  # noqa: E731
                loc = it.get("location") or {}
                fin = [f"{f.get('funder') or '—'} · {INSTRUMENTS.get(f.get('instrument'), f.get('instrument') or '')} · "
                       f"{_amount(f.get('amount'), f.get('currency'))} · {FUNDING_STATUS.get(f.get('status'), f.get('status') or '')}"
                       + (f" · {f['date']}" if f.get("date") else "") + (f"({f['note']})" if f.get("note") else "") + rt(f)
                       for f in it.get("financing") or []]
                proc = it.get("procurement") or {}
                pkgs = [f"{p.get('name') or '标段'} · {PKG_TYPES.get(p.get('type'), p.get('type') or '')} · {PKG_STATUS.get(p.get('status'), p.get('status') or '')}"
                        + (f" · 中标 {p['awardee']}" if p.get("awardee") else "") + (f" · {p['value']}" if p.get("value") else "")
                        + (f"({p['note']})" if p.get("note") else "") + rt(p) for p in proc.get("packages") or []]
                scope = [f"{s.get('item') or ''}" + (f" {s['qty']}" if s.get("qty") is not None else "") + (f" {s['unit']}" if s.get("unit") else "")
                         + (f"({s['note']})" if s.get("note") else "") + rt(s) for s in it.get("scope") or []]
                tc = it.get("total_cost") or {}
                if tc.get("amount") is not None:
                    scope.insert(0, f"总投资 {_amount(tc.get('amount'), tc.get('currency'))}" + (f"({tc['note']})" if tc.get("note") else "") + rt(tc))
                parties = [f"{p.get('role') or '参与方'}:{p.get('name') or ''}" + (f"({p['note']})" if p.get("note") else "") + rt(p)
                           for p in it.get("parties") or []]
                ver = it.get("verification") or {}
                pending = [str(x) for x in ver.get("pending") or []] + [f"来源冲突:{x}" for x in ver.get("conflicts") or []]
                el = eligibility(it, self.rules, nat, has_entity)
                why = [str(it.get("brief") or "").strip()] + [str(n.get("text") if isinstance(n, dict) else n) + (rt(n) if isinstance(n, dict) else "")
                                                             for n in it.get("notes") or []]
                sources = [no for r in dict.fromkeys(_refs(it)) if (no := self.lead_for_source(led, r))]
                size = size_usd(it, led.fx)
                o = ov.get(iid) or {}
                note = _lines([f"业务线:{o['business_line']}" if o.get("business_line") else "",
                               f"我方角色:{o['role']}" if o.get("role") else "",
                               f"判断:{o['judgement']}" if o.get("judgement") else "",
                               *[f"建议动作:{a.get('action')}" + (f"(到期 {a['due']}" + (f",{a['owner']}" if a.get("owner") else "") + ")"
                                                              if a.get("due") else "") for a in o.get("next_actions") or [] if isinstance(a, dict)],
                               *[f"{x.get('date', '')} 情报:{x.get('content', '')}" + (f"(来源 {x['source']})" if x.get("source") else "")
                                 for x in o.get("intel") or [] if isinstance(x, dict)],
                               str(o.get("note") or "").strip()]) or ""
                if iid not in self.tracked and iid in self.declined and self.declined[iid].get("note"):
                    note = _lines([note, f"不列入正式商机的理由:{self.declined[iid]['note']}"]) or ""
                row = Row(iid, {
                    "商机编号": iid, "名称": it.get("name_zh") or it.get("name_en") or iid,
                    "原文名称": it.get("name_local") or it.get("name_en"),
                    "别名": _lines([str(a) for a in it.get("aliases") or []]),
                    "外部编号": _lines([f"{k}: {v}" for k, v in (it.get("ids") or {}).items()]),
                    "地点": " · ".join(x for x in (loc.get("city_zh") or loc.get("city_en"), loc.get("detail")) if x) or None,
                    "领域": [SEG[s] for s in it.get("segments") or [] if s in SEG] or None,
                    "业主与执行机构": ";".join(f"{k}:{v}" for k, v in (it.get("owner") or {}).items()) or None,
                    "资金": _lines(fin), "公开进度": PUBLIC_STAGE.get(it.get("stage")), "进度说明": it.get("stage_note"),
                    "建设内容与规模": _lines(scope), "标段": _lines(pkgs), "投标资格": ELIG_ZH.get(el.status), "资格依据": el.basis,
                    "建议理由": _lines(why), "核实状态": VERIFY.get(ver.get("status")), "参与方": _lines(parties), "待核实": _lines(pending),
                    "规模(美元)": round(size) if size else None,
                    "我方阶段": self.stage_of(iid, it), "优先级": o.get("priority") if o.get("priority") in ("A", "B", "C") else None,
                    "内部备注": note or None,
                }, links={"国别": [led.country], "出处线索": sources})
                if o.get("owner"):
                    row.people["负责人"] = str(o["owner"])
                self.plan.add("opportunity", row)
                if not sources:
                    self.w(f"商机 {iid} 没有可建成线索的出处(出处都没有网址,或是 D 级、内部成稿)")

    # ---------- 线索 ----------
    def leads(self) -> None:
        owners, nexts = current_owners(self.events), current_next(self.events)
        sugg = {s.sig["id"][:8]: s for s in self.book.suggestions}
        for sid, sig in self.sigs.items():
            no = sid[:8]
            r = lead_row(sig, self.today, self.triage)
            x = _llm(sig)
            src = sig.get("source")
            # 判定方式(R8、A03):上传与台账出处不判定;模型判过的为模型;规则初筛没过、没送模型的为规则;送了模型没结果
            # (没配密钥、出错)的留空,新库里 A03 再判
            judged = ("不判定" if src in (MANUAL, LEDGER) else "模型" if x else
                      None if (sig.get("rules") or {}).get("candidate") else "规则")
            m = self.book.members.get(no)
            sg = sugg.get(no)
            notes = [r["note"]]
            if not m:  # 线索上的负责人、下一步(旧做法)没有商机可放的,写进处理备注
                if (ow := owners.get(no)) and ow.get("value"):
                    notes.append(f"(迁移)线索上记的负责人:{ow['value']}")
                if (nx := nexts.get(no)) and (nx.get("value") or nx.get("due")):
                    notes.append(f"(迁移)线索上记的下一步:{nx.get('value') or ''}" + (f",到期 {nx['due']}" if nx.get("due") else ""))
            row = Row(no, {
                "标题": sig.get("title") or sig.get("url") or no, "线索号": no, "原文网址": sig.get("url"),
                "来源方式": "用户上传" if src == MANUAL else "机器采集",
                "发布日期": _day(sig.get("published")), "发现时间": sig.get("first_seen"),
                "原文摘录": (sig.get("excerpt") or "")[:EXCERPT_MAX] or None,
                "是否相关": relevant(sig) or None, "类别": CAT.get(category(sig)), "领域": [SECTOR_ZH[k] for k in sectors_of(sig) if k in SECTOR_ZH] or None,
                "中文摘要": r["summary_zh"] or None, "出资方": r["funder"] or None, "项目名称": r["project"] or None, "金额": r["amount"] or None,
                "归属可靠": any(lk.get("confidence") == "high" for lk in links_of(sig)) or None,
                "可能是新项目": bool((x or {}).get("new_opportunity")) and not sig.get("links") or None,
                "判定方式": judged, "处理状态": STATUS_ZH.get(r["status"], "待处理"), "处理备注": _lines(notes),
            }, links={"信源": [src] if src else [], "国别": [sig["country"]] if sig.get("country") else [],
                      "所属商机": [m.item] if m else [],
                      "建议所属商机": [sg.items[0][0]] if sg and sg.kind == "join" and sg.items else []})
            self.plan.add("lead", row)
            if src and src not in self.plan.rows["source"]:
                st = self.state.get(src) or {}
                self.plan.add("source", Row(src, {"名称": sig.get("source_name") or src, "信源编号": src, "启用": False,
                                                  "类型": SOURCE_KIND.get(sig.get("kind")), "最近采集": st.get("last_run"),
                                                  "最近状态": SOURCE_STATUS.get(st.get("status"))}))
                self.w(f"线索的信源 {src} 不在现在的监测配置里(已停用或换了编号的旧信源):建了一行,启用为否")

    def ledger_source_leads(self) -> None:
        """台账出处里有网址、还不是线索的,建成线索(信源「台账出处」,已处理、相关、不判定);一个出处只被一个商机引用的归它。"""
        cited: dict[str, list[str]] = {}
        for led in self.ledgers:
            for x in [*(led.meta.get("events") or []), *(led.meta.get("access") or [])]:  # 国别级日期、市场准入的出处也建成线索
                for ref in _refs(x):
                    self.lead_for_source(led, ref)
            for it in led.items:
                for ref in dict.fromkeys(_refs(it)):
                    if no := self.lead_for_source(led, ref):
                        cited.setdefault(no, []).append(it["id"])
        made = 0
        for led in self.ledgers:
            for ref, sv in led.sources.items():
                no = self.src_lead.get((led.country, ref))
                if not no or no in self.plan.rows["lead"]:
                    continue
                citers = list(dict.fromkeys(cited.get(no, [])))
                formal = [i for i in citers if i in self.tracked]
                owner = citers[0] if len(citers) == 1 else formal[0] if len(formal) == 1 else None
                checked = sorted(d for i in citers if (d := _day(((self.items.get(i) or (None, {}))[1].get("verification") or {}).get("checked_at"))))
                pub, title = str(sv.get("publisher") or "").strip(), str(sv.get("title") or ref).strip()[:300]
                segs = [s for i in citers for s in (self.items[i][1].get("segments") or [])]
                from core.watch.outputs import SEGMENT_SECTOR
                self.plan.add("lead", Row(no, {
                    "标题": title, "线索号": no, "原文网址": str(sv["url"]).strip(), "来源方式": "机器采集",
                    "发布日期": _day(sv.get("date")),
                    "发现时间": f"{checked[0] if checked else self.today.isoformat()}T00:00:00+00:00",
                    "是否相关": True, "类别": "项目动态",
                    "领域": list(dict.fromkeys(SECTOR_ZH[SEGMENT_SECTOR.get(s, "other")] for s in segs)) or None,
                    "中文摘要": f"台账出处({pub or '—'}):{title}", "判定方式": "不判定", "处理状态": "已处理",
                    "处理备注": f"台账出处 {led.country} {ref}(内容已在台账里)",
                }, links={"信源": [LEDGER], "国别": [led.country], "所属商机": [owner] if owner else [],
                          "建议所属商机": [citers[0]] if not owner and citers else []}))
                made += 1
        self.ledger_made = made

    # ---------- 国别 ----------
    def countries(self) -> None:
        from core.country_facts import METRICS, SRC_ZH, load_facts, water_desc
        isos = {c.iso3 for c in self.profile.countries}
        isos |= {lk[0] for r in self.plan.rows["lead"].values() for lk in [r.links.get("国别") or [None]] if lk[0]}
        isos |= {r.links["国别"][0] for r in self.plan.rows["opportunity"].values()}
        if self.markets:
            isos |= set(self.markets.iso3s())
        named = {c.iso3: c.name_zh for c in self.profile.countries if c.name_zh}
        isos = {i for i in isos if i in geo.countries() or i in named}  # 认不出的代码不建;profile 里配了的国别总建
        facts = load_facts(self.data_dir, self.markets, sorted(isos))
        notes = self.country_notes()
        tiered = {c.iso3 for c in self.profile.countries}  # 「重点」不由迁移定:人在表格里标(实施审查三-6,使用方 2026-10-03 定的)
        access = self.access_notes()
        for iso in sorted(isos):
            f = facts.get(iso)
            mk = self.markets.get(iso) if self.markets else None
            row = Row(iso, {"国名": named.get(iso) if iso not in geo.countries() else geo.name_zh(iso), "ISO3": iso,
                            "大洲与区域": " · ".join(x for x in (geo.continent(iso), geo.region(iso)) if x) or None,
                            "市场分级": "市场" if (mk or iso in tiered) else "其他",
                            "经营区域": mk.region if mk else None, "我方在当地": mk.presence() if mk else None,
                            "国别备注": notes.get(iso)})
            if f:
                row.fields["基础信息"] = _lines([f"{k}:{v}" + (f"({s} {u})" if u else f"({s})") for k, v, s, u in f.basics_rows(internal=False)])
                vals = [f.values[m.key] for m in METRICS if m.key in f.values]
                row.fields["宏观与水务指标"] = _lines([f"{v.metric.label}:{v.text(f.currency_code)}({SRC_ZH.get(v.source, v.source)} {v.url})"
                                                  for v in vals])
                if f.meta:
                    row.fields["收入组与贷款类别"] = f"{f.income()};{f.lending()}"
                for part, label in (("institutions", "水务管理机构"), ("enterprises", "水务国企"), ("laws", "相关法律法规")):
                    row.fields[label] = _lines([" · ".join(x for x in (
                        str(e.get("name_zh") or e.get("name") or e.get("name_en") or ""), str(e.get("name_local") or e.get("name_en") or ""),
                        water_desc(e), str(e.get("url") or "")) if x) for e in (f.water.get(part) or [])])
            if access.get(iso):
                row.fields["市场准入与竞争"] = _lines(access[iso])
            self.plan.add("country", row)

    def access_notes(self) -> dict[str, list[str]]:
        """台账的 access(市场准入与竞争格局,每条带出处线索号)→ 国别的「市场准入与竞争」(本体 v1.2 的属性,使用方 2026-10-03 定的)。"""
        out: dict[str, list[str]] = {}
        for led in self.ledgers:
            items = [str(a.get("text") if isinstance(a, dict) else a).strip() + (self.refs_text(led, _refs(a)) if isinstance(a, dict) else "")
                     for a in led.meta.get("access") or []]
            if items := [x for x in items if x.strip()]:
                out[led.country] = items
        return out

    def country_notes(self) -> dict[str, str]:
        """内部叠加里的国别备注、情报、建议动作,按日期并成一段(国别级的事不是跟进,R7)。"""
        out = {}
        for iso, ov in self.overlays.items():
            lines = []
            for x in ov.get("country_notes") or []:
                lines.append(("", f"[备注] {x}"))
            for x in ov.get("intel") or []:
                if isinstance(x, dict):
                    d = str(x.get("date") or "")
                    lines.append((d, f"{d} [情报] {x.get('content') or ''}" + (f"(来源 {x['source']})" if x.get("source") else "")))
            for x in ov.get("actions") or []:
                if isinstance(x, dict):
                    d = str(x.get("due") or "")
                    lines.append((d, f"[建议动作] {x.get('action') or ''}" + (f"(到期 {d}" + (f",{x['owner']}" if x.get("owner") else "") + ")" if d else "")))
            if lines:
                out[iso] = "\n".join(t for _, t in sorted(lines, key=lambda z: z[0]))
        return out

    # ---------- 时间节点 ----------
    def milestones(self) -> None:
        def add(owner: str, kind: str, raw: str, fields: dict, links: dict) -> None:
            try:
                pd = parse_pdate(raw)
            except Exception:  # noqa: BLE001 — 认不出的日期记警告,不迁
                self.w(f"时间节点日期认不出,没迁:{owner} {kind} {raw}")
                return
            k = f"{owner}|{kind}|{pd.raw}" + (f"|{fields.get('说明') or ''}" if kind == "其他" else "")
            self.plan.add("milestone", Row(k, {"类型": kind, "日期": pd.start.isoformat(), "日期原文": pd.raw, **fields}, links))

        for led in self.ledgers:
            for it in led.items:
                iid = it["id"]
                for m in it.get("milestones") or []:
                    kind = MS_KIND.get(m.get("type"), "其他")
                    label = str(m.get("note") or "") or ("" if kind != "其他" else MILESTONES.get(m.get("type"), "其他"))
                    src = [no for r in (_refs(m) or []) if (no := self.lead_for_source(led, r))]
                    add(f"opp:{iid}", kind, str(m.get("date")), {"状态": MS_STATUS.get(m.get("status")), "说明": label or None},
                        {"商机": [iid], "出处线索": src[:1]})
                for p in (it.get("procurement") or {}).get("packages") or []:
                    src = [no for r in (_refs(p) or []) if (no := self.lead_for_source(led, r))]
                    st = "官方计划" if p.get("status") == "open" else MS_STATUS.get(p.get("date_status") or "expected")
                    if p.get("deadline") is not None:
                        add(f"opp:{iid}", DEADLINE_KIND.get(p.get("deadline_type") or "bid_deadline", "投标截止"), str(p["deadline"]),
                            {"状态": "已发生" if p.get("status") in ("closed", "awarded") else st, "说明": p.get("name")},
                            {"商机": [iid], "出处线索": src[:1]})
                    if p.get("published") is not None:
                        add(f"opp:{iid}", "招标发布", str(p["published"]), {"状态": "已发生" if p.get("status") != "planned" else st,
                                                                         "说明": p.get("name")}, {"商机": [iid], "出处线索": src[:1]})
            for ev in led.meta.get("events") or []:
                src = [no for r in (_refs(ev) or []) if (no := self.lead_for_source(led, r))]
                label = " · ".join(str(x) for x in (ev.get("name"), ev.get("place"), ev.get("note")) if x)
                add(f"cty:{led.country}", "其他", str(ev.get("date")), {"状态": "官方计划", "说明": label or None},
                    {"国别": [led.country], "出处线索": src[:1]})
        opp_days = {(r.links["商机"][0], r.fields["类型"], r.fields["日期"]) for r in self.plan.rows["milestone"].values() if r.links.get("商机")}
        for sid, sig in self.sigs.items():
            no = sid[:8]
            lead = self.plan.rows["lead"].get(no)
            if not lead or not relevant(sig) or lead.fields.get("处理状态") == "已忽略":
                continue
            m = self.book.members.get(no)
            dls = [(d.get("date"), DEADLINE_KIND.get(d.get("type"), "其他")) for d in (_llm(sig) or {}).get("deadlines") or []]
            if (fd := (sig.get("fields") or {}).get("deadline")) and all(fd != d for d, _ in dls):
                dls.append((fd, "其他"))
            for d, kind in dls:
                day = _day(d)
                if not day or (m and (m.item, kind, day) in opp_days):
                    continue
                add(f"lead:{no}", kind, day, {"状态": "官方计划", "说明": "截止" if kind == "其他" else None},
                    {"线索": [no], "商机": [m.item] if m else [], "出处线索": [no]})

    # ---------- 跟进记录 ----------
    def followups(self) -> None:
        gone = opp.retracted(self.events)
        owner_of = {no: m.item for no, m in self.book.members.items()}
        def item_of(e: dict) -> str | None:
            k = str(e.get("id", ""))
            return k if k in self.items else owner_of.get(k[:8])
        last_ref: dict[str, int] = {}
        for i, e in enumerate(self.events):
            if e.get("kind") == "follow" and e.get("ref"):
                last_ref[e["ref"]] = i
        prev: dict[str, str] = {}
        stage_rows: dict[str, Row] = {}
        for i, e in enumerate(self.events):
            iid, kind = item_of(e), e.get("kind")
            if opp.entry_key(e) in gone or (kind == "follow" and e.get("ref") and last_ref.get(e["ref"]) != i):
                continue
            day = _day(e.get("day")) or _day(e.get("at")) or self.today.isoformat()
            if not iid:  # 没有商机的线索上的跟进(旧表允许只关联线索):写进线索的处理备注(审查二-2)
                if kind == "follow" or (kind == "note" and opp.is_work(e)):
                    self._lead_follow(e, day)
                continue
            if kind == "follow":
                self._follow(iid, day, str(e.get("note") or "").strip() or JUDGMENTS.get(e.get("value"), "跟进"), e, {
                    "判断": JUDGMENTS.get(e.get("value")), "判断依据": e.get("reason") or None, "下一步": e.get("step") or None,
                    "下一步到期": _day(e.get("due"))})
            elif kind == "note" and opp.is_work(e):
                self._follow(iid, day, str(e.get("note")).strip(), e, {})
            elif kind in ("stage", "listing"):
                new = (STAGES.get(e.get("value")) if kind == "stage" else "不跟进" if e.get("value") == "no" else NO_STAGE) or NO_STAGE
                if kind == "listing" and iid in self.tracked:
                    continue
                old = prev.get(iid, NO_STAGE)
                if new == old:
                    continue
                prev[iid] = new
                content = str(e.get("note") or "").strip() if not e.get("auto") else ""
                stage_rows[iid] = self._follow(iid, day, content or f"阶段从「{old}」改为「{new}」", e, {"阶段变更": new})
        for iid, (led, it) in self.items.items():  # 每个不是「建议」的商机,最后一条阶段变更 = 它迁入时的阶段(A18 不会再补记)
            want = self.plan.rows["opportunity"][iid].fields["我方阶段"]
            have = prev.get(iid, NO_STAGE)
            if want != have:
                why = "台账里标为不在业务范围" if it.get("in_scope") is False and want == "不跟进" else "迁移时的阶段"
                day = led.as_of.isoformat()  # 取台账的 as_of,与运行日期无关:换一天重跑还是同一条
                self._follow(iid, day, f"(迁移){why}:阶段为「{want}」", {"by": SYSTEM}, {"阶段变更": want})

    def _lead_follow(self, e: dict, day: str) -> None:
        """没有商机的线索上的跟进:新库的跟进必须选商机,写进线索的「处理备注」(长文本)一行并警告,不丢(审查二-2)。"""
        no = str(e.get("id", ""))[:8]
        row = self.plan.rows["lead"].get(no)
        if not row:
            return
        who = _who(e)
        text = str(e.get("note") or "").strip() or JUDGMENTS.get(e.get("value"), "跟进")
        bits = [f"(迁移)线索上的跟进 {day}" + (f" {who}" if who else "") + f":{text}"]
        if e.get("kind") == "follow":
            if JUDGMENTS.get(e.get("value")):
                bits.append(f"判断:{JUDGMENTS[e['value']]}")
            if e.get("step"):
                bits.append(f"下一步:{e['step']}" + (f"(到期 {_day(e['due'])})" if _day(e.get("due")) else ""))
        row.fields["处理备注"] = _lines([row.fields.get("处理备注") or "", ";".join(bits)])
        self.w(f"线索 {no} 上的跟进没有商机可放(新库的跟进必须选商机),写进了处理备注:{text[:40]}")

    def _follow(self, iid: str, day: str, content: str, e: dict, extra: dict) -> Row:
        who = _who(e)
        key = followup_key(iid, day, content, extra.get("阶段变更"), extra.get("判断"), extra.get("下一步"))
        row = Row(key, {"日期": day, "内容": content, **{k: v for k, v in extra.items() if v}}, links={"商机": [iid]})
        if who:
            row.people["记录人"] = who
        return self.plan.add("followup", row)

    # ---------- 报告 ----------
    def reports(self) -> None:
        known = set(self.by_no) | set(self.plan.rows["lead"])
        roots = [self.data_dir / "reports", Path(self.profile.path).parent / "reports"]  # data 分支的与 profile 同目录的(private/reports)
        mine = {no: r.links["所属商机"][0] for no, r in self.plan.rows["lead"].items()  # 线索 → 所属正式商机(含台账出处新建的)
                if r.links.get("所属商机") and r.links["所属商机"][0] in self.tracked}
        seen = {no: str(r.fields.get("发现时间") or "") for no, r in self.plan.rows["lead"].items()}
        for root in roots:
            for rp in flow.load_reports(root, known):
                kind = REPORT_KIND.get(rp.kind)
                if not kind:
                    self.w(f"报告 {rp.name} 的类型「{rp.kind}」新库里没有,没迁")
                    continue
                fp = rp.docx or rp.md
                cites = flow.covered(rp, mine, seen) if flow.formal(rp) else list(rp.leads)
                d = flow.when_date(rp.when)
                item = next((i for i in rp.items if i in self.items and i in rp.name), None) if kind == "项目报告" else None
                self.plan.add("report", Row(fp.name, {
                    "文件名": fp.name, "类型": kind, "日期": d.isoformat() if d else None,
                    "期间": rp.when if "-W" in rp.when else (rp.when[:7] if kind == "月报" and rp.when else None),
                    "版本": "公开版" if "公开版" in rp.name else "内部版", "生成状态": "完成"},
                    links={"范围国别": rp.countries[:1], "范围商机": [item] if item else [], "引用的线索": cites,
                           "写进的商机": [i for i in rp.items if i in self.items]}, files=[fp]))


def _who(e: dict) -> str:
    """操作记录里的操作人:去掉 teable: 前缀;系统自己的不算人。"""
    by = str(e.get("by") or "")
    who = by[len("teable:"):] if by.startswith("teable:") else by
    return who if who and who != SYSTEM else ""


def _pending(r: Row) -> bool:
    """待确认归属(V2):所属商机为空,且(有建议所属商机、没被忽略)或(可能是新项目、还待处理)。"""
    st = r.fields.get("处理状态")
    return not r.links.get("所属商机") and ((bool(r.links.get("建议所属商机")) and st != "已忽略")
                                         or (bool(r.fields.get("可能是新项目")) and st == "待处理"))


def _amount(v, cur) -> str:
    if v is None:
        return "金额待核实"
    try:
        return f"{float(v):,.0f} {cur or ''}".strip()
    except (TypeError, ValueError):
        return f"{v} {cur or ''}".strip()


# ---------- 对账 ----------

def git_head(path: Path) -> str:
    try:
        out = subprocess.run(["git", "-C", str(path), "log", "-1", "--format=%h %cI %s"], capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or "(不是 git 目录)"
    except (OSError, subprocess.SubprocessError):
        return "(读不到)"


def reconcile(b: Builder) -> list[str]:
    p, out = b.plan, []
    out.append(f"- data 分支:`{git_head(b.data_dir)}`;main:`{git_head(ROOT)}`")
    sig_ids = set(b.sigs)
    submitted = {s["id"] for s in b.sigs.values() if (b.store.root / "submitted" / f"{s['id']}.json").exists()}
    from core.teable import load_links, load_state
    old = load_links(b.store)
    old_leads = set((old.get("leads") or {}))
    out.append("\n## 线索\n")
    out.append(f"- 数据分支:{len(sig_ids)} 条(采集 `watch/signals/` {len(sig_ids - submitted)} 条,`watch/submitted/` {len(submitted)} 条,"
               f"其中台账出处 {sum(1 for s in submitted if b.sigs[s].get('source') == LEDGER)} 条、人工上传 "
               f"{sum(1 for s in submitted if b.sigs[s].get('source') == MANUAL)} 条)。")
    out.append(f"- 旧库线索表:{len(old_leads)} 行(按 `watch/teable_links.json`,旧同步推送过的线索)。")
    only_data, only_old = sorted(sig_ids - old_leads), sorted(old_leads - sig_ids)
    out.append(f"- 数据分支有、旧库没有:{len(only_data)} 条" + (";" + "、".join(
        f"{s[:8]}(入库 {b.sigs[s]['first_seen'][:10]})" for s in only_data[:20]) + ("……" if len(only_data) > 20 else "") if only_data else "") + "。")
    out.append(f"- 旧库有、数据分支没有:{len(only_old)} 条" + (";" + "、".join(s[:8] for s in only_old[:20]) if only_old else "") + "。")
    out.append("- 数据分支两头都算上(`watch/signals/` 与 `watch/submitted/`;旧库按链接表)以后,差异只剩上面两行:采集时间晚于旧库最近一次推送的,"
               "会随下一次推送进旧库;迁移以数据分支为准。")
    leads = p.rows["lead"]
    out.append(f"- 新库线索:{len(leads)} 条 = 数据分支 {len(sig_ids)} 条 + 台账出处新建 {b.ledger_made} 条(AI 推荐商机等的出处,原文网址还不是线索的)。")
    cnt = Counter(r.fields.get("判定方式") for r in leads.values())
    st = Counter(r.fields.get("处理状态") for r in leads.values())
    out.append(f"  判定方式:{dict(cnt)};处理状态:{dict(st)};相关 {sum(1 for r in leads.values() if r.fields.get('是否相关'))} 条;"
               f"有所属商机 {sum(1 for r in leads.values() if r.links.get('所属商机'))} 条;待确认归属(V2)"
               f" {sum(1 for r in leads.values() if _pending(r))} 条。")
    out.append("\n## 商机\n")
    opps = p.rows["opportunity"]
    out.append(f"- 台账项目 {len(opps)} 个 → 新库商机 {len(opps)} 行;我方阶段:{dict(Counter(r.fields['我方阶段'] for r in opps.values()))}。")
    old_p = set(old.get("projects") or {})
    out.append(f"- 旧库商机表 {len(old_p)} 行:{'、'.join(sorted(old_p))};都在新库里、阶段不是「建议」:"
               + ("是" if all(k in opps and opps[k].fields["我方阶段"] != NO_STAGE for k in old_p) else "**否**") + "。")
    no_src = [k for k, r in opps.items() if not r.links.get("出处线索")]
    out.append("- 每个商机至少一条出处线索:" + ("是" if not no_src else f"**否**,没有的:{'、'.join(no_src)}") + "。")
    skipped = sorted({f"{c} {r}" for (c, r), no in b.src_lead.items() if no is None})
    out.append(f"- 不建成线索的出处(没有网址、D 级或内部成稿):{len(skipped)} 个,在长文本里写成〔编号〕引用。")
    out.append("\n## 跟进记录\n")
    fol = p.rows["followup"]
    out.append(f"- 新库跟进记录 {len(fol)} 条:其中带阶段变更的 {sum(1 for r in fol.values() if r.fields.get('阶段变更'))} 条。")
    state = load_state(b.store)
    pushed = set(state.get("fpush") or {})
    have = {opp.entry_key(e) for e in b.events}
    out.append(f"- 旧库跟进记录表(旧同步推过去的 {len(pushed)} 条):都在操作记录里、会迁进新库:"
               + ("是" if pushed <= have else f"**否**,缺 {len(pushed - have)} 条") + "。")
    last = {}
    for r in fol.values():
        if r.fields.get("阶段变更"):
            k = r.links["商机"][0]
            if r.fields["日期"] >= last.get(k, ("", ""))[0]:
                last[k] = (r.fields["日期"], r.fields["阶段变更"])
    bad = [k for k, r in opps.items() if r.fields["我方阶段"] != NO_STAGE and last.get(k, ("", NO_STAGE))[1] != r.fields["我方阶段"]]
    out.append("- 每个不是「建议」的商机,最后一条阶段变更 = 它的我方阶段:" + ("是" if not bad else f"**否**:{'、'.join(bad)}") + "。")
    unmatched = Counter(r.people.get("记录人") for r in fol.values() if r.people.get("记录人"))
    out.append(f"- 记录人(写入时按表格账号对上,对不上的写进内容):{len(unmatched)} 个人。")
    out.append("\n## 其余各表\n")
    out.append(f"- 时间节点 {len(p.rows['milestone'])} 个:{dict(Counter(k.split('|')[0].split(':')[0] for k in p.rows['milestone']))}"
               "(opp = 台账节点与标段日期,lead = 线索里抽出的截止日,cty = 国别级日期如投资论坛)。")
    out.append(f"- 国别 {len(p.rows['country'])} 个:市场分级 {dict(Counter(r.fields['市场分级'] for r in p.rows['country'].values()))}"
               "(市场 = 市场分布表与 profile 里的国别,其余为其他;「重点」由使用方在表格里标,机器不写)。")
    out.append(f"- 信源 {len(p.rows['source'])} 个:{dict(Counter(r.fields.get('类型') for r in p.rows['source'].values()))}。")
    rp = p.rows["report"]
    out.append(f"- 报告 {len(rp)} 份:{dict(Counter(r.fields['类型'] for r in rp.values()))};旧库报告表的行由旧同步上传,迁移以文件为准。")
    out.append("- 不迁:旧库「每日摘要」表(旧同步的推送记录,架构方案第 12 节 B)。")
    out.append("\n## 字段核对(每个字段有值的行数;全空的标出来)\n")
    for oid in ORDER:
        rows = p.rows[oid]
        cnt = Counter(k for r in rows.values() for k, v in r.fields.items() if v not in (None, "", [], False))
        cnt.update(k for r in rows.values() for k, v in r.links.items() if v)
        cnt.update(k for r in rows.values() for k in r.people)
        every = list(dict.fromkeys(k for r in rows.values() for k in [*r.fields, *r.links, *r.people]))
        empty = [k for k in every if not cnt.get(k)]
        out.append(f"- {NAMES[oid]}({len(rows)} 行):" + "、".join(f"{k} {n}" for k, n in sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0])))
                   + (f";**全空**:{'、'.join(empty)}" if empty else "") + "。")
    refs = {no for oid in ("opportunity", "country", "milestone") for r in p.rows[oid].values() for v in r.fields.values()
            if isinstance(v, str) for no in re.findall(r"\[([0-9a-f]{8})\]", v)}
    missing = sorted(refs - set(p.rows["lead"]))
    out.append(f"- 长文本里引用的线索号 {len(refs)} 个,都在新库里:" + ("是" if not missing else f"**否**,缺 {'、'.join(missing)}") + "。")
    people = sorted({r.people.get("记录人") for r in fol.values() if r.people.get("记录人")} - set(load_team_codes(b.profile)))
    out.append("\n## 迁移时的取舍(审查时请看)\n")
    out += [
        "- 台账的「市场准入与竞争」(access,每条带出处线索号)写进国别的同名属性(使用方 2026-10-03 定的)。",
        "- 线索简报归为「月报」(本体的报告类型只有国别报告、项目报告、周报、月报四种);其余认不出类型的报告不迁,列在警告里。",
        "- 内部叠加的业务线、我方角色、判断、建议动作、情报并进商机的「内部备注」(人写);国别级的备注、情报、建议动作并进国别备注。",
        "- 不在业务范围(台账 in_scope: false)与「不列入正式商机」的,我方阶段为「不跟进」,各补一条阶段变更跟进,注明原因。",
        "- 待确认归属(V2):已处理的「可能是新项目」不再提(同旧系统,人看过了);有建议所属商机的照提。",
        "- 旧操作记录里的操作人" + (f"「{'」「'.join(people)}」" if people else "") + "对不上岗位代号表:写入时对不上表格账号的写进内容;"
        "若是某位同事当时的表格显示名,迁移时加 `--alias 旧名=表格显示名`。",
    ]
    return out


def load_team_codes(profile) -> set[str]:
    from core.teable import load_team
    team = load_team(profile)
    return set(team) | set(team.values())


# ---------- 写进新库 ----------

class Writer:
    def __init__(self, api: tstore.Client, base_name: str, plans: list, mode: str = "fill", log=print, team: dict | None = None,
                 alias: dict[str, str] | None = None):
        self.api, self.log, self.mode = api, log, mode
        self.base = api.ensure_new_base(base_name, create=False)  # 迁移不建库:库与表由 core.schema 建
        if self.base is None:
            raise tstore.TStoreError(f"没有「{base_name}」库:先跑 python -m core.schema")
        tables = {t["name"]: t for t in api.tables(self.base["id"])}
        self.plans = {tp.obj: tp for tp in plans}
        self.t = {}
        self.f: dict[str, dict[str, dict]] = {}
        for tp in plans:
            if tp.name not in tables:
                raise tstore.TStoreError(f"新库里没有「{tp.name}」表:先跑 python -m core.schema")
            self.t[tp.obj] = tables[tp.name]
            self.f[tp.obj] = {x["name"]: x for x in api.fields(tables[tp.name]["id"])}
        self.users = self._users(team or {})
        for old, new in (alias or {}).items():  # 旧记录里的名字 → 表格显示名(如 teable:admin 那时的账号名)
            if new in self.users:
                self.users[old] = self.users[new]
        self.ids: dict[str, dict[str, str]] = {}  # 对象 → 键 → 记录编号
        self.key_by_id: dict[str, dict[str, str]] = {}  # 反过来:记录编号 → 键(关联反查用,建一次;审查二-5)
        self.recs: dict[str, dict[str, dict]] = {}  # 对象 → 记录编号 → 记录:每表每次运行只下载一遍,关联与附件复用(审查二-5)
        self.stats: Counter = Counter()
        self.warnings: list[str] = []

    def _users(self, team: dict[str, str]) -> dict[str, str]:
        """人(岗位代号或表格显示名)→ 表格账号编号。team:显示名 → 岗位代号。"""
        out = {}
        for c in self.api.collaborators(self.base["id"]):
            uid, name = c.get("userId") or c.get("id"), c.get("userName") or c.get("name")
            if uid and name:
                out[name] = uid
                if name in team:
                    out[team[name]] = uid
        return out

    def _primary(self, oid: str) -> dict:
        return next(f for f in self.f[oid].values() if f.get("isPrimary"))

    def writable(self, oid: str) -> set[str]:
        """可写的字段:普通字段与关联;主字段按表里的实际类型——不是公式(schema 用了后备的单行文本)就也要写。"""
        tp = self.plans[oid]
        names = {f["name"] for f in tp.fields} | {lp.prop.name for lp in tp.links}
        prim = self._primary(oid)
        if prim["type"] != "formula":
            names.add(prim["name"])
        return names

    def _key_of(self, oid: str, rec: dict) -> str | None:
        f, v = self.f[oid], rec.get("fields") or {}
        def link_key(name: str, target: str) -> str | None:
            x = v.get(f[name]["id"]) if name in f else None
            x = x[0] if isinstance(x, list) and x else x
            rid = x.get("id") if isinstance(x, dict) else None
            return self.key_by_id.get(target, {}).get(rid) if rid else None
        if oid in ("country", "source", "opportunity", "lead"):
            name = {"country": "ISO3", "source": "信源编号", "opportunity": "商机编号", "lead": "线索号"}[oid]
            return v.get(f[name]["id"])
        if oid == "milestone":
            kind, raw = v.get(f["类型"]["id"]), v.get(f["日期原文"]["id"])
            # 所属:线索抽出的截止日关联了线索(也可能关联商机)→ 线索;台账的节点 → 商机;国别级日期 → 国别
            owner = (("lead:" + ld) if (ld := link_key("线索", "lead")) else ("opp:" + o) if (o := link_key("商机", "opportunity"))
                     else ("cty:" + c) if (c := link_key("国别", "country")) else None)
            if not (owner and kind and raw):
                return None
            return f"{owner}|{kind}|{raw}" + (f"|{v.get(f['说明']['id']) or ''}" if kind == "其他" else "")
        if oid == "followup":
            iid, day, content = link_key("商机", "opportunity"), tstore.day(v.get(f["日期"]["id"]), TMAP["timezone"]), v.get(f["内容"]["id"])
            content = re.sub(r"^\(记录人:[^)]*\) ?", "", str(content or ""))  # 写入时对不上账号加的前缀,不算进键

            def g(n: str):
                return v.get(f[n]["id"]) if n in f else None
            return followup_key(iid, day, content, g("阶段变更"), g("判断"), g("下一步")) if iid and day and content else None
        if oid == "report":
            return v.get(f["文件名"]["id"])  # 标识字段(与线索号同类):附件上传失败也认得出这一行,重跑不重复建
        return None

    def run(self, plan: Plan) -> Counter:
        for oid in ORDER:
            self._rows(oid, plan.rows[oid])
        for oid in ORDER:
            self._links(oid, plan.rows[oid])
        self._files(plan.rows["report"])
        return self.stats

    def _value(self, oid: str, name: str, v):
        f = self.f[oid][name]
        if v is None or v == "" or v == []:
            return None
        if f["type"] == "date":
            return v
        if f["type"] == "checkbox":
            return bool(v) or None
        if f["type"] == "number":
            return float(v)
        if f["type"] == "singleLineText" and isinstance(v, str):
            return " ".join(v.split()) or None  # 单行文本里的换行(旧标题里有)换成空格(审查二-1)
        return v

    def _rows(self, oid: str, rows: dict[str, Row]) -> None:
        ok = self.writable(oid)
        bad = {k for r in rows.values() for k in list(r.fields) + list(r.people) if k not in ok}
        if bad:
            raise ValueError(f"{oid}:要写的字段不是可写字段(派生的不写,R10):{sorted(bad)}")
        existing = {}
        self.recs[oid] = {}
        for rec in self.api.iter_records(self.t[oid]["id"]):
            self.recs[oid][rec["id"]] = rec
            if k := self._key_of(oid, rec):
                existing[k] = rec
        self.ids[oid] = {k: rec["id"] for k, rec in existing.items()}
        self.key_by_id[oid] = {rec["id"]: k for k, rec in existing.items()}
        prim = self._primary(oid)
        new, upd = [], []
        for k, r in rows.items():
            fields = dict(r.fields)
            if prim["type"] != "formula" and not fields.get(prim["name"]):  # 主字段用了后备的单行文本:按公式的意思合成
                fields[prim["name"]] = primary_text(oid, r)
            vals = {self.f[oid][n]["id"]: self._value(oid, n, v) for n, v in fields.items()}
            for n, who in r.people.items():
                uid = self.users.get(who)
                if uid:
                    vals[self.f[oid][n]["id"]] = {"id": uid}
                else:
                    self.warnings.append(f"{self.t[oid]['name']} {k}:{n}「{who}」在表格里没有账号,写进了内容/备注")
                    tgt = "内容" if "内容" in r.fields else "内部备注" if oid == "opportunity" else None
                    if tgt:
                        cur = r.fields.get(tgt) or ""
                        vals[self.f[oid][tgt]["id"]] = f"({n}:{who})" + (" " + cur if cur else "")
            vals = {fid: v for fid, v in vals.items() if v is not None}
            if k not in existing:
                new.append((k, vals))
            else:
                have = existing[k].get("fields") or {}
                ch = {fid: v for fid, v in vals.items() if (_empty(have.get(fid)) or (self.mode == "sync" and not _same(have.get(fid), v)))}
                if ch:
                    upd.append((existing[k]["id"], ch))
        if new:
            made = self.api.create_records(self.t[oid]["id"], [v for _, v in new])
            for (k, _), rec in zip(new, made):
                self.ids[oid][k] = rec["id"]
                self.key_by_id[oid][rec["id"]] = k
        if upd:
            self.api.update_records(self.t[oid]["id"], upd)
        self.stats[f"{self.t[oid]['name']}·新建"] += len(new)
        self.stats[f"{self.t[oid]['name']}·补字段"] += len(upd)
        self.log(f"  {self.t[oid]['name']}:新建 {len(new)} 行,补字段 {len(upd)} 行,已有 {len(existing)} 行")

    def _links(self, oid: str, rows: dict[str, Row]) -> None:
        tp = self.plans[oid]
        target = {lp.prop.name: lp.target for lp in tp.links}
        multi = {lp.prop.name: lp.relationship == "manyMany" for lp in tp.links}
        recs = self.recs[oid]  # _rows 下载过的;关联字段 _rows 不写,仍是准的
        upd = []
        for k, r in rows.items():
            rid = self.ids[oid].get(k)
            if not rid:
                continue
            have = recs.get(rid, {}).get("fields") or {}
            ch = {}
            for name, keys in r.links.items():
                if not keys:
                    continue
                ids = [i for x in keys if (i := self.ids[target[name]].get(x))]
                missing = [x for x in keys if x not in self.ids[target[name]]]
                if missing:
                    self.warnings.append(f"{self.t[oid]['name']} {k}:{name} 指向的 {', '.join(missing[:5])} 不在新库里,没关联")
                if not ids:
                    continue
                fid = self.f[oid][name]["id"]
                cur = have.get(fid)
                cur_ids = sorted(x.get("id") for x in (cur if isinstance(cur, list) else [cur] if cur else []))
                if cur_ids and (self.mode != "sync" or cur_ids == sorted(set(ids if multi[name] else ids[:1]))):
                    continue
                ch[fid] = [{"id": i} for i in dict.fromkeys(ids)] if multi[name] else {"id": ids[0]}
            if ch:
                upd.append((rid, ch))
        if upd:
            self.api.update_records(self.t[oid]["id"], upd)
        self.stats[f"{self.t[oid]['name']}·关联"] += len(upd)

    def _files(self, rows: dict[str, Row]) -> None:
        fid = self.f["report"]["文件"]["id"]
        recs = self.recs["report"]
        n = 0
        for k, r in rows.items():
            rid = self.ids["report"].get(k)
            if not rid or (recs.get(rid, {}).get("fields") or {}).get(fid):
                continue
            for fp in r.files:
                self.api.upload_file(self.t["report"]["id"], rid, fid, fp)
                n += 1
        self.stats["报告·上传文件"] += n


def primary_text(oid: str, r: Row) -> str:
    """主字段用了后备(单行文本)时的值,与 teable_map.yaml 里公式的意思一致:跟进"日期 · 内容前 40 字"、节点"类型 · 日期原文"、
    报告"类型 · 范围 · 日期 · 版本"。"""
    f = r.fields
    if oid == "followup":
        return f"{f.get('日期') or ''} · {str(f.get('内容') or '')[:40]}"
    if oid == "milestone":
        return f"{f.get('类型') or ''} · {f.get('日期原文') or ''}"
    if oid == "report":
        scope = "".join(r.links.get("范围国别") or []) + "".join(r.links.get("范围商机") or [])
        return " · ".join(str(x) for x in (f.get("类型"), scope, f.get("日期"), f.get("版本")) if x)
    return r.key


def _empty(v) -> bool:
    return v is None or v == "" or v == [] or v is False


def _same(have, want) -> bool:
    if isinstance(have, dict):
        have = have.get("id") or have.get("title")
    if isinstance(want, dict):
        want = want.get("id")
    if isinstance(have, str) and "T" in have and isinstance(want, str):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", want):
            return tstore.day(have, TMAP["timezone"]) == want
        if "T" in want:  # 两边都是带时间的 ISO:按时刻比(….000Z 与 …+00:00 是同一时刻,不算改了)
            try:
                return datetime.fromisoformat(have.replace("Z", "+00:00")) == datetime.fromisoformat(want.replace("Z", "+00:00"))
            except ValueError:
                return have == want
    if isinstance(have, list) and isinstance(want, list):
        return sorted(map(str, have)) == sorted(map(str, want))
    if isinstance(have, (int, float)) and isinstance(want, (int, float)):
        return float(have) == float(want)
    return have == want


def report_md(b: Builder, base_name: str, stats: Counter | None = None, warnings: list[str] | None = None) -> str:
    head = [f"# 迁移对账报告({b.today.isoformat()},新库「{base_name}」)", "",
            "由 `scripts/migrate_to_teable.py` 生成;" + ("**试运行(--dry-run),没有写表格**。" if stats is None else "已写进新库。"), ""]
    body = reconcile(b)
    rows = ["", "## 各表行数(迁移数据)", "", "| 表 | 行数 |", "|---|---|"]
    rows += [f"| {NAMES[o]} | {len(b.plan.rows[o])} |" for o in ORDER]
    if stats is not None:
        rows += ["", "## 写入结果", ""] + [f"- {k}:{v}" for k, v in sorted(stats.items())]
    ws = (b.plan.warnings + (warnings or []))
    tail = ["", f"## 警告({len(ws)})", ""] + [f"- {w}" for w in ws[:200]] + (["- ……"] if len(ws) > 200 else [])
    return "\n".join(head + body + rows + tail) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="阶段 C:把现有数据迁进新库(先 --dry-run 对账)")
    ap.add_argument("--profile", required=True)
    ap.add_argument("--data", help="data 分支的目录(缺省 profile 的 data_dir)")
    ap.add_argument("--base", help="新库名(缺省见 teable_map.yaml,要以「新版」结尾)")
    ap.add_argument("--today", help="YYYY-MM-DD(缺省今天)")
    ap.add_argument("--dry-run", action="store_true", help="只出对账报告,不连表格")
    ap.add_argument("--sync", action="store_true", help="已有的行按迁移数据覆盖(切换前、同事还没用新库时)")
    ap.add_argument("--report", help="对账报告另存为 Markdown")
    ap.add_argument("--alias", action="append", default=[], metavar="旧名=表格显示名",
                    help="旧记录里的操作人对应到表格账号(可多次),如 admin=张三")
    a = ap.parse_args(argv)
    from core.profile import load_profile
    from core.teable import load_team
    profile = load_profile(a.profile)
    data_dir = Path(a.data or profile.data_dir)
    today = date.fromisoformat(a.today) if a.today else datetime.now(timezone.utc).date()
    base_name = tstore.require_new_base(a.base or TMAP["base"])
    b = Builder(profile, data_dir, today)
    b.build()
    stats, warnings = None, []
    if not a.dry_run:
        url, token = tstore.credentials(admin=True)
        plans = schema.build_plan(onto_mod.load(), TMAP, profile.lead_times)
        alias = dict(x.split("=", 1) for x in a.alias if "=" in x)
        w = Writer(tstore.Client(url, token), base_name, plans, "sync" if a.sync else "fill", team=load_team(profile), alias=alias)
        stats = w.run(b.plan)
        warnings = w.warnings
    md = report_md(b, base_name, stats, warnings)
    print(md)
    if a.report:
        Path(a.report).write_text(md, encoding="utf-8")
        print(f"已存:{a.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
