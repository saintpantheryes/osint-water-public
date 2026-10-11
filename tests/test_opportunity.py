"""商机(core.opportunity):所属线索只有一个口径——人的决定 > 写进台账的记录 > 唯一的可靠挂接;其余进待确认。
进展取商机与所属线索上最近设的一次;新国家的第一个商机连台账文件一起建;profile 按台账目录认出新国家。"""
import shutil
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from core import opportunity as op
from core.ledger import load_ledger, load_rules
from core.ledger_auto import enter_ledger
from core.profile import load_profile
from core.watch.events import event_title, set_member, set_stage
from core.watch.store import Store

FX = Path(__file__).parent / "fixtures"
RULES = load_rules(FX / "rules.yaml")
TODAY = date(2026, 9, 26)
ITEMS = {"ZZZ-001", "ZZZ-002", "ZZZ-003"}


def _sig(no: str, links=(), relevant=True, new=False, url=None, country="ZZZ", cat="tender", first="2026-09-21") -> dict:
    return {"id": no * 2, "title": f"Tender {no}", "url": url or f"https://example.org/{no}", "published": first,
            "first_seen": f"{first}T08:00:00+00:00", "source": "zz", "source_name": "虚构采购网", "kind": "html_list", "country": country,
            "links": [{"item": i, "confidence": c, "by": b} for i, c, b in links],
            "llm": {"relevant": relevant, "category": cat, "sectors": ["wastewater"], "summary_zh": f"摘要 {no}", "deadlines": [],
                    "ledger_links": [], "new_opportunity": new}}


def test_membership_order_and_suggestions(tmp_path):
    st = Store(tmp_path / "data")
    sigs = [
        _sig("aaaaaaa1", links=[("ZZZ-001", "high", "rule")]),                      # 编号吻合,只一个 → 自动并入
        _sig("aaaaaaa2", links=[("ZZZ-001", "high", "rule"), ("ZZZ-002", "high", "config")]),  # 可靠挂接两个 → 待确认
        _sig("aaaaaaa3", links=[("ZZZ-003", "medium", "rule")]),                    # 名称相近 → 待确认
        _sig("aaaaaaa4", links=[("ZZZ-002", "medium", "llm")]),                     # 人并入 ZZZ-003(以人为准)
        _sig("aaaaaaa5", links=[("ZZZ-001", "high", "rule")]),                      # 人说不属于任何商机
        _sig("aaaaaaa6", new=True),                                                  # 可能的新商机
        _sig("aaaaaaa7", new=True),                                                  # 已处理的:不再列为新商机
        _sig("aaaaaaa8", links=[("ZZZ-001", "high", "rule")], relevant=False),       # 模型判为不相关:不自动并入
        _sig("aaaaaaa9", links=[("ZZZ-002", "high", "rule")]),                      # 写进过台账 ZZZ-003 → 以写进的为准
        _sig("aaaaaab1", url="https://example.org/cited"),                           # 原文已是 ZZZ-002 的来源
        _sig("aaaaaab2", links=[("ZZZ-001", "high", "rule")]),                      # 被忽略:不自动并入
    ]
    set_member(st, "aaaaaaa4" * 2, "ZZZ-003", by="部门甲-05")
    set_member(st, "aaaaaaa5" * 2, op.NONE)
    reviewed = {"aaaaaaa9": {"action": "updated", "item": "ZZZ-003", "at": "2026-09-25"}}
    triage = {"aaaaaaa7": {"status": "done"}, "aaaaaab2": {"status": "ignore"}}
    cited = {"https://example.org/cited": {"ZZZ-002"}}
    members = op.membership(sigs, ITEMS, st.events(), reviewed, [], triage, cited)
    assert {k: (m.item, m.how) for k, m in members.items()} == {
        "aaaaaaa1": ("ZZZ-001", "link"), "aaaaaaa4": ("ZZZ-003", "member"), "aaaaaaa9": ("ZZZ-003", "ledger"),
        "aaaaaab1": ("ZZZ-002", "ledger")}
    assert members["aaaaaaa4"].by == "部门甲-05"
    sug = {x.sig["id"][:8]: (x.kind, x.items) for x in op.suggestions(sigs, ITEMS, st.events(), reviewed, [], triage, cited)}
    assert sug == {"aaaaaaa2": ("join", (("ZZZ-001", "●编号吻合或模型确认"), ("ZZZ-002", "●编号吻合或模型确认"))),
                   "aaaaaaa3": ("join", (("ZZZ-003", "○名称相近"),)), "aaaaaaa6": ("new", ())}
    # 清除决定:回到系统判断(aaaaaaa5 只可靠挂上 ZZZ-001 → 自动并入)
    set_member(st, "aaaaaaa5" * 2, "")
    assert op.membership(sigs, ITEMS, st.events(), reviewed, [], triage, cited)["aaaaaaa5"].item == "ZZZ-001"
    assert [event_title(e) for e in st.events()] == ["并入商机 ZZZ-003", "不属于任何商机", "清除并入决定(回到系统判断)"]


def test_auto_written_leads_and_reverts(tmp_path):
    st = Store(tmp_path / "data")
    sigs = [_sig("bbbbbbb1"), _sig("bbbbbbb2"), _sig("bbbbbbb3")]
    auto = [{"id": "bbbbbbb1" * 2, "item": "ZZZ-002", "at": "2026-09-22"},
            {"id": "bbbbbbb2" * 2, "item": "ZZZ-001"}, {"id": "bbbbbbb2" * 2, "item": "ZZZ-002"},  # 自动写进了两个 → 交人决定
            {"id": "bbbbbbb3" * 2, "item": "ZZZ-003"}]
    st.log_event("bbbbbbb3" * 2, "ledger", "reverted", item="ZZZ-003")  # 撤销了的不算
    members = op.membership(sigs, ITEMS, st.events(), {}, auto, {})
    assert {k: (m.item, m.how, m.by) for k, m in members.items()} == {"bbbbbbb1": ("ZZZ-002", "auto", "system")}
    assert [(x.sig["id"][:8], x.items) for x in op.suggestions(sigs, ITEMS, st.events(), {}, auto, {})] == [
        ("bbbbbbb2", (("ZZZ-001", "●写进过这几个商机的台账"), ("ZZZ-002", "●写进过这几个商机的台账")))]


def test_stage_is_the_latest_on_the_project_or_its_leads(tmp_path):
    st = Store(tmp_path / "data")
    set_stage(st, "ccccccc1" * 2, "tracking", at="2026-09-20T08:00:00+00:00")   # 表格里设在线索上(过渡期)
    assert op.stage_of("ZZZ-001", ["ccccccc1"], st.events())["value"] == "tracking"
    set_stage(st, "ZZZ-001", "bid", at="2026-09-22T08:00:00+00:00")              # 商机页设在商机上
    cur = op.stage_of("ZZZ-001", ["ccccccc1"], st.events())
    assert (cur["value"], cur["id"]) == ("bid", "ZZZ-001")
    set_stage(st, "ccccccc1" * 2, "won", at="2026-09-25T08:00:00+00:00")         # 后来又在线索上改:以最近的为准
    assert op.stage_of("ZZZ-001", ["ccccccc1"], st.events())["value"] == "won"
    assert op.stage_of("ZZZ-002", [], st.events()) is None
    evs = op.timeline_events("ZZZ-001", ["ccccccc1"], st.events())
    assert [e["value"] for e in evs] == ["tracking", "bid", "won"]


def test_build_book_groups_leads_newest_first(tmp_path):
    led = load_ledger(FX / "ZZZ" / "ledger.yaml", RULES)
    sigs = [_sig("ddddddd1", links=[("ZZZ-001", "high", "rule")], first="2026-09-10"),
            _sig("ddddddd2", links=[("ZZZ-001", "high", "rule")], first="2026-09-20"),
            _sig("ddddddd3", new=True)]
    book = op.build(sigs, {"ZZZ": led}, [], {}, [], {})
    assert book.leads == {"ZZZ-001": ["ddddddd2", "ddddddd1"]} and [x.kind for x in book.suggestions] == ["new"]
    assert set(book.items) == {it["id"] for it in led.items} and book.stage("ZZZ-001", []) is None


def test_first_opportunity_in_a_new_country_creates_its_ledger(tmp_path, monkeypatch):
    """跟踪即入账:所在国还没有台账时,连台账文件一起建(KEN-001);人先前说过不属于任何商机的,以这次设进展为准并入;
    已并入别的商机的不另建。"""
    monkeypatch.delenv("OPERATOR", raising=False)
    shutil.copytree(FX / "ZZZ", tmp_path / "countries" / "ZZZ")
    prof = SimpleNamespace(rules=str(FX / "rules.yaml"), ledger_dir=tmp_path / "countries",
                           countries=[SimpleNamespace(iso3="ZZZ", ledger=str(tmp_path / "countries" / "ZZZ" / "ledger.yaml"))])
    st = Store(tmp_path / "data")
    sig = {**_sig("eeeeeee1", country="KEN"), "fields": {"buyer": "Nairobi Water"}}
    set_member(st, sig["id"], op.NONE)
    e = enter_ledger(prof, st, sig, TODAY, by="teable:张三")
    lp = tmp_path / "countries" / "KEN" / "ledger.yaml"
    assert e.created and lp.exists() and (lp.parent / "sources.yaml").exists(), e.msg
    led = load_ledger(lp, RULES)
    assert led.country == "KEN" and [it["id"] for it in led.items] == ["KEN-001"] and list(led.sources) == ["S501"]
    assert yaml.safe_load(lp.read_text(encoding="utf-8"))["title"] == "肯尼亚水务项目机会台账"
    assert [(x["kind"], x["value"]) for x in st.events()][-2:] == [("ledger", "created"), ("member", "KEN-001")]
    assert op.decisions(st.events())[sig["id"][:8]]["value"] == "KEN-001"
    other = _sig("eeeeeee2", country="KEN")
    set_member(st, other["id"], "KEN-001")
    assert enter_ledger(prof, st, other, TODAY).msg == "已并入商机 KEN-001,不另建"


def test_profile_picks_up_new_country_ledgers(tmp_path):
    shutil.copytree(FX / "ZZZ", tmp_path / "countries" / "ZZZ")
    (tmp_path / "countries" / "KEN").mkdir()
    (tmp_path / "countries" / "KEN" / "ledger.yaml").write_text("schema: osint-water/ledger@1\ncountry: KEN\nopportunities:\n",
                                                               encoding="utf-8")
    (tmp_path / "countries" / "notes").mkdir()  # 不是国别码的目录不算
    (tmp_path / "countries" / "notes" / "ledger.yaml").write_text("x: 1\n", encoding="utf-8")
    (tmp_path / "prof").mkdir()
    text = (FX / "profile.yaml").read_text(encoding="utf-8").replace("ledger: ZZZ/ledger.yaml", "ledger: ../countries/ZZZ/ledger.yaml")
    (tmp_path / "prof" / "profile.yaml").write_text(text.replace("overlay: overlay.yaml", "overlay: overlay/ZZZ.yaml"), encoding="utf-8")
    p = load_profile(tmp_path / "prof" / "profile.yaml")
    assert p.iso3s == ("ZZZ", "KEN") and p.ledger_dir == (tmp_path / "countries").resolve()
    ken = p.country("KEN")
    assert (ken.tier, ken.name_zh, ken.ledger, ken.overlay) == ("market", "肯尼亚", (tmp_path / "countries/KEN/ledger.yaml").resolve(),
                                                               (tmp_path / "prof/overlay/KEN.yaml").resolve())
    assert p.ledger_file("JOR") == (tmp_path / "countries" / "JOR" / "ledger.yaml").resolve() and p.ledger_file("ZZZ").name == "ledger.yaml"


def test_tracked_follow_ups_judgments_and_calendar(tmp_path):
    """在跟踪的商机 = 设了进展的(含已结束);跟进记在商机上,带判断与依据,最近一次带判断的为当前判断;
    日历排出公开时间节点(台账节点、采购标段、所属线索的截止日)与工作留痕(跟进、进展、记录、下一步到期),外加上报的报告;
    清除进展即不再跟踪,所属线索上以前设的进展一并清除。"""
    from core.watch.events import add_follow, add_note, set_next
    st = Store(tmp_path / "data")
    led = load_ledger(FX / "ZZZ" / "ledger.yaml", RULES)
    lead = {**_sig("fffffff1", links=[("ZZZ-001", "high", "rule")]), "id": "fffffff1" * 2}
    lead["llm"]["deadlines"] = [{"type": "bid_deadline", "date": "2026-10-20", "evidence": "…"}]
    set_stage(st, "ZZZ-001", "tracking", at="2026-09-20T08:00:00+00:00")
    set_stage(st, "ZZZ-002", "dropped", at="2026-09-21T08:00:00+00:00")  # 已结束的也算(在跟踪过)
    set_stage(st, lead["id"], "preparing", at="2026-09-22T08:00:00+00:00")  # 过渡期:设在所属线索上
    add_follow(st, "ZZZ-001", "约见业主", "continue", "", "2026-09-23", at="2026-09-23T09:00:00+00:00")
    add_follow(st, "ZZZ-001", "业主说 11 月发标", "focus", "资金已到位", "2026-09-25", at="2026-09-25T09:00:00+00:00")
    add_note(st, "ZZZ-001", "已购买招标文件", at="2026-09-26T09:00:00+00:00")
    set_next(st, "ZZZ-001", "递交资格预审文件", "2026-10-10")
    book = op.build([lead], {"ZZZ": led}, st.events(), {}, [], {})
    tracked = book.tracked(st.events())
    assert {k: v["value"] for k, v in tracked.items()} == {"ZZZ-001": "preparing", "ZZZ-002": "dropped"}
    assert op.judgment_text(op.judgments(st.events())["ZZZ-001"]) == "重点推进(2026-09-25):资金已到位"
    assert [e["note"] for e in op.follows("ZZZ-001", st.events())] == ["约见业主", "业主说 11 月发标"]
    assert event_title(op.follows("ZZZ-001", st.events())[-1]) == "跟进(2026-09-25) · 判断:重点推进"
    rep = SimpleNamespace(when="2026-09-28", kind="国别报告", name="国别报告_测试国_2026-09-28", countries=["ZZZ"])
    cal = op.calendar(book, st.events(), {"fffffff1": lead}, "2026-09-26", ["ZZZ-001"], [rep])
    got = {(e.date, e.kind, e.text, e.exact) for e in cal}
    assert ("2026-11-15", "node", "资格预审截止(官方计划)", True) in got and ("2026-04-01", "node", "总采购公告(GPN)(已发生)", True) in got
    assert ("2026-12-01", "package", "截止·设计施工:污水厂设计施工", False) in got  # 只知道月份:日期未定(没写截止类型)
    assert ("2026-10-20", "deadline", "投标截止·摘要 fffffff1", True) in got  # 所属线索的截止日
    assert {("2026-09-23", "follow", "判断:继续跟进·约见业主"), ("2026-09-25", "follow", "判断:重点推进·业主说 11 月发标"),
            ("2026-09-26", "note", "已购买招标文件"), ("2026-10-10", "next", "下一步:递交资格预审文件"),
            ("2026-09-28", "report", "国别报告:国别报告_测试国_2026-09-28")} <= {(e.date, e.kind, e.text) for e in cal}
    assert ("2026-09-22", "stage", "进展:准备投标") in {(e.date, e.kind, e.text) for e in cal}
    assert all(e.item == "ZZZ-001" for e in cal if e.kind != "report") and [e.date for e in cal] == sorted(e.date for e in cal)
    md = op.progress_md(book, st.events(), "ZZZ", "2026-09-21", "2026-09-27", {"ZZZ-001": "资格预审截止 2026-11-15"})
    assert "| ZZZ-001 甲市污水处理厂 | 准备投标 | 重点推进(2026-09-25):资金已到位 |" in md
    assert "09-25 跟进:业主说 11 月发标(判断:重点推进)" in md and "递交资格预审文件(到期 2026-10-10)" in md
    assert "| ZZZ-002 乙市灌溉渠 | 放弃 |" in md  # 本期把进展改为放弃的也列出
    assert op.progress_md(book, st.events(), "KEN", "2026-09-21", "2026-09-27").startswith("没有在跟踪的商机")
    op.set_progress(st, "ZZZ-001", [lead["id"]], None, by="部门甲-05")  # 不再跟踪:线索上的进展一并清除
    assert "ZZZ-001" not in book.tracked(st.events()) and [(e["id"], e["value"]) for e in st.events()[-2:]] == [
        ("ZZZ-001", ""), (lead["id"], "")]


def test_work_log_is_one_list_with_the_follow_table(tmp_path):
    """商机的工作留痕(= 跟进记录里这个商机的行):商机上与所属线索上的跟进、人记的一笔、带说明的进展;系统代记的(auto、system)
    与撤回的不算;所属线索上的跟进也算进商机的当前判断;撤回的跟进不进日历与周报。"""
    from core.watch.events import add_follow, add_note
    st = Store(tmp_path / "data")
    led = load_ledger(FX / "ZZZ" / "ledger.yaml", RULES)
    lead = {**_sig("fffffff1", links=[("ZZZ-001", "high", "rule")]), "id": "fffffff1" * 2}
    set_stage(st, "ZZZ-001", "tracking", "发给伙伴打听进展", by="teable:admin", at="2026-09-30T15:05:31+00:00")
    set_stage(st, lead["id"], "tracking", "在询问有无适格的伙伴", by="teable:admin", at="2026-09-30T08:07:16+00:00")
    add_follow(st, lead["id"], "线索上的跟进", "watch", day="2026-09-30")
    set_stage(st, "ZZZ-001", "preparing", "在线索 fffffff1 上改的", auto=True)  # 系统代记的说明
    add_note(st, lead["id"], "表格里的备注改为:x", auto=True)
    add_note(st, "ZZZ-001", "跟踪即入账:已建", by="system")
    wrong = add_follow(st, "ZZZ-001", "记错了", "drop", day="2026-09-30")
    op.retract(st, wrong, "表格里删掉了这一行", by="teable:表格")
    book = op.build([lead], {"ZZZ": led}, st.events(), {}, [], {})
    log = op.work_log("ZZZ-001", st.events(), book.leads["ZZZ-001"])
    assert [e["note"] for e in log] == ["发给伙伴打听进展", "在询问有无适格的伙伴", "线索上的跟进"]
    assert [e["note"] for e in op.work_log("ZZZ-001", st.events())] == ["发给伙伴打听进展"]  # 不给所属线索:只算商机上的
    assert op.judgments(st.events(), book.leads)["ZZZ-001"]["note"] == "线索上的跟进"  # 撤回的「放弃」不算
    assert op.entry_key(wrong) == op.entry_key(dict(wrong)) and op.entry_key(wrong) != op.entry_key(log[0])
    assert event_title(st.events()[-1]) == "撤回一条跟进"
    cal = {(e.kind, e.text) for e in op.calendar(book, st.events(), {"fffffff1": lead}, "2026-10-01", ["ZZZ-001"])}
    assert ("stage", "进展:准备投标") in cal and ("follow", "判断:放弃·记错了") not in cal  # 代记的说明不写进日历;撤回的不排
    assert not any("表格里的备注" in t or "跟踪即入账" in t for _, t in cal)
    row = next(x for x in op.progress_md(book, st.events(), "ZZZ", "2026-09-30", "2026-09-30").splitlines() if x.startswith("| ZZZ-001"))
    assert "记错了" not in row and "观望(2026-09-30)" in row and "线索上的跟进" in row


def test_ai_recommended_items_listed_or_declined(tmp_path):
    """台账里还没设进展的项目是 AI 推荐商机:不列入记 kind listing(value no),撤回为空串,以最后一次为准。"""
    st = Store(tmp_path / "data")
    op.decline(st, "ZZZ-002", "不在我们的领域")
    op.decline(st, "ZZZ-003")
    op.undecline(st, "ZZZ-003")
    no = op.declined(st.events())
    assert list(no) == ["ZZZ-002"] and no["ZZZ-002"]["note"] == "不在我们的领域"


def test_follow_up_needs_content_or_a_known_judgment(tmp_path):
    from core.watch.events import add_follow
    st = Store(tmp_path / "data")
    with pytest.raises(ValueError):
        add_follow(st, "ZZZ-001", "  ")
    with pytest.raises(ValueError):
        add_follow(st, "ZZZ-001", "x", "maybe")
    e = add_follow(st, "ZZZ-001", "", "watch", "等资金落实")
    assert (e["kind"], e["value"], e["reason"]) == ("follow", "watch", "等资金落实") and "note" not in e


def test_user_submitted_links_are_always_relevant():
    """使用方的流程图:用户自己上传的链接不再需要判定——人工提交的一律算相关(模型只写摘要、定国别与领域)。"""
    from core.watch.outputs import relevant
    sig = {"source": "manual", "llm": {"relevant": False}, "rules": {}}
    assert relevant(sig) and not relevant({**sig, "source": "zz"})
