"""正式商机的台账出处补成线索(core.ledger_leads):不联网、不调模型,按出处登记补;只补正式商机的,补过的、线索库里已有同一网址的
不补;算相关、标已处理、入库时间取商机的核实日期;只被一个台账项目引用的直接并入,几个项目引用、只有一个正式商机的并入它,
几个正式商机都引用的交待确认。"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from core import ledger_leads
from core import opportunity as op
from core.profile import load_profile
from core.watch.events import set_stage, timeline
from core.watch.query import load_rows
from core.watch.store import Store

FX = Path(__file__).parent / "fixtures"
TODAY = date(2026, 10, 1)


def _prof():
    return load_profile(FX / "profile.yaml")  # ZZZ 台账:ZZZ-001 引用 S01、S02;S02 另被 ZZZ-002、ZZZ-003 引用


def test_sources_of_formal_opportunities_become_their_leads(tmp_path):
    prof, st = _prof(), Store(tmp_path / "data")
    assert ledger_leads.backfill(prof, st, today=TODAY) == []  # 还没有正式商机:什么也不补
    set_stage(st, "ZZZ-001", "tracking")
    added = ledger_leads.backfill(prof, st, today=TODAY)
    assert len(added) == 2 and ledger_leads.backfill(prof, st, today=TODAY) == []  # 补过的不重复补
    rows = {r["no"]: r for r in load_rows(st, TODAY)}
    a = next(r for r in rows.values() if r["url"] == "https://example.org/project/1")
    assert (a["source_name"], a["status"], a["relevant"], a["judged_by"], a["first_seen"], a["published"]) == (
        "台账出处", "done", True, "台账", "2026-09-20", "2026-05-01")
    assert a["summary_zh"] == "台账出处(虚构银行):虚构出资方项目页" and a["sectors_zh"] == "污水" and a["links"] == "●ZZZ-001"
    book = op.book_for(prof, st)
    assert set(book.leads["ZZZ-001"]) == {x[:8] for x in added}  # 都并进了 ZZZ-001
    b = next(r for r in rows.values() if r["url"] == "https://example.org/news/2")
    assert book.members[b["no"]].how == "member"  # S02 还被 ZZZ-002、ZZZ-003(不是正式商机)引用:记一笔并入 ZZZ-001
    sig = next(s for s in st.load_signals() if s["id"][:8] == a["no"])
    assert any(x["title"] == "台账出处,写进台账时已核对(不另判定)" for x in timeline(sig, st.events(), st.triage(), {}))


def test_existing_leads_and_shared_sources(tmp_path):
    """线索库里已有同一网址的线索不补(网址末尾的斜杠不算);几个正式商机都引用的出处交待确认。"""
    prof, st = _prof(), Store(tmp_path / "data")
    st.append_signals([{"id": "c0ffee00c0ffee00", "title": "Project page", "url": "https://example.org/project/1/", "published": "2026-05-01",
                        "first_seen": "2026-09-21T08:00:00+00:00", "source": "zz", "source_name": "虚构采购网", "kind": "html_list",
                        "country": "ZZZ", "links": [], "llm": None}])
    set_stage(st, "ZZZ-001", "tracking")
    set_stage(st, "ZZZ-002", "tracking")
    added = ledger_leads.backfill(prof, st, today=TODAY)
    assert [s["url"] for s in st.load_signals() if s["id"] in added] == ["https://example.org/news/2"]
    book = op.book_for(prof, st)
    sug = next(x for x in book.suggestions if x.sig["id"] in added)
    assert sug.kind == "join" and {i for i, _ in sug.items} >= {"ZZZ-001", "ZZZ-002"}
    assert not [e for e in st.events() if e["kind"] == "member"]
