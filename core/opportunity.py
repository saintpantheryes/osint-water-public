"""商机:一个项目一条,沿用台账项目的编号(如 MAR-005)。公开事实在台账里(每条带来源);内部信息(优先级、负责人、内部备注)
在内部叠加与团队表格的项目表里;进展、并入决定与操作记录在操作记录里(data 分支 watch/events.jsonl)。

所属线索——一条线索至多属于一个商机。按下面的先后定,这是全系统唯一的口径(商机页、待确认、项目报告都用它):
1. 人的决定:操作记录里的 member 事件,value 为商机编号即并入,"-" 为不属于任何商机,""(清除)回到系统判断;最后一次为准。
2. 写进台账的记录:界面「更新台账 / 新建项目」与跟踪即入账(台账核对记录 updated 及编号)、系统自动写入(ledger_auto;
   撤销了的不算)、原文网址已是商机引用的台账来源;同一条线索写进了几个商机的,交人决定。
3. 可靠挂接(编号吻合、监测配置指定,或规则挂上后模型读原文也确认):模型判为相关、没被忽略、只可靠挂上一个商机时自动并入。
其余的交「待确认」:挂接不可靠(名称相近、只有模型认为)或可靠挂接不止一个的——可能属于已有商机;
模型认为是新项目、又没挂上任何商机的——可能的新商机。人核对过、标了「不用更新台账」或「忽略」的不再提示。

进展:商机与所属线索上设过的进展(清除了的不算)里最近的一次。过渡期里团队表格线索表的「跟踪进展」仍设在线索上,照样算进商机。

在跟踪的商机(正式商机):设了进展的(跟踪中、准备投标、已投标为在跟踪,中标、未中标、放弃为已结束)。台账里其余的项目是
AI 推荐商机(系统整理进台账、国别报告「项目机会清单」里推荐的),不算商机,由人决定列不列入正式商机(列入即设进展为跟踪中;
不列入记 kind listing,value no)——商机页、团队表格的商机表与跟进记录、日历、周期性报告都只看正式商机。
跟进(kind follow)记在商机编号下,带对商机的判断。商机的工作留痕(work_log:跟进、人记的一笔、带说明的进展,含所属线索上的)
就是团队表格「跟进记录」里这个商机的那些行(core.teable 两边同步,撤回的不算)。
日历(calendar):在跟踪的商机的公开时间节点(台账节点、采购标段的发布与截止、所属线索的截止日)与工作留痕
(跟进与判断、进展、记录、下一步到期),以及上报的国别报告、项目报告。
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field

from core.ledger import MILESTONES, MS_STATUS, PKG_TYPES, Ledger, _refs, parse_pdate
from core.watch.events import JUDGMENTS, STAGES, _when, current_next, current_stages, set_stage
from core.watch.outputs import DEADLINE_ZH, _llm, deadlines, links_of, relevant
from core.watch.store import SYSTEM

NONE = "-"  # member 事件的 value:不属于任何商机
HOW = {"member": "人工并入", "ledger": "写进台账", "auto": "系统自动写入台账", "link": "编号吻合,自动并入"}
ACTIVE = ("tracking", "preparing", "bid")  # 在跟踪
CLOSED = ("won", "lost", "dropped")  # 已结束


@dataclass(frozen=True)
class Member:
    """一条线索所属的商机:item 商机编号;how 怎么定的(见 HOW);at、by 那次操作的时间与操作人(有的话)。"""

    item: str
    how: str
    at: str = ""
    by: str = ""


@dataclass(frozen=True)
class Suggestion:
    """待确认的一条:kind "join" 可能属于已有商机(items 为 [(商机编号, 依据)],可靠的在前),"new" 可能的新商机。"""

    sig: dict
    kind: str
    items: tuple[tuple[str, str], ...] = ()


def _url_key(u: object) -> str:
    return str(u or "").strip().rstrip("/")


def cited_urls(ledgers: Iterable[Ledger]) -> dict[str, set[str]]:
    """原文网址 → 引用这条来源的商机编号(台账里有网址的来源)。"""
    out: dict[str, set[str]] = {}
    for led in ledgers:
        for it in led.items:
            for ref in set(_refs(it)):
                if u := _url_key((led.sources.get(ref) or {}).get("url")):
                    out.setdefault(u, set()).add(it["id"])
    return out


def decisions(events: list[dict]) -> dict[str, dict]:
    """线索号(前 8 位)→ 人最后一次的并入决定(member 事件;清除了的不在内)。"""
    cur: dict[str, dict] = {}
    for e in events:
        if e.get("kind") == "member":
            no = str(e.get("id", ""))[:8]
            if e.get("value"):
                cur[no] = e
            else:
                cur.pop(no, None)
    return cur


def _written(sigs: list[dict], items: set[str], events: list[dict], reviewed: dict, auto: list[dict],
             cited: dict[str, set[str]]) -> tuple[dict[str, Member], dict[str, set[str]]]:
    """写进台账的记录 → (线索号 → 所属商机, 线索号 → 写进了的几个商机〔不止一个的,交人决定〕)。"""
    out: dict[str, Member] = {}
    for no, v in reviewed.items():
        if isinstance(v, dict) and v.get("action") == "updated" and v.get("item") in items:
            out[str(no)[:8]] = Member(v["item"], "ledger", str(v.get("at") or ""))
    reverted = {(str(e.get("id", ""))[:8], str(e.get("item") or "")) for e in events
                if e.get("kind") == "ledger" and e.get("value") == "reverted"}
    autos: dict[str, dict[str, str]] = {}
    for r in auto:
        no, it = str(r.get("id", ""))[:8], str(r.get("item") or "")
        if it in items and (no, it) not in reverted:
            autos.setdefault(no, {}).setdefault(it, str(r.get("at") or ""))
    many: dict[str, set[str]] = {}
    for no, its in autos.items():
        if no in out:
            continue
        if len(its) == 1:
            (it, at), = its.items()
            out[no] = Member(it, "auto", at, "system")
        else:
            many[no] = set(its)
    for sg in sigs:  # 原文网址已是台账来源(自动写入、手工登记,或以后台账来源补登成的线索)
        no = sg["id"][:8]
        if no in out or no in many:
            continue
        its = cited.get(_url_key(sg.get("url")), set()) & items if sg.get("url") else set()
        if len(its) == 1:
            out[no] = Member(next(iter(its)), "ledger", str(sg.get("first_seen") or ""))
        elif its:
            many[no] = its
    return out, many


def _skip(triage: dict, reviewed: dict) -> tuple[set[str], set[str], set[str]]:
    """→ (忽略的, 处理过或忽略的, 核对后不用更新台账的) 线索号(前 8 位)。"""
    status = {str(k)[:8]: v.get("status") for k, v in triage.items() if isinstance(v, dict)}
    ignored = {k for k, v in status.items() if v == "ignore"}
    settled = {k for k, v in status.items() if v in ("done", "ignore")}
    skipped = {str(k)[:8] for k, v in reviewed.items() if isinstance(v, dict) and v.get("action") == "skipped"}
    return ignored, settled, skipped


def membership(sigs: Iterable[dict], items: set[str], events: list[dict], reviewed: dict, auto: list[dict],
               triage: dict, cited: dict[str, set[str]] | None = None) -> dict[str, Member]:
    """线索号(前 8 位)→ 所属商机;不属于任何商机的不在内。items:全部商机(台账项目)编号;cited:见 cited_urls。"""
    sigs = list(sigs)
    dec = decisions(events)
    written, _ = _written(sigs, items, events, reviewed, auto, cited or {})
    ignored, _, _ = _skip(triage, reviewed)
    out: dict[str, Member] = {}
    for s in sigs:
        no = s["id"][:8]
        if d := dec.get(no):
            if d["value"] in items:
                out[no] = Member(d["value"], "member", str(d.get("at") or ""), str(d.get("by") or ""))
            continue  # 人说了不属于(或并入的商机已不在台账里)
        if no in written:
            out[no] = written[no]
            continue
        if no in ignored or not relevant(s):
            continue
        high = {lk["item"] for lk in links_of(s) if lk["item"] in items and lk.get("confidence") == "high"}
        if len(high) == 1:
            out[no] = Member(high.pop(), "link", str(s.get("first_seen") or ""))
    return out


def _reason(lk: dict) -> str:
    if lk.get("confidence") == "high":
        return "●编号吻合或模型确认"
    return "○模型认为涉及" if "llm" in str(lk.get("by") or "") else "○名称相近"


def suggestions(sigs: Iterable[dict], items: set[str], events: list[dict], reviewed: dict, auto: list[dict], triage: dict,
                cited: dict[str, set[str]] | None = None, members: dict[str, Member] | None = None) -> list[Suggestion]:
    """待确认:可能属于已有商机的线索、可能的新商机(模型判为相关、没被忽略、人还没决定、没核对过的;新的在前)。"""
    sigs = list(sigs)
    members = membership(sigs, items, events, reviewed, auto, triage, cited) if members is None else members
    dec = decisions(events)
    _, many = _written(sigs, items, events, reviewed, auto, cited or {})
    ignored, settled, skipped = _skip(triage, reviewed)
    out: list[Suggestion] = []
    for s in sorted(sigs, key=lambda x: x.get("published") or x.get("first_seen") or "", reverse=True):
        no = s["id"][:8]
        if no in members or no in dec or no in ignored or no in skipped or not relevant(s):
            continue
        cands: dict[str, str] = {it: "●写进过这几个商机的台账" for it in sorted(many.get(no, ()))}
        for lk in sorted(links_of(s), key=lambda lk: lk.get("confidence") != "high"):
            if lk["item"] in items:
                cands.setdefault(lk["item"], _reason(lk))
        if cands:
            out.append(Suggestion(s, "join", tuple(cands.items())))
        elif (_llm(s) or {}).get("new_opportunity") and not s.get("links") and no not in settled:
            out.append(Suggestion(s, "new"))
    return out


def stage_of(item: str, nos: Iterable[str], events: list[dict]) -> dict | None:
    """商机的进展 → 那条 stage 记录(value 为进展;id 为商机编号即设在商机上,否则设在那条线索上);没设过为 None。"""
    cur = current_stages(events)
    cands = [cur[k] for k in (item, *nos) if k in cur]
    return max(cands, key=lambda e: (_when(e.get("at")) is not None, _when(e.get("at")) or 0), default=None) if cands else None


def timeline_events(item: str, nos: Iterable[str], events: list[dict]) -> list[dict]:
    """商机的操作记录:商机自己的(进展、记录、表格里只关联项目的跟进)+ 所属线索的,按时间先后。"""
    keys = {item, *nos}
    mine = [e for e in events if str(e.get("id", "")) == item or str(e.get("id", ""))[:8] in keys]
    return sorted(mine, key=lambda e: (_when(e.get("at")) is None, _when(e.get("at")) or 0))


@dataclass
class Book:
    """一次算好的商机全貌:items 商机编号 → (台账, 条目);members 线索号 → 所属商机;leads 商机编号 → 所属线索号;
    suggestions 待确认。"""

    items: dict[str, tuple[Ledger, dict]] = field(default_factory=dict)
    members: dict[str, Member] = field(default_factory=dict)
    leads: dict[str, list[str]] = field(default_factory=dict)
    suggestions: list[Suggestion] = field(default_factory=list)

    def stage(self, item: str, events: list[dict]) -> dict | None:
        return stage_of(item, self.leads.get(item, []), events)

    def tracked(self, events: list[dict]) -> dict[str, dict]:
        """在跟踪的商机(设了进展的,含已结束的)→ 当前进展那条记录(value 为进展)。"""
        cur = current_stages(events)
        out = {}
        for iid in self.items:
            cands = [cur[k] for k in (iid, *self.leads.get(iid, [])) if k in cur]
            if cands:
                out[iid] = max(cands, key=lambda e: (_when(e.get("at")) is not None, _when(e.get("at")) or 0))
        return out


def build(sigs: Iterable[dict], ledgers: dict[str, Ledger], events: list[dict], reviewed: dict, auto: list[dict],
          triage: dict) -> Book:
    """全部线索与台账 → Book(所属线索按首见时间新的在前)。"""
    sigs = list(sigs)
    items = {it["id"]: (led, it) for led in ledgers.values() for it in led.items}
    cited = cited_urls(ledgers.values())
    members = membership(sigs, set(items), events, reviewed, auto, triage, cited)
    first = {s["id"][:8]: s.get("first_seen") or "" for s in sigs}
    leads: dict[str, list[str]] = {}
    for no in sorted(members, key=lambda n: first.get(n, ""), reverse=True):
        leads.setdefault(members[no].item, []).append(no)
    return Book(items, members, leads, suggestions(sigs, set(items), events, reviewed, auto, triage, cited, members))


# ---------- 跟进与判断 ----------

def entry_key(e: dict) -> str:
    """一条操作记录的键(跟进记录同步、撤回用):id、时间、类别、值与内容的指纹。"""
    raw = "|".join(str(e.get(k) or "") for k in ("id", "at", "kind", "value", "note"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def retracted(events: list[dict]) -> set[str]:
    """撤回了的跟进的键(kind retract 的 value)。"""
    return {str(e["value"]) for e in events if e.get("kind") == "retract" and e.get("value")}


def retract(store, e: dict, note: str = "", by: str | None = None, at: str | None = None) -> dict:
    """撤回一条跟进(表格里删了那一行,或那一行改过、换成新的一条):记一条 kind retract,记在原来那条的 id 下。"""
    return store.log_event(str(e.get("id", "")), "retract", entry_key(e), note, by=by, at=at)


def is_work(e: dict) -> bool:
    """算不算工作留痕:跟进;人记的一笔;带说明的进展变化。系统代记的(操作人 system,或带 auto)不算。"""
    if e.get("kind") == "follow":
        return True
    return e.get("kind") in ("note", "stage") and bool(e.get("note")) and not e.get("auto") and e.get("by") != SYSTEM


def work_log(item: str, events: list[dict], leads: Iterable[str] = ()) -> list[dict]:
    """商机的工作留痕:记在商机编号下与所属线索(leads:线索号)上的跟进、人记的一笔、带说明的进展变化,按记下的先后;
    撤回了的不算;表格里同一行(ref)拉回过几次的只算最后一条。商机页的「跟进与判断」、团队表格的「跟进记录」(这个商机的行)
    与商机表的「最近跟进」都是它。"""
    nos, gone = set(leads), retracted(events)
    out = [e for e in events if is_work(e) and (str(e.get("id", "")) == item or str(e.get("id", ""))[:8] in nos)
           and entry_key(e) not in gone]
    last = {e["ref"]: i for i, e in enumerate(out) if e.get("ref")}  # 表格里同一行只算一条(取最后记下的)
    return [e for i, e in enumerate(out) if not e.get("ref") or last[e["ref"]] == i]


def follows(item: str, events: list[dict]) -> list[dict]:
    """商机的跟进记录(kind follow,记在商机编号下;撤回的不算),按记下的先后。"""
    gone = retracted(events)
    return [e for e in events if e.get("kind") == "follow" and str(e.get("id", "")) == item and entry_key(e) not in gone]


def judgments(events: list[dict], leads: dict[str, list[str]] | None = None) -> dict[str, dict]:
    """商机编号 → 最近一次带判断的跟进(value 为判断,reason 为依据,day 为跟进日期);撤回的不算。
    leads(商机编号 → 所属线索号)给了时,记在所属线索上的跟进也算这个商机的。"""
    owner = {no: iid for iid, nos in (leads or {}).items() for no in nos}
    gone = retracted(events)
    out: dict[str, dict] = {}
    for e in events:
        if e.get("kind") == "follow" and e.get("value") and entry_key(e) not in gone:
            k = str(e.get("id", ""))
            out[owner.get(k[:8], k)] = e
    return out


def declined(events: list[dict]) -> dict[str, dict]:
    """AI 推荐商机里决定不列入正式商机的:商机编号 → 那次决定(kind listing,value no;撤回为空串,最后一次为准)。"""
    out: dict[str, dict] = {}
    for e in events:
        if e.get("kind") == "listing":
            k = str(e.get("id", ""))
            if e.get("value") == "no":
                out[k] = e
            else:
                out.pop(k, None)
    return out


def decline(store, item: str, note: str = "", by: str | None = None) -> dict:
    """AI 推荐商机 → 不列入正式商机(仍在台账里,以后可撤回)。"""
    return store.log_event(item, "listing", "no", note.strip(), by=by)


def undecline(store, item: str, by: str | None = None) -> dict:
    """撤回"不列入",回到待决定的 AI 推荐商机。"""
    return store.log_event(item, "listing", "", by=by)


def judgment_text(e: dict | None) -> str:
    """一次判断 → "重点推进(2026-10-01):依据……"。"""
    if not e:
        return ""
    when = e.get("day") or str(e.get("at") or "")[:10]
    return f"{JUDGMENTS.get(e.get('value'), e.get('value'))}" + (f"({when})" if when else "") + (f":{e['reason']}" if e.get("reason") else "")


# ---------- 日历 ----------

@dataclass(frozen=True)
class CalEntry:
    """日历上的一项。date 那天(exact 为 False 的是只知道月、季度的节点,date 为期间的第一天、end 为最后一天);
    kind:node 台账节点 / package 采购标段 / deadline 线索截止 / follow 跟进 / stage 进展 / note 记录 / next 下一步到期 / report 报告;
    item 商机编号(报告为空);done 已发生(节点已发生、截止已过)。"""

    date: str
    kind: str
    item: str
    text: str
    url: str = ""
    country: str = ""
    end: str = ""
    exact: bool = True
    done: bool = False


CAL_KINDS = {"node": "台账节点", "package": "采购", "deadline": "截止", "follow": "跟进", "stage": "进展", "note": "记录",
             "next": "下一步到期", "report": "报告"}
PUBLIC_KINDS = ("node", "package", "deadline")  # 公开时间节点;其余为工作留痕


def _pd(v: object):
    try:
        return parse_pdate(v)
    except (TypeError, ValueError):
        return None


def calendar(book: Book, events: list[dict], sigs: dict[str, dict], today_iso: str, items: Iterable[str] | None = None,
             reports: Iterable = ()) -> list[CalEntry]:
    """在跟踪的商机(items 缺省为全部在跟踪的)的公开时间节点与工作留痕,加上上报的国别报告、项目报告(reports:core.watch.flow.Report,
    已按"上报领导的"筛过)→ 日历上的项。sigs:线索号(前 8 位)→ 线索。"""
    items = list(book.tracked(events) if items is None else items)
    nxt, gone = current_next(events), retracted(events)
    out: list[CalEntry] = []
    for iid in items:
        if iid not in book.items:
            continue
        led, it = book.items[iid]
        for m in it.get("milestones") or []:
            pd = _pd(m.get("date"))
            if pd:
                what = f"{MILESTONES.get(m.get('type'), m.get('type'))}({MS_STATUS.get(m.get('status'), '')})"
                out.append(CalEntry(pd.start.isoformat(), "node", iid, what + (f"·{m['note']}" if m.get("note") else ""),
                                    country=led.country, end=pd.end.isoformat(), exact=pd.precision == "day",
                                    done=m.get("status") == "actual"))
        for pk in (it.get("procurement") or {}).get("packages") or []:
            label = f"{PKG_TYPES.get(pk.get('type'), '')}{':' if pk.get('type') else ''}{pk.get('name') or ''}"
            for key, what in (("published", "发布"), ("deadline", DEADLINE_ZH.get(pk.get("deadline_type") or "other", "截止"))):
                pd = _pd(pk.get(key)) if pk.get(key) else None
                if pd:
                    out.append(CalEntry(pd.start.isoformat(), "package", iid, f"{what}·{label}", country=led.country,
                                        end=pd.end.isoformat(), exact=pd.precision == "day", done=pd.end.isoformat() < today_iso))
        nos = book.leads.get(iid, [])
        for no in nos:
            s = sigs.get(no) or {}
            title = ((_llm(s) or {}).get("summary_zh") or s.get("title") or no)[:80]
            for d, kind in deadlines(s):
                out.append(CalEntry(d, "deadline", iid, f"{kind}·{title}", url=s.get("url") or "", country=led.country, done=d < today_iso))
        keys = {iid, *nos}
        for e in events:
            k = str(e.get("id", ""))
            if k != iid and k[:8] not in keys or entry_key(e) in gone:
                continue
            w = _when(e.get("at"))
            day = str(e.get("day") or (w.date().isoformat() if w else ""))
            kind = e.get("kind")
            if not day:
                continue
            if kind == "follow":
                j = JUDGMENTS.get(e.get("value"), "")
                text = (f"判断:{j}·" if j else "") + (e.get("note") or "")[:80]
                out.append(CalEntry(day, "follow", iid, text, country=led.country, done=True))
            elif kind == "stage" and e.get("value"):
                text = f"进展:{STAGES.get(e['value'], e['value'])}" + (f"·{e['note'][:60]}" if is_work(e) else "")
                out.append(CalEntry(day, "stage", iid, text, country=led.country, done=True))
            elif kind == "note" and is_work(e):
                out.append(CalEntry(day, "note", iid, e["note"][:80], country=led.country, done=True))
        for k in keys:  # 当前的下一步(商机上的,过渡期里线索上的)
            e = nxt.get(k[:8])
            if e and e.get("due"):
                out.append(CalEntry(e["due"], "next", iid, f"下一步:{e.get('value') or '—'}", country=led.country,
                                    done=e["due"] < today_iso))
    for r in reports:
        d = getattr(r, "when", "")
        if d and len(d) == 10:
            out.append(CalEntry(d, "report", "", f"{r.kind}:{r.name}", country=",".join(r.countries), done=True))
    return sorted(out, key=lambda x: (x.date, x.kind, x.item, x.text))


# ---------- 设进展 ----------

def set_progress(store, item: str, lead_ids: Iterable[str], stage: str | None, note: str = "", by: str | None = None,
                 at: str | None = None, conflict: str = "", auto: bool = False) -> None:
    """设商机的进展(记在商机编号下)。清除(stage 为空)即不再跟踪:所属线索上以前设的进展也一并清除,
    不然商机仍按线索上的算作在跟踪。lead_ids:所属线索的 id(或线索号);auto:note 是系统代记的说明(不算工作留痕)。"""
    set_stage(store, item, stage or None, note, by=by, at=at, conflict=conflict, auto=auto)
    if not stage:
        cur = current_stages(store.events())
        for sid in lead_ids:
            if str(sid)[:8] in cur:
                set_stage(store, sid, None, f"商机 {item} 不再跟踪", by=by, at=at, auto=True)


def book_for(profile, store) -> Book:
    """按 profile 里的各国台账与数据目录算商机全貌(定时任务、团队表格同步、周报用;界面用 app.common.book)。"""
    from core.ledger import load_ledger, load_rules
    from core.ledger_auto import history  # 放在这里:core.ledger_auto 也要用本模块

    rules = load_rules(profile.rules) if profile.rules else load_rules()
    leds = {c.iso3: load_ledger(c.ledger, rules) for c in profile.countries if c.ledger}
    return build(store.load_signals(), leds, store.events(), store.ledger_reviewed(), history(store), store.triage())


# ---------- 周期性报告:商机跟踪进展 ----------

def progress_md(book: Book, events: list[dict], iso3: str, start: str, end: str, nodes: dict[str, str] | None = None) -> str:
    """周报、月报等周期性报告的「商机跟踪进展」:这一国在跟踪的商机一行——进展、当前判断、这一期(start 至 end,含)的跟进、
    判断与进展变化、下一步(到期)、下一节点(nodes:商机编号 → 台账下一节点)。只用团队自己的跟踪记录,不用内部叠加里的情报。"""
    from core.report import table

    tracked = book.tracked(events)
    judg, nxt, gone = judgments(events, book.leads), current_next(events), retracted(events)
    order = list(STAGES)
    rows = []
    for iid, st in sorted(tracked.items(), key=lambda kv: (order.index(kv[1]["value"]), kv[0])):
        led, it = book.items[iid]
        if led.country != iso3:
            continue
        keys = {iid, *book.leads.get(iid, [])}
        done = []
        for e in events:
            k, w = str(e.get("id", "")), _when(e.get("at"))
            day = str(e.get("day") or (w.date().isoformat() if w else ""))
            if (k == iid or k[:8] in keys) and start <= day <= end and entry_key(e) not in gone:
                if e.get("kind") == "follow":
                    j = JUDGMENTS.get(e.get("value"), "")
                    done.append(f"{day[5:]} 跟进:{(e.get('note') or '').strip()[:80]}" + (f"(判断:{j})" if j else ""))
                elif e.get("kind") == "stage" and e.get("value"):
                    done.append(f"{day[5:]} 进展改为{STAGES.get(e['value'], e['value'])}")
        n = nxt.get(iid) or {}
        rows.append([f"{iid} {it.get('name_zh') or it.get('name_en') or ''}", STAGES.get(st["value"], st["value"]),
                     judgment_text(judg.get(iid)) or "—", ";".join(done) or "本期没有", (n.get("value") or "—")
                     + (f"(到期 {n['due']})" if n.get("due") else ""), (nodes or {}).get(iid) or "—"])
    if not rows:
        return "没有在跟踪的商机(在「商机」页或团队表格里设进展即开始跟踪)。"
    return table(["商机", "进展", "当前判断", "本期跟进与进展", "下一步", "下一节点"], rows)
