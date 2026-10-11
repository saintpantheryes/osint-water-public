"""助手「看一条链接」(core/link_check.py 与 POST /link):认网址、网站类型、只抓公网、库里找同一网址与同一项目、
模型分析归一、入库建议与要写的值;游客不能用。用内存版的 Teable、假网页、假模型,不联网。"""
from __future__ import annotations

import json
import threading
from datetime import date

import httpx
import pytest

from core import link_check as L
from core import ontology, schema, tstore
from core.assistant_server import Assistant, serve
from core.watch.fetch import Page
from core.watch.llm import Classifier, DeepSeekClient, LLMConfig
from tests.teable_fake import FakeTeable

ONTO, TMAP = ontology.load(), ontology.load_map()
TODAY = date(2026, 10, 9)
URL = "https://expo.example.uz/news/new-wwtp-tashkent"
HTML = ("<html><head><title>New treatment plant for Tashkent</title><meta property='article:published_time' content='2023-04-27T10:00:00Z'></head>"
        "<body><h1>Construction of a new treatment plant for Tashkent planned for early 2024</h1><p>Abu Dhabi Sewerage Services Company (ADSSC) "
        "plans to build a wastewater treatment plant in Surum mahalla, Kuyichirchik district, Tashkent region, Uzbekistan, 1.5 million m3 per day in two phases. "
        "New Tashkent will also be connected. Source: gazeta.uz</p></body></html>").encode()
MODEL = {"title_zh": "塔什干拟于2024年初新建污水处理厂", "summary_zh": "阿布扎比 ADSSC 拟在塔什干州 Surum 建污水处理厂,两期日处理150万立方米。",
         "project_name": "塔什干苏鲁姆污水处理厂", "names": ["Surum", "Kuyichirchik", "ADSSC", "UZ"], "country": "UZB", "city": "Tashkent region",
         "funder": "ADSSC(阿联酋)", "amount": "1.5 million m3/day", "info_date": "2023-04-27", "stage": "立项或规划", "category": "立项或规划",
         "sectors": ["污水", "火箭"], "credibility": {"level": "中", "reason": "展会网站转载 gazeta.uz 的报道"},
         "freshness": "2023 年的信息,需查是否已开工", "mismatch": "原文出资方是阿布扎比(阿联酋),不是沙特", "news_query": '"Surum" Tashkent wastewater',
         "advice": {"action": "入库", "reason": "规模大、与我方业务相关"}, "verify": ["出资方", "是否已开工", "招标方式", "多余的一条"]}


def _page(url=URL, body=HTML):
    return Page(url, 200, body, "text/html; charset=utf-8")


def _clf(reply, seen):
    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={"model": "deepseek-flash", "usage": {}, "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(reply, ensure_ascii=False)}}]})
    return Classifier(LLMConfig.for_assistant({"provider": "deepseek"}, None), client=DeepSeekClient("sk-test", transport=httpx.MockTransport(handler), sleep=lambda s: None))


@pytest.fixture
def db():
    fake = FakeTeable()
    admin = tstore.Client("https://water.example", "admin-token", transport=fake.transport, sleep=lambda s: None)
    schema.apply(admin, schema.build_plan(ONTO, TMAP, {}), "水务项目机会 新版", log=lambda *a: None)
    b = tstore.Base(tstore.Client("https://water.example", "bot-token", transport=fake.transport, sleep=lambda s: None))
    uz = b.create("country", [{"name_zh": "乌兹别克斯坦", "ISO3": "UZB"}])[0]
    eg = b.create("country", [{"name_zh": "埃及", "ISO3": "EGY"}])[0]
    b.create("source", [{"name": "Gazeta", "url": "https://www.gazeta.uz/ru/"}])
    b.create("lead", [{"title": "Surum WWTP feasibility", "url": "https://other.example/a", "country": uz["id"], "summary_zh": "苏鲁姆污水厂可研"},
                      {"title": "Surum pumping station", "url": "https://other.example/b", "country": eg["id"]},  # 别国的同名词:不算
                      {"title": "Bukhara water supply", "url": "https://other.example/c", "country": uz["id"]}])
    b.create("opportunity", [{"code": "UZB-001", "name_zh": "塔什干 Surum 污水厂", "country": uz["id"]}])
    return b


def test_find_url_and_source_kind():
    assert L.find_url("看看这个 https://a.example/x?y=1,沙特资本") == "https://a.example/x?y=1"
    assert L.find_url("https://a.example/x。") == "https://a.example/x" and L.find_url("没有网址") is None
    kind = lambda u, k=None: L.source_kind(u, k)["kind"]  # noqa: E731
    assert kind("https://projects.worldbank.org/p") == "开发银行与国际组织" and kind("https://www.minvodxoz.gov.uz/") == "官方"
    assert kind("https://www.mlit.go.jp/") == "官方" and kind("https://go.com/") == "其他网站" and kind("https://www.gazeta.uz/x") == "新闻媒体"
    s = L.source_kind("https://expo.example.uz/x")
    assert s["kind"] == "其他网站" and s["level"] == "待核实"
    assert L.source_kind("https://news.gazeta.uz/x", {"gazeta.uz": "Gazeta"})["known"] == "Gazeta"


def test_only_public_hosts_are_fetched():
    for host in ("127.0.0.1", "10.0.0.8", ".".join(["169", "254"] * 2), "localhost", "::1"):  # 第三个是云主机的元数据地址
        with pytest.raises(L.LinkError):
            L._host_ok(host)
    hops = []

    def handler(req):
        hops.append(req.url.host)
        if req.url.host == "pub.example":
            return httpx.Response(302, headers={"Location": "http://inside.example/secret"})
        return httpx.Response(200, content=b"secret")

    def check(host):
        if host == "inside.example":
            raise L.LinkError("只能看公网上的网页")
    with pytest.raises(L.LinkError, match="公网"):
        L.fetch_page("https://pub.example/x", transport=httpx.MockTransport(handler), check_host=check)
    assert hops == ["pub.example"]  # 跳到内网的那一跳没有发出去
    ok = L.fetch_page("https://inside2.example/x", transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"<p>hi</p>")), check_host=lambda h: None)
    assert ok.content == b"<p>hi</p>"
    with pytest.raises(L.LinkError, match="403"):
        L.fetch_page("https://x.example/", transport=httpx.MockTransport(lambda r: httpx.Response(403)), check_host=lambda h: None)


def test_check_with_model_library_and_outside(db):
    seen: list = []
    out = L.check(URL, "沙特资本投资", TODAY, _clf(MODEL, seen), db, fetch=lambda u: _page(),
                  outside=lambda q, iso: ([{"kind": "新闻(近三个月)", "title": "x", "url": "https://n.example", "date": "2026-09-01"}], []) if q and iso == "UZB" else ([], []))
    msg = seen[0]["messages"]
    assert "用户附言:沙特资本投资" in msg[1]["content"] and "Surum mahalla" in msg[1]["content"] and "今天是 2026-10-09" in msg[0]["content"]
    a = out["analysis"]
    assert a["names"] == ["Surum", "Kuyichirchik", "ADSSC"] and a["sectors"] == ["污水"] and len(a["verify"]) == 3  # 太短的词、不认识的领域、多余的要点去掉
    assert out["country"] == {"iso3": "UZB", "name": "乌兹别克斯坦"} and out["source"]["kind"] == "其他网站" and out["published"] == "2023-04-27"
    assert [x["title"] for x in out["related_leads"]] == ["Surum WWTP feasibility"] and out["related_opps"][0]["code"] == "UZB-001"
    assert out["same_url"] == [] and out["outside"][0]["url"] == "https://n.example"
    assert out["advice"]["action"] == "入库" and out["advice"]["reason"] == "规模大、与我方业务相关。库里已有 1 条相关线索,入库后请挂到同一商机。"
    d = out["draft"]
    assert d["url"] == URL and d["origin"] == "用户上传" and d["country_iso3"] == "UZB" and d["project_name"] == "塔什干苏鲁姆污水处理厂"
    assert d["published"] == "2023-04-27" and d["sectors"] == ["污水"] and "与附言不符" in d["note"] and "用户附言:沙特资本投资" in d["note"]
    # 网址已在库:建议「已在库」
    db.create("lead", [{"title": "已有的", "url": URL}])
    out = L.check(URL, "", TODAY, _clf(MODEL, seen), db, fetch=lambda u: _page(), outside=lambda q, iso: ([], []))
    assert out["advice"]["action"] == "已在库" and out["same_url"][0]["title"] == "已有的"


def test_check_without_model_and_bad_pages(db):
    out = L.check(URL, "", TODAY, None, db, fetch=lambda u: _page(), outside=lambda q, iso: ([], []))
    assert out["analysis"] is None and out["advice"]["action"] == "先核实" and "没有配置模型" in out["notes"][0]
    assert out["country"]["iso3"] == "UZB" and out["related_leads"] == [] and out["title"].startswith("Construction of a new treatment plant")
    with pytest.raises(L.LinkError, match="正文"):
        L.check(URL, "", TODAY, None, db, fetch=lambda u: _page(body=b"<html></html>"))
    with pytest.raises(L.LinkError, match="网址"):
        L.check("ftp://x", "", TODAY, None, db)
    # 模型出错:照样给网站类型和库里比对
    bad = _clf("x", [])
    bad.cfg.max_calls = 0
    out = L.check(URL, "", TODAY, bad, None, fetch=lambda u: _page(), outside=lambda q, iso: ([], []))
    assert out["analysis"] is None and "模型没有给出分析" in out["notes"][0]


def test_pasted_text_stands_in_when_the_site_refuses(db):
    """网站不让程序读(403)或读不到正文时:链接后面一起贴来的正文照样能看;没贴正文还是报原因,提示一起贴。"""
    def refuse(u):
        raise L.LinkError("网站回了 403,可能不让程序访问;可以把链接和正文一起贴给我")
    text = ("Invitation for Bids. Loan 4511-LAO: Water Supply and Sanitation Improvement Sector Project. "
            "CW-02 Construction of Water Supply System in Anouvong District, Xaisomboun Province. "
            "Deadline for Submission of Bids: 18 November 2026. The Lao People's Democratic Republic has received financing from ADB.")
    out = L.check("https://www.adb.org/node/1181131", text, TODAY, None, db, fetch=refuse, outside=lambda q, iso: ([], []))
    assert out["ok"] and out["source"]["label"] == "亚洲开发银行" and out["title"].startswith("Invitation for Bids")
    assert out["country"]["iso3"] == "LAO" and "按你贴的正文" in out["notes"][0]
    with pytest.raises(L.LinkError, match="一起贴"):
        L.check("https://www.adb.org/node/1181131", "看看这个", TODAY, None, db, fetch=refuse)
    out = L.check(URL, text, TODAY, None, db, fetch=lambda u: _page(body=b"<html></html>"), outside=lambda q, iso: ([], []))
    assert "按你贴的正文" in out["notes"][0]


def test_link_endpoint(db):
    bot = Assistant(None)
    bot.terms_base = lambda: db
    bot.link_kw = {"fetch": lambda u: _page(), "outside": lambda q, iso: ([], [])}
    srv = serve(bot, 0, host="127.0.0.1")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{srv.server_port}"
        assert httpx.post(base + "/link", json={"url": URL}).status_code == 403  # 没经过登录检查
        h = {"X-Portal-Auth": "1"}
        r = httpx.post(base + "/link", json={"url": URL, "note": "沙特"}, headers=h).json()
        assert r["ok"] and r["source"]["kind"] == "其他网站" and r["advice"]["action"] == "先核实"
        assert httpx.post(base + "/link", json={"url": "不是网址"}, headers=h).json() == {"ok": False, "error": "这不是一个完整的网址"}

        class Outsider:
            def is_outsider(self, cookie): return True
        bot.guest = Outsider()
        r = httpx.post(base + "/link", json={"url": URL}, headers=h).json()
        assert r["ok"] is False and "游客" in r["error"]
    finally:
        srv.shutdown()
