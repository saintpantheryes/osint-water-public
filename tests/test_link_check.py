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
     "funder": "World Bank", "amount": "12.3 million USD", "stage": "招标", "deadline": "2026-11-20", "url": None, "query": "NRWWP-7 Namangan wastewater", "names": ["Namangan", "NRWWP"]},
    {"ref": "CAWRMP-ICW-08", "title": "Modernization of Kegeyli main canal — structures", "title_zh": "克盖利干渠现代化改造", "country": "UZB", "funder": "World Bank loan 4207-UZB",
     "amount": "9.6 million USD (est.)", "stage": "招标", "deadline": "2026-10-19", "url": None, "query": "CAWRMP-ICW-08 Kegeyli canal", "names": ["Kegeyli", "CAWRMP"]},
    {"ref": "SDP/RFB/05", "title": "Kitab wastewater network construction", "title_zh": "基塔布市污水管网建设", "country": "UZB", "funder": "AFD", "amount": "12.85 million USD (est.)",
     "stage": "招标", "deadline": "2026-12-01", "url": None, "query": "SDP/RFB/05 Kitab sewerage", "names": ["Kitab", "Kashkadarya"]},
    {"ref": None, "title": "Something with a link", "title_zh": "材料里自带网址的一条", "country": None, "funder": None, "amount": None, "stage": None, "deadline": None,
     "url": "https://www.adb.org/node/1181131", "query": None, "names": ["Anouvong"]},
    {"ref": "", "title": "", "title_zh": "", "country": None, "funder": None, "amount": None, "stage": None, "deadline": None, "url": None, "query": None, "names": []},  # 空的丢掉
]
WB_HITS = {
    "CAWRMP-ICW-08 Kegeyli canal": [{"kind": "世行采购公告", "title": "Request for Bids:CAWRMP-ICW-08 Modernization of Kegeyli Main Canal", "url": "https://projects.worldbank.org/notice/1",
                                     "date": "2026-10-01", "deadline": "2026-10-19", "text": "Loan 4207-UZB … CAWRMP-ICW-08"}],
    "SDP/RFB/05 Kitab sewerage": [{"kind": "世行项目(Active)", "title": "P123 Kashkadarya Water Supply", "url": "https://projects.worldbank.org/p/P123", "date": "2024-01-01",
                                   "deadline": None, "text": "Kashkadarya region water supply"}],  # 名字沾边、编号对不上:只算候选
}


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
                  progress=stages.append)
    assert out["ok"] and out["advice"]["action"] == "逐条看" and out["draft"] is None and "清单型材料" in out["notes"][-1]
    lst = out["list"]
    assert lst["n"] == 4 and lst["counts"] == {"已在库": 1, "建议入库": 2, "先核实": 1}
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
    d = by["Something with a link"]
    assert d["status"] == "建议入库" and d["original"]["url"] == "https://www.adb.org/node/1181131" and d["original"]["kind"] == "亚洲开发银行" and d["country"]["iso3"] == "UZB"  # 没写国别的用整篇的
    assert stages[0] == "读文件" and "找原公告 0/4" in stages and stages[-1] == "找原公告 4/4" and "模型分析" in stages
    assert '"items"' in "".join(m["content"] for m in seen[0]["messages"])  # 提示词里要了 items
    assert "清单" in L.GUIDE and "items" in L.SCHEMA["properties"]


def test_single_notice_is_not_a_list(db):
    """只讲一个项目的材料(items 为空)照旧整篇分析,有「收进线索库」的草稿。"""
    out = L.check(URL, "", TODAY, _clf({**MODEL, "items": [ITEMS[0]]}, []), db, fetch=lambda u: _page(), outside=lambda q, iso: ([], []))
    assert "list" not in out and out["draft"] and out["advice"]["action"] == "入库" and len(out["analysis"]["items"]) == 1
