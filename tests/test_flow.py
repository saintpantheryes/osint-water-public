"""首页全过程视图(core/watch/flow.py):线索走过的节点、桑基图连线(选中的单独一条)、报告引用的线索、国别统计、筛选。"""
from __future__ import annotations

from collections import Counter

import pytest

from core.markets import ALL
from core.watch import flow


def _row(no: str, src: str = "世行采购公告", relevant: bool = True, status: str = "new", stage: str = "", links: str = "",
         country: str = "AZE") -> dict:
    return {"no": no, "source_name": src, "relevant": relevant, "status": status, "stage": stage, "links": links, "country": country}


def test_kind_and_date_from_file_name():
    assert flow.kind_of("AZE_周报_2026-W40") == "周报" and flow.when("AZE_周报_2026-W40") == "2026-W40"
    assert flow.kind_of("国别报告_阿塞拜疆_2026-09-26") == "国别报告" and flow.when("国别报告_阿塞拜疆_2026-09-26") == "2026-09-26"
    assert flow.kind_of("AZE_项目机会分析_2026-09-24") == "月度台账分析"
    assert flow.kind_of("项目报告_AZE-001_甲城_2026-09-29") == "项目报告"
    assert flow.kind_of("随便什么") == "报告" and flow.when("随便什么") == ""
    assert str(flow.when_date("2026-W39")) == "2026-09-27" < "2026-09-28" and flow.when_date("") is None


def test_path_through_the_four_stages():
    """线索 → 判定 → 商机 → 报告:第三列看线索是否属于在跟踪的商机(opp:所属商机的进展),不看处理状态、挂接。"""
    cited = {"a0000001"}
    assert flow.path(_row("a0000001"), "S", cited, "tracking") == ["S", flow.RELEVANT, flow.OPP, flow.IN_REPORT]
    assert flow.path(_row("a0000002"), "S", cited) == ["S", flow.RELEVANT, flow.PENDING]
    assert flow.path(_row("a0000003", status="done"), "S", cited)[2] == flow.DONE  # 看过了、没成商机
    assert flow.path(_row("a0000004", links="●AZE-001"), "S", cited)[2] == flow.PENDING  # 挂上台账但商机没在跟踪:不算
    assert flow.path(_row("a0000005", stage="tracking"), "S", cited)[2] == flow.PENDING  # 线索上设了进展、还没成商机
    assert flow.path(_row("a0000006", relevant=False), "S", cited) == ["S", flow.IRRELEVANT]
    assert flow.path(_row("a0000007", status="ignore"), "S", cited)[2] == flow.DROPPED
    assert flow.path(_row("a0000008", stage="dropped"), "S", cited)[2] == flow.DROPPED
    assert flow.path(_row("a0000009"), "S", cited, "dropped")[2] == flow.DROPPED  # 商机放弃了
    assert flow.path(_row("a000000a", status="ignore"), "S", cited, "won")[2] == flow.OPP  # 属于商机的,以商机为准
    assert flow.path(_row("a000000b", relevant=False), "S", cited, "bid") == ["S", flow.IRRELEVANT, flow.OPP]  # 人工并入的


def test_small_sources_are_merged():
    rows = [_row(f"{i:08x}", src=f"信源{i % 10}") for i in range(40)] + [_row("ffffffff", src="大信源")] * 20
    nodes = flow.source_nodes(rows)
    assert nodes["大信源"] == "大信源" and sum(v == flow.OTHER for v in nodes.values()) == 3  # 11 个信源:前 8 个各一个节点
    few = [_row(f"{i:08x}", src=f"信源{i}") for i in range(9)]
    assert flow.OTHER not in flow.source_nodes(few).values()  # 只多一个时不并


def test_sankey_splits_selected_leads_and_keeps_totals():
    rows = [_row("a0000001"), _row("a0000002"), _row("a0000003"),
            _row("a0000004", relevant=False, src="联合国开发署", country="MAR")]
    cited = {"a0000001", "a0000003"}
    opps = {"a0000001": "tracking", "a0000002": "bid"}  # 属于在跟踪的商机
    plain = flow.sankey(rows, cited, opps=opps)
    assert not any(lk["selected"] for lk in plain["links"])
    sel = flow.sankey(rows, cited, {"a0000001"}, opps)
    # 每一段:选中的一条 + 其余一条,合计不变
    total = Counter()
    for lk in sel["links"]:
        total[(lk["source"], lk["target"])] += lk["value"]
    assert total == Counter({(lk["source"], lk["target"]): lk["value"] for lk in plain["links"]})
    on = {(lk["source"], lk["target"]): lk["value"] for lk in sel["links"] if lk["selected"]}
    assert on == {("世行采购公告", flow.RELEVANT): 1, (flow.RELEVANT, flow.OPP): 1, (flow.OPP, flow.IN_REPORT): 1}
    nodes = {n["name"]: n for n in sel["nodes"]}
    assert nodes[flow.OPP]["value"] == 2 and nodes[flow.OPP]["selected"] == 1
    assert [n["depth"] for n in sel["nodes"]] == sorted(n["depth"] for n in sel["nodes"])  # 按列排
    assert nodes[flow.IN_REPORT]["value"] == 2 and nodes[flow.PENDING]["value"] == 1 and nodes[flow.IRRELEVANT]["value"] == 1
    assert flow.through(rows, cited, flow.IN_REPORT, opps) == {"a0000001", "a0000003"}
    assert flow.stats(rows, cited, opps) == {"sources": 2, "leads": 4, "relevant": 3, "opps": 2, "reported": 2}
    members = {"a0000001": type("M", (), {"item": "AZE-001"})(), "a0000003": type("M", (), {"item": "AZE-009"})()}
    assert flow.opp_stages(members, {"AZE-001": {"value": "bid"}}) == {"a0000001": "bid"}  # AZE-009 没在跟踪


def test_countries_count_and_selected():
    rows = [_row("a0000001"), _row("a0000002"), _row("a0000003", country="MAR"), _row("a0000004", country="")]
    out = flow.countries(rows, {"a0000002", "a0000003"})
    assert [(c["iso3"], c["value"], c["selected"]) for c in out] == [("AZE", 2, 1), ("MAR", 1, 1)]
    assert out[0]["name"] == "阿塞拜疆"


def test_reports_and_the_leads_they_cite(tmp_path):
    root = tmp_path / "reports"
    (root / "weekly").mkdir(parents=True)
    (root / "monthly").mkdir()
    (root / "weekly" / "AZE_周报_2026-W40.md").write_text(
        "| 发布 | 摘要 |\n|---|---|\n| 2026-09-24 | 招标公告 [a0000001] |\n另见 [a0000002]、[deadbeef] 与台账 [AZE-006]。\n", encoding="utf-8")
    (root / "weekly" / "AZE_周报_2026-W40.docx").write_bytes(b"docx")
    (root / "monthly" / "国别报告_阿塞拜疆_2026-09-26.docx").write_bytes(b"docx")  # 没有 Markdown:读不到引用
    reps = flow.load_reports(root, {"a0000001", "a0000002", "a0000003"})
    assert [r.name for r in reps] == ["AZE_周报_2026-W40", "国别报告_阿塞拜疆_2026-09-26"]  # 新的在前:第 40 周到 10-04
    wk = reps[0]
    assert (wk.kind, wk.when, wk.where, wk.key) == ("周报", "2026-W40", "每周自动", "weekly/AZE_周报_2026-W40")
    assert wk.leads == ["a0000001", "a0000002"] and wk.items == ["AZE-006"]  # 库里没有的 deadbeef 不算
    assert wk.docx and wk.md and wk.countries == ["AZE"]
    assert reps[1].countries == ["AZE"] and reps[1].md is None and reps[1].leads == []
    assert flow.load_reports(tmp_path / "none", set()) == []
    # 上报领导的报告:国别报告(每月自动生成的与自建的都算)、项目报告;周报、月度台账分析、线索简报(自动的、自建的)不算
    (root / "custom").mkdir()
    for name in ("国别报告_阿塞拜疆_2026-10-02", "项目报告_AZE-001_甲城_2026-10-02", "线索简报_2026-10-02"):
        (root / "custom" / f"{name}.docx").write_bytes(b"docx")
    for name in ("AZE_项目机会分析_2026-10-01", "线索简报_2026-10-01"):
        (root / "monthly" / f"{name}.docx").write_bytes(b"docx")
    assert sorted(r.name for r in flow.load_reports(root, set()) if flow.formal(r)) == [
        "国别报告_阿塞拜疆_2026-09-26", "国别报告_阿塞拜疆_2026-10-02", "项目报告_AZE-001_甲城_2026-10-02"]
    assert flow.is_formal("国别报告_摩洛哥_2026-10-01") and not flow.is_formal("MAR_项目机会分析_2026-10-01_公开版")


def test_a_report_covers_the_leads_of_the_opportunities_it_writes_up():
    """报告写进的线索:引用了线索号的(在前),加上所属正式商机写进了报告的——报告日期不早于线索入库;认不出报告日期的不比。"""
    rep = flow.Report(key="monthly/国别报告_阿塞拜疆_2026-10-01", name="国别报告_阿塞拜疆_2026-10-01", kind="国别报告",
                      when="2026-10-01", where="每月自动", leads=["a0000002", "a0000001"], items=["AZE-001", "AZE-009"])
    items_of = {"a0000001": "AZE-001", "b0000001": "AZE-001", "b0000002": "AZE-001", "c0000001": "AZE-002"}
    seen = {"a0000001": "2026-09-20", "b0000001": "2026-09-24", "b0000002": "2026-10-02T08:00:00+00:00", "c0000001": "2026-09-01"}
    assert flow.covered(rep, items_of, seen) == ["a0000002", "a0000001", "b0000001"]  # b0000002 报告以后才入库;AZE-002 没写进报告
    assert flow.covered(rep, {}, seen) == ["a0000002", "a0000001"]  # 没有正式商机:只算引用了线索号的
    undated = flow.Report(key="custom/项目报告_AZE-001", name="项目报告_AZE-001", kind="项目报告", when="", where="自建", items=["AZE-001"])
    assert flow.covered(undated, items_of, seen) == ["a0000001", "b0000001", "b0000002"]
    ps = flow.paths([{"no": "b0000001", "source_name": "台账出处", "relevant": True, "status": "done"}],
                    set(flow.covered(rep, items_of, seen)), {"b0000001": "tracking"})
    assert ps["b0000001"] == ["台账出处", flow.RELEVANT, flow.OPP, flow.IN_REPORT]


def test_funders_are_grouped():
    f = lambda text, src="x": flow.funders_of({"funder": text, "source": src})  # noqa: E731
    assert f("World Bank (IDA)") == ["世界银行"] and f("Banque mondiale / AFD") == ["世界银行", "法国开发署"]
    assert f("IDB") == ["美洲开发银行"] and f("IsDB") == ["伊斯兰开发银行"] and f("EBRD, EU") == ["欧洲复兴开发银行", "欧盟"]
    assert f("Ministère de l'Eau") == ["本国政府与业主"] and f("GEF") == ["其他出资方"]
    assert f("EUR 5 millions") == ["其他出资方"]  # 缩写要单独成词,也分大小写
    assert f("") == ["未注明"] and f("", "wb_notices") == ["世界银行"] and f("", "undp_procurement") == ["联合国开发署"]


def test_narrow_and_facets():
    rows = [{"no": "1", "first_seen": "2026-09-01", "published": "2026-08-01", "sectors": ["water_supply"], "market": "北非区域",
             "continent": "非洲", "country": "MAR", "funder": "World Bank", "source": "x"},
            {"no": "2", "first_seen": "2026-09-10", "published": "", "sectors": ["wastewater", "water_supply"], "market": "",
             "continent": "亚洲", "country": "IND", "funder": "", "source": "wb_notices"},
            {"no": "3", "first_seen": "2026-09-20", "published": "2026-09-19", "sectors": ["wastewater"], "market": "北非区域",
             "continent": "非洲", "country": "DZA", "funder": "AfDB", "source": "x"}]
    nos = lambda rs: [r["no"] for r in rs]  # noqa: E731
    assert nos(flow.narrow(rows)) == ["1", "2", "3"]
    assert nos(flow.narrow(rows, "2026-09-10")) == ["2", "3"] and nos(flow.narrow(rows, "", "2026-09-10")) == ["1", "2"]  # 含首尾
    assert nos(flow.narrow(rows, "2026-09-05", field="published")) == ["2", "3"]  # 没写发布日期的按入库日期
    assert nos(flow.narrow(rows, sectors=["water_supply"])) == ["1", "2"]  # 几个领域的,命中一个就算
    assert nos(flow.narrow(rows, sectors=["water_supply", "wastewater"], funders=["世界银行"])) == ["1", "2"]  # 类里"或",类间"且"
    assert nos(flow.narrow(rows, markets=[ALL])) == ["1", "3"] and nos(flow.narrow(rows, markets=[flow.OUTSIDE])) == ["2"]
    assert nos(flow.narrow(rows, continents=["非洲"], countries=["IND"])) == []
    fc = flow.facets(rows, sectors=["wastewater"])
    assert fc["sectors"] == Counter({"water_supply": 2, "wastewater": 2})  # 自己这一类不按自己数
    assert fc["countries"] == Counter({"IND": 1, "DZA": 1}) and fc["funders"] == Counter({"世界银行": 1, "非洲开发银行": 1})
    assert fc["markets"] == Counter({flow.OUTSIDE: 1, "北非区域": 1, ALL: 1})
    with pytest.raises(KeyError):
        flow.narrow(rows, colours=["red"])
