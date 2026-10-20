"""日历订阅(core/calendar_feed.py + 助手接口的 /calendar.ics、/feed-link):内容口径与页面日历一致(跟踪中商机的节点、跟进到期、
国别级节点;未归商机的线索截止不要);.ics 格式合规(全天事件、转义、折行);登录后来的放行、凭令牌来的核令牌、都没有就 403。"""
from __future__ import annotations

import threading
from datetime import date, timedelta

import httpx

from core import calendar_feed, tstore
from core.assistant_server import Assistant, serve
from tests.test_portal import _fake_with_schema, _seed, _today


def _base_with_data():
    fake, admin = _fake_with_schema()
    c, o, ld = _seed(admin, fake)
    b = tstore.Base(admin)
    today = _today()  # 与种数据同一个「今天」(北京时间)
    # 国别级节点(没挂商机、没挂线索)要;未归商机的线索截止不要;不跟踪的商机的节点不要
    b.create("milestone", [{"country": c["id"], "kind": "其他", "day": (today + timedelta(days=10)).isoformat(), "日期原文": "x", "status": "官方计划", "label": "投资论坛"},
                           {"lead": ld["id"], "kind": "投标截止", "day": (today + timedelta(days=12)).isoformat(), "日期原文": "y", "status": "官方计划", "label": "未归商机"}])
    sug = b.rows("opportunity", filter=b.filter(b.cond("opportunity", "code", "is", "AZE-002")))[0]
    b.create("milestone", [{"opportunity": sug["id"], "kind": "开标", "day": (today + timedelta(days=20)).isoformat(), "日期原文": "z", "status": "推断"}])
    return fake, admin, b, today


def test_collect_matches_the_page_calendar_scope():
    fake, admin, b, today = _base_with_data()
    ev = calendar_feed.collect(b, today, site="https://water.example")
    summaries = sorted(e["summary"] for e in ev)
    assert any("意向书截止 · AZE-001 甘贾污水厂" == s for s in summaries)  # 跟踪中商机的节点
    assert any(s.startswith("到期 · AZE-001 · 递交意向书") for s in summaries)  # 跟进的下一步到期
    assert any(s == "其他 · AZE" for s in summaries)  # 国别级节点
    assert not any("未归商机" in (e["description"] or "") for e in ev) and not any("开标" in s for s in summaries)  # 线索截止、建议商机的节点不要
    ms = next(e for e in ev if e["summary"].startswith("意向书截止"))
    assert ms["url"].startswith("https://water.example/portal/opp.html?id=") and ms["day"] == today + timedelta(days=3)
    # 按国别、商机筛
    assert {e["summary"] for e in calendar_feed.collect(b, today, country="MAR")} == set()
    only = calendar_feed.collect(b, today, opp="aze-001")
    assert only and all("AZE-001" in e["summary"] for e in only)
    # 放弃跟踪(勾了「已归档」,A50)的商机不进日历,与页面同一口径
    o = b.rows("opportunity", filter=b.filter(b.cond("opportunity", "code", "is", "AZE-001")))[0]
    b.update("opportunity", o["id"], {"archived": True, "archive_reason": "业主改为本国预算", "archived_at": today.isoformat()})
    assert not any("AZE-001" in e["summary"] for e in calendar_feed.collect(b, today))


def test_render_is_valid_ics_with_all_day_events_and_folding():
    d = date(2026, 10, 15)
    text = calendar_feed.render([{"uid": "ms-1@water-osint", "day": d, "summary": "投标截止 · AZE-001 甘贾污水厂, 一期; 二期", "description": "官方计划\n第二行", "url": "https://x/portal/opp.html?id=1",
                                 "categories": "时间节点"},
                                {"uid": "due-2@water-osint", "day": d, "summary": "到期 · " + "很长的内容" * 30}], tz="Asia/Shanghai")
    lines = text.split("\r\n")
    assert lines[0] == "BEGIN:VCALENDAR" and lines[-2] == "END:VCALENDAR" and text.endswith("\r\n") and "METHOD:PUBLISH" in lines
    assert "DTSTART;VALUE=DATE:20261015" in lines and "DTEND;VALUE=DATE:20261016" in lines
    assert "SUMMARY:投标截止 · AZE-001 甘贾污水厂\\, 一期\\; 二期" in lines and "DESCRIPTION:官方计划\\n第二行" in lines
    assert all(len(x.encode("utf-8")) <= 75 for x in lines), [x for x in lines if len(x.encode("utf-8")) > 75]  # 折行
    long = [x for x in lines if x.startswith(" ")]
    assert long and text.count("BEGIN:VEVENT") == 2


def test_feed_token_from_env_or_derived(monkeypatch):
    monkeypatch.delenv("OW_FEED_TOKEN", raising=False)
    assert calendar_feed.feed_token("bot-token") == calendar_feed.feed_token("bot-token") and len(calendar_feed.feed_token("bot-token")) == 32
    assert calendar_feed.feed_token("other") != calendar_feed.feed_token("bot-token")
    monkeypatch.setenv("OW_FEED_TOKEN", "abc123")
    assert calendar_feed.feed_token("bot-token") == "abc123"
    monkeypatch.delenv("OW_FEED_TOKEN", raising=False)
    monkeypatch.delenv("TEABLE_TOKEN", raising=False)
    assert calendar_feed.feed_token() == ""


def test_server_calendar_endpoints_check_login_or_token(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OW_FEED_TOKEN", "team-token")
    fake, admin, b, today = _base_with_data()
    feed = calendar_feed.Feed(base_factory=lambda: b)
    bot = Assistant(None, feed=feed)
    srv = serve(bot, 0, host="127.0.0.1")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{srv.server_port}"
        assert httpx.get(base + "/calendar.ics").status_code == 403  # 既没登录标记也没令牌
        assert httpx.get(base + "/calendar.ics?t=wrong").status_code == 403
        r = httpx.get(base + "/calendar.ics?t=team-token&country=AZE", headers={"Host": "water.example"})
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/calendar") and "BEGIN:VEVENT" in r.text and "AZE-001" in r.text
        assert "https://water.example/portal/opp.html?id=" in r.text  # 链接按请求的主机名
        r2 = httpx.get(base + "/calendar.ics", headers={"X-Portal-Auth": "1"})  # 登录后经 Caddy 来的:不用令牌
        assert r2.status_code == 200 and "BEGIN:VEVENT" in r2.text
        assert httpx.get(base + "/feed-link").status_code == 403  # 订阅链接只给登录后的
        lk = httpx.get(base + "/feed-link?country=AZE", headers={"X-Portal-Auth": "1", "Host": "water.example"}).json()
        assert lk["ok"] and lk["url"] == "webcal://water.example/portal/feed/calendar.ics?t=team-token&country=AZE" and lk["https"].startswith("https://water.example/portal/feed/")
        # 缓存:5 分钟内同样的参数不再查表
        n = len(fake.calls)
        httpx.get(base + "/calendar.ics?t=team-token&country=AZE", headers={"Host": "water.example"})
        assert len(fake.calls) == n
    finally:
        srv.shutdown()


def test_server_without_feed_says_so(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    bot = Assistant(None)
    assert bot.feed is None
    srv = serve(bot, 0, host="127.0.0.1")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert httpx.get(f"http://127.0.0.1:{srv.server_port}/calendar.ics", headers={"X-Portal-Auth": "1"}).status_code == 503
    finally:
        srv.shutdown()
