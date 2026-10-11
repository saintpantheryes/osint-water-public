"""正式商机的台账出处补成线索。

商机(在跟踪的,core.opportunity)在台账里引用的出处(各国 sources.yaml 里有网址的;D 级、没有网址的不补),线索库里还没有
同一网址的线索时,补成一条——信源「台账出处」,一条一个文件 watch/submitted/{id}.json(与人工提交同一处,与定时任务写的
文件分开,同步时不冲突)。这样商机页、首页、团队表格里,商机的出处都是它的线索。

- 不联网、不调模型:标题、发布机构、日期、等级取自出处登记;中文摘要写"台账出处(发布机构):标题";领域取商机的细分领域。
- 算相关(写进台账时核对过,不另判定);标为已处理(内容已在台账里,不进待处理);入库时间取引用它的正式商机的核实日期
  (整理台账时就见过),免得当成本周新线索写进周报、月报。
- 所属商机按"原文网址已是商机的台账来源"认(core.opportunity):只有一个台账项目引用的直接并入;几个项目都引用、其中只有
  一个是正式商机的,并入那个(记一笔并入,操作人 system);几个正式商机都引用的交「待确认」。
- 什么时候补:服务器上与表格同步时(core.live_sync,30 秒内)、商机页点「列入正式商机」时;也可 `python -m core.cli ledger-leads`。
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date

from core import opportunity as opp
from core.ledger import _refs, load_ledger, load_rules
from core.watch.events import set_member
from core.watch.outputs import LEDGER, SEGMENT_SECTOR
from core.watch.query import set_status
from core.watch.store import SYSTEM, Store, signal_id

SOURCE_NAME = "台账出处"
NOTE = "台账出处(内容已在台账里)"
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _key(u: object) -> str:
    return str(u or "").strip().rstrip("/")


def _day(v: object) -> str | None:
    s = str(v or "").strip()[:10]
    return s if _DAY.match(s) else None


def _checked(book: opp.Book, iid: str) -> str | None:
    """商机的核实日期(台账 verification.checked_at)。"""
    it = (book.items.get(iid) or (None, {}))[1]
    return _day((it.get("verification") or {}).get("checked_at"))


def backfill(profile, store: Store, items: Iterable[str] | None = None, today: date | None = None) -> list[str]:
    """补正式商机(items 缺省为全部;只补其中正式的)的台账出处为线索 → 新补的线索 id。已有同一网址线索的不补,补过的不重复补。"""
    rules = load_rules(profile.rules) if profile.rules else load_rules()
    leds = [load_ledger(c.ledger, rules) for c in profile.countries if c.ledger]
    events = store.events()
    book = opp.book_for(profile, store)
    formal = set(book.tracked(events))
    want = formal if items is None else formal & set(items)
    if not want:
        return []
    cited: dict[str, list[str]] = {}  # 网址 → 引用它的台账项目(全部台账)
    for led in leds:
        for it in led.items:
            for ref in set(_refs(it)):
                if u := _key((led.sources.get(ref) or {}).get("url")):
                    cited.setdefault(u, []).append(it["id"])
    have = {_key(s.get("url")) for s in store.load_signals() if s.get("url")}
    made: list[tuple[str, list[str], list[str]]] = []
    for led in leds:
        for it in led.items:
            if it["id"] not in want:
                continue
            for ref in sorted(set(_refs(it))):
                sv = led.sources.get(ref) or {}
                u = _key(sv.get("url"))
                if not u or u in have or sv.get("grade") == "D" or "内部" in str(sv.get("publisher") or ""):
                    continue
                sid = signal_id(LEDGER, u)
                have.add(u)
                if store.submitted(sid):
                    continue
                citers = sorted(set(cited.get(u, [it["id"]])))
                seen = sorted(d for x in citers if x in formal and (d := _checked(book, x)))
                first = f"{seen[0]}T00:00:00+00:00" if seen else f"{(today or date.today()).isoformat()}T00:00:00+00:00"
                pub, title = str(sv.get("publisher") or "").strip(), str(sv.get("title") or ref).strip()[:300]
                secs = list(dict.fromkeys(SEGMENT_SECTOR.get(s, "other") for s in it.get("segments") or [])) or ["other"]
                store.put_submitted({
                    "id": sid, "country": led.country, "scope": LEDGER, "source": LEDGER, "source_name": SOURCE_NAME,
                    "kind": "ledger_source", "key": u, "url": str(sv["url"]).strip(), "title": title, "published": _day(sv.get("date")),
                    "first_seen": first, "change": "new", "lang": None,
                    "fields": {"ledger": f"{led.country} {ref}", "publisher": pub, "date": str(sv.get("date") or ""),
                               "grade": str(sv.get("grade") or ""), "items": citers},
                    "rules": {"candidate": True, "sectors": secs, "stage": "project_news"}, "docs": [], "llm": None,
                    "links": [{"item": x, "confidence": "high", "by": "ledger"} for x in citers],
                    "excerpt": " · ".join(x for x in (pub, str(sv.get("date") or ""), f"等级 {sv['grade']}" if sv.get("grade") else "") if x),
                    "summary_zh": f"台账出处({pub or '—'}):{title}",
                })
                made.append((sid, citers, [x for x in citers if x in formal]))
    if made:
        set_status(store, [sid for sid, _, _ in made], "done", NOTE, by=SYSTEM)
        for sid, citers, formal_citers in made:
            if len(citers) > 1 and len(formal_citers) == 1:  # 几个台账项目都引用,只有一个是正式商机:并入它
                set_member(store, sid, formal_citers[0], "台账出处:几个台账项目都引用,只有它是正式商机", by=SYSTEM)
    return [sid for sid, _, _ in made]
