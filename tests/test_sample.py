"""游客的示例数据(core/sample.py,本体 guest.data: sample):游客通道读到的记录全部是编的示例,真实库的记录一条也出不来;
表与字段编号与真实库一致(页面不用改);条件、排序、计数在内存里算;只读;日历下载给游客换成示例,订阅链接不给。"""
from __future__ import annotations

import json
import threading
from datetime import date, timedelta

import httpx
import pytest

from core import calendar_feed, guest as guest_mod, sample, tstore
from core.assistant_server import Assistant, serve
from tests.test_portal import _fake_with_schema, _seed

TODAY = date(2026, 10, 9)


def _sample_guest():
    fake, admin = _fake_with_schema()
    _seed(admin, fake)  # 真实库里有一个真商机 AZE-001,游客不该看到
    bot = tstore.Client("https://water.example", "bot-token", transport=fake.transport, sleep=lambda s: None)
    sc = sample.SampleClient(bot, today=lambda: TODAY)
    g = guest_mod.Guest(base_factory=lambda: tstore.Base(sc))
    return fake, tstore.Base(admin), g, sc


def test_ontology_turns_sample_mode_on():
    assert guest_mod.Guest.sample_mode() is True
    assert guest_mod.Guest.sample_mode({"guest": {}}) is False


def test_guest_sees_only_sample_records_with_real_ids():
    fake, b, g, sc = _sample_guest()
    n = len(fake.calls)
    opps = g.records(b.t["opportunity"], {})["records"]
    codes = {r["fields"][b.fid("opportunity", "code")] for r in opps}
    assert "AZE-001" not in codes and {"UZB-901", "KEN-901"} <= codes and len(codes) == len(sample.OPPS)
    assert all(b.fid("opportunity", "internal_note") not in r["fields"] and b.fid("opportunity", "priority") not in r["fields"] for r in opps)  # 放行范围照旧
    leads = g.records(b.t["lead"], {"take": ["1000"]})["records"]
    assert len(leads) == 36 and all(r["fields"][b.fid("lead", "url")].startswith("https://example.org/") for r in leads)
    # 关联是 {id, title},指向示例里的记录
    one = opps[0]["fields"][b.fid("opportunity", "country")]
    assert isinstance(one, dict) and one["id"].startswith("rec") and one["title"]
    cs = g.records(b.t["country"], {})["records"]
    assert {r["fields"][b.fid("country", "key")] for r in cs} == {c[0] for c in sample.COUNTRIES}
    uzb = next(r for r in cs if r["fields"][b.fid("country", "key")] == "UZB")
    assert uzb["fields"][b.fid("country", "lead_count")] == sum(1 for x in sample.build(TODAY)["lead"] if x["country"] == "UZB")
    assert b.fid("country", "note") not in uzb["fields"]
    # 报告:定期的公开版,日报有正文
    reps = g.records(b.t["report"], {})["records"]
    assert {r["fields"][b.fid("report", "kind")] for r in reps} == {"日报", "周报"} and "示例" in reps[0]["fields"][b.fid("report", "summary")]
    # 真实库没动、也没被读记录
    assert fake.calls[n:] and all(m == "GET" and "/record" not in p for m, p in fake.calls[n:])  # 只读了结构


def test_filters_orders_counts_and_single_records():
    fake, b, g, sc = _sample_guest()
    tracked = json.dumps(b.filter(b.cond("opportunity", "stage", "isAnyOf", ["跟踪中", "准备投标", "已投标", "中标"])))
    page = g.records(b.t["opportunity"], {"filter": [tracked], "orderBy": [json.dumps([{"fieldId": b.fid("opportunity", "size_value"), "order": "desc"}])]})
    sizes = [r["fields"][b.fid("opportunity", "size_value")] for r in page["records"]]
    assert len(sizes) == 5 and sizes == sorted(sizes, reverse=True)
    assert g.count(b.t["opportunity"], {"filter": [tracked]}) == {"rowCount": 5}
    uzb = next(r for r in g.records(b.t["country"], {})["records"] if r["fields"][b.fid("country", "key")] == "UZB")
    by_c = json.dumps(b.filter(b.cond("opportunity", "country", "is", uzb["id"])))
    assert g.count(b.t["opportunity"], {"filter": [by_c]})["rowCount"] == 2
    win = json.dumps(b.filter(b.cond("milestone", "day", "isOnOrAfter", {"mode": "exactDate", "exactDate": TODAY.isoformat() + "T00:00:00.000Z", "timeZone": "Asia/Shanghai"})))
    future = g.records(b.t["milestone"], {"filter": [win]})["records"]
    assert future and all(str(tstore.day(r["fields"][b.fid("milestone", "day")], "Asia/Shanghai")) >= TODAY.isoformat() for r in future)
    recent = json.dumps(b.filter(b.cond("lead", "found_at", "isOnOrAfter", {"mode": "exactDate", "exactDate": (TODAY - timedelta(days=6)).isoformat(), "timeZone": "Asia/Shanghai"})))
    assert 0 < g.count(b.t["lead"], {"filter": [recent]})["rowCount"] < 36
    paged = g.records(b.t["lead"], {"take": ["10"], "skip": ["30"]})["records"]
    assert len(paged) == 6
    rid = page["records"][0]["id"]
    assert g.record(b.t["opportunity"], rid)["fields"][b.fid("opportunity", "code")] == page["records"][0]["fields"][b.fid("opportunity", "code")]
    with pytest.raises(tstore.TStoreError):
        g.record(b.t["opportunity"], "recNOPE")
    with pytest.raises(guest_mod.Denied):  # 越界照旧拦
        g.records(b.t["followup"], {})


def test_sample_client_is_read_only_and_rebuilds_daily():
    fake, b, g, sc = _sample_guest()
    with pytest.raises(tstore.TStoreError):
        sc.req("PATCH", f"/table/{b.t['opportunity']}/record", json_body={})
    with pytest.raises(tstore.TStoreError):
        sc.req("GET", f"/table/{b.t['opportunity']}/record/recX/history")
    first = sc.store()
    assert sc.store() is first
    sc._today = lambda: TODAY + timedelta(days=1)
    assert sc.store() is not first and sc.store().today == TODAY + timedelta(days=1)


def test_calendar_from_sample_has_tracked_milestones_only():
    fake, b, g, sc = _sample_guest()
    evs = calendar_feed.collect(g.base(), TODAY)
    text = json.dumps(evs, ensure_ascii=False, default=str)
    assert evs and "UZB-901" in text and "AZE-001" not in text
    assert "VNM-902" not in text and "PER-901" not in text  # 只有跟踪中的商机(建议、不跟进的不进日历)


def test_server_gives_outsiders_sample_calendar_and_no_feed_link(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    fake, b, g, sc = _sample_guest()
    real_feed = calendar_feed.Feed(base_factory=lambda: b)
    bot = Assistant(None, guest=g, feed=real_feed)
    bot.feed_token = "tok"
    outsider = {"v": True}
    monkeypatch.setattr(g, "is_outsider", lambda cookie: outsider["v"])
    srv = serve(bot, 0, host="127.0.0.1")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base, H = f"http://127.0.0.1:{srv.server_port}", {"X-Portal-Auth": "1", "Cookie": "auth_session=x"}
        ics = httpx.get(base + "/calendar.ics", headers=H).text
        assert "AZE-001" not in ics and "UZB-901" in ics
        assert httpx.get(base + "/feed-link", headers=H).json()["ok"] is False
        outsider["v"] = False  # 库里的人:真实数据
        ics2 = httpx.get(base + "/calendar.ics", headers=H).text
        assert "UZB-901" not in ics2
        assert httpx.get(base + "/feed-link", headers=H).json()["ok"] is True
        assert "UZB-901" not in httpx.get(base + "/calendar.ics", params={"t": "tok"}).text  # 订阅(没登录、凭令牌)照旧是真实数据
    finally:
        srv.shutdown()
