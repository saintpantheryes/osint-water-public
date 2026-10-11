"""线索的操作记录与跟踪进展:日志读写、处理状态与台账核对自动记日志、进展设置与当前进展、只记一笔、时间线排序、
旧记录兼容;监测页日历的取数。"""
from datetime import date, datetime, timedelta, timezone

import pytest

from core.watch.events import (
    add_note, by_zh, current_next, current_owners, current_stages, event_detail, event_title, latest, latest_events, log_submit, set_next,
    set_owner, set_stage, timeline,
)
from core.watch.query import calendar_items, load_rows, row, set_status
from core.watch.store import SYSTEM, Store

SIG = {"id": "abcd1234ef567890", "first_seen": "2026-09-20T01:00:00+00:00", "published": "2026-09-18",
       "source": "ma_pmmp", "source_name": "摩洛哥公共采购门户", "title": "STEP Taza", "key": "atexo:g3h:1024267",
       "url": "https://www.marchespublics.gov.ma/index.php?page=entreprise.EntrepriseDetailConsultation&refConsultation=1024267",
       "rules": {"water_terms": ["boues"]}, "links": [{"item": "MAR-001", "confidence": "high", "by": "rule", "reason": "编号"}],
       "fields": {"deadline": "2026-10-20"},
       "llm": {"relevant": True, "category": "tender", "sectors": ["wastewater"], "summary_zh": "Taza 污水处理厂招标",
               "deadlines": [{"type": "bid_deadline", "date": "2026-10-20"}], "judged_at": "2026-09-20T01:05:00+00:00"}}


def test_event_log_roundtrip_and_auto_logging(tmp_path):
    st = Store(tmp_path)
    set_status(st, [SIG["id"]], "done", "已看")  # 处理状态自动记日志
    st.mark_ledger_reviewed(SIG["id"], "updated", item="MAR-001", source="S501")  # 台账核对也是
    set_stage(st, SIG["id"], "tracking", "开始跟踪")
    with (st.root / "events.jsonl").open("a", encoding="utf-8") as f:
        f.write("<<<<<<< 同步合并留下的坏行\n")
    set_stage(st, SIG["id"], "bid")
    ev = st.events()
    assert [(e["kind"], e["value"]) for e in ev] == [("status", "done"), ("ledger", "updated"), ("stage", "tracking"), ("stage", "bid")]
    assert ev[0]["note"] == "已看" and ev[1]["item"] == "MAR-001" and datetime.fromisoformat(ev[0]["at"]).tzinfo is not None
    assert current_stages(ev)[SIG["id"][:8]]["value"] == "bid"
    set_stage(st, SIG["id"], None)  # 清除进展
    assert SIG["id"][:8] not in current_stages(st.events())
    with pytest.raises(ValueError):
        set_stage(st, SIG["id"], "winning")


def test_timeline_order_and_legacy_records():
    now = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
    events = [{"id": SIG["id"], "at": "2026-09-21T09:00:00+08:00", "kind": "stage", "value": "tracking", "note": "开始跟踪"},
              {"id": SIG["id"][:8], "at": "2026-09-25T10:00:00+08:00", "kind": "stage", "value": "bid"},
              {"id": "zzzz9999", "at": "2026-09-22T10:00:00+08:00", "kind": "status", "value": "done"}]  # 别的线索
    changed = {**SIG, "id": "abcd9999ffff0000", "first_seen": "2026-09-24T02:00:00+00:00", "change": "changed"}
    tl = timeline(SIG, events, {SIG["id"][:8]: {"status": "done", "note": "旧标记"}},
                  {SIG["id"][:8]: {"action": "skipped", "at": "2026-09-23"}}, related=[SIG, changed], now=now)
    titles = [e["title"] for e in tl]
    assert titles[:3] == ["系统采集", "提示涉及台账项目", "模型判定:相关"]  # 涉及台账项目的提示在采集时就有,模型判定稍后
    assert titles.index("进展:跟踪中") < titles.index("核对后不用更新台账") < titles.index("原公告有变化") < titles.index("进展:已投标")
    assert titles[-2] == "标为已处理(标记时间未记录)" and tl[-2]["detail"] == "旧标记"  # 记日志以前的标记:没有时间
    assert tl[-1]["kind"] == "deadline" and tl[-1]["date_only"] and tl[-1]["at"] == "2026-10-20"  # 未来的截止日在最后
    assert tl[0]["detail"] == "信源「摩洛哥公共采购门户」;公告发布于 2026-09-18"
    # 日志里已有对应操作的,旧记录不再重复列出;模型判定没记时间的按采集时间列出并注明
    old = {**SIG, "llm": {k: v for k, v in SIG["llm"].items() if k != "judged_at"}}
    tl2 = timeline(old, [{"id": SIG["id"], "at": "2026-09-26T08:00:00+08:00", "kind": "status", "value": "done"}],
                   {SIG["id"][:8]: {"status": "done"}}, {}, now=now)
    assert [e["title"] for e in tl2].count("标为已处理") == 1 and not any("未记录" in e["title"] for e in tl2)
    assert "判定时间未记录" in next(e for e in tl2 if e["kind"] == "judge")["detail"]


def test_note_and_latest_activity(tmp_path):
    st = Store(tmp_path)
    add_note(st, SIG["id"], "  已购买招标文件 ")
    with pytest.raises(ValueError):
        add_note(st, SIG["id"], "   ")
    ev = st.events()
    assert len(ev) == 1 and ev[0]["kind"] == "note" and ev[0]["note"] == "已购买招标文件" and event_title(ev[0]) == "记录"
    tl = timeline(SIG, ev, {}, {}, now=datetime(2026, 9, 27, 23, 0, tzinfo=timezone.utc))
    assert any(e["kind"] == "note" and e["title"] == "记录" and e["detail"] == "已购买招标文件" for e in tl)
    evs = [{"id": "aaaa1111", "at": "2026-09-21T09:00:00+08:00"}, {"id": "aaaa1111bbbb", "at": "2026-09-21T02:30:00+00:00"},
           {"id": "cccc2222", "at": "坏时间"}]
    assert latest(evs) == {"aaaa1111": "2026-09-21T02:30:00+00:00"}  # 按实际时刻比(北京 9:00 早于 UTC 2:30),坏时间不算


def test_every_event_records_its_operator(tmp_path, monkeypatch):
    """每条操作记录带操作人:缺省取本机 .env 的 OPERATOR(岗位代号),没设时"本机";定时任务写 system;可指定时间。"""
    st = Store(tmp_path)
    monkeypatch.delenv("OPERATOR", raising=False)
    set_status(st, [SIG["id"]], "done")
    monkeypatch.setenv("OPERATOR", " 部门甲-01 ")
    set_stage(st, SIG["id"], "tracking", "开始跟踪")
    add_note(st, SIG["id"], "已购买招标文件")
    st.mark_ledger_reviewed(SIG["id"], "skipped")
    set_status(st, [SIG["id"]], "ignore", by="teable:王五", at="2026-09-28T01:02:03+00:00")
    st.mark_ledger_reviewed(SIG["id"], "skipped", by=SYSTEM, note="系统核对")
    ev = st.events()
    assert [e["by"] for e in ev] == ["本机", "部门甲-01", "部门甲-01", "部门甲-01", "teable:王五", SYSTEM]
    assert ev[4]["at"] == "2026-09-28T01:02:03+00:00" and "by" not in st.ledger_reviewed()[SIG["id"][:8]]  # 核对记录文件不变
    assert [by_zh(e.get("by")) for e in ev[3:]] == ["部门甲-01", "王五(表格)", "系统"] and by_zh(None) == ""
    legacy = {"id": SIG["id"], "at": "2020-01-01T09:00:00+08:00", "kind": "status", "value": "done"}  # 加操作人以前的记录
    tl = timeline(SIG, [legacy, *ev], {}, {}, now=datetime.now(timezone.utc) + timedelta(days=1))
    ops = [(e["title"], e["by"]) for e in tl if e["kind"] in ("status", "stage", "note", "ledger")]
    assert ops[0] == ("标为已处理", "") and ("进展:跟踪中", "部门甲-01") in ops and ("标为忽略", "王五(表格)") in ops
    assert all(e["by"] == "" for e in tl if e["kind"] in ("collect", "judge", "link", "deadline"))  # 系统采集等不是操作
    same_second = [legacy, {**legacy, "value": "ignore", "by": SYSTEM}]
    assert latest_events(same_second)[SIG["id"][:8]]["value"] == "ignore"  # 同一秒的取后记的
    assert latest(same_second) == {SIG["id"][:8]: legacy["at"]}


def test_owner_and_next_step_events_and_row_columns(tmp_path):
    """负责人、下一步与到期(表格拉回的人管字段)记成操作记录 owner / next;现值由记录派生,线索的行里有这三列。"""
    st = Store(tmp_path)
    st.append_signals([SIG])
    set_owner(st, SIG["id"], " 部门甲-01 ", by="teable:张三")
    set_next(st, SIG["id"], "约见业主", "2026-10-15", by="teable:张三")
    ev = st.events()
    assert (ev[0]["kind"], ev[0]["value"]) == ("owner", "部门甲-01") and (ev[1]["value"], ev[1]["due"]) == ("约见业主", "2026-10-15")
    assert [event_title(e) for e in ev] == ["负责人:部门甲-01", "下一步:约见业主(到期 2026-10-15)"]
    r = load_rows(st, date(2026, 9, 27))[0]
    assert (r["owner"], r["next_step"], r["next_due"]) == ("部门甲-01", "约见业主", "2026-10-15")
    set_owner(st, SIG["id"], "")
    set_next(st, SIG["id"], "", None)
    ev = st.events()
    assert current_owners(ev) == {} and current_next(ev) == {} and [event_title(e) for e in ev[2:]] == ["清除负责人", "清除下一步"]
    assert "due" not in ev[3] and (load_rows(st, date(2026, 9, 27))[0]["owner"], row(SIG, date(2026, 9, 27), {})["next_due"]) == ("", "")
    set_next(st, SIG["id"], "", "2026-11-01")  # 只有到期日也算
    assert current_next(st.events())[SIG["id"][:8]]["due"] == "2026-11-01" and event_title(st.events()[-1]) == "下一步:—(到期 2026-11-01)"
    assert event_detail({"note": "表格备注", "conflict": "本机为「已处理」"}) == "表格备注;本机也改过(本机为「已处理」),以表格为准"


def test_calendar_items():
    today = date(2026, 9, 27)
    two = {**SIG, "id": "ffff0000aaaa1111", "published": None, "fields": {},
           "llm": {**SIG["llm"], "deadlines": [{"type": "bid_deadline", "date": "2026-11-02"}, {"type": "eoi_deadline", "date": "2026-11-02"},
                                               {"type": "other", "date": "2026-10"}, {"type": "pq_deadline", "date": "2026-08-01"}]}}
    rows = [row(SIG, today, {}), row(two, today, {}), row({**SIG, "id": "eeee0000", "llm": None, "fields": {}, "published": ""}, today, {})]
    raw = {s["id"]: s for s in (SIG, two)}
    dl = calendar_items(rows, raw, "deadline")
    assert [(it["date"], it["kind"], it["row"]["no"]) for it in dl] == [
        ("2026-08-01", "资格预审截止", "ffff0000"), ("2026-10-20", "投标截止", "abcd1234"),  # 已过的截止日也在;日期写法不对的不在
        ("2026-11-02", "投标截止、意向书截止", "ffff0000")]  # 同一天的几种截止并成一格
    assert all(it["basis"] == "deadline" for it in dl)
    pub = calendar_items(rows, raw, "published")
    assert [(it["date"], it["kind"], it["row"]["no"]) for it in pub] == [("2026-09-18", "发布日", "abcd1234")]
    assert [it["date"] for it in calendar_items(rows, raw, "first_seen")] == ["2026-09-20"] * 3


def test_manual_submission_event(tmp_path):
    """人工入库记一笔:谁交进来的、怎么交的(网址、粘贴文字、上传文件);时间线上显示为"人工入库(…)"。"""
    s = Store(tmp_path / "data")
    e = log_submit(s, "a1b2c3d4e5f60718", "url", "同事转来的", by="部门甲-01")
    assert (e["kind"], e["value"], e["note"], e["by"]) == ("submit", "url", "同事转来的", "部门甲-01")
    assert event_title(e) == "人工入库(网址)" and event_title({"kind": "submit", "value": "file"}) == "人工入库(上传文件)"
    with pytest.raises(ValueError):
        log_submit(s, "a1b2c3d4e5f60718", "fax")
    assert current_stages(s.events()) == {} and latest_events(s.events())["a1b2c3d4"]["kind"] == "submit"
