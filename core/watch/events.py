"""线索(商机)的跟踪进展与操作记录。

- 进展:跟踪中、准备投标、已投标、中标、未中标、放弃。在界面「商机」页(设在商机上)或线索详情(设在线索上)设置,记进操作记录;
  进展不变时也可以只记一笔备注(如"已购买招标文件")。
- 操作记录(时间线)把几处信息并起来按时间排:采集(首见时间、信源、发布日)、模型判定(判定时间;2026-09-27 以前的
  判定没记时间,按采集时间列出并注明)、系统挂台账建议、同一公告后来的变化、界面上的处理状态 / 进展 / 台账核对
  (watch/events.jsonl),以及截止日。记日志以前做的处理标记、台账核对只有结果或日期,也列出并注明时间未记录。
- 每条操作记下操作人(by):本机界面为本机的岗位代号(.env 的 OPERATOR),服务器上的界面为登录表格的人的代号,定时任务为 system;
  缺省见 core.watch.store.operator。
- 人工入库(界面"搜索入库")记一条 kind submit(value 为 url / text / file),记下是谁交进来的。
- 线索并入商机的决定记 kind member(value 为商机编号;"-" 为不属于任何商机;空串为清除决定,回到系统判断),
  商机的进展、记录记在商机编号下(id 为 MAR-005 这样的编号),见 core.opportunity。
- 跟进记 kind follow(记在商机编号下):note 为做了什么、了解到什么,value 为对商机的判断(见 JUDGMENTS,可空),
  reason 为判断依据,day 为跟进日期(人填的;at 是记下的时间);stage、step、due 为这次跟进后改的进展与下一步(到期)。
  团队表格「跟进记录」拉回的、商机页上记的都是它;表格拉回的带 ref(那一行的记录 ID)。
- 跟进撤回记 kind retract(value 为那条的键,core.opportunity.entry_key):表格里删了那一行、或那一行改过(换成新的一条)。
- 系统按别处的改动代记的进展说明、记录(如"在线索 xxx 上改的""表格里的备注改为……")带 auto,不算商机的工作留痕。
"""
from __future__ import annotations

from datetime import date, datetime, time

from core.watch.outputs import CAT_ZH, LEDGER, MANUAL, SECTOR_ZH, category, deadlines, links_of, relevant, sectors_of, summary
from core.watch.store import SYSTEM, Store

STAGES = {"tracking": "跟踪中", "preparing": "准备投标", "bid": "已投标", "won": "中标", "lost": "未中标", "dropped": "放弃"}
STATUS_ACT = {"done": "标为已处理", "ignore": "标为忽略", "new": "恢复待处理"}
LEDGER_ACT = {"updated": "内容写进台账", "skipped": "核对后不用更新台账", "undone": "撤销台账核对",
              "auto": "系统自动写进台账", "confirmed": "确认系统自动写入的台账内容", "reverted": "撤销系统自动写入的台账内容",
              "created": "跟踪即入账:按线索新建台账项目"}
SUBMIT_ACT = {"url": "人工入库(网址)", "text": "人工入库(粘贴文字)", "file": "人工入库(上传文件)"}
JUDGMENTS = {"continue": "继续跟进", "focus": "重点推进", "watch": "观望", "drop": "放弃"}  # 跟进时对商机的判断


def set_stage(store: Store, sid: str, stage: str | None, note: str = "", by: str | None = None, at: str | None = None,
              conflict: str = "", auto: bool = False) -> dict:
    """设置线索的跟踪进展(None 或空串为清除),记进操作记录;by 为操作人(缺省本机的);conflict 见 event_detail;
    auto:note 是系统代记的说明(不算工作留痕)。"""
    if stage and stage not in STAGES:
        raise ValueError(f"进展只能是:{'、'.join(STAGES.values())}")
    return store.log_event(sid, "stage", stage or "", note, by=by, at=at, conflict=conflict, auto="1" if auto else "")


def add_note(store: Store, sid: str, note: str, by: str | None = None, at: str | None = None, auto: bool = False) -> dict:
    """给线索记一笔备注(不改进展),记进操作记录。auto:系统代记的(如表格里改了备注),不算工作留痕。"""
    if not note.strip():
        raise ValueError("备注是空的")
    return store.log_event(sid, "note", "", note.strip(), by=by, at=at, auto="1" if auto else "")


def set_owner(store: Store, sid: str, owner: str, by: str | None = None, at: str | None = None) -> dict:
    """设置线索的负责人(岗位代号或表格账号的显示名;空串为清除),记进操作记录(kind owner)。"""
    return store.log_event(sid, "owner", owner.strip(), by=by, at=at)


def set_next(store: Store, sid: str, step: str, due: str | None = None, by: str | None = None, at: str | None = None) -> dict:
    """设置线索的下一步与到期日(YYYY-MM-DD,可空;两者都空为清除),记进操作记录(kind next,到期日记在 due)。"""
    return store.log_event(sid, "next", step.strip(), by=by, at=at, due=due or "")


def log_submit(store: Store, sid: str, how: str, note: str = "", by: str | None = None) -> dict:
    """人工入库的线索记一笔是谁交进来的(how:url / text / file;note 为提交时写的备注)。"""
    if how not in SUBMIT_ACT:
        raise ValueError(f"入库方式只能是:{'、'.join(SUBMIT_ACT)}")
    return store.log_event(sid, "submit", how, note.strip(), by=by)


def add_follow(store: Store, sid: str, content: str, judgment: str = "", reason: str = "", day: str | None = None,
               by: str | None = None, at: str | None = None, stage: str = "", step: str = "", due: str | None = None,
               ref: str = "") -> dict:
    """记一次跟进(kind follow):content 做了什么、了解到什么;judgment 对商机的判断(JUDGMENTS 的键,可空);reason 判断依据;
    day 跟进日期(YYYY-MM-DD,缺省为记下的那天);stage、step、due:这次跟进后改的进展、下一步与到期(只记下来,
    改进展、下一步另由调用方做);ref:团队表格「跟进记录」里那一行的记录 ID(表格拉回的)。"""
    if judgment and judgment not in JUDGMENTS:
        raise ValueError(f"判断只能是:{'、'.join(JUDGMENTS.values())}")
    if stage and stage not in STAGES:
        raise ValueError(f"进展只能是:{'、'.join(STAGES.values())}")
    if not content.strip() and not judgment:
        raise ValueError("跟进内容是空的")
    return store.log_event(sid, "follow", judgment, content.strip(), by=by, at=at, reason=reason.strip(), day=day or "",
                           stage=stage, step=step.strip(), due=due or "", ref=ref)


def set_member(store: Store, sid: str, item: str, note: str = "", by: str | None = None, at: str | None = None) -> dict:
    """线索并入商机的决定(item:商机编号;"-" 不属于任何商机;空串清除决定、回到系统判断),记进操作记录(kind member)。"""
    return store.log_event(sid, "member", item.strip(), note.strip(), by=by, at=at)


def _current(events: list[dict], kind: str) -> dict[str, dict]:
    """线索号(前 8 位)→ 某类操作的现值记录(最后一次设置;清除了的不在内)。"""
    cur: dict[str, dict] = {}
    for e in events:
        if e.get("kind") == kind:
            no = str(e.get("id", ""))[:8]
            if e.get("value") or e.get("due"):
                cur[no] = e
            else:
                cur.pop(no, None)
    return cur


def current_owners(events: list[dict]) -> dict[str, dict]:
    """线索号 → 当前负责人的那条记录(value 为负责人)。"""
    return _current(events, "owner")


def current_next(events: list[dict]) -> dict[str, dict]:
    """线索号 → 当前下一步的那条记录(value 为下一步,due 为到期日)。"""
    return _current(events, "next")


def by_zh(by: str | None) -> str:
    """操作人 → 显示:system 为"系统",teable:<名> 为"<名>(表格)",岗位代号原样;旧记录没有操作人,为空。"""
    if not by:
        return ""
    if by == SYSTEM:
        return "系统"
    if by.startswith("teable:"):
        return f"{by[len('teable:'):]}(表格)"
    return by


def latest_events(events: list[dict]) -> dict[str, dict]:
    """线索号(前 8 位)→ 最近的一次操作(按实际时刻比,同一秒的取后记的;时间写坏了的不算)。"""
    out: dict[str, dict] = {}
    for e in events:
        no, w = str(e.get("id", ""))[:8], _when(e.get("at"))
        if w and (no not in out or w >= _when(out[no]["at"])):
            out[no] = e
    return out


def latest(events: list[dict]) -> dict[str, str]:
    """线索号(前 8 位)→ 最近一次操作的时间(ISO)。"""
    return {no: e["at"] for no, e in latest_events(events).items()}


def current_stages(events: list[dict]) -> dict[str, dict]:
    """线索号(前 8 位)→ 当前进展的那条记录(最后一次设置;清除了的不在内)。"""
    cur: dict[str, dict] = {}
    for e in events:
        if e.get("kind") == "stage":
            no = str(e.get("id", ""))[:8]
            if e.get("value"):
                cur[no] = e
            else:
                cur.pop(no, None)
    return cur


def event_title(e: dict) -> str:
    """操作记录里的一条 → 中文说明,如"标为已处理""进展:已投标""内容写进台账 MAR-001"。"""
    kind, v = e.get("kind"), e.get("value") or ""
    if kind == "status":
        return STATUS_ACT.get(v, v)
    if kind == "stage":
        return f"进展:{STAGES.get(v, v)}" if v else "清除进展"
    if kind == "ledger":
        return LEDGER_ACT.get(v, v) + (f" {e['item']}" if e.get("item") else "")
    if kind == "note":
        return "记录"
    if kind == "owner":
        return f"负责人:{v}" if v else "清除负责人"
    if kind == "next":
        if not (v or e.get("due")):
            return "清除下一步"
        return f"下一步:{v or '—'}" + (f"(到期 {e['due']})" if e.get("due") else "")
    if kind == "submit":
        return SUBMIT_ACT.get(v, "人工入库")
    if kind == "member":
        return "不属于任何商机" if v == "-" else f"并入商机 {v}" if v else "清除并入决定(回到系统判断)"
    if kind == "follow":
        return "跟进" + (f"({e['day']})" if e.get("day") else "") + (f" · 判断:{JUDGMENTS.get(v, v)}" if v else "")
    if kind == "retract":
        return "撤回一条跟进"
    return str(kind)


def event_detail(e: dict) -> str:
    """备注、台账来源;表格拉回时与本机的改动冲突的,注明本机原来的值(以表格为准)。"""
    return ";".join(s for s in (e.get("note"), f"判断依据:{e['reason']}" if e.get("reason") else "",
                                f"跟进后进展:{STAGES.get(e['stage'], e['stage'])}" if e.get("kind") == "follow" and e.get("stage") else "",
                                f"下一步:{e['step']}" + (f"(到期 {e['due']})" if e.get("due") else "")
                                if e.get("kind") == "follow" and e.get("step") else "",
                                f"台账来源 {e['source']}" if e.get("source") else "",
                                f"本机也改过({e['conflict']}),以表格为准" if e.get("conflict") else "") if s)


def _when(at: str | None, end_of_day: bool = False) -> datetime | None:
    """ISO 时间或日期 → 带时区的时间(只有日期的按当天中午;截止日按当天结束)。"""
    if not at:
        return None
    try:
        if len(at) == 10:
            return datetime.combine(date.fromisoformat(at), time(23, 59) if end_of_day else time(12, 0)).astimezone()
        dt = datetime.fromisoformat(at)
        return dt if dt.tzinfo else dt.astimezone()
    except ValueError:
        return None


def timeline(sig: dict, events: list[dict], triage: dict, reviewed: dict, related: list[dict] | None = None,
             now: datetime | None = None) -> list[dict]:
    """一条线索的操作记录 → [{at, when, date_only, kind, title, detail, by}]:已发生的按时间排,时间未记录的随后,未来的截止日在最后。
    by 是操作记录里的操作人(显示用,见 by_zh;系统采集、判定等不是操作的为空)。
    related:同一公告(同一信源同一键)的其他线索,如后来内容有变化又采集的。"""
    now = now or datetime.now().astimezone()
    no = sig["id"][:8]
    out: list[dict] = []

    def add(at: str | None, kind: str, title: str, detail: str = "", end_of_day: bool = False, by: str = "") -> None:
        out.append({"at": at, "when": _when(at, end_of_day), "date_only": bool(at) and len(at) == 10,
                    "kind": kind, "title": title, "detail": detail, "by": by})

    add(sig.get("first_seen"), "collect", "系统采集",
        f"信源「{sig.get('source_name') or sig.get('source')}」" + (f";公告发布于 {sig['published']}" if sig.get("published") else "")
        + (";原公告内容有变化,作为新线索采集" if sig.get("change") == "changed" else ""))
    x = sig.get("llm") or {}
    if x and not x.get("_error"):
        cat = category(sig)
        what = "、".join(v for v in (CAT_ZH.get(cat, cat), "、".join(SECTOR_ZH.get(k, k) for k in sectors_of(sig))) if v)
        verdict = "人工提交,不判定相关与否" if sig.get("source") == MANUAL else f"模型判定:{'相关' if relevant(sig) else '不相关'}"
        add(x.get("judged_at") or sig.get("first_seen"), "judge", verdict,
            what + (f" — {summary(sig)}" if summary(sig) else "") + ("" if x.get("judged_at") else "(判定时间未记录,多在采集当次)"))
    elif sig.get("source") == LEDGER:
        f = sig.get("fields") or {}
        add(sig.get("first_seen"), "judge", "台账出处,写进台账时已核对(不另判定)",
            f"台账 {f.get('ledger', '')} 引用" + (f"({'、'.join(f['items'])})" if f.get("items") else ""))
    elif (sig.get("rules") or {}).get("water_terms"):
        add(sig.get("first_seen"), "judge", "规则初筛(模型未判定)", "命中:" + "、".join(sig["rules"]["water_terms"]))
    lks = links_of(sig)
    if lks:
        add(sig.get("first_seen"), "link", "提示涉及台账项目",
            ";".join(f"{lk['item']}({'可靠' if lk['confidence'] == 'high' else '需核对'})" for lk in lks))
    for r in related or []:
        if r["id"] != sig["id"]:
            add(r.get("first_seen"), "change", "原公告有变化" if r.get("change") == "changed" else "同一公告的另一条线索",
                f"线索号 {r['id'][:8]}")
    mine = [e for e in events if str(e.get("id", ""))[:8] == no]
    for e in mine:
        add(e.get("at"), str(e.get("kind")), event_title(e), event_detail(e), by=by_zh(e.get("by")))
    kinds = {e.get("kind") for e in mine}
    t = triage.get(sig["id"]) or triage.get(no) or {}
    if t.get("status") and "status" not in kinds:  # 记日志以前做的标记
        add(None, "status", STATUS_ACT.get(t["status"], t["status"]) + "(标记时间未记录)", t.get("note") or "")
    rv = reviewed.get(no) or reviewed.get(sig["id"])
    if rv and "ledger" not in kinds:
        add(rv.get("at"), "ledger", LEDGER_ACT.get(rv.get("action"), str(rv.get("action"))) + (f" {rv['item']}" if rv.get("item") else ""),
            f"台账来源 {rv['source']}" if rv.get("source") else "")
    for d, kind in deadlines(sig):
        add(d, "deadline", kind, "已过" if d < now.date().isoformat() else "", end_of_day=True)
    past = sorted((e for e in out if e["when"] and e["when"] <= now), key=lambda e: e["when"])
    unknown = [e for e in out if not e["when"]]
    future = sorted((e for e in out if e["when"] and e["when"] > now), key=lambda e: e["when"])
    return past + unknown + future
