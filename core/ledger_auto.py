"""台账自动更新:挂接可靠的线索(台账编号吻合、监测配置指定、或模型读原文后确认)里的事实,自动写进台账——
登记来源(标"系统自动写入,未经人工核对")、新增或给已有条目加引用的时间节点与采购标段、前期项目出了采购公告时改为"采购进行中"。
只按名称相近挂上的线索、可能的新商机,仍由人在界面「待确认」页确认。

- 定时任务每天在监测之后运行(python -m core.cli autoledger):每条线索单独一次提交,最后变基推送到 main。
  推送不成功就不记,下次运行重来;已写进台账的内容会被认出来(原文网址已登记为来源),不重复写。
- 写了什么记在数据目录 watch/ledger_auto.jsonl(data 分支),线索的操作记录里也记一笔;界面「待确认」页据此提醒核对,
  可一键撤销(按内容删掉那次写入的来源、节点与标段,并把这条线索记为"不用更新",以后不再自动写)。
- 模型没判为相关的、人在界面上标了"忽略"或"不用更新"的线索不写;不改线索的处理状态(仍在待处理清单里,等人看)。
  挂接可靠、但台账里已有它说的内容(或它只是动态、没有日期标段可写)的,记为"不用更新"(系统核对),不再等人核对。
- 界面上也可以当场运行(「待确认」页"现在写入"):同样逐条提交、推送,推送失败时提交留在本机,联网后推送。
- 对账(reconcile,定时任务每次运行开始时):main 上有"台账自动更新"提交、记录里却没有的(那天 data 分支的提交没成功,
  评审 F26),按提交补记一条待过目的记录(状态"记录缺失"),「待确认」页照常提醒、可撤销。
- 自动写入与系统核对的操作记录,操作人记 system。

跟踪即入账(enter_ledger → create_from_lead):线索设了跟踪进展(本机界面的线索详情,或表格里拉回的)、
还没并入商机、没有可靠(●)挂接、也没写进过台账时,按线索新建项目(商机),提交并推送程序仓库;所在国还没有台账的,
连台账文件一起建(profile 按台账目录自动认出新国家)。本机界面与表格拉回共用这一处规则。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

from core import geo, sync
from core.ledger import LedgerError, load_ledger, load_rules
from core.ledger_edit import _clean, apply, brief_changes, propose, remove_source
from core.opportunity import NONE, decisions
from core.profile import Profile, ledger_file
from core.watch.outputs import links_of, sectors_of
from core.watch.store import SYSTEM, Store

AUTO_FILE = "ledger_auto.jsonl"
MISSING = "记录缺失"  # 对账补记的记录的状态:main 上有这次自动写入的提交,当天的记录没有同步到 data 分支
_AUTO_MSG = re.compile(r"^台账自动更新:(?P<item>\S+) (?P<brief>.*)\(线索 (?P<no>[0-9a-f]{8})\)$")  # run() 写的提交说明


def _url_key(u: object) -> str:
    return str(u or "").strip().rstrip("/")


def candidates(sigs: list[dict], led, skip: set[str]) -> list[tuple[str, dict]]:
    """→ [(台账编号, 线索)],按首见时间先后:模型判为相关、没被人忽略或标"不用更新"(skip:线索号前 8 位)、
    原文网址还没登记为台账来源、挂接可靠(编号吻合、监测配置指定、或模型读原文后确认)。"""
    items = {it["id"] for it in led.items}
    known = {_url_key(s.get("url")) for s in led.sources.values() if isinstance(s, dict) and s.get("url")}
    out: list[tuple[str, dict]] = []
    for s in sorted(sigs, key=lambda x: x.get("first_seen") or ""):
        x = s.get("llm") or {}
        if s["id"][:8] in skip or x.get("_error") or x.get("relevant") is not True or (s.get("url") and _url_key(s["url"]) in known):
            continue
        out += [(lk["item"], s) for lk in links_of(s) if lk["item"] in items and lk.get("confidence") == "high"]
    return out


# apply() 的这两种"不用写"不算出错:线索挂接可靠,但台账里已有它说的全部内容(或它只是动态,没有日期、标段可写)
NOTHING = ("没有选择要写入台账的内容", "台账里已有这些内容")


def _write(lp: Path, item_id: str, sig: dict, today: date, rules: dict) -> tuple[dict | None, str]:
    """按线索写一个台账项目 → (记录, "");没写时 (None, 原因)。"""
    led = load_ledger(lp, rules)  # 每条都重新读:前一条写进去的节点、标段,这一条就只加引用
    item = next((it for it in led.items if it["id"] == item_id), None)
    if item is None:
        return None, f"台账里没有 {item_id}"
    upd = propose(sig, item, today)
    upd.source = {**(upd.source or {}), "via": "auto_extract",
                  "note": f"系统线索 {sig['id'][:8]} 挂接可靠,自动写入,未经人工核对({today.isoformat()})"}
    res = apply(lp, upd, today, rules, write=True)
    if res.errors:
        return None, ";".join(res.errors)
    added = {"milestones": [[m["type"], _clean(m["date"])] for m in upd.milestones],  # 撤销时按内容删(core.ledger_edit.remove_source)
             "packages": [[_clean(x["name"]), _clean(x.get("deadline"))] for x in upd.packages],
             "stage": [item.get("stage"), upd.stage] if upd.stage and upd.stage != item.get("stage") else None}
    return {"id": sig["id"], "item": item_id, "name": item.get("name_zh") or "", "iso": led.country, "source": res.source_id or "",
            "changes": res.changes, "title": sig.get("title") or "", "url": sig.get("url") or "", "added": added}, ""


def run(profile: Profile, store: Store, today: date, push: bool = True, identity: tuple[str, ...] = sync.BOT_ID,
        keep_unpushed: bool = False, reconcile_first: bool = True) -> tuple[list[dict], list[str]]:
    """写台账、逐条提交、推送,记下写了什么 → (记录, 警告)。
    挂接可靠、但没有新内容可写的线索记为"不用更新"(系统核对),不再列在「待确认」页的待核对里。
    push=False 时只写文件与本机提交(测试、试跑用)。keep_unpushed:推送失败时仍记下(本机界面:提交留在本机,联网后推送);
    定时任务不留——它的检出用完即弃,没推上去的提交就没了。
    reconcile_first:先对账(reconcile),补记的列进警告。本机界面不对账:本机的数据目录可能还没拉到定时任务刚记下的记录。"""
    rules = load_rules(profile.rules) if profile.rules else load_rules()
    warns: list[str] = []
    if reconcile_first:
        warns += [f"对账补记:{r['item']} 线索 {r['id'][:8]} 的自动写入(提交 {r['sha'][:7]})当时没有记录下来,已补记为待过目"
                  for r in reconcile(profile, store, rules)]
    tri, reviewed = store.triage(), store.ledger_reviewed()
    skip = {str(k)[:8] for k, v in tri.items() if isinstance(v, dict) and v.get("status") == "ignore"}
    skip |= {str(k)[:8] for k, v in reviewed.items() if isinstance(v, dict) and v.get("action") == "skipped"}
    sigs = store.load_signals()
    done: list[dict] = []
    roots: set[Path] = set()
    for c in profile.countries:
        if not c.ledger:
            continue
        lp = Path(c.ledger).resolve()
        root = sync.toplevel(lp)
        nothing: dict[str, dict] = {}
        written: set[str] = set()
        for item_id, sig in candidates(sigs, load_ledger(lp, rules), skip):
            rec, err = _write(lp, item_id, sig, today, rules)
            if rec is None:
                if err.startswith(NOTHING):
                    nothing[sig["id"]] = sig
                else:
                    warns.append(f"{item_id} 线索 {sig['id'][:8]} 没写进台账:{err[:200]}")
                continue
            rec["message"] = f"台账自动更新:{item_id} {brief_changes(rec['changes'])}(线索 {sig['id'][:8]})"
            if root:
                r = sync.commit_files(root, [lp, lp.with_name("sources.yaml")], rec["message"], identity)
                if not r.ok:
                    warns.append(f"{item_id} 线索 {sig['id'][:8]} 写进台账了,但提交失败:{r.output[:200]}")
                    continue
                roots.add(root)
            written.add(sig["id"])
            done.append(rec)
        for sid in nothing.keys() - written:
            store.mark_ledger_reviewed(sid, "skipped", today, by=SYSTEM,
                                       note="系统核对:挂接可靠,但台账里已有它说的内容,或它没有日期、标段可写")
    if push and roots:
        for root in roots:
            r = sync.push_retry(root, identity)
            if not r.ok and not keep_unpushed:  # 本机提交留着(定时任务的检出用完即弃);不记,下次运行重来
                return [], warns + [f"推送台账失败,这次的自动更新不记:{r.output[:300]}"]
            if not r.ok:
                warns.append(f"台账已提交在本机,推送到 GitHub 失败(联网后在\"设置与同步\"点\"检查并更新程序\"推送):{r.output[:200]}")
    for root in roots:
        sha = {subj: h for h, subj in sync.recent_commits(root, len(done) + 20)}
        for rec in done:
            rec["sha"] = sha.get(rec["message"], rec.get("sha", ""))
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with (store.root / AUTO_FILE).open("a", encoding="utf-8") as f:
        for rec in done:
            f.write(json.dumps({"at": at, **rec}, ensure_ascii=False) + "\n")
    for rec in done:
        store.log_event(rec["id"], "ledger", "auto", item=rec["item"], source=rec["source"], by=SYSTEM)
    return done, warns


def history(store: Store) -> list[dict]:
    """自动写进台账的记录(新的在后);坏行跳过。"""
    fp = store.root / AUTO_FILE
    out = []
    for line in fp.read_text(encoding="utf-8").splitlines() if fp.exists() else []:
        try:
            if line.strip():
                out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _key(r: dict) -> tuple[str, str]:
    return str(r.get("id", ""))[:8], str(r.get("item") or "")


def pending(store: Store, events: list[dict] | None = None) -> list[dict]:
    """还没人确认或撤销的自动更新(新的在前)。确认、撤销记在操作记录里(ledger / confirmed、reverted)。
    同一次写入既有当时的记录、又有对账补记的(两边的数据后来才合到一起),只列当时的那条。"""
    events = store.events() if events is None else events
    handled = {(str(e.get("id", ""))[:8], str(e.get("item") or "")) for e in events
               if e.get("kind") == "ledger" and e.get("value") in ("confirmed", "reverted")}
    recs = list(reversed(history(store)))
    best: dict[tuple[str, str], dict] = {}
    for r in recs:
        if _key(r) not in best or (best[_key(r)].get("status") == MISSING and r.get("status") != MISSING):
            best[_key(r)] = r
    return [r for r in recs if best[_key(r)] is r and _key(r) not in handled]


def _added_by_commit(root: Path, sha: str, lp: Path, item_id: str) -> dict | None:
    """一次自动写入新增了什么(撤销按内容删用):比较那次提交前后的台账里这个项目的节点、标段与阶段;取不到时 None。"""
    def item_at(rev: str) -> dict | None:
        text = sync.show(root, rev, lp)
        try:
            raw = yaml.safe_load(text) if text else None
        except yaml.YAMLError:
            return None
        return next((it for it in (raw or {}).get("opportunities") or [] if it.get("id") == item_id), {}) if raw else None

    old, new = item_at(f"{sha}^"), item_at(sha)
    if not new or old is None:
        return None

    def ms(it: dict) -> set[tuple[str, str]]:
        return {(_clean(m.get("type")), _clean(m.get("date"))) for m in it.get("milestones") or []}

    def ps(it: dict) -> set[tuple[str, str]]:
        return {(_clean(p.get("name")), _clean(p.get("deadline"))) for p in (it.get("procurement") or {}).get("packages") or []}

    return {"milestones": [list(x) for x in sorted(ms(new) - ms(old))], "packages": [list(x) for x in sorted(ps(new) - ps(old))],
            "stage": [old.get("stage"), new.get("stage")] if old and old.get("stage") != new.get("stage") else None}


def reconcile(profile: Profile, store: Store, rules: dict | None = None) -> list[dict]:
    """对账(评审 F26):main 上有"台账自动更新"提交、watch/ledger_auto.jsonl 里却没有记录的——自动写入先推送 main,
    当天 data 分支的提交却没成功——按提交补记一条待过目的记录(status 为"记录缺失",撤销所需的"新增了什么"从提交前后的台账算出),
    操作记录也补一笔(操作人 system,时间取提交时间)。那次登记的来源已不在台账里(撤销过、人改过)的不补。→ 补记的记录。
    提交历史要够深:定时任务检出 main 时取全部历史(monitor.yml 的 fetch-depth: 0)。"""
    rules = rules if rules is not None else (load_rules(profile.rules) if profile.rules else load_rules())
    have = {_key(r) for r in history(store)}
    by8: dict[str, dict] | None = None
    out: list[dict] = []
    for c in profile.countries:
        if not c.ledger:
            continue
        lp = Path(c.ledger).resolve()
        root = sync.toplevel(lp)
        if root is None:
            continue
        led = None
        for sha, at, subject in sync.file_log(root, [lp, lp.with_name("sources.yaml")]):
            m = _AUTO_MSG.match(subject)
            if not m or (m["no"], m["item"]) in have:
                continue
            led = led or load_ledger(lp, rules)
            src = next((k for k, s in led.sources.items() if isinstance(s, dict) and s.get("via") == "auto_extract"
                        and f"系统线索 {m['no']} 挂接可靠" in str(s.get("note") or "")), None)
            item = next((it for it in led.items if it["id"] == m["item"]), None)
            if src is None or item is None:
                continue
            if by8 is None:
                by8 = {s["id"][:8]: s for s in store.load_signals()}
            sig = by8.get(m["no"]) or {}
            out.append({"at": at, "id": sig.get("id") or m["no"], "item": m["item"], "name": item.get("name_zh") or "",
                        "iso": led.country, "source": src, "changes": [x for x in m["brief"].split(";") if x],
                        "title": sig.get("title") or led.sources[src].get("title") or "",
                        "url": sig.get("url") or led.sources[src].get("url") or "",
                        "added": _added_by_commit(root, sha, lp, m["item"]), "message": subject, "sha": sha, "status": MISSING})
            have.add((m["no"], m["item"]))
    if out:
        with (store.root / AUTO_FILE).open("a", encoding="utf-8") as f:
            for rec in out:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        for rec in out:
            store.log_event(rec["id"], "ledger", "auto", note=f"{MISSING}:当天的记录没有同步成功,按 main 上的提交补记",
                            item=rec["item"], source=rec["source"], by=SYSTEM, at=rec["at"])
    return out


def confirm(store: Store, rec: dict, by: str | None = None) -> None:
    """人看过、认可这次自动更新:操作记录记一笔,提醒里不再列出(台账内容不变,来源仍标"系统自动写入")。"""
    store.log_event(rec["id"], "ledger", "confirmed", item=rec["item"], source=rec.get("source", ""), by=by)


def undo(store: Store, rec: dict, ledger_path: Path, today: date, rules: dict | None = None, by: str | None = None) -> sync.Result:
    """撤销一次自动更新:先把台账更新到最新,按内容删掉那次写入的来源、新增的节点与标段、改的阶段(别的来源也引用了的留着),
    提交并推送;成功后这条线索记为"不用更新"(以后不再自动写),操作记录记一笔。"""
    lp = Path(ledger_path)
    root = sync.toplevel(lp)
    if root:
        u = sync.update_program(root)
        if not u.ok:
            return sync.Result(False, "更新程序仓库失败,先在\"设置与同步\"里点\"检查并更新程序\":" + u.output[:300])
    res = remove_source(lp, rec.get("source", ""), rec["item"], rec.get("added"), today, rules)
    if res.errors:
        return sync.Result(False, ";".join(res.errors)[:400])
    msg = f"撤销台账自动更新:{rec['item']} {brief_changes(res.changes)}(线索 {rec['id'][:8]})"
    r = sync.commit_program(root, [lp, lp.with_name("sources.yaml")], msg) if root else sync.Result(True, "")
    store.mark_ledger_reviewed(rec["id"], "skipped", today, by=by, note="撤销了系统自动写入的内容,以后不再自动写")
    store.log_event(rec["id"], "ledger", "reverted", item=rec["item"], source=rec.get("source", ""), by=by)
    if not r.ok:
        return sync.Result(True, f"已撤销(提交在本机,推送失败,联网后在\"设置与同步\"推送):{r.output[:200]}")
    return sync.Result(True, ";".join(res.changes))


# ---------- 跟踪即入账:线索设了跟踪进展、台账里还没有这个项目时,按线索建项目 ----------

def lead_ids(sig: dict) -> dict[str, str]:
    """线索里能唯一认出项目的编号(建项目时写进 ids,以后同一项目的公告按编号挂上):世行项目号、摩洛哥门户的公告号。
    招标编号不算:各家业主常用"12/2026"这类通用写法,会把别家的公告误挂上来(写在标段备注里)。"""
    f = sig.get("fields") or {}
    out: dict[str, str] = {}
    if str(sig.get("source", "")).startswith("wb") and re.fullmatch(r"P\d{6}", str(f.get("project_id") or "")):
        out["世行"] = f["project_id"]
    if m := re.search(r"refConsultation=(\d+)", sig.get("url") or ""):
        out["门户"] = f"refConsultation={m[1]}"
    return out


def _segments(sig: dict) -> list[str]:
    """线索的领域 → 台账的细分领域(水利按标题再分防洪、灌溉、水库,都不像时算灌溉)。"""
    t = (sig.get("title") or "").lower()
    out = []
    for k in sectors_of(sig):
        if k == "hydraulic":
            out.append("flood" if re.search(r"inondation|flood|\bcrues?\b|\bsel\b", t)
                       else "irrigation" if re.search(r"irrigation|irrigu|suvarma", t)
                       else "reservoir" if re.search(r"barrage|\bdams?\b|reservoir|réservoir|su anbar", t) else "irrigation")
        elif k in ("water_supply", "wastewater", "desalination", "stormwater", "environment"):
            out.append(k)
    return list(dict.fromkeys(out))


_BUYER_ZH = r"(?:局|公司|办公室|机构|门户|银行|基金|政府|委员会|中心|部|署|厅|市|省|地区|大区)"
_BUYER = rf"(?:{_BUYER_ZH}|[A-Z][A-Z-]+)"  # 业主名的结尾;拉丁字母缩写如 SRMSM、ONEE
_LEAD_IN = re.compile(rf"^.{{0,40}}?{_BUYER}\s*(?:就|发布了?|公布了?|(?:国际)?(?:公开)?招标[:\uff1a]?(?=.)|[:\uff1a])")
_FUNDER = re.compile(r"^(?:世界银行|世行|亚洲开发银行|亚行|伊斯兰开发银行|欧洲复兴开发银行|欧洲投资银行|非洲开发银行|亚投行|"
                     r"欧佩克基金|阿拉伯经济社会发展基金|科威特基金|沙特发展基金|法国开发署|德国复兴信贷银行)(?:资助的|出资的)?")
_TAIL = re.compile(r"(?:的)?(?:(?:咨询)?(?:公司|顾问|服务)?意向书?(?:征集)?|征集.*|(?:特定|总)?(?:采购|招标)公告"
                   r"|(?:进行)?(?:国际)?(?:公开)?(?:招标|采购))$")
_DATES = re.compile(r"截止|截标|估算|估价|预算|预估|金额|投标|提交")
_PAREN = re.compile("[(\uff08][^)\uff09]*[)\uff09]")


def _name_zh(sig: dict) -> str:
    """项目中文名:模型中文摘要里说"招什么"的那一句——去掉国名、出资方、"某局就/某公司发布/某公司招标"开头、
    括注和"公开招标""意向书征集"结尾;一句只剩业主名时看下一句,名称太短时补上下一句;都不成时用原标题。"""
    country = geo.name_zh(sig.get("country")) if sig.get("country") else ""

    def lead(s: str) -> str:
        return s[len(country):] if country and s.startswith(country) else s

    clauses = [c.strip() for c in re.split("[,\uff0c\u3002;\uff1b]", (sig.get("llm") or {}).get("summary_zh") or "")]
    for i, clause in enumerate(clauses):
        if _DATES.search(clause):  # 截止日、金额那一句不是名称
            continue
        s = _PAREN.sub("", re.sub("[\u201c\u201d\"]", "", clause))
        s = lead(_FUNDER.sub("", lead(s)))
        s = re.sub(r"^(?:由)?.{0,20}?(?:资助|出资|融资)的", "", s)
        s = _LEAD_IN.sub("", lead(s), count=1).strip()
        s = _TAIL.sub("", re.sub(r"^(?:就|发布了?|公布了?|(?:国际)?(?:公开)?招标[:\uff1a]?)", "", s)).strip()
        if m := re.search(r"(?<=.{4})(?:发布|公布|征集|启动)", s):  # "某项目发布…":留项目;"某地区发布…":留发布的内容
            s = s[:m.start()] if re.search(r"(?:项目|工程|计划|建设)$", s[:m.start()]) else _TAIL.sub("", s[m.end():])
        s = s.strip()
        if len(s) < 6 or re.fullmatch(rf".*{_BUYER_ZH}", s):
            continue
        nxt = clauses[i + 1] if i + 1 < len(clauses) else ""
        if len(s) < 10 and nxt and not _DATES.search(nxt):
            s = f"{s}({_PAREN.sub('', nxt)[:30]})"
        return s[:60]
    return (sig.get("title") or "")[:60]


# 动态新闻、政策、其他不自动建项目(说不清是哪个项目);采购公告、立项、融资、授标才建
CREATE_CATS = {"tender", "eoi", "prequalification", "gpn", "spn", "award", "financing", "plan"}


ACTIVE = ("tracking", "preparing", "bid", "won")  # 设了这些进展即入账;未中标、放弃不建


def should_enter(old: str | None, new: str | None) -> bool:
    """进展从没设(或未中标、放弃)改成跟踪中、准备投标、已投标、中标时,试着入账。"""
    return new in ACTIVE and old not in ACTIVE


@dataclass(frozen=True)
class Entry:
    """跟踪即入账的结果:msg 给人看的说明;created 建了项目;settled=False 是定时任务推送失败、已退回,下次再试。"""

    msg: str
    created: bool = False
    settled: bool = True


def enter_ledger(profile: Profile, store: Store, sig: dict, today: date, identity: tuple[str, ...] = sync.APP_ID,
                 by: str | None = None, keep_unpushed: bool = True) -> Entry:
    """跟踪即入账(本机界面设进展、表格里设进展共用):线索所在国有台账、线索还没有可靠(●)挂接、也没写进过台账
    (核对记录里写入过、系统自动写入过、原文已登记为来源)时,按线索新建项目,提交并推送程序仓库,
    记下核对记录与操作记录(ledger created,by 为设进展的人)。
    keep_unpushed:推送失败时提交留在本机、照常记下(本机界面,联网后推送);定时任务传 False——退回这次提交、什么都不记,
    下次拉回再建(检出用完即弃,记下了却没推上去,台账与记录就对不上了)。"""
    iso, no = sig.get("country"), sig["id"][:8]
    lp = ledger_file(profile, iso) if iso else None
    if lp is None:
        return Entry(f"{geo.name_zh(iso) if iso else '国别未识别'}还没有国别台账,暂不入账")
    dec = decisions(store.events()).get(no)
    if dec and dec.get("value") != NONE:
        return Entry(f"已并入商机 {dec['value']},不另建")
    rv = store.ledger_reviewed().get(no) or {}
    if rv.get("action") == "updated":
        return Entry(f"已在台账 {rv.get('item', '')}")
    if lp.exists():  # 没有台账文件的国家(还没有商机)直接建
        try:
            led = load_ledger(lp, load_rules(profile.rules) if profile.rules else load_rules())
        except LedgerError as e:  # 台账文件本身校验不过:先修台账(CI 也会报)
            return Entry(f"{lp.parent.name} 台账校验不通过,暂不入账:{str(e)[:200]}")
        items = {it["id"] for it in led.items}
        auto = [r["item"] for r in history(store) if str(r.get("id", ""))[:8] == no and r.get("item") in items]
        if auto:
            return Entry(f"已由系统自动写进台账 {'、'.join(dict.fromkeys(auto))},不另建")
        url = _url_key(sig.get("url"))
        src = next((k for k, s in led.sources.items() if url and isinstance(s, dict) and _url_key(s.get("url")) == url), None)
        if src:
            return Entry(f"原文已登记为台账来源 {src},不另建")
        high = [lk["item"] for lk in links_of(sig) if lk["item"] in items and lk.get("confidence") == "high"]
        if high:
            return Entry(f"涉及台账项目 {'、'.join(dict.fromkeys(high))}(挂接可靠),不另建;内容由系统自动写入")
    else:
        items = set()
    lp, res = create_from_lead(profile, sig, today)
    if lp is None:
        return Entry(f"没有自动建台账项目:{res}")
    root = sync.toplevel(lp)
    files, msg = [lp, lp.with_name("sources.yaml")], f"台账:{brief_changes(res.changes)}(线索 {no} 设了跟踪进展,系统自动建)"
    r = sync.commit_program(root, files, msg, identity) if root else None
    if r is not None and not r.ok and not keep_unpushed:
        sync.drop_commit(root, msg, files)
        return Entry(f"按线索建了项目,但提交或推送失败,已退回,下次再建:{r.output[:160]}", settled=False)
    store.mark_ledger_reviewed(sig["id"], "updated", today, by=by, event="created", item=res.item_id or "", source=res.source_id or "")
    if dec:  # 人先前说过不属于任何商机:现在设了进展、建了商机,以这次为准
        store.log_event(sig["id"], "member", res.item_id or "", by=by)
    tail = "已推送到 GitHub" if r and r.ok else ("已保存在本机" if r is None else f"已提交在本机,推送失败:{r.output[:160]}")
    name = res.changes[0].replace("新建项目 ", "") if res.changes else res.item_id
    weak = [lk["item"] for lk in links_of(sig) if lk["item"] in items and lk.get("confidence") != "high"]
    return Entry(f"已按线索在台账新建项目 {name}({tail});名称、领域是系统取的,请抽空在「商机」页核对"
                 + (f";另有名称相近的台账项目 {'、'.join(dict.fromkeys(weak))},请核对是不是同一个项目" if weak else ""), created=True)


def create_from_lead(profile: Profile, sig: dict, today: date) -> tuple[Path | None, object]:
    """按线索在它所在国的台账里新建项目(来源与核实方式标"系统自动写入"),写文件、不提交 → (台账路径, core.ledger_edit.Result 或说明)。
    国别没有台账、线索是动态新闻、领域认不出、写了通不过校验时不建,返回 (None, 说明)。"""
    lp = ledger_file(profile, sig["country"]) if sig.get("country") else None
    if lp is None:
        return None, f"{geo.name_zh(sig.get('country')) if sig.get('country') else '国别未识别'},还没有国别台账"
    x = sig.get("llm") if sig.get("llm") and not sig["llm"].get("_error") else {}
    if x.get("category") not in CREATE_CATS:
        return None, "这条是动态、政策类信息,说不清对应哪个项目,不自动建;要建商机请在「商机」页点「建成商机」核对后建"
    segs = _segments(sig)
    if not segs:
        return None, "认不出细分领域,不自动建;要建商机请在「商机」页点「建成商机」核对后建"
    rules = load_rules(profile.rules) if profile.rules else load_rules()
    f = sig.get("fields") or {}
    upd = propose(sig, None, today)
    upd.new_item.update(name_zh=_name_zh(sig), segments=segs, name_en="", name_local=(sig.get("title") or "")[:200],
                        brief=x.get("summary_zh") or "", owner={"业主": f.get("buyer") or ""},
                        location={"detail": f"执行地点 {f['place']}" if f.get("place") else ""})
    for p in upd.packages:
        p["value"] = f"预估 {f['estimate']}" if f.get("estimate") else ""
        p["note"] = f"招标编号 {f['reference']}" if f.get("reference") else ""
    upd.ids = lead_ids(sig)
    upd.source = {**(upd.source or {}), "via": "auto_extract",
                  "note": f"系统线索 {sig['id'][:8]} 设了跟踪进展,按线索自动建项目,未经人工核对({today.isoformat()})"}
    res = apply(lp, upd, today, rules, write=True)
    return (lp, res) if not res.errors else (None, ";".join(res.errors)[:300])
