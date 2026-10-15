"""答错了当场改正、助手会查城市(本体 v2.27;2026-10-15 使用方问「吉尔吉斯斯坦有多少个规模以上城市」答成了营商环境,
反馈之后问「如何让 AI 立即学习,而不是等它每日复盘」)。"""
from __future__ import annotations

import os
import threading
import time
from datetime import date
from pathlib import Path

import httpx
import pytest

from core import ask_log, assistant as A, tstore

TODAY = date(2026, 10, 15)


@pytest.fixture(autouse=True)
def _clean():
    yield
    A.set_terms(None)
    A.set_phrases(None)
    A.set_learned(None)


def _model(**kw) -> dict:
    """模型的一份解析(SCHEMA 的全部字段,缺省都空)。"""
    d = {k: [] for k in A.SCHEMA["required"]}
    d.update({k: None for k in ("tracked", "relevant", "days", "from", "to", "future_days", "capacity_min", "capacity_max", "measure", "agg", "group_by", "limit", "unsupported")})
    d.update(intent="list")
    d.update(kw)
    return d


class Fake:
    """假模型:按 answer(提问 → 解析)作答,记下每次问了什么。"""
    enabled, client, label, fatal, note, errors = True, object(), "fake", "", "", []

    def __init__(self, answer):
        self.answer, self.asked = answer, []

    def ask(self, system, q, schema, guide):
        self.asked.append(q)
        return dict(self.answer(q))

    def forgive(self):
        pass


def test_city_questions_on_users_own_phrasings():
    """问城市本身的说法认作城市(人口门槛、前几座、按国别分、接着问);说了线索、营商环境、水价、中标的不算。"""
    want = {
        "吉尔吉斯斯坦有多少个规模以上城市?": ("count", "吉尔吉斯斯坦", None, 50),
        "吉尔吉斯斯坦有几个城市": ("count", "吉尔吉斯斯坦", None, 50),
        "摩洛哥人口最多的5个城市": ("list", "摩洛哥", None, 5),
        "阿塞拜疆人口超过50万的城市": ("list", "阿塞拜疆", "50 万以上", 50),
        "百万人口以上的城市按国家分": ("chart", None, "100 万以上", 50),
        "摩洛哥最大的三座城市": ("list", "摩洛哥", None, 3),
    }
    for q, (intent, country, pop, limit) in want.items():
        p = A.plan(q, TODAY)
        u = dict(p["understood"])
        assert (p["object"], p["intent"], u.get("国别"), u.get("人口"), p["limit"]) == ("city", intent, country, pop, limit), q
    for q in ("埃及城市供水项目的线索", "吉尔吉斯斯坦的营商环境", "摩洛哥城市水价", "巴库城市污水处理厂的中标信息", "城市污水排放标准"):
        assert A.plan(q, TODAY)["object"] != "city", q
    prev = A.plan("吉尔吉斯斯坦有多少个规模以上城市", TODAY)
    prev = {"q": "吉尔吉斯斯坦有多少个规模以上城市", "object": "city", "filters": prev["filters"], "measures": [], "group_by": None}
    p = A.plan("那人口超过50万的呢", TODAY, None, prev)
    assert p["object"] == "city" and dict(p["understood"])["国别"] == "吉尔吉斯斯坦" and dict(p["understood"])["人口"] == "50 万以上"
    assert A.plan("那摩洛哥呢", TODAY, None, prev)["filters"]["countries"] == [{"iso3": "MAR", "name": "摩洛哥"}]


def test_model_that_says_country_for_a_city_question_is_overruled():
    """10-15 截图:模型把「规模以上城市」当成国别(营商环境总分);提问里说的是城市,按城市答。"""
    clf = Fake(lambda q: _model(object="country", intent="count", countries=["吉尔吉斯斯坦"]))
    p = A.plan("吉尔吉斯斯坦有多少个规模以上城市?", TODAY, clf)
    assert p["object"] == "city" and p["parsed_by"] == "model" and dict(p["understood"])["国别"] == "吉尔吉斯斯坦"
    assert "指标" not in dict(p["understood"])


def test_model_can_say_it_cannot_answer():
    """问的不在助手会查的几类里:直说不会查(理解为「问的是:污水厂(助手还不会查)」),不按词表硬套成线索。"""
    clf = Fake(lambda q: _model(object="lead", intent="count", countries=["吉尔吉斯斯坦"], unsupported="污水厂的数量"))
    p = A.plan("吉尔吉斯斯坦有几座污水厂", TODAY, clf)
    assert (p["intent"], p["unsupported"], p["understood"]) == ("help", "污水厂的数量", [["问的是", "污水厂的数量(助手还不会查)"]])
    assert "unsupported" in A.SCHEMA["required"] and "city" in A.SCHEMA["properties"]["object"]["enum"]


def test_fix_learn_and_similar_examples():
    """答错了写了哪里不对 → 带着上一次的理解和这句说明请模型重新理解;学会之后同一句直接按它(不问模型),说法差不多的给模型当参考。"""
    first = _model(object="lead", intent="count", countries=["阿塞拜疆"])
    fixed = _model(object="opportunity", intent="count", countries=["阿塞拜疆"], tracked=True)
    clf = Fake(lambda q: fixed if "用户说这样理解不对" in q else first)
    assert A.plan("阿塞拜疆的项目有多少", TODAY, clf)["object"] == "lead"
    p = A.plan("阿塞拜疆的项目有多少", TODAY, clf, fix={"understood": "对象:线索\n国别:阿塞拜疆", "note": "我问的是我们在跟的商机"})
    assert p["object"] == "opportunity" and p["_raw"]["object"] == "opportunity"
    assert "上一次把这个问题理解为:对象:线索" in clf.asked[-1] and "用户说这样理解不对:我问的是我们在跟的商机" in clf.asked[-1]
    A.add_learned("阿塞拜疆的项目有多少", "", p["_raw"])
    n = len(clf.asked)
    p = A.plan("阿塞拜疆的项目有多少?", TODAY, clf)  # 标点、空格不同也算同一句
    assert (p["object"], p["parsed_by"], len(clf.asked)) == ("opportunity", "learned", n)
    assert A.plan("阿塞拜疆的项目有多少", TODAY, clf, {"q": "最近的线索", "object": "lead", "filters": {}})["parsed_by"] == "learned"  # 前面问过别的:学会的单问照样用
    A.add_learned("那摩洛哥呢", "阿塞拜疆的项目有多少", fixed)  # 接着问学会的:只在接着同一问时用
    assert A.learned_for("那摩洛哥呢") is None and A.learned_for("那摩洛哥呢", "阿塞拜疆的项目有多少") == fixed
    assert A.plan("那摩洛哥呢", TODAY, clf, {"q": "最近的线索", "object": "lead", "filters": {}})["parsed_by"] == "model"
    A.plan("摩洛哥的项目有多少", TODAY, clf)
    assert "参考:下面是用户纠正过的类似问题" in clf.asked[-1] and '"object": "opportunity"' in clf.asked[-1]
    A.plan("最近 7 天埃及有哪些新线索", TODAY, clf)
    assert "参考:" not in clf.asked[-1]  # 不像的不给
    # 学会的那句又答错了:重答时不用学到的
    p = A.plan("阿塞拜疆的项目有多少", TODAY, clf, fix={"understood": "对象:商机", "note": "要线索"})
    assert p["parsed_by"] == "model"
    # 没配模型:不能当场重答,直说
    p = A.plan("阿塞拜疆的项目有多少", TODAY, None, fix={"understood": "对象:线索", "note": "要商机"})
    assert p["parsed_by"] == "rules" and "没法按你的说明重新理解" in p["note"]
    assert A.plan("阿塞拜疆的项目有多少", TODAY)["parsed_by"] == "learned"  # 没配模型时学到的照样能用


def _bot_server(clf):
    from core.assistant_server import Assistant, serve
    from tests.test_portal import _fake_with_schema
    fake, _ = _fake_with_schema()
    b = tstore.Base(tstore.Client("https://water.example", "bot-token", transport=fake.transport, sleep=lambda s: None))
    bot = Assistant(None)
    bot.clf = clf
    bot.terms_base = lambda: tstore.Base(tstore.Client("https://water.example", "bot-token", transport=fake.transport, sleep=lambda s: None))
    bot.who = lambda cookie: "管理员 <admin@example.org>"
    srv = serve(bot, 0, host="127.0.0.1")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return bot, srv, b, f"http://127.0.0.1:{srv.server_port}"


def _row(b, key, wait=5.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < wait:
        r = ask_log.find(b, key)
        if r:
            return r
        time.sleep(0.1)
    raise AssertionError(key)


def test_server_fixes_on_the_spot_and_learns_at_once():
    """/ask 带 fix 当场重答(不给模型的原始解析);/feedback 对重答点答对了(fixes = 原来那一问):原来那一行写学到的解析、应理解为、已修好,
    马上生效;重读表也认得;状态改「不改」就不再用。游客不能带 fix。"""
    first = _model(object="lead", intent="count", countries=["阿塞拜疆"])
    fixed = _model(object="opportunity", intent="count", countries=["阿塞拜疆"], tracked=True)
    clf = Fake(lambda q: fixed if "用户说这样理解不对" in q else first)
    bot, srv, b, url = _bot_server(clf)
    h = {"X-Portal-Auth": "1", "Content-Type": "application/json", "Cookie": "auth_session=admin-token"}
    q = "阿塞拜疆的项目有多少"
    try:
        r1 = httpx.post(url + "/ask", json={"q": q}, headers=h).json()
        assert r1["object"] == "lead" and "_raw" not in r1
        k1 = r1["log_key"]
        assert httpx.post(url + "/feedback", json={"key": k1, "verdict": "答错了", "note": "我问的是我们在跟的商机", "q": q}, headers=h).json() == {"ok": True}
        r2 = httpx.post(url + "/ask", json={"q": q, "fix": {"key": k1, "note": "我问的是我们在跟的商机", "understood": "对象:线索"}}, headers=h).json()
        assert r2["object"] == "opportunity" and r2["refixed"] is True and "_raw" not in r2
        k2 = r2["log_key"]
        _row(b, k2)
        f = httpx.post(url + "/feedback", json={"key": k2, "verdict": "答对了", "fixes": k1, "q": q, "understood": "对象:商机\n国别:阿塞拜疆"}, headers=h).json()
        assert f == {"ok": True, "learned": True}
        rec = ask_log.find(b, k1)
        assert (b.get("ask_log", rec, "status"), b.get("ask_log", rec, "expected"), b.get("ask_log", rec, "verdict")) == ("已修好", "对象:商机\n国别:阿塞拜疆", "答错了")
        assert '"object": "opportunity"' in b.get("ask_log", rec, "learned")
        n = len(clf.asked)
        r3 = httpx.post(url + "/ask", json={"q": q}, headers=h).json()
        assert (r3["object"], r3["parsed_by"], len(clf.asked)) == ("opportunity", "learned", n)  # 马上生效,不再问模型
        # 进程重启(内存里的忘了):重读表就认得
        A.set_learned(None)
        bot._terms_at = None
        bot.refresh_terms()
        assert A.learned_for(q)["object"] == "opportunity"
        # 人把状态改成「不改」:不再用
        b.update("ask_log", rec["id"], {"status": "不改"})
        bot._terms_at = None
        bot.refresh_terms()
        assert A.learned_for(q) is None
        # 游客(库外帐号)带 fix 也不重答、不记
        bot.guest = type("G", (), {"is_outsider": staticmethod(lambda cookie: True)})()
        r4 = httpx.post(url + "/ask", json={"q": q, "fix": {"key": k1, "note": "要商机"}}, headers=h).json()
        assert "refixed" not in r4 and "log_key" not in r4
    finally:
        srv.shutdown()


def test_portal_city_answer_and_fix_on_the_spot():
    """首页:问规模以上城市给卡片与清单;答错了写一句 → 下面当场重答;对重答点答对了 → 记住,再问同一句标「按纠正过的理解」。手机宽度不横向滚动。"""
    pytest.importorskip("playwright.sync_api", reason="没有 Playwright")
    from urllib.parse import quote
    from playwright.sync_api import Error as PWError, sync_playwright
    from core import schema
    from tests.test_portal import NEW, ONTO, _fake_with_schema, _seed, _serve
    fake, admin = _fake_with_schema()
    _seed(admin, fake)
    b = tstore.Base(admin)
    cfg = schema.read_config(admin, NEW, ONTO)
    srv = _serve(fake, cfg, log_asks=True)
    first = _model(object="lead", intent="count", countries=["阿塞拜疆"])
    fixed = _model(object="opportunity", intent="list", countries=["阿塞拜疆"])
    clf = Fake(lambda q: _model(object="country", intent="count", countries=["阿塞拜疆"]) if q.startswith("阿塞拜疆有多少个规模以上城市") else fixed if "用户说这样理解不对" in q else first)
    srv.bot.clf = clf
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except PWError:
                browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium")
            page = browser.new_page(viewport={"width": 1366, "height": 900})
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            done = "(sel) => { const a = document.querySelector(sel); return a && !a.innerText.includes('解析中') && !a.innerText.includes('查表中') && !a.innerText.includes('重新理解中') && a.querySelector('.a-fb'); }"
            # 规模以上城市:模型说成国别,按提问认作城市;卡片、清单(巴库)、到国别概况的链接
            page.goto(base + "/index.html?q=" + quote("阿塞拜疆有多少个规模以上城市?"))
            page.wait_for_function(done, arg="#a1", timeout=15000)
            a1 = page.locator("#a1")
            assert "对象:城市" in a1.locator(".understood").inner_text() and "营商环境" not in a1.inner_text()
            n_city = len([r for r in b.rows("city") if tstore.link_id(b.get("city", r, "country"))])
            assert a1.locator(".kpi").first.inner_text().startswith(str(n_city)) and "巴库" in a1.locator(".a-body tbody").inner_text()
            assert "在国别概况看阿塞拜疆的规模以上城市" in a1.inner_text()
            # 答非所问 → 写一句 → 当场重答 → 点答对了 → 记住
            page.fill("#q", "阿塞拜疆的项目有多少")
            page.press("#q", "Enter")
            page.wait_for_function(done, arg="#a2", timeout=15000)
            a2 = page.locator("#a2")
            assert "对象:线索" in a2.locator(".understood").inner_text()
            a2.locator(".a-fb button", has_text="答错了").click()
            a2.locator(".a-fb textarea").fill("我问的是我们在跟的商机")
            a2.locator(".a-fb button", has_text="提交").click()
            page.wait_for_function(done, arg=".answer.refix", timeout=15000)
            fx = page.locator(".answer.refix")
            assert "按你的说明重答:我问的是我们在跟的商机" in fx.inner_text() and "对象:商机" in fx.locator(".understood").inner_text()
            assert "用户说这样理解不对:我问的是我们在跟的商机" in clf.asked[-1]
            fx.locator(".a-fb button", has_text="答对了").click()
            fx.locator(".a-fb", has_text="记住了").wait_for(timeout=15000)
            if os.environ.get("PORTAL_SHOTS"):
                page.locator("#a2").screenshot(path=str(Path(os.environ["PORTAL_SHOTS"]) / "助手_答错了.png"))
                fx.screenshot(path=str(Path(os.environ["PORTAL_SHOTS"]) / "助手_当场重答.png"))
                a1.screenshot(path=str(Path(os.environ["PORTAL_SHOTS"]) / "助手_规模以上城市.png"))
            n = len(clf.asked)
            page.fill("#q", "阿塞拜疆的项目有多少")
            page.press("#q", "Enter")
            page.wait_for_function(done, arg="#a3", timeout=15000)
            a3 = page.locator("#a3")
            assert "按纠正过的理解" in a3.locator(".understood").inner_text() and "对象:商机" in a3.locator(".understood").inner_text() and len(clf.asked) == n
            rows = [r for r in b.rows("ask_log") if b.get("ask_log", r, "learned")]
            assert len(rows) == 1 and b.get("ask_log", rows[0], "status") == "已修好" and b.get("ask_log", rows[0], "note") == "我问的是我们在跟的商机"
            # 手机:不撑出横向滚动
            page.set_viewport_size({"width": 390, "height": 800})
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
            # 本体页错题本:当场学会 1 句
            page.set_viewport_size({"width": 1366, "height": 900})
            page.goto(base + "/ontology.html?t=sem")
            page.wait_for_function("document.querySelector('#semLog') && !document.querySelector('#semLog').innerText.includes('读取中')", timeout=15000)
            log = page.inner_text("#semLog")
            assert "当场学会 1 句" in log and "阿塞拜疆的项目有多少" in log
            assert not errors, errors
            browser.close()
    finally:
        srv.shutdown()
