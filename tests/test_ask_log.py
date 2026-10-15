"""助手的错题本、发前核对、歧义先问(本体 v2.26;2026-10-15 使用方看了「Data Agent 全流程」「RAG 消息流」两张图说「要做」)。"""
from __future__ import annotations

import json
import os
import threading
from datetime import date
from pathlib import Path

import httpx
import pytest

from core import ask_log, assistant as A, law, tstore

TODAY = date(2026, 10, 15)


@pytest.fixture(autouse=True)
def _own_terms():
    """连了内存库的助手接口会读那里的「行业术语」「语义层说法」表(空的):用完换回本体的初始词,不影响别的测试。"""
    yield
    A.set_terms(None)
    A.set_phrases(None)
    A.set_learned(None)


def _back(q, prev=None):
    return {a["id"]: a for a in A.plan(q, TODAY, None, prev).get("ask_back") or []}


def test_ask_back_on_users_own_phrasings():
    """使用方问过的话:只在真没说清时给另几种理解;照缺省答(答案不变),改写后的问题不再有同一个歧义,而且真按那一种理解。"""
    # 「项目」没说是线索、已中标项目还是商机:缺省线索,另给已中标项目、商机
    b = _back("摩洛哥有哪些污水项目")
    assert set(b) == {"project_object"} and b["project_object"]["chosen"] == "招标线索"
    alt = {o["label"]: o["q"] for o in b["project_object"]["options"]}
    assert set(alt) == {"已中标项目", "我们跟踪的商机"}
    assert A.plan(alt["已中标项目"], TODAY)["object"] == "award" and "project_object" not in _back(alt["已中标项目"])
    assert A.plan(alt["我们跟踪的商机"], TODAY)["object"] == "opportunity" and "project_object" not in _back(alt["我们跟踪的商机"])
    assert A.plan("塔什干新城沙特投资水项目", TODAY)["object"] == "lead" and "project_object" in _back("塔什干新城沙特投资水项目")
    # 说清了的不问:有「中标」「线索」「商机」「条」,问了只有已中标项目才有的指标、合同类型、金额,在跟踪的
    for q in ("近三年摩洛哥地区运营维护的项目中标信息，规模在5000m3/d以上", "世界银行出资的供水项目有多少条", "摩洛哥近3年来的污水项目中标金额与招标金额什么比例",
              "摩洛哥近3年来5000万人民币以上的污水厂项目，平均评标时间多长?", "在跟踪的商机有哪些", "最近 7 天埃及有哪些新线索", "摩洛哥的项目机会", "阿塞拜疆的污水厂工程量"):
        assert "project_object" not in _back(q), q
    # 接着问沿用了上一问的对象:不问
    prev = {"q": "摩洛哥近三年运营维护的中标信息", "object": "award", "filters": A.plan("摩洛哥近三年运营维护的中标信息", TODAY)["filters"], "measures": [], "group_by": None}
    assert _back("那吉尔吉斯斯坦的项目呢", prev) == {}
    # 只说「刚果」:两国;说清了的(刚果金、刚果布、民主刚果、刚果共和国、刚果(布))不问
    b = _back("刚果有哪些供水线索")["congo"]
    assert b["chosen"] == "刚果(布)" and b["options"] == [{"label": "刚果(金)", "q": "刚果金有哪些供水线索"}]
    assert [c["iso3"] for c in A.plan("刚果金有哪些供水线索", TODAY)["filters"]["countries"]] == ["COD"]
    for q in ("刚果金的线索", "刚果布的线索", "民主刚果的线索", "刚果共和国的线索", "刚果(布)的线索", "刚果民主共和国的线索"):
        assert "congo" not in _back(q), q
    assert "congo" in _back("刚果的污水排放标准")  # 法规问题也问
    # 「近三年」:缺省往前推满 1095 天;另给「今年算一年」「三个整年」,改写后真按日历年
    b = _back("近三年摩洛哥运营维护的项目中标信息")["recent_years"]
    assert b["chosen"] == "往前推满 3 年(1095 天)"
    alt = {o["label"]: o["q"] for o in b["options"]}
    assert alt == {"2024 年 1 月 1 日至今(今年算一年)": "2024 年以来摩洛哥运营维护的项目中标信息", "2023–2025 年(3 个整年)": "2023–2025 年摩洛哥运营维护的项目中标信息"}
    f = A.plan(alt["2023–2025 年(3 个整年)"], TODAY)["filters"]
    assert (f["from"], f["to"], f["days"]) == ("2023-01-01", "2025-12-31", None) and "recent_years" not in _back(alt["2023–2025 年(3 个整年)"])
    assert A.plan(alt["2024 年 1 月 1 日至今(今年算一年)"], TODAY)["filters"]["from"] == "2024-01-01"
    assert "recent_years" in _back("摩洛哥近3年来的污水项目中标金额与招标金额什么比例")
    for q in ("最近 7 天埃及有哪些新线索", "近一年的中标", "2024年以来摩洛哥的中标", "八十年代的项目"):
        assert "recent_years" not in _back(q), q


def test_law_answer_numbers_must_be_in_the_cited_clauses():
    """法规按条文作答,发前核对:回答里的数要在所引条文(原文、中文、法规名、条号)里找得到;括注的约合美元、角标、年份不查。
    1 000 / 1,000 / 10.000(法文千分位)/ 6,5(法文小数)/ 1 万 都认。"""
    c = [{"text": "Article 72 : la valeur limite de la DBO5 est de 30 mg/l ; MES 1 000 mg/l ; pH 6,5 - 8,5. Amende de 10.000 à 50.000 dirhams.",
          "text_zh": "第七十二条", "law": "Loi n° 36-15 relative à l'eau", "label": "Article 72"}]
    ok = "BOD5 限值 30 mg/L [1],悬浮物 1000 mg/L,pH 6.5–8.5;罚款 1 万至 5 万迪拉姆(约合 1,000–5,000 美元)[1]。见第 72 条(36-15 号法,2016 年)。"
    assert law.unsupported(ok, c) == []
    assert law.unsupported("COD 125 mg/L,罚款 3 万迪拉姆", c) == ["125", "3"]
    assert law.unsupported("BOD5 300 mg/L", c) == ["5 300"]  # 「BOD5 300」连成了一个数,拆开 300 也不在条文里


class _Clf:
    enabled = True

    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def ask(self, system, user, schema, guide):
        if system == law.SEARCH_SYSTEM:  # 先请模型补检索词:这里不补
            return {"zh": [], "orig": []}
        self.calls.append(json.loads(user))
        return self.answers.pop(0)


class _Lib:
    def __init__(self, clauses):
        self.clauses = clauses

    def country_rows(self, iso):
        return self.clauses, {"L1": {"name": "Loi 36-15", "key": "MAR-L1", "url": None, "language": "fr"}}


def test_law_answer_rewrites_once_then_falls_back_to_clauses():
    clauses = [{"id": "r1", "key": "MAR-L1-72", "label": "Article 72", "text": "La valeur limite de la DBO5 est de 30 mg/l.", "text_zh": "BOD5 限值 30 毫克/升。",
                "page": 3, "seq": 72, "law": "L1"}]
    plan = {"keywords": ["BOD5"]}
    clf = _Clf([{"answer": "BOD5 限值 25 mg/L [1]", "used": [1]}, {"answer": "BOD5 限值 30 mg/L [1]", "used": [1]}])
    r = law.answer(_Lib(clauses), "摩洛哥 BOD5 限值", plan, ["MAR"], clf, log=lambda *a: None)
    assert r["answer"] == "BOD5 限值 30 mg/L [1]" and r["checked"] == {"ok": True} and len(clf.calls) == 2
    assert "25" in clf.calls[1]["要改"]  # 重写时告诉模型哪个数找不到
    clf = _Clf([{"answer": "BOD5 限值 25 mg/L [1]", "used": [1]}, {"answer": "BOD5 限值 20 mg/L [1]", "used": [1]}])
    r = law.answer(_Lib(clauses), "摩洛哥 BOD5 限值", plan, ["MAR"], clf, log=lambda *a: None)
    assert r["answer"] == "" and r["checked"] == {"ok": False, "numbers": ["25"]} and "没有采用" in r["note"] and r["cites"]  # 不用这段回答,只列条文


def test_compare_ignores_dates_and_names_each_difference():
    p = {"understood": [["对象", "线索"], ["国别", "摩洛哥"], ["时间", "2026-10-01 至 2026-10-15"], ["呈现", "清单(最多 50 条)"]]}
    assert ask_log.compare("对象:线索\n国别:摩洛哥\n时间:2026-09-01 至 2026-09-30\n呈现:清单(最多 50 条)", p) == []
    assert ask_log.compare("对象:已中标项目\n国别:摩洛哥\n领域:污水", p) == ["对象 应为「已中标项目」,现在是「线索」", "少了「领域:污水」", "多了「呈现:清单(最多 50 条)」"]


def _base():
    from tests.test_portal import _fake_with_schema
    fake, admin = _fake_with_schema()
    return fake, tstore.Base(tstore.Client("https://water.example", "bot-token", transport=fake.transport, sleep=lambda s: None))


def test_record_feedback_and_daily_recheck():
    """记一问(A44);答对了抄理解为、答错了记待看(A45);每天复查(A46):应理解为不空、没标不改的重问,对不上的写进复查;接着问的先问上一问。"""
    from datetime import datetime
    _, b = _base()
    now = datetime(2026, 10, 15, 10, 30)
    p1 = A.plan("摩洛哥有哪些污水项目", TODAY)
    ask_log.record(b, "Q1", "摩洛哥有哪些污水项目", "", p1, "管理员 <a@x>", now)
    p2 = A.plan("近三年摩洛哥运营维护的中标", TODAY)
    ask_log.record(b, "Q2", "近三年摩洛哥运营维护的中标", "", p2, "", now)
    prev = {"q": "近三年摩洛哥运营维护的中标", "object": "award", "filters": p2["filters"], "measures": [], "group_by": None}
    p3 = A.plan("那吉尔吉斯斯坦呢", TODAY, None, prev)
    ask_log.record(b, "Q3", "那吉尔吉斯斯坦呢", "近三年摩洛哥运营维护的中标", p3, "", now)
    rows = {b.get("ask_log", r, "key"): r for r in b.rows("ask_log")}
    assert set(rows) == {"Q1", "Q2", "Q3"} and b.get("ask_log", rows["Q1"], "asker") == "管理员 <a@x>"
    assert b.get("ask_log", rows["Q1"], "understood").startswith("对象:线索\n国别:摩洛哥\n领域:污水") and b.get("ask_log", rows["Q1"], "parsed_by") == "词表"
    ask_log.feedback(b, "Q1", "答错了", "我问的是已中标项目", "列了 3 条线索")
    ask_log.feedback(b, "Q2", "答对了")
    ask_log.feedback(b, "Q3", "答对了")
    rows = {b.get("ask_log", r, "key"): r for r in b.rows("ask_log")}
    assert (b.get("ask_log", rows["Q1"], "verdict"), b.get("ask_log", rows["Q1"], "status"), b.get("ask_log", rows["Q1"], "note")) == ("答错了", "待看", "我问的是已中标项目")
    assert not b.get("ask_log", rows["Q1"], "expected") and b.get("ask_log", rows["Q1"], "result") == "列了 3 条线索"
    assert b.get("ask_log", rows["Q2"], "expected") == b.get("ask_log", rows["Q2"], "understood") and not b.get("ask_log", rows["Q2"], "status")
    # 有人把 Q1 改好了(这里模拟:应理解为写成已中标项目,程序还没改)→ 复查不通过;Q2、Q3 照旧通过
    b.update("ask_log", rows["Q1"]["id"], {"expected": "对象:已中标项目\n国别:摩洛哥\n类别:污水\n呈现:清单(最多 50 条)", "status": "已修好"})
    out = ask_log.recheck(b, TODAY, None, log=lambda *a: None)
    rows = {b.get("ask_log", r, "key"): r for r in b.rows("ask_log")}
    assert out == {"rechecked": 3, "failed": 1}
    assert b.get("ask_log", rows["Q1"], "recheck").startswith("不通过(10-15):对象 应为「已中标项目」,现在是「线索」")
    assert b.get("ask_log", rows["Q2"], "recheck") == "通过(10-15)" and b.get("ask_log", rows["Q3"], "recheck") == "通过(10-15)"  # 接着问的先问了上一问
    b.update("ask_log", rows["Q1"]["id"], {"status": "不改"})
    assert ask_log.recheck(b, TODAY, None, log=lambda *a: None) == {"rechecked": 2, "failed": 0}
    try:
        ask_log.feedback(b, "Q2", "还行")
        raise AssertionError("只认答对了、答错了")
    except ValueError:
        pass
    # 记行的线程还没写完、或没记上:按页面带来的提问补一行
    ask_log.feedback(b, "Q9", "答错了", info={"q": "刚果有哪些线索", "understood": "对象:线索\n国别:刚果(布)"}, wait=0)
    r9 = ask_log.find(b, "Q9")
    assert b.get("ask_log", r9, "question") == "刚果有哪些线索" and b.get("ask_log", r9, "status") == "待看"


def test_server_logs_asks_and_takes_feedback_but_not_from_outsiders():
    """/ask 返回这一行的编号并另起线程记下(登录后经 Caddy 来的;游客不记);/feedback 按编号记反馈;表没建时不给编号。"""
    from core.assistant_server import Assistant, serve
    fake, b = _base()
    bot = Assistant(None)
    bot.terms_base = lambda: tstore.Base(tstore.Client("https://water.example", "bot-token", transport=fake.transport, sleep=lambda s: None))
    bot.who = lambda cookie: "管理员 <admin@example.org>"
    srv = serve(bot, 0, host="127.0.0.1")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}"
    h = {"X-Portal-Auth": "1", "Content-Type": "application/json", "Cookie": "auth_session=admin-token"}
    try:
        r = httpx.post(url + "/ask", json={"q": "摩洛哥有哪些污水项目"}, headers=h).json()
        key = r["log_key"]
        assert key.startswith("Q") and r["ask_back"][0]["id"] == "project_object"
        f = httpx.post(url + "/feedback", json={"key": key, "verdict": "答错了", "note": "要已中标项目", "result": "0 条", "q": "摩洛哥有哪些污水项目"}, headers=h).json()
        assert f == {"ok": True}
        rec = ask_log.find(b, key)
        assert b.get("ask_log", rec, "question") == "摩洛哥有哪些污水项目" and b.get("ask_log", rec, "verdict") == "答错了" and b.get("ask_log", rec, "asker") == "管理员 <admin@example.org>"
        assert httpx.post(url + "/feedback", json={"key": key, "verdict": "随便"}, headers=h).json()["ok"] is False
        assert httpx.post(url + "/feedback", json={"key": key, "verdict": "答对了"}, headers={"Content-Type": "application/json"}).status_code == 403  # 没经过登录
        n = len(b.rows("ask_log"))
        bot.guest = type("G", (), {"is_outsider": staticmethod(lambda cookie: True)})()  # 游客:不记、不能反馈
        assert "log_key" not in httpx.post(url + "/ask", json={"q": "摩洛哥的线索"}, headers=h).json()
        assert httpx.post(url + "/feedback", json={"key": key, "verdict": "答对了"}, headers=h).status_code == 403
        import time
        time.sleep(0.5)
        assert len(b.rows("ask_log")) == n
    finally:
        srv.shutdown()
    # 表没建:不给编号(页面也就不显示答对答错)
    bot2 = Assistant(None)
    bot2.terms_base = lambda: type("B", (), {"t": {}})()
    assert bot2.log_ask("摩洛哥的线索", None, A.plan("摩洛哥的线索", TODAY), None) is None


def test_portal_ask_back_loosening_and_feedback():
    """首页:有歧义的给另几种理解(点了按改写的问题重问);0 条时去掉一个条件再查并写明;答案下面答对了、答错了记进表。
    手机宽度下这几块不撑出横向滚动。"""
    import pytest
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
            done = "(n) => { const a = document.querySelectorAll('.answer'); return a.length >= n && !a[n - 1].innerText.includes('解析中') && !a[n - 1].innerText.includes('查表中') && a[n - 1].querySelector('.a-fb'); }"
            # 「项目」没说清:照线索答,上面给「已中标项目」「我们跟踪的商机」两个按钮
            page.goto(base + "/index.html?q=" + quote("阿塞拜疆有哪些项目"))
            page.wait_for_function(done, arg=1, timeout=15000)
            a = page.locator("#a1")
            note = a.locator(".ask-back").inner_text()
            assert "「项目」可以指招标线索、已中标项目或我们跟踪的商机,这次按招标线索算的" in note and "已中标项目" in note and "我们跟踪的商机" in note
            assert a.locator(".a-body tbody tr").count() == 3
            a.locator(".ask-back button", has_text="我们跟踪的商机").click()
            page.wait_for_function(done, arg=2, timeout=15000)
            a2 = page.locator("#a2")
            assert a2.locator(".a-q").inner_text() == "阿塞拜疆有哪些项目(只看商机)" and "对象:商机" in a2.locator(".understood").inner_text() and a2.locator(".ask-back").count() == 0
            # 0 条:去掉一个条件(这里是关键词)再查,上面写明
            page.fill("#q", "阿塞拜疆标题含「不存在的词」的线索")
            page.press("#q", "Enter")
            page.wait_for_function(done, arg=3, timeout=15000)
            a3 = page.locator(".answer").nth(2)
            assert a3.locator(".loosened").inner_text() == "按你说的条件查到 0 条。去掉「标题或摘要含:不存在的词」后有 3 条,下面列的是这些。"
            assert a3.locator(".a-body tbody tr").count() == 3
            # 国别不去:查不到也直说
            page.fill("#q", "乌兹别克斯坦的线索")
            page.press("#q", "Enter")
            page.wait_for_function(done, arg=4, timeout=15000)
            assert "每次去掉一个条件(国别不去)也都是 0 条" in page.locator(".answer").nth(3).locator(".loosened").inner_text()
            # 答错了:写一句、提交;答对了:一点就记
            a.locator(".a-fb button", has_text="答错了").click()
            a.locator(".a-fb textarea").fill("我想看已中标项目")
            a.locator(".a-fb button", has_text="提交").click()
            a.locator(".a-fb", has_text="下面按你的说明重答").wait_for(timeout=15000)
            page.wait_for_function("() => { const r = document.querySelector('.answer.refix'); return r && r.querySelector('.a-fb'); }", timeout=15000)
            assert "模型现在用不了,没法按你的说明重新理解" in page.locator(".answer.refix").inner_text()  # 没配模型:照旧进错题本
            a2.locator(".a-fb button", has_text="答对了").click()
            a2.locator(".a-fb", has_text="记下了").wait_for(timeout=15000)
            rows = {b.get("ask_log", r, "question"): r for r in reversed(b.rows("ask_log"))}  # 同一句(重答)取先问的那行
            r1, r2 = rows["阿塞拜疆有哪些项目"], rows["阿塞拜疆有哪些项目(只看商机)"]
            assert (b.get("ask_log", r1, "verdict"), b.get("ask_log", r1, "status"), b.get("ask_log", r1, "note")) == ("答错了", "待看", "我想看已中标项目")
            assert "Ganja WWTP EOI" in b.get("ask_log", r1, "result") and b.get("ask_log", r1, "asker") == "管理员 <admin@example.org>"
            assert b.get("ask_log", r2, "verdict") == "答对了" and b.get("ask_log", r2, "expected").startswith("对象:商机")
            assert len(b.rows("ask_log")) == 5  # 四问 + 一次重答
            if os.environ.get("PORTAL_SHOTS"):
                for i, nm in ((0, "助手_歧义与反馈"), (2, "助手_零条放宽")):
                    page.locator(".answer").nth(i).screenshot(path=str(Path(os.environ["PORTAL_SHOTS"]) / f"{nm}.png"))
            # 手机:说明、按钮、反馈条都在屏幕里
            page.set_viewport_size({"width": 390, "height": 800})
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
            for sel in (".ask-back", ".loosened", ".a-fb"):
                box = page.locator(sel).first.bounding_box()
                assert box and box["x"] >= 0 and box["x"] + box["width"] <= 391, sel
            if os.environ.get("PORTAL_SHOTS"):
                page.locator(".answer").nth(0).scroll_into_view_if_needed()
                page.screenshot(path=str(Path(os.environ["PORTAL_SHOTS"]) / "助手_歧义与反馈_手机.png"), animations="disabled")
            # 本体页「语义层」:有歧义的说法、错题本(待看 1 句)
            page.set_viewport_size({"width": 1366, "height": 900})
            page.goto(base + "/ontology.html?t=sem")
            page.wait_for_function("document.querySelector('#semLog') && !document.querySelector('#semLog').innerText.includes('读取中')", timeout=15000)
            assert "「刚果」是哪一国" in page.inner_text("#semAmb") and "往前推满 N 年" in page.inner_text("#semAmb")
            log = page.inner_text("#semLog")
            if os.environ.get("PORTAL_SHOTS"):
                page.locator("#semAmb").screenshot(path=str(Path(os.environ["PORTAL_SHOTS"]) / "本体_有歧义的说法.png"))
                page.locator("#semLog").screenshot(path=str(Path(os.environ["PORTAL_SHOTS"]) / "本体_错题本.png"))
            assert "待看 1 句" in log and "阿塞拜疆有哪些项目" in log and "我想看已中标项目" in log
            assert not errors, errors
            browser.close()
    finally:
        srv.shutdown()
