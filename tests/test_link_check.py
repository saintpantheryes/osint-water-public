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


def _text_image(lines, fmt="PNG"):
    import io

    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (1400, 90 * len(lines) + 60), "white")
    d = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=40)
    for i, ln in enumerate(lines):
        d.text((40, 30 + 90 * i), ln, fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, fmt)
    return buf.getvalue()


ADB_LINES = ["Invitation for Bids", "Water Supply and Sanitation Improvement Project", "Lao People's Democratic Republic",
             "Deadline for Submission of Bids: 18 November 2026"]


def test_uploaded_file_stands_in_for_the_page(db, monkeypatch):
    """网站不让程序读时,用户传来的 PDF、Word、截图照样能看:有文件就不去抓网页;没给网址时从文件里找,找不到也能看。"""
    from core import file_text as F

    def never(u):
        raise AssertionError("有文件时不该去抓网页")
    pdf = b"%PDF-1.4 fake"
    monkeypatch.setattr(F, "pdf_text", lambda data: ("\n".join(ADB_LINES + ["See https://www.adb.org/node/1181131 for details."]), 1))
    out = L.check("", "这个能跟吗", TODAY, None, db, fetch=never, outside=lambda q, iso: ([], []), upload=pdf)
    assert out["ok"] and out["url"] == "https://www.adb.org/node/1181131" and out["source"]["label"] == "亚洲开发银行"
    assert out["country"]["iso3"] == "LAO" and out["notes"][0] == "按你上传的文件分析"
    monkeypatch.setattr(F, "pdf_text", lambda data: ("\n".join(ADB_LINES), 1))
    out = L.check("", "", TODAY, None, db, fetch=never, outside=lambda q, iso: ([], []), upload=pdf)  # 文件里没有网址
    assert out["ok"] and out["url"] == "" and out["source"]["level"] == "待核实" and out["draft"]["url"] == ""
    with pytest.raises(L.LinkError, match="只能看 PDF"):
        L.check("", "", TODAY, None, db, fetch=never, upload=b"hello world")
    with pytest.raises(L.LinkError, match="太大"):
        L.check("", "", TODAY, None, db, fetch=never, upload=b"%PDF-" + b"0" * F.MAX_BYTES)


@pytest.mark.skipif(not __import__("shutil").which("tesseract"), reason="本机没装 tesseract")
def test_screenshot_and_scanned_pdf_are_read_by_ocr():
    from core.file_text import FileError, file_text
    text, how = file_text(_text_image(ADB_LINES))
    assert "Water Supply" in text and "18 November 2026" in text and "文字识别" in how
    text, how = file_text(_text_image(ADB_LINES, "PDF"))  # 只有图片的 PDF = 扫描件
    assert "Water Supply" in text and "扫描件" in how
    with pytest.raises(FileError, match="没读到文字"):
        file_text(_text_image([""]))


def test_link_endpoint_takes_a_file(db, monkeypatch):
    import base64

    from core import file_text as F
    monkeypatch.setattr(F, "pdf_text", lambda data: ("\n".join(ADB_LINES), 1))
    bot = Assistant(None)
    bot.terms_base = lambda: db
    bot.link_kw = {"fetch": lambda u: (_ for _ in ()).throw(AssertionError()), "outside": lambda q, iso: ([], [])}
    srv = serve(bot, 0, host="127.0.0.1")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base, h = f"http://127.0.0.1:{srv.server_port}", {"X-Portal-Auth": "1"}
        r = httpx.post(base + "/link", json={"url": "https://www.adb.org/node/1181131", "file": base64.b64encode(b"%PDF-1.4").decode()},
                       headers=h).json()
        assert r["ok"] and r["country"]["iso3"] == "LAO" and "上传的文件" in r["notes"][0]
        assert httpx.post(base + "/link", json={"file": "不是base64"}, headers=h).json()["error"].startswith("文件没传完整")
    finally:
        srv.shutdown()


def test_link_runs_as_a_job_the_page_polls(db, monkeypatch):
    """2026-10-16 使用方在 iPhone 上传 PDF 报「读不到数据:Load failed」:模型加检索要近一分钟,手机浏览器一条请求 60 秒就断。
    页面带 job: true 先拿编号,后台线程算;GET /link/{编号} 给阶段和已过秒数,算完给结果;编号不认识说明原因;没登录 403。"""
    import base64
    import time

    from core import file_text as F
    monkeypatch.setattr(F, "pdf_text", lambda data: ("\n".join(ADB_LINES), 1))
    gate = threading.Event()

    def slow_outside(q, iso):
        gate.wait(5)
        return [], ["新闻检索没做成(假)"]
    bot = Assistant(None)
    bot.terms_base = lambda: db
    bot.link_kw = {"fetch": lambda u: (_ for _ in ()).throw(AssertionError()), "outside": slow_outside}
    srv = serve(bot, 0, host="127.0.0.1")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base, h = f"http://127.0.0.1:{srv.server_port}", {"X-Portal-Auth": "1"}
        r = httpx.post(base + "/link", json={"url": "https://www.adb.org/node/1181131", "job": True,
                                           "file": base64.b64encode(b"%PDF-1.4").decode()}, headers=h).json()
        assert r["ok"] and r["job"] and r["stage"] in ("读文件", "读原文", "模型分析", "查库里相关", "查公开渠道")
        assert httpx.get(base + "/link/" + r["job"]).status_code == 403  # 没经过登录检查
        time.sleep(0.3)
        st = httpx.get(base + "/link/" + r["job"], headers=h).json()
        assert st == {"ok": True, "done": False, "stage": "查公开渠道", "elapsed": st["elapsed"]} and st["elapsed"] >= 0
        gate.set()
        for _ in range(50):
            st = httpx.get(base + "/link/" + r["job"], headers=h).json()
            if st["done"]:
                break
            time.sleep(0.1)
        assert st["done"] and st["ok"] and st["country"]["iso3"] == "LAO" and "上传的文件" in st["notes"][0] and "新闻检索没做成(假)" in st["notes"]
        bad = httpx.get(base + "/link/nope", headers=h).json()
        assert bad["ok"] is False and bad["done"] and "重新提问" in bad["error"]
        # 文件读不了(不是 PDF、Word、图片)也走任务:结果里是原因,不是 500
        r = httpx.post(base + "/link", json={"job": True, "file": base64.b64encode(b"hello world").decode()}, headers=h).json()
        for _ in range(50):
            st = httpx.get(base + "/link/" + r["job"], headers=h).json()
            if st["done"]:
                break
            time.sleep(0.05)
        assert st["ok"] is False and "只能看 PDF" in st["error"]
    finally:
        gate.set()
        srv.shutdown()


def test_link_jobs_expire_and_are_capped():
    bot = Assistant(None)
    bot.link_kw = {"fetch": lambda u: _page(), "outside": lambda q, iso: ([], [])}
    ids = [bot.start_link_job(URL) for _ in range(3)]
    for j in ids:
        for _ in range(100):
            if bot.job_state(j)["done"]:
                break
            __import__("time").sleep(0.02)
    assert all(bot.job_state(j)["ok"] for j in ids)
    bot.jobs[ids[0]]["at"] -= 10_000  # 半小时前的:下次起任务时清掉
    bot.start_link_job(URL)
    assert "重新提问" in bot.job_state(ids[0])["error"] and bot.job_state(ids[1])["done"]


ITEMS = [
    {"ref": "NRWWP-7", "title": "NRWWP-7 Construction and Rehabilitation of Wastewater Networks", "title_zh": "纳曼干污水管网建设与修复", "country": "UZB",
     "funder": "World Bank", "amount": "12.3 million USD", "stage": "招标", "deadline": "2026-11-20", "url": None, "query": "NRWWP-7 Namangan wastewater", "names": ["Namangan", "NRWWP"],
     "sectors": ["污水"]},
    {"ref": "CAWRMP-ICW-08", "title": "Modernization of Kegeyli main canal — structures", "title_zh": "克盖利干渠现代化改造", "country": "UZB", "funder": "World Bank loan 4207-UZB",
     "amount": "9.6 million USD (est.)", "stage": "招标", "deadline": "2026-10-19", "url": None, "query": "CAWRMP-ICW-08 Kegeyli canal", "names": ["Kegeyli", "CAWRMP"], "sectors": ["水利"],
     "requirements": {"similar_usd": 8000000, "turnover_usd": 10000000, "financial_usd": 1250000, "years": 3, "specific": "灌溉渠网土方不少于 150 000 m³"}},
    {"ref": "SDP/RFB/05", "title": "Kitab wastewater network construction", "title_zh": "基塔布市污水管网建设", "country": "UZB", "funder": "AFD", "amount": "12.85 million USD (est.)",
     "stage": "招标", "deadline": "2026-12-01", "url": None, "query": "SDP/RFB/05 Kitab sewerage", "names": ["Kitab", "Kashkadarya"], "sectors": ["污水"],
     "requirements": {"similar_usd": 12000000, "turnover_usd": None, "financial_usd": None, "years": 5, "specific": None}},
    {"ref": None, "title": "Something with a link", "title_zh": "材料里自带网址的一条", "country": None, "funder": None, "amount": None, "stage": None, "deadline": None,
     "url": "https://www.adb.org/node/1181131", "query": None, "names": ["Anouvong"], "sectors": []},
    {"ref": "SLSDP/RFB/16", "title": "Veterinary clinic and animal house, Nukus branch", "title_zh": "努库斯分校兽医诊所及动物饲养楼建设", "country": "UZB", "funder": "IDA",
     "amount": "0.6 million USD", "stage": "招标", "deadline": "2026-10-14", "url": None, "query": "SLSDP/RFB/16 veterinary clinic", "names": ["SLSDP", "Veterinary"], "sectors": ["其他"],
     "requirements": {"similar_usd": 500000, "turnover_usd": 500000, "financial_usd": 84000, "years": 5, "specific": None}},
    {"ref": "", "title": "", "title_zh": "", "country": None, "funder": None, "amount": None, "stage": None, "deadline": None, "url": None, "query": None, "names": []},  # 空的丢掉
]
WB_HITS = {
    "CAWRMP-ICW-08 Kegeyli canal": [{"kind": "世行采购公告", "title": "Request for Bids:CAWRMP-ICW-08 Modernization of Kegeyli Main Canal", "url": "https://projects.worldbank.org/notice/1",
                                     "date": "2026-10-01", "deadline": "2026-10-19", "text": "Loan 4207-UZB … CAWRMP-ICW-08"}],
    "SDP/RFB/05 Kitab sewerage": [{"kind": "世行项目(Active)", "title": "P123 Kashkadarya Water Supply", "url": "https://projects.worldbank.org/p/P123", "date": "2024-01-01",
                                   "deadline": None, "text": "Kashkadarya region water supply"}],  # 名字沾边、编号对不上:只算候选
}
CHECKED = {"original": None, "sure": False, "candidates": [], "wb_scanned": 480, "wb_since": "2026-02-11", "funder": "法开署", "notes": [],
           "news": [{"kind": "新闻(近三个月)", "title": "Kitab sewerage works to start", "url": "https://news.example/kitab", "date": "2026-10-01"}],
           "links": [{"name": "到法开署招标页找", "url": "https://afd.dgmarket.com/tenders/list.do?keywords=SDP%2FRFB%2F05"},
                     {"name": "到世行采购网找", "url": "https://projects.worldbank.org/en/projects-operations/procurement?srce=both&qterm=SDP%2FRFB%2F05"}]}


def _verify(item, iso):
    assert item["ref"] == "SDP/RFB/05" and iso == "UZB"  # 只有没找到原公告的那条才再核
    return CHECKED


def test_list_file_is_checked_item_by_item(db, monkeypatch):
    """2026-10-16 使用方:同事发来的、像是 AI 整理的商机清单——助手逐条找原公告、比对库里有没有:有的说已在库,没有的给入库建议、由人决定。
    编号对得上才算同一条(同项目别的标段只列作相关);找到原公告的按原公告入库(网址、日期取原公告);只有名字沾边的算先核实;
    材料自带网址的那条按那个网址;空条目丢掉;清单型不做整篇的旁证检索。"""
    from core import file_text as F
    uz = next(r for r in db.rows("country") if db.get("country", r, "key") == "UZB")
    db.create("lead", [{"title": "Namangan Regional Water and Wastewater: NRWWP-7 Construction and Rehabilitation of Wastewater Networks",
                        "url": "https://projects.worldbank.org/notice/7", "country": uz["id"]},
                       {"title": "Namangan Regional Water and Wastewater: NRWWP-8 Water Intakes", "url": "https://projects.worldbank.org/notice/8", "country": uz["id"]}])
    monkeypatch.setattr(F, "pdf_text", lambda data: ("招标项目监测 07.10.2026\n1 NRWWP-7 …\n2 CAWRMP-ICW-08 …\n3 SDP/RFB/05 …", 1))
    seen: list = []
    clf = _clf({**MODEL, "items": ITEMS}, seen)
    stages: list[str] = []
    asked: list = []

    def wb(query, iso):
        asked.append((query, iso))
        return WB_HITS.get(query, [])
    out = L.check("", "同事发来的清单", TODAY, clf, db, fetch=lambda u: (_ for _ in ()).throw(AssertionError()),
                  outside=lambda q, iso: (_ for _ in ()).throw(AssertionError("清单型不做整篇旁证")), upload=b"%PDF-1.4", find_original=wb,
                  progress=stages.append, verify=_verify)
    assert out["ok"] and out["advice"]["action"] == "逐条看" and out["draft"] is None and "清单型材料" in out["notes"][-1]
    lst = out["list"]
    assert lst["n"] == 5 and lst["counts"] == {"已在库": 1, "建议入库": 2, "先核实": 1, "非水务": 1}
    assert "1 条不是水务项目" in out["advice"]["reason"] and "由你勾选决定" not in out["advice"]["reason"]
    by = {it["ref"] or it["title"]: it for it in lst["items"]}
    a = by["NRWWP-7"]
    assert a["status"] == "已在库" and a["in_library"][0]["url"].endswith("/notice/7") and [r["title"][-6:] for r in a["related"]] == ["ntakes"]
    assert ("NRWWP-7 Namangan wastewater", "UZB") not in asked  # 已在库的不去找原公告
    b = by["CAWRMP-ICW-08"]
    assert b["status"] == "建议入库" and b["sure"] and b["original"]["url"].endswith("/notice/1") and b["country"]["name"] == "乌兹别克斯坦"
    assert b["draft"]["url"].endswith("/notice/1") and b["draft"]["title"].startswith("Request for Bids") and b["draft"]["published"] == "2026-10-01"
    assert b["draft"]["country_iso3"] == "UZB" and b["draft"]["origin"] == "用户上传" and "CAWRMP-ICW-08" in b["draft"]["note"] and "截止 2026-10-19" in b["draft"]["note"]
    c = by["SDP/RFB/05"]
    assert c["status"] == "先核实" and not c["sure"] and c["original"]["url"].endswith("/P123") and c["candidates"] and "编号对不上" in c["why"]
    assert "url" not in c["draft"] and c["draft"]["title"] == "Kitab wastewater network construction" and c["draft"]["funder"] == "AFD"
    # 2026-10-16 使用方:先核实的要助手自己核,不能只让人选——整国世行公告、新闻、出资方链接,结论写明
    assert c["checked"] == CHECKED and "最近 480 条公告(自 2026-02-11)里没有这个编号" in c["why"] and "新闻里有 1 条相关报道" in c["why"]
    assert "像是法开署的项目" in c["why"] and "到法开署招标页找" in c["why"] and "暂不入库" in c["why"]
    v = by["SLSDP/RFB/16"]  # 兽医诊所:跟水务无关,直接判,不查库不找公告
    assert v["status"] == "非水务" and v["why"] == "不是水务项目(领域:其他),不入库。" and v["draft"] is None and ("SLSDP/RFB/16 veterinary clinic", "UZB") not in asked
    d = by["Something with a link"]
    assert d["status"] == "建议入库" and d["original"]["url"] == "https://www.adb.org/node/1181131" and d["original"]["kind"] == "亚洲开发银行" and d["country"]["iso3"] == "UZB"  # 没写国别的用整篇的
    assert stages[0] == "读文件" and "找原公告 0/5" in stages and stages[-1] == "找原公告 5/5" and "模型分析" in stages
    assert '"items"' in "".join(m["content"] for m in seen[0]["messages"])  # 提示词里要了 items
    assert "清单" in L.GUIDE and "items" in L.SCHEMA["properties"] and "sectors" in L.SCHEMA["properties"]["items"]["items"]["properties"]


def test_verify_outside_scans_the_country_and_guesses_the_funder():
    """没找到原公告的一条:整国世行公告按正文比对(编号对上 = 原公告;只名字沾边 = 候选);没对上就查新闻、猜出资方、给检索链接。"""
    item = {"ref": "CAWRMP-ICW-07", "title": "R-8 canal structures", "funder": "World Bank loan 4207-UZB (COL)", "names": ["Kegeyli", "CAWRMP"], "query": "CAWRMP-ICW-07 canal"}
    scan_rows = [{"kind": "世行采购公告", "title": "Request for Bids:ICW-07 R-8 Main Canal", "url": "https://projects.worldbank.org/notice/77", "date": "2026-09-01", "deadline": "2026-10-19",
                  "text": "Contract CAWRMP-ICW-07 Modernization of R-8 canal"},
                 {"kind": "世行采购公告", "title": "Kegeyli canal supervision", "url": "https://projects.worldbank.org/notice/78", "date": "2026-02-11", "deadline": None, "text": "Kegeyli"}]
    out = L.verify_outside(item, "UZB", scan=lambda iso: scan_rows, outside=lambda q, iso: (_ for _ in ()).throw(AssertionError("对上了不用查新闻")))
    assert out["sure"] and out["original"]["url"].endswith("/notice/77") and out["wb_scanned"] == 2 and out["wb_since"] == "2026-02-11" and out["links"] == []
    # 对不上:名字沾边的作候选;出资方按「4207-UZB」猜是亚行;链接:亚行招标页、世行采购网、搜索引擎
    out = L.verify_outside(item, "UZB", scan=lambda iso: scan_rows[1:], outside=lambda q, iso: ([{"kind": "新闻(近三个月)", "title": "n", "url": "https://n/1", "date": "2026-10-02"},
                                                                                             {"kind": "世行采购公告", "title": "x", "url": "https://w/1", "date": None}], ["新闻检索没做成(X)"]))
    assert not out["sure"] and out["original"]["url"].endswith("/notice/78") and len(out["candidates"]) == 1 and out["funder"] == "亚行"
    assert [n["url"] for n in out["news"]] == ["https://n/1"] and out["notes"] == ["新闻检索没做成(X)"]
    assert [x["name"] for x in out["links"]] == ["到亚行招标页找", "到世行采购网找", "搜索引擎查这个编号"] and "keywords=CAWRMP-ICW-07" in out["links"][0]["url"]
    # 整国核对出错不影响其余
    out = L.verify_outside({"ref": "X-1", "funder": "AFD", "names": []}, "UZB", scan=lambda iso: (_ for _ in ()).throw(OSError("down")), outside=lambda q, iso: ([], []))
    assert out["wb_scanned"] == 0 and out["notes"][0].startswith("整国核对世行公告没做成") and out["funder"] == "法开署" and out["links"][0]["name"] == "到法开署招标页找"
    assert L.funder_guess({"funder": "世界银行(IDA)"}) == "世行" and L.funder_guess({"funder": None, "title": "x"}) is None


def test_single_notice_is_not_a_list(db):
    """只讲一个项目的材料(items 为空)照旧整篇分析,有「收进线索库」的草稿。"""
    out = L.check(URL, "", TODAY, _clf({**MODEL, "items": [ITEMS[0]]}, []), db, fetch=lambda u: _page(), outside=lambda q, iso: ([], []))
    assert "list" not in out and out["draft"] and out["advice"]["action"] == "入库" and len(out["analysis"]["items"]) == 1


def _company(db):
    """公司资料:本单位两条(水利 2020 大、污水 2023 小)、兄弟单位一条(污水 2023 大)、一条资质。"""
    db.create("company", [
        {"kind": "业绩", "owner": "本单位", "name": "尼泊尔Bheri Babai引水隧道项目", "year": 2020, "amount": "10,569,832,141.52尼泊尔卢比,折 10733万美元(≈7.62亿元人民币)", "sectors": ["水利"]},
        {"kind": "业绩", "owner": "本单位", "name": "摩洛哥西迪斯里玛尼省排水整治项目", "year": 2023, "amount": "6257965.44迪拉姆,折 65万美元(≈462万元人民币)", "sectors": ["污水"]},
        {"kind": "业绩", "owner": "兄弟单位", "name": "中铁一局某市污水厂", "year": 2023, "amount": "9000万美元", "sectors": ["污水"]},
        {"kind": "资质", "owner": "本单位", "name": "市政公用工程施工总承包特级", "amount": ""},
    ])


FIN_ROW = {"kind": "其他", "owner": "本单位", "name": "近三年审计报告与主要财务数据(2023—2025)", "year": 2025,
           "amount": "近三年平均年营业收入约 294,418.88 万元人民币(≈4.15亿美元)",
           "content": "合并口径,单位:人民币万元。\n营业收入:2023 193,598.67;2024 277,288.89;2025 412,369.08(三年平均约 294,418.88,≈4.15 亿美元)\n"
                      "流动资产合计:2023 248,797.77;2024 258,570.57;2025 339,111.83\n流动负债合计:2023 136,069.66;2024 155,688.57;2025 226,383.66\n流动比率:2023 1.83;2024 1.66;2025 1.50"}


def test_qualify_compares_tender_thresholds_with_our_own_records(db):
    """2026-10-16 使用方:上传清单问「我们的业绩能满足吗」,回答的却是线索——要按问题答:拿公告的门槛对照公司资料里本单位的业绩。
    只比单个类似合同金额(有年限的只看年限内);兄弟单位的只能另列参考;营业额、财务资源系统里没有,写明要财务部出数。"""
    _company(db)
    rec = L.our_records(db)
    assert [r["name"][:5] for r in rec["本单位"]] == ["尼泊尔Bh", "摩洛哥西迪"] and rec["本单位"][0]["usd"] == 107330000 and [r["usd"] for r in rec["兄弟单位"]] == [90000000]
    canal = L.qualify({"similar_usd": 8000000, "turnover_usd": 10000000, "financial_usd": 1250000, "years": 10, "specific": "土方 150 000 m³"}, ["水利"], rec, TODAY)
    assert canal["verdict"] == "满足" and "「尼泊尔Bheri Babai引水隧道项目」10,733 万美元(2020),够" in canal["why"] and canal["records"][0]["year"] == 2020
    assert canal["gaps"] == ["年均营业额要 1,000 万美元:公司资料里没有近三年审计数据行,要财务部出数", "财务资源要 125 万美元:公司资料里没有近三年审计数据行,要财务部出数", "特定资格要对照招标文件:土方 150 000 m³"]
    assert canal["finance"] == [] and rec["财务"] is None
    recent = L.qualify({"similar_usd": 8000000, "turnover_usd": None, "financial_usd": None, "years": 3, "specific": None}, ["水利"], rec, TODAY)
    assert recent["verdict"] == "不满足" and "近 3 年内" in recent["why"] and "没有同领域(或年限内)的业绩" in recent["why"] and recent["gaps"] == []  # 2020 的不在近三年
    sewer = L.qualify({"similar_usd": 12000000, "turnover_usd": None, "financial_usd": None, "years": 5, "specific": None}, ["污水"], rec, TODAY)
    assert sewer["verdict"] == "不满足" and "只有 65 万美元(2023)" in sewer["why"] and "兄弟单位有够门槛的(仅供参考,不能当本单位业绩):中铁一局某市污水厂" in sewer["why"]
    assert L.qualify(None, ["污水"], rec, TODAY)["verdict"] == "不确定" and "没写单个类似合同的金额门槛" in L.qualify(None, ["污水"], rec, TODAY)["why"]
    assert L.usd_of("1.2亿美元") == 120000000 and L.usd_of("5,000万元") is None and L.our_records(None) == {"本单位": [], "兄弟单位": [], "财务": None}


def test_qualify_reads_turnover_and_liquidity_from_the_audit_row(db):
    """2026-10-26 使用方把近三年审计数据录进公司资料(「其他」类一行)后说「做」:营业额、财务资源门槛不再一律推给财务部,按这一行对照。
    年均营业额取金额里的美元数;财务资源比最新年「流动资产 − 流动负债」(人民币按 7.1 折),并写明以招标文件口径为准;不够时结论是不满足。"""
    _company(db)
    db.create("company", [FIN_ROW, {"kind": "其他", "owner": "本单位", "name": "公司章程(2025 年公证本)", "content": "正文 29 页"}])
    rec = L.our_records(db)
    fin = rec["财务"]
    assert fin["name"].startswith("近三年审计") and fin["year"] == 2025 and round(fin["turnover_usd"]) == 415_000_000
    assert round(fin["current_assets_usd"] / 1e4) == 47_762 and round(fin["net_current_usd"] / 1e4) == 15_877  # (339,111.83 − 226,383.66) 万元 ÷ 7.1
    assert len(rec["本单位"]) == 2  # 「其他」行不算业绩
    ok = L.qualify({"similar_usd": 8000000, "turnover_usd": 100_000_000, "financial_usd": 50_000_000, "years": 10, "specific": None}, ["水利"], rec, TODAY)
    assert ok["verdict"] == "满足" and ok["gaps"] == [] and [f["ok"] for f in ok["finance"]] == [True, True]
    assert "年均营业额要 10,000 万美元,我方近三年平均约 41,500 万美元(公司资料「近三年审计报告与主要财务数据(2023—2025)」),够。" in ok["why"]
    assert "财务资源要 5,000 万美元,我方 2025 年流动资产 47,762 万美元、扣除流动负债后 15,877 万美元" in ok["why"] and "以招标文件对财务资源的口径为准" in ok["why"]
    bad = L.qualify({"similar_usd": 8000000, "turnover_usd": 500_000_000, "financial_usd": None, "years": 10, "specific": None}, ["水利"], rec, TODAY)
    assert bad["verdict"] == "不满足" and "10,733 万美元(2020),够。年均营业额要 50,000 万美元,我方近三年平均约 41,500 万美元" in bad["why"] and "不够" in bad["why"]
    assert L.finance_of("公司章程", "正文 29 页", "", None) is None
    assert L.finance_of("财务数据", "营业收入:2023 1,000.00;2024 2,000.00(两年平均 1,500.00,≈ 0.21 亿美元)", "", 2024)["turnover_usd"] == 21_000_000


def test_list_file_with_a_question_answers_the_question_first(db, monkeypatch):
    """附言「我们的业绩能满足吗」+ 清单:先答问题(逐条 业绩满足 / 不满足 / 不确定,非水务不评),再给清单;单篇也答。"""
    from core import file_text as F
    _company(db)
    monkeypatch.setattr(F, "pdf_text", lambda data: ("招标项目监测 07.10.2026 清单与资格要求……" * 3, 1))
    q = {"kind": "资格对照", "text": "我们的业绩能满足吗?"}
    stages: list[str] = []
    out = L.check("", "我们的业绩能满足吗?", TODAY, _clf({**MODEL, "items": ITEMS, "question": q}, []), db, fetch=lambda u: (_ for _ in ()).throw(AssertionError()),
                  outside=lambda q, iso: ([], []), upload=b"%PDF-1.4", find_original=lambda q, iso: WB_HITS.get(q, []), verify=lambda it, iso: CHECKED, progress=stages.append)
    assert out["question"]["kind"] == "资格对照" and out["question"]["per_item"] and "对照我方业绩" in stages
    assert out["question"]["summary"].startswith("按我方资料里 2 条本单位业绩对照(业绩只比单个类似合同金额):4 条水务项目里,业绩够门槛的 0 条、不够的 2 条、材料没写门槛的 2 条。营业额、财务资源公司资料里没有,要财务部出数")
    assert out["advice"]["reason"].startswith("按我方资料里 2 条本单位业绩对照")
    by = {it["ref"] or it["title"]: it for it in out["list"]["items"]}
    assert by["CAWRMP-ICW-08"]["qualify"]["verdict"] == "不满足" and "近 3 年内" in by["CAWRMP-ICW-08"]["qualify"]["why"]  # 门槛 800 万、近 3 年:2020 的不算
    assert by["SDP/RFB/05"]["qualify"]["verdict"] == "不满足" and "中铁一局某市污水厂" in by["SDP/RFB/05"]["qualify"]["why"]
    assert by["NRWWP-7"]["qualify"]["verdict"] == "不确定" and by["SLSDP/RFB/16"]["qualify"] is None and by["Something with a link"]["qualify"]["verdict"] == "不确定"
    # 附言没问问题:question 为 None,清单照旧
    out = L.check("", "同事发来的清单", TODAY, _clf({**MODEL, "items": ITEMS, "question": None}, []), db, fetch=lambda u: (_ for _ in ()).throw(AssertionError()),
                  outside=lambda q, iso: ([], []), upload=b"%PDF-1.4", find_original=lambda q, iso: [], verify=lambda it, iso: CHECKED)
    assert out["question"] is None and "qualify" not in out["list"]["items"][0]
    # 单篇公告 + 问题:按整篇的门槛对照
    single = {**MODEL, "items": [], "question": q, "sectors": ["水利"], "requirements": {"similar_usd": 5000000, "turnover_usd": None, "financial_usd": None, "years": 10, "specific": None}}
    out = L.check(URL, "我们的业绩能满足吗?", TODAY, _clf(single, []), db, fetch=lambda u: _page(), outside=lambda q, iso: ([], []))
    assert out["question"]["qualify"]["verdict"] == "满足" and out["question"]["summary"].startswith("按我方资料里 2 条本单位业绩对照:满足。")
    assert "question" in L.SCHEMA["properties"] and "requirements" in L.SCHEMA["properties"]["items"]["items"]["properties"] and "资格对照" in L.GUIDE
