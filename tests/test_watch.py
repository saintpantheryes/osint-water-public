"""监测流水线:日期解析、规则初筛、台账挂接、采集与变更检测、跟进附件、模型判定(假客户端)、提醒页与周报。
全部离线:网页抓取与 Claude API 都用假对象替换;国别 ZZZ 与所有网址均为虚构。"""
import io
import json
import shutil
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx
import pytest
import yaml

from core import storage
from core.ledger import load_ledger, load_rules
from core.profile import load_profile
from core.sources.http import SourceBlocked
from core.watch import fetch, rules
from core.watch.collectors import KINDS, Ctx
from core.watch.link import _id_variants, build_index, item_countries, suggest
from core.watch.llm import FALLBACK_BETA, Classifier, DeepSeekClient, LLMConfig, ledger_lines
from core.watch.outputs import category, deadline_rows, deadlines, ledger_suggestions, links_of, relevant, render_inbox
from core.watch.publish import _untriaged, llm_notes, write_digest, write_outputs
from core.watch.query import row
from core.watch.rules import evaluate
from core.watch import run as runmod
from core.watch.run import DEFAULTS, WatchConfigError, _merge_llm, _old, _stored, ledger_key, load_watch, relink, run_country
from core.watch.store import Item, Store, signal_id
from core.watch.text import html_text, parse_date, parse_date_infer_year, region_text

ROOT = Path(__file__).resolve().parent.parent
FX = Path(__file__).resolve().parent / "fixtures"
TODAY = date(2026, 9, 24)
CFG = {**DEFAULTS}


# ---------------------------------------------------------------- 日期与正文


@pytest.mark.parametrize("raw,iso", [
    ("2026-09-24T10:00:00Z", "2026-09-24"),
    ("20260924T150000Z", "2026-09-24"),
    ("23/09/2026", "2026-09-23"),
    ("23.09.2026", "2026-09-23"),
    ("24-Aug-2026", "2026-08-24"),
    ("23 Sentyabr, 2026", "2026-09-23"),
    ("5 İyun 2026", "2026-06-05"),
    ("12 сентября 2026", "2026-09-12"),
    ("12 October 2026", "2026-10-12"),
    ("September 18, 2026", "2026-09-18"),
    ("Wed, 24 Sep 2026 10:00:00 +0400", "2026-09-24"),
    ("31/02/2026", None),
    ("no date here", None),
    (None, None),
])
def test_parse_date(raw, iso):
    assert parse_date(raw) == iso


def test_parse_date_infer_year():
    assert parse_date_infer_year("19 Sentyabr 19:21", TODAY) == "2026-09-19"
    assert parse_date_infer_year("28 Dekabr 10:00", date(2027, 1, 3)) == "2026-12-28"  # 晚于明天的算去年
    assert parse_date_infer_year("2026-01-02", TODAY) == "2026-01-02"


def test_html_text_and_regions():
    html = ("<html><nav>Menu</nav><div class='a'>Alpha</div><div class='b'>Beta</div>"
            "<script>var x</script><footer>Foot</footer></html>")
    assert html_text(html) == "Alpha\nBeta"
    assert region_text(html, [".b", ".a"]) == "Beta\nAlpha"
    assert region_text(html, ".missing") == "Alpha\nBeta"  # 选择器没匹配到时退回全页正文


# ---------------------------------------------------------------- 规则初筛


def _it(title, text="", fields=None):
    return Item(key=title, url="https://ex.test/" + title, title=title, text=text, fields=fields or {})


def test_rules_water_terms_multilingual():
    r = evaluate(_it("İÇMƏLİ SU TƏCHİZATI layihəsi"), {}, [])
    assert r["candidate"] and {"su təchizatı", "içməli su"} <= set(r["water_terms"])  # 大写 İ 折叠后仍能配上
    assert not evaluate(_it("Damage assessment of roads"), {}, [])["candidate"]  # dam 不收,免得误中 damage
    assert evaluate(_it("Sumgayit WWTP rehabilitation"), {}, [])["water_terms"] == ["WWTP"]
    assert evaluate(_it("wwtp"), {}, [])["water_terms"] == []  # 缩写区分大小写
    assert evaluate(_it("Board meeting"), {"topic": "water"}, [])["candidate"]  # 水务信源不要求水务词
    assert evaluate(_it("ZWA announces results"), {}, ["ZWA"])["entities"] == ["ZWA"]


def test_rules_desalination_terms():
    """海水淡化:各语种的写法、海湾国家 PPP 的缩写(IWP、IWPP)与反渗透工艺都算;缩写区分大小写。"""
    for title in ("Barka 5 IWP: Request for Qualification", "Hassyan IWPP financial close", "Construction d'une station de dessalement",
                  "Planta desalinizadora de agua de mar", "Опреснительная установка в Актау", "Supply of reverse osmosis membranes",
                  "Desalinisation plant in Namibia", "Dəniz suyunun şirinləşdirilməsi zavodu", "محطة تحلية مياه البحر"):
        assert "desalination" in evaluate(_it(title), {}, [])["sectors"], title
    assert evaluate(_it("Riyadh East ISTP bids received"), {}, [])["sectors"] == ["wastewater"]  # 独立污水处理厂 PPP
    assert evaluate(_it("Oman extends bid deadline for water transmission project"), {}, [])["sectors"] == ["water_supply"]
    assert not evaluate(_it("iwp committee"), {}, [])["candidate"] and not evaluate(_it("Wind power IPP tender"), {}, [])["candidate"]


def test_rules_stage_prefers_head_then_earliest_in_body():
    r = evaluate(_it("Consulting services", "... bidding documents ... invitation for bids",
                     {"type": "Expression of Interest"}), {}, [])
    assert (r["stage"], r["stage_from"]) == ("eoi", "head")  # 公告类型栏优先于正文顺带提到的阶段
    r = evaluate(_it("Alpha layihəsi", "REQUEST FOR EXPRESSIONS OF INTEREST. Later the bidding documents will be issued."), {}, [])
    assert (r["stage"], r["stage_from"]) == ("eoi", "body")  # 正文里最早出现的阶段
    assert evaluate(_it("Reconstruction of canal - GPN"), {}, [])["stage"] == "gpn"


def test_rules_french_titles_and_mdb_project_names():
    # 法语招标标题常全大写、不带重音,或用弯引号(摩洛哥、西非、中非常见):照样配得上
    r = evaluate(_it("TRAVAUX DE CONSTRUCTION DE LA STATION D'EPURATION DES EAUX USEES DE TAOURIRT"), {}, [])
    assert r["candidate"] and r["sectors"] == ["wastewater"] and {"eaux usées", "station d'épuration"} <= set(r["water_terms"])
    assert "adduction d'eau" in evaluate(_it("Travaux d’adduction d’eau potable du douar X"), {}, [])["water_terms"]
    assert evaluate(_it("Renforcement de l'AEP du centre Y"), {}, [])["water_terms"] == ["AEP"]
    assert not evaluate(_it("aep du centre"), {}, [])["candidate"]  # 缩写区分大小写
    # 词组里前面的词带复数词尾也算(stations de pompage、réseaux d'eau)
    assert evaluate(_it("Equipement des stations de pompage en énergie solaire"), {}, [])["water_terms"] == ["station de pompage"]
    assert "réseau d'eau" in evaluate(_it("Renouvellement des réseaux d'eau potable"), {}, [])["water_terms"]
    # EBRD 标题只写合同包号,项目名在字段里;节水(water conservation)项目算水务
    ebrd = _it("Morocco: PP2 Works", fields={"project": "[Saiss III water conservation project, 55198, Morocco, PP2 Works]"})
    assert evaluate(ebrd, {}, [])["water_terms"] == ["water conservation"]
    assert not evaluate(_it("Morocco: TC2 Capacity Building Programme for Nador West Med"), {}, [])["candidate"]


def test_open_tenders_are_never_old():
    def it(**kw):
        return Item(key="k", url="u", title="t", **kw)
    today, cutoff = date(2026, 9, 26), "2026-08-27"
    assert not _old(it(published="2026-08-19", fields={"deadline": "2026-10-07"}), cutoff, today)  # 发布早于回溯期但还能投
    assert _old(it(published="2026-08-19", fields={"deadline": "2026-09-01"}), cutoff, today)
    assert not _old(it(published="2026-09-01"), cutoff, today)
    assert _old(it(), cutoff, today) and not _old(it(fields={"deadline": "2026-09-26"}), cutoff, today)


# ---------------------------------------------------------------- 挂接台账


def test_id_variants():
    assert {"AZE 1028", "AZE-1028", "AZE1028"} <= set(_id_variants("AZE 1028"))
    assert "17052" in _id_variants("17052PA")
    assert "018783" in _id_variants("PR 018783")


def test_suggest_links():
    led = SimpleNamespace(items=[
        {"id": "ZZZ-001", "name_en": "Alpha Wastewater Plant", "ids": {"贷款": "ZZ 1234"}, "aliases": ["Alfa WWTP"]},
        {"id": "ZZZ-002", "name_en": "Beta Canal", "ids": {"短号": "12"}},  # 太短的纯数字不建索引
    ])
    idx = build_index(led)
    got = suggest("Notice for ZZ-1234: Alpha wastewater plant, 12 lots; beta canal", idx)
    assert got[0] == {"item": "ZZZ-001", "confidence": "high", "by": "rule", "reason": "命中 贷款 ZZ-1234"}
    assert [x["item"] for x in got] == ["ZZZ-001", "ZZZ-002"] and got[1]["confidence"] == "medium"
    assert suggest("nothing relevant", idx) == []


def test_links_stay_within_country():
    """台账别名常是通用说法(如 non-revenue water):全球信源里别国的同类项目不挂(约旦的减漏咨询曾挂到巴库减漏 AZE-007);
    编号吻合的不看国别;线索国别未定时照配。模型给的别国项目也不收;以前入库的,读取时不算。"""
    led = SimpleNamespace(items=[{"id": "AZE-007", "name_zh": "巴库减漏", "name_en": "Baku NRW performance contract",
                                  "aliases": ["non-revenue water"], "ids": {"世行": "P123456"}}], countries={"AZE-007": "AZE"})
    idx, where = build_index(led), item_countries(led)
    text = "Consultancy services for non-revenue water reduction, Zarqa Governorate"
    assert suggest(text, idx, "JOR") == []
    assert [x["item"] for x in suggest(text, idx, "AZE")] == ["AZE-007"] and suggest(text, idx)[0]["confidence"] == "medium"
    assert suggest(text + " (P123456)", idx, "JOR")[0]["confidence"] == "high"  # 编号是唯一的
    sig = {"country": None, "links": suggest(text, idx)}  # 采集时国别未定,按名称先挂上
    _merge_llm(sig, {"country": "JOR", "ledger_links": [{"item": "AZE-007", "confidence": "high", "reason": "同属减漏"}]}, where)
    assert sig["country"] == "JOR" and sig["links"] == []  # 模型定出约旦:按名称挂上的去掉,模型给的别国项目不收
    same = {"country": "AZE", "links": []}
    _merge_llm(same, {"ledger_links": [{"item": "AZE-007", "confidence": "high", "reason": "读原文确认"}]}, where)
    assert [(x["item"], x["by"]) for x in same["links"]] == [("AZE-007", "llm")]
    old = {"country": "JOR", "llm": None, "links": [
        {"item": "AZE-007", "confidence": "medium", "by": "rule", "reason": "命中 non-revenue water"},
        {"item": "AZE-001", "confidence": "high", "by": "rule", "reason": "命中 世行 P1"},
        {"item": "AZE-002", "confidence": "high", "by": "llm", "reason": "模型"}]}
    assert [x["item"] for x in links_of(old)] == ["AZE-001"]
    assert "AZE-007 巴库减漏|国别 阿塞拜疆|" in ledger_lines(led)  # 给模型的台账清单写明国别


def test_relink_stored_signals_after_ledger_change(tmp_path):
    """台账新增项目或补了编号、别名后,以前入库的线索按新台账重挂;监测配置与模型给的保留,模型给的别国项目、台账里已删的去掉;重跑不再改动。"""
    st = Store(tmp_path)
    base = {"source": "ma_pmmp", "kind": "atexo_search", "published": None, "first_seen": "2026-09-27T01:00:00+00:00", "change": "new",
            "fields": {"reference": "53.R1/DCL/2026"}, "rules": {}, "docs": [], "llm": None, "excerpt": ""}
    taza = {**base, "id": "aaaa000000000001", "key": "atexo:g3h:1024267", "country": "MAR", "title": "STEP à boues activées à TAZA",
            "url": "https://www.marchespublics.gov.ma/index.php?page=entreprise.EntrepriseDetailConsultation&refConsultation=1024267&orgAcronyme=g3h",
            "links": [{"item": "AZE-007", "confidence": "high", "by": "llm", "reason": "同属污水"}]}
    page = {**base, "id": "aaaa000000000002", "key": "https://ex.test/p", "country": "AZE", "title": "项目页变化", "url": "https://ex.test/p",
            "fields": {"item": "AZE-001"}, "links": [{"item": "AZE-001", "confidence": "high", "by": "config", "reason": "监测该项目的页面"},
                                                      {"item": "AZE-009", "confidence": "medium", "by": "rule", "reason": "已删的项目"}]}
    st.append_signals([taza, page])
    led = SimpleNamespace(items=[{"id": "MAR-003", "ids": {"门户": "refConsultation=1024267"}},
                                 {"id": "AZE-001", "name_en": "Sheki"}, {"id": "AZE-007", "aliases": ["non-revenue water"]}],
                          countries={"MAR-003": "MAR", "AZE-001": "AZE", "AZE-007": "AZE"})
    assert relink(st, led) == 2
    got = {s["id"]: [(x["item"], x["by"]) for x in s["links"]] for s in st.load_signals()}
    assert got["aaaa000000000001"] == [("MAR-003", "rule")]  # 编号吻合挂上;模型给的别国项目去掉
    assert got["aaaa000000000002"] == [("AZE-001", "config")]  # 监测配置保留;台账里没有了的去掉
    assert relink(st, led) == 0
    assert ledger_key(led) != ledger_key(SimpleNamespace(items=led.items[:2], countries=led.countries)) and ledger_key(None) == ""


# ---------------------------------------------------------------- 监测配置


def test_real_watch_configs_load():
    for fp in [*(ROOT / "countries").glob("*/watch.yaml"), *(ROOT / "private" / "countries").glob("*/watch.yaml")]:  # 真实国别的在私有配置仓
        wc = load_watch(fp)
        assert wc["country"] == fp.parent.name
        for s in wc["sources"]:
            if s["kind"] == "html_list":
                assert s.get("url") and s.get("item") and s.get("title"), s["id"]


def test_watch_config_validation(tmp_path):
    fp = tmp_path / "w.yaml"
    for raw, msg in [({"schema": "x", "sources": []}, "schema"),
                     ({"schema": "osint-water/watch@1", "sources": [{"id": "a", "kind": "rss"}, {"id": "a", "kind": "rss"}]}, "重复"),
                     ({"schema": "osint-water/watch@1", "sources": [{"id": "a", "kind": "telepathy"}]}, "不认识")]:
        fp.write_text(yaml.safe_dump(raw), encoding="utf-8")
        with pytest.raises(WatchConfigError, match=msg):
            load_watch(fp)


# ---------------------------------------------------------------- 采集到入库(假网站)

LIST_HTML = """<table>
<tr><th>Date</th><th>Title</th><th>Country</th><th>Closing</th></tr>
<tr><td>20/09/2026</td><td><a href="/n/1">Request for Expressions of Interest: Alpha wastewater treatment plant</a></td>
    <td>Testland</td><td>15 October 2026</td></tr>
<tr><td>01/01/2026</td><td><a href="/n/2">Old water supply notice</a></td><td>Testland</td><td>01/02/2026</td></tr>
<tr><td>21/09/2026</td><td><a href="/n/3">Road rehabilitation works</a></td><td>Testland</td><td>01/11/2026</td></tr>
<tr><td>21/09/2026</td><td><a href="/n/4">Sewerage network</a></td><td>Otherland</td><td>01/11/2026</td></tr>
{extra}</table>"""
DETAIL_HTML = "<html><nav>menu</nav><div class='body'>Details of the assignment. <a href='/f/tor.docx'>ToR</a></div></html>"


def _docx(text: str) -> bytes:
    from docx import Document

    d = Document()
    d.add_paragraph(text)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


class FakeWeb:
    """按网址返回预设内容;值为异常时抛出。记录请求过的网址。"""

    def __init__(self, pages: dict):
        self.pages = pages
        self.hits: list[str] = []

    def get(self, url, params=None, **kw):
        self.hits.append(url)
        v = self.pages[url]
        if isinstance(v, Exception):
            raise v
        body = v if isinstance(v, bytes) else v.encode("utf-8")
        return fetch.Page(url, 200, body, "text/html; charset=utf-8")


SOURCES = [
    {"id": "notices", "name": "测试公告栏", "kind": "html_list", "url": "https://ex.test/list", "item": "table tr",
     "title": "td:nth-of-type(2) a", "date": "td:nth-of-type(1)",
     "fields": {"country": "td:nth-of-type(3)", "closing": "td:nth-of-type(4)"}, "country_in": "country",
     "deadline_field": "closing", "follow": {"selector": ".body", "attachments": True}},
    {"id": "pages", "name": "项目页", "kind": "page", "ignore": ["Updated: .*"],
     "pages": [{"url": "https://ex.test/p/1", "item": "ZZZ-003", "selector": ".main", "label": "页面一"},
               {"url": "https://ex.test/p/2", "label": "页面二"}]},
]


def _page(status: str, updated: str) -> str:
    return f"<html><div class='main'>Project Gamma\nStatus: {status}\nUpdated: {updated}</div></html>"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """临时 profile(虚构国别 ZZZ,台账加了编号与别名)+ 假网站 + 数据目录;不读真实凭据。"""
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "ZZZ").mkdir()
    shutil.copy(FX / "ZZZ" / "sources.yaml", tmp_path / "ZZZ" / "sources.yaml")
    led = yaml.safe_load((FX / "ZZZ" / "ledger.yaml").read_text(encoding="utf-8"))
    led["opportunities"][0].update(ids={"贷款": "ZZ 1234"}, aliases=["Alpha wastewater"])
    (tmp_path / "ZZZ" / "ledger.yaml").write_text(yaml.safe_dump(led, allow_unicode=True), encoding="utf-8")
    (tmp_path / "ZZZ" / "watch.yaml").write_text(yaml.safe_dump(
        {"schema": "osint-water/watch@1", "country": "ZZZ", "names": ["Testland"], "entities": ["ZWA"], "sources": SOURCES},
        allow_unicode=True), encoding="utf-8")
    prof = yaml.safe_load((FX / "profile.yaml").read_text(encoding="utf-8"))
    prof["countries"][0].update(ledger="ZZZ/ledger.yaml", watch="ZZZ/watch.yaml", overlay=str(FX / "overlay.yaml"))
    prof.update(procurement_rules=str(FX / "rules.yaml"), data_dir="data", watch={"inbox_days": 14})
    (tmp_path / "profile.yaml").write_text(yaml.safe_dump(prof, allow_unicode=True), encoding="utf-8")
    profile = load_profile(tmp_path / "profile.yaml")
    monkeypatch.setattr(storage, "DATA_DIR", profile.data_dir)
    web = FakeWeb({
        "https://ex.test/list": LIST_HTML.format(extra=""),
        "https://ex.test/n/1": DETAIL_HTML,
        "https://ex.test/f/tor.docx": _docx("Terms of Reference. Loan ZZ-1234. Deadline for expressions of interest: 15 October 2026."),
        "https://ex.test/p/1": _page("Approved", "2026-09-01"),
        "https://ex.test/p/2": _page("Draft", "2026-09-01"),
    })
    monkeypatch.setattr(fetch, "get", web.get)
    return SimpleNamespace(profile=profile, country=profile.country("ZZZ"), web=web, store=Store(profile.data_dir))


def _run(env, clf=None, today=TODAY):
    res = run_country(env.profile, env.country, env.store, clf, today, CFG)
    env.store.save()
    return res


def test_first_run_baseline_follow_and_links(env):
    res = _run(env)
    by_key = {s["key"]: s for s in res.signals}
    # 早于回溯期的只记为已见;别国条目被过滤;非水务条目入库但不算相关
    assert set(by_key) == {"https://ex.test/n/1", "https://ex.test/n/3"}
    s1, s3 = by_key["https://ex.test/n/1"], by_key["https://ex.test/n/3"]
    assert relevant(s1) and not relevant(s3)
    assert s1["published"] == "2026-09-20" and s1["fields"]["deadline"] == "2026-10-15"
    assert (s1["rules"]["stage"], category(s1)) == ("eoi", "eoi")
    # 跟进了详情页与 Word 附件;附件里的贷款号挂上台账(高),名称别名(中)被高覆盖
    assert [d["url"] for d in s1["docs"]] == ["https://ex.test/n/1", "https://ex.test/f/tor.docx"]
    assert "Loan ZZ-1234" in s1["excerpt"]
    assert s1["links"] == [{"item": "ZZZ-001", "confidence": "high", "by": "rule", "reason": "命中 贷款 ZZ-1234"}]
    assert "https://ex.test/n/3" not in env.web.hits  # 非候选不跟进详情页
    # 页面监测首次只建基线
    assert res.sources["pages"] == {"status": "ok", "items": 2, "new": 0, "baseline": True, "error": None}
    assert res.sources["notices"]["baseline"] is True


def test_second_run_new_items_and_page_changes(env):
    _run(env)
    env.web.pages["https://ex.test/list"] = LIST_HTML.format(
        extra="<tr><td>24/09/2026</td><td><a href='/n/5'>Desalination plant tender notice</a></td><td>Testland</td><td></td></tr>")
    env.web.pages["https://ex.test/n/5"] = "<div class='body'>Invitation for bids.</div>"
    env.web.pages["https://ex.test/p/1"] = _page("Signed", "2026-09-20")
    env.web.pages["https://ex.test/p/2"] = _page("Draft", "2026-09-20")  # 只有被忽略的更新时间变了
    res = _run(env, today=date(2026, 9, 25))
    assert [s["key"] for s in res.signals] == ["https://ex.test/n/5", "https://ex.test/p/1"]
    page = res.signals[1]
    assert page["change"] == "changed" and category(page) == "page" and relevant(page)
    assert page["excerpt"] == "-Status: Approved\n+Status: Signed"
    assert page["links"][0] == {"item": "ZZZ-003", "confidence": "high", "by": "config", "reason": "监测该项目的页面"}
    # 同一页面再变一次:新线索(id 带新指纹),不与上一条冲突
    env.web.pages["https://ex.test/p/1"] = _page("Closed", "2026-09-21")
    again = _run(env, today=date(2026, 9, 26)).signals
    assert len(again) == 1 and again[0]["id"] != page["id"]


def test_rejudge_after_terms_change(env, monkeypatch):
    """词表改了:以前没过初筛、仍在回溯期或截止前、没入库的条目按新词表重判一次;已入库、已过期的不重出;同一版词表只重判一次。"""
    fp = Path(env.country.watch)
    wc = yaml.safe_load(fp.read_text(encoding="utf-8"))
    wc["sources"][0]["store"] = "candidates"  # 像全球信源一样只存初筛命中的
    fp.write_text(yaml.safe_dump(wc, allow_unicode=True), encoding="utf-8")
    env.web.pages["https://ex.test/list"] = LIST_HTML.format(  # 发布早于回溯期、但还没截止
        extra="<tr><td>01/07/2026</td><td><a href='/n/6'>Saiss III water conservation project - PP2 Works</a></td>"
              "<td>Testland</td><td>07 October 2026</td></tr>")
    env.web.pages["https://ex.test/n/6"] = "<div class='body'>Invitation for bids.</div>"
    terms = rules.load_terms()  # 有缓存,不能原地改
    old = {**terms, "water": [t for t in terms["water"] if t != "water conservation"]}
    monkeypatch.setattr(runmod, "evaluate", lambda it, src, ent: rules.evaluate(it, src, ent, old))
    monkeypatch.setattr(runmod, "terms_version", lambda entities=(): "old")
    assert {s["key"] for s in _run(env).signals} == {"https://ex.test/n/1"}  # 旧词表下 n/6 没过初筛,道路项目 n/3 不存
    monkeypatch.setattr(runmod, "evaluate", rules.evaluate)
    monkeypatch.setattr(runmod, "terms_version", rules.terms_version)
    again = _run(env, today=date(2026, 9, 25)).signals
    assert [s["key"] for s in again] == ["https://ex.test/n/6"]  # 已入库的 n/1、过期的 n/2、仍不相关的 n/3 都不重出
    assert again[0]["rules"]["water_terms"] == ["water conservation"] and again[0]["fields"]["deadline"] == "2026-10-07"
    assert _run(env, today=date(2026, 9, 26)).signals == []  # 同一版词表不再重判


def test_rejudge_after_source_config_change(env):
    """信源配置改了(如标成 topic: water):以前没过初筛、仍在截止前的条目重判一次。"""
    fp = Path(env.country.watch)
    wc = yaml.safe_load(fp.read_text(encoding="utf-8"))
    wc["sources"][0]["store"] = "candidates"
    fp.write_text(yaml.safe_dump(wc, allow_unicode=True), encoding="utf-8")
    env.web.pages["https://ex.test/list"] = LIST_HTML.format(
        extra="<tr><td>22/09/2026</td><td><a href='/n/8'>Travaux de régulation des canaux principaux du périmètre</a></td>"
              "<td>Testland</td><td>20/10/2026</td></tr>")
    env.web.pages["https://ex.test/n/8"] = "<div class='body'>Appel d'offres.</div>"
    assert "https://ex.test/n/8" not in {s["key"] for s in _run(env).signals}  # 标题里没有水务词
    wc["sources"][0]["topic"] = "water"
    fp.write_text(yaml.safe_dump(wc, allow_unicode=True), encoding="utf-8")
    again = _run(env, today=date(2026, 9, 25)).signals
    assert "https://ex.test/n/8" in {s["key"] for s in again}
    assert _run(env, today=date(2026, 9, 26)).signals == []  # 同一版配置不再重判


def test_rejudge_skips_leads_stored_by_predecessor_source(tmp_path):
    st = Store(tmp_path)
    st.append_signals([{"id": signal_id("isdb_aze", "k1"), "first_seen": "2026-09-01T00:00:00+00:00"}])
    assert _stored(st, {"id": "isdb_tenders", "seed_from": ["isdb_aze"]}, "k1")  # 旧信源存过的,换了信源编号也不重出
    assert not _stored(st, {"id": "isdb_tenders"}, "k1") and not _stored(st, {"id": "isdb_tenders", "seed_from": ["isdb_aze"]}, "k2")
    st.append_signals([{"id": signal_id("isdb_tenders", "k2"), "first_seen": "2026-09-02T00:00:00+00:00"}])
    assert _stored(st, {"id": "isdb_tenders"}, "k2")  # 本次运行新存的也算


def test_partial_and_blocked_sources(env):
    _run(env)
    env.web.pages["https://ex.test/p/2"] = fetch.FetchError("HTTP 500 from https://ex.test/p/2")
    res = _run(env)
    assert res.sources["pages"]["status"] == "ok" and "页面二" in res.sources["pages"]["error"]  # 一页失败不拖累其余
    for u in ("https://ex.test/p/1", "https://ex.test/p/2"):
        env.web.pages[u] = SourceBlocked(f"403 from {u}")
    env.web.pages["https://ex.test/list"] = httpx.ConnectError("boom")
    res = _run(env)
    assert res.sources["pages"]["status"] == "blocked"
    assert res.sources["notices"]["status"] == "error" and "ConnectError" in res.sources["notices"]["error"]
    st = env.store.state["sources"]
    assert st["pages"]["status"] == "blocked" and st["pages"]["last_ok"]  # 保留上次成功时间


def test_fetch_429_retry_only_when_asked_and_block_resets_per_run(monkeypatch):
    """GDELT 限速「每 5 秒一次」:429 隔一会儿再试(只在调用方要求时);403 不重试;新一轮采集清掉上一轮的停止名单。"""
    codes = []

    class R:
        def __init__(self, code):
            self.status_code, self.url, self.headers = code, "https://api.ex.test/doc", {"content-type": "application/json"}

        def iter_bytes(self):
            yield b"{}"

    class S:
        def __init__(self, code):
            self.r = R(code)

        def __enter__(self):
            return self.r

        def __exit__(self, *a):
            return False

    seq = []
    monkeypatch.setattr(fetch.httpx, "stream", lambda *a, **k: (codes.append(seq[0]), S(seq.pop(0)))[1])
    monkeypatch.setattr(fetch.time, "sleep", lambda s: None)
    monkeypatch.setattr(fetch, "MIN_INTERVAL_S", 0)
    fetch.reset_blocked()
    seq[:] = [429, 429, 200]
    assert fetch.get("https://api.ex.test/doc", wait_429=10, retries_429=2).status == 200 and codes == [429, 429, 200]
    codes.clear(); seq[:] = [429]
    with pytest.raises(SourceBlocked):
        fetch.get("https://api.ex.test/doc")  # 没要求重试的:429 即停
    with pytest.raises(SourceBlocked):
        fetch.get("https://api.ex.test/doc", wait_429=10, retries_429=2)  # 本轮已停止请求的主机:不再发请求
    assert codes == [429]
    fetch.reset_blocked(); codes.clear(); seq[:] = [403]
    with pytest.raises(SourceBlocked):
        fetch.get("https://api.ex.test/doc", wait_429=10, retries_429=2)  # 403 不重试
    assert codes == [403]
    fetch.reset_blocked()


# ---------------------------------------------------------------- 模型判定(假客户端)


def _resp(obj=None, stop="end_turn", text=None):
    return SimpleNamespace(
        stop_reason=stop, model="claude-opus-5",
        content=[SimpleNamespace(type="thinking", thinking=""),
                 SimpleNamespace(type="text", text=json.dumps(obj, ensure_ascii=False) if text is None else text)],
        usage=SimpleNamespace(input_tokens=1000, output_tokens=200, cache_read_input_tokens=800, cache_creation_input_tokens=0))


class FakeClient:
    """模拟 client.beta.messages.stream(...) 上下文与 get_final_message();响应按顺序给出,异常则抛出。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[dict] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kw):
        self.requests.append(kw)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return _Ctx(r)


class _Ctx:
    def __init__(self, resp):
        self.resp = resp

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.resp


OK = {"relevant": True, "category": "eoi", "summary_zh": "阿尔法污水厂咨询意向书征集,10 月 15 日截止",
      "project": "Alpha WWTP", "funder": "EBRD", "amount": None,
      "deadlines": [{"type": "eoi_deadline", "date": "2026-10-15", "evidence": "15 October 2026"}],
      "eligibility": None,
      "ledger_links": [{"item": "ZZZ-003", "confidence": "medium", "reason": "同一业主"},
                       {"item": "ZZZ-999", "confidence": "high", "reason": "台账里没有"}],
      "new_opportunity": False, "evidence": ["Request for Expressions of Interest"]}
SIG = {"id": "x", "source": "s", "source_name": "测试", "url": "https://ex.test/a", "title": "标题", "fields": {}, "rules": {}}


def _conn_error():
    return anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))


def test_classifier_request_and_results(env):
    fake = FakeClient([_resp(OK), _resp(stop="refusal", text=""), _resp(stop="max_tokens", text='{"relev'),
                       _resp(text="not json"), _conn_error()])
    clf = Classifier(LLMConfig(max_doc_chars=20), client=fake)
    out = clf.classify("SYS", SIG, "x" * 50)
    assert out["category"] == "eoi" and out["_model"] == "claude-opus-5" and "只送前 20 字" in out["_truncated"]
    kw = fake.requests[0]
    assert kw["model"] == "claude-opus-5" and kw["betas"] == [FALLBACK_BETA] and kw["fallbacks"] == "default"
    assert kw["output_config"]["format"]["type"] == "json_schema" and kw["output_config"]["effort"] == "medium"
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "x" * 20 in kw["messages"][0]["content"] and "x" * 21 not in kw["messages"][0]["content"]
    assert clf.classify("SYS", SIG, "t")["_error"].startswith("模型拒绝")
    assert "max_tokens" in clf.classify("SYS", SIG, "t")["_error"]
    assert "JSON" in clf.classify("SYS", SIG, "t")["_error"]
    assert clf.classify("SYS", SIG, "t") is None and clf.errors[0].startswith("连接失败")  # 出错不入库,下次补判
    assert clf.calls == 5 and clf.usage["input"] == 4000 and clf.usage["cache_read"] == 3200


def test_classifier_budget_error_stop_and_no_credentials(env):
    clf = Classifier(LLMConfig(max_calls=1), client=FakeClient([_resp(OK)]))
    assert clf.classify("S", SIG, "t") and clf.classify("S", SIG, "t") is None and "上限 1 次" in clf.note
    clf = Classifier(LLMConfig(), client=FakeClient([_conn_error() for _ in range(6)]))
    for _ in range(6):
        clf.classify("S", SIG, "t")
    assert clf.calls == 5 and not clf.enabled  # 连续出错 5 次就停
    clf = Classifier(LLMConfig())  # 环境里没有凭据
    assert clf.client is None and not clf.enabled and "未配置" in clf.note and clf.classify("S", SIG, "t") is None


def test_run_with_model_merges_links_and_reclassifies_later(env):
    res = _run(env, clf=Classifier(LLMConfig()))  # 第一次没有凭据:只有规则
    assert all(s["llm"] is None for s in res.signals)
    fake = FakeClient([_resp(OK)])
    res = _run(env, clf=Classifier(LLMConfig(), client=fake), today=date(2026, 9, 25))
    assert res.signals == [] and res.reclassified == 1  # 补判上次入库、还没有模型结果的候选线索
    sys_prompt, user = fake.requests[0]["system"][0]["text"], fake.requests[0]["messages"][0]["content"]
    assert "测试国" in sys_prompt and "ZZZ-001 甲市污水处理厂" in sys_prompt and "CHN" in sys_prompt
    assert "Loan ZZ-1234" in user  # 补判时从正文缓存拼回附件文本
    s1 = next(s for s in env.store.load_signals() if s["key"] == "https://ex.test/n/1")
    assert s1["llm"]["summary_zh"].startswith("阿尔法")
    assert [(x["item"], x["by"]) for x in s1["links"]] == [("ZZZ-001", "rule"), ("ZZZ-003", "llm")]  # 台账外编号不收
    s3 = next(s for s in env.store.load_signals() if s["key"] == "https://ex.test/n/3")
    assert s3["llm"] is None  # 非候选不送模型


def test_model_can_mark_irrelevant(env):
    res = _run(env, clf=Classifier(LLMConfig(), client=FakeClient([_resp({**OK, "relevant": False, "category": "other"})])))
    s1 = next(s for s in res.signals if s["key"] == "https://ex.test/n/1")
    assert s1["rules"]["candidate"] and not relevant(s1)  # 模型判定优先于规则


# ---------------------------------------------------------------- DeepSeek 接入(假服务端)


def _ds(handler, **cfg):
    return Classifier(LLMConfig(provider="deepseek", **cfg),
                      client=DeepSeekClient("sk-test", transport=httpx.MockTransport(handler), sleep=lambda s: None))


def _ds_resp(content, finish="stop"):
    return httpx.Response(200, json={
        "model": "deepseek-v4-pro", "usage": {"prompt_tokens": 1200, "prompt_cache_hit_tokens": 1000,
                                              "prompt_cache_miss_tokens": 200, "completion_tokens": 300},
        "choices": [{"index": 0, "finish_reason": finish,
                     "message": {"role": "assistant", "content": content, "reasoning_content": "……"}}]})


def test_deepseek_request_and_normalization(env):
    seen = []
    raw = {**OK, "category": "EOI", "relevant": "true", "amount": {"value": "很多", "currency": "usd"},
           "deadlines": [{"type": "eoi_deadline", "date": "2026-10-12", "evidence": "12 October 2026"},
                         {"type": "whatever", "date": "2026-13-40", "evidence": "坏日期"}],
           "ledger_links": {"item": "zzz-003", "confidence": "HIGH", "reason": "编号"}, "evidence": "Request for EOI"}

    def handler(req):
        seen.append(req)
        return _ds_resp(json.dumps(raw, ensure_ascii=False))

    clf = _ds(handler)
    out = clf.classify("系统提示", SIG, "正文")
    body = json.loads(seen[0].content)
    assert seen[0].url.path == "/chat/completions" and seen[0].headers["authorization"] == "Bearer sk-test"
    assert body["model"] == "deepseek-v4-pro" and body["response_format"] == {"type": "json_object"}
    assert body["thinking"] == {"type": "enabled"} and body["reasoning_effort"] == "high" and body["max_tokens"] == 32000
    assert "JSON" in body["messages"][0]["content"] and body["messages"][1]["content"].startswith("信源:测试")
    # 逐字段归一:类别小写、字符串 true、金额数值不对置空、坏日期丢弃、单个对象当数组、编号大写
    assert out["category"] == "eoi" and out["relevant"] is True and out["amount"] is None
    assert out["deadlines"] == [{"type": "eoi_deadline", "date": "2026-10-12", "evidence": "12 October 2026"}]
    assert out["ledger_links"] == [{"item": "ZZZ-003", "confidence": "high", "reason": "编号"}]
    assert out["evidence"] == ["Request for EOI"] and out["_model"] == "deepseek-v4-pro"
    assert clf.usage == {"input": 200, "output": 300, "cache_read": 1000, "cache_write": 0}
    assert llm_notes(clf)[0] == "模型 deepseek-v4-pro(DeepSeek):调用 1 次,输入 200 tokens(另缓存命中 1,000),输出 300 tokens"


def test_deepseek_retries_errors_and_stop(env):
    replies = [httpx.Response(429, json={"error": {"message": "rate limit"}}), _ds_resp(json.dumps(OK))]
    clf = _ds(lambda req: replies.pop(0))
    assert clf.classify("S", SIG, "t")["category"] == "eoi" and not clf.errors  # 限流重试后成功
    assert "没有返回内容" in _ds(lambda req: _ds_resp("")).classify("S", SIG, "t")["_error"]  # 空内容重试一次仍空
    assert "截断" in _ds(lambda req: _ds_resp('{"relev', finish="length")).classify("S", SIG, "t")["_error"]
    hits = []

    def broke(req):
        hits.append(req)
        return httpx.Response(402, json={"error": {"message": "Insufficient Balance"}})

    clf = _ds(broke)
    assert clf.classify("S", SIG, "t") is None and clf.classify("S", SIG, "t") is None
    assert len(hits) == 1 and not clf.enabled  # 余额不足:本次运行立即停用,不再发请求
    assert llm_notes(clf)[0].startswith("⚠ 模型 API 账户余额不足,请到 platform.deepseek.com 充值")
    clf = _ds(lambda req: httpx.Response(401, json={"error": {"message": "Authentication Fails"}}))
    assert clf.classify("S", SIG, "t") is None and "密钥无效" in clf.fatal


def test_deepseek_credentials_and_run(env, monkeypatch):
    clf = Classifier(LLMConfig(provider="deepseek"))
    assert clf.client is None and "DEEPSEEK_API_KEY" in clf.note
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-x")
    clf = Classifier(LLMConfig(provider="deepseek", model="deepseek-flash"))
    assert isinstance(clf.client, DeepSeekClient) and clf.enabled and clf.label == "deepseek-flash(DeepSeek)"
    with pytest.raises(ValueError, match="provider"):
        LLMConfig(provider="gpt")
    res = _run(env, clf=_ds(lambda req: _ds_resp(json.dumps(OK, ensure_ascii=False))))
    s1 = next(s for s in res.signals if s["key"] == "https://ex.test/n/1")
    assert s1["llm"]["_model"] == "deepseek-v4-pro" and s1["llm"]["summary_zh"].startswith("阿尔法")
    assert [(x["item"], x["by"]) for x in s1["links"]] == [("ZZZ-001", "rule"), ("ZZZ-003", "llm")]


# ---------------------------------------------------------------- 提醒页与周报


def _sig(sid, title, links=(), deadlines=(), first="2026-09-24T01:00:00+00:00", category="tender"):
    return {"id": sid, "country": "ZZZ", "source": "s", "source_name": "测", "kind": "html_list", "key": sid,
            "url": f"https://ex.test/{sid}", "title": title, "published": None, "first_seen": first, "change": "new",
            "fields": {}, "rules": {"candidate": True, "stage": category}, "docs": [], "links": list(links), "excerpt": "",
            "llm": {"relevant": True, "category": category, "summary_zh": title, "new_opportunity": False,
                    "deadlines": [{"type": "pq_deadline", "date": d, "evidence": ""} for d in deadlines]}}


def test_deadline_rows_skip_duplicates_of_ledger():
    rules = load_rules(FX / "rules.yaml")
    led = load_ledger(FX / "ZZZ" / "ledger.yaml", rules)
    today = date(2026, 11, 1)
    sigs = [_sig("aaaaaaaa1", "PQ notice", links=[{"item": "ZZZ-001", "confidence": "high"}], deadlines=["2026-11-15"]),
            _sig("bbbbbbbb2", "Other notice", deadlines=["2026-11-20", "2026-11-20"])]
    rows = deadline_rows(led, [], sigs, today, 30)
    assert [(r[0], r[5]) for r in rows] == [("2026-11-15", "台账"), ("2026-11-20", "线索 bbbbbbbb"), ("2026-12-01", "台账")]


def test_model_confirmation_and_cross_source_dedupe():
    rules = load_rules(FX / "rules.yaml")
    led = load_ledger(FX / "ZZZ" / "ledger.yaml", rules)
    a = _sig("aaaaaaaa1", "业主网站上的意向书", links=[{"item": "ZZZ-003", "confidence": "medium", "by": "rule", "reason": "名称"}],
             deadlines=["2026-10-12"])
    a["llm"]["ledger_links"] = [{"item": "ZZZ-003", "confidence": "high", "reason": "读原文确认"}]
    b = _sig("bbbbbbbb2", "世行接口里的同一份意向书", links=[{"item": "ZZZ-003", "confidence": "high", "by": "rule", "reason": "编号"}])
    b["fields"] = {"deadline": "2026-10-12"}
    assert links_of(a) == [{"item": "ZZZ-003", "confidence": "high", "by": "rule+llm", "reason": "名称"}]  # 模型确认后升级
    assert deadlines(b) == [("2026-10-12", "截止")] and deadlines({**b, "llm": a["llm"]}) == [("2026-10-12", "资格预审截止")]
    rows = deadline_rows(led, [], [a, b], date(2026, 10, 1), 30)
    assert [(r[0], r[2]) for r in rows] == [("2026-10-12", "资格预审截止")]  # 两个信源的同一公告只留一行,类型具体的优先
    assert any("ZZZ-003" in x and "业主网站上的意向书" in x for x in ledger_suggestions([a], led))


def test_triage_hides_handled_signals():
    sigs = [_sig("aaaaaaaa1111", "A"), _sig("bbbbbbbb2222", "B"), _sig("cccccccc3333", "C")]
    left = _untriaged(sigs, {"aaaaaaaa": {"status": "done"}, "bbbbbbbb2222": {"status": "ignore"}, "cccccccc": {"status": "todo"}})
    assert [s["id"] for s in left] == ["cccccccc3333"]


def test_render_inbox_sections():
    md = render_inbox("测试国", [_sig("aaaaaaaa1", "Water tender|x")], None, [], {"sources": {}}, ["s"], {"s": "测试源"},
                      TODAY, CFG, ["提示一"])
    assert md.startswith("# 测试国 · 系统提醒") and "> 提示一" in md
    assert "Water tender/x" in md  # 表格里的竖线被替换
    for h in ("## 一、新线索", "## 二、截止临近", "## 三、建议更新台账", "## 四、过期未更新的计划节点", "## 五、信源状态"):
        assert h in md


def test_outputs_and_weekly_digest(env):
    _run(env)
    fp = write_outputs(env.profile, [], None, TODAY)
    root = env.profile.data_dir
    inbox = (root / "inbox" / "ZZZ.md").read_text(encoding="utf-8")
    assert "Alpha wastewater treatment plant" in inbox and "Road rehabilitation" not in inbox  # 只显示相关线索
    overview = (root / "README.md").read_text(encoding="utf-8")
    assert "[inbox/ZZZ.md](inbox/ZZZ.md)" in overview and "--no-llm" in overview
    assert fp == root / "watch" / "summary.md"
    md, = write_digest(env.profile, "ZZZ", TODAY, docx=False)
    assert md.name == "ZZZ_周报_2026-W39.md"
    text = md.read_text(encoding="utf-8")
    assert "测试国水务项目机会周报(2026-09-18 至 2026-09-24)" in text and "Alpha wastewater" in text
    assert "## 一、商机跟踪进展" in text and "没有在跟踪的商机" in text and "## 二、本周要点" in text  # 周期性报告先讲跟踪进展
    # 标记处理过的线索后,提醒页不再显示
    sid = next(s["id"] for s in env.store.load_signals() if s["key"] == "https://ex.test/n/1")
    (root / "watch" / "triage.yaml").write_text(yaml.safe_dump({sid[:8]: {"status": "done"}}), encoding="utf-8")
    write_outputs(env.profile, [], None, TODAY)
    assert "Alpha wastewater" not in (root / "inbox" / "ZZZ.md").read_text(encoding="utf-8")
    ov = (root / "README.md").read_text(encoding="utf-8")
    assert "[ZZZ_周报_2026-W39](reports/weekly/ZZZ_周报_2026-W39.md)" in ov
    md, = write_digest(env.profile, "ZZZ", TODAY, docx=False)
    assert "Alpha wastewater" in md.read_text(encoding="utf-8")  # 已处理的仍是有效线索,照样进周报
    (root / "watch" / "triage.yaml").write_text(yaml.safe_dump({sid[:8]: {"status": "ignore"}}), encoding="utf-8")
    md, = write_digest(env.profile, "ZZZ", TODAY, docx=False)
    assert "Alpha wastewater" not in md.read_text(encoding="utf-8")  # 忽略的(无关或重复)不进周报
    # 总览的月报链到内部版;同名的公开版(传到表格用)不算
    (root / "reports" / "monthly").mkdir(parents=True, exist_ok=True)
    for name in ("ZZZ_项目机会分析_2026-09-24.md", "ZZZ_项目机会分析_2026-09-24_公开版.md"):
        (root / "reports" / "monthly" / name).write_text("# 月报", encoding="utf-8")
    write_outputs(env.profile, [], None, TODAY)
    ov = (root / "README.md").read_text(encoding="utf-8")
    assert "[ZZZ_项目机会分析_2026-09-24](reports/monthly/ZZZ_项目机会分析_2026-09-24.md)" in ov and "公开版" not in ov


# ---------------------------------------------------------------- 其他采集器


# ---------------------------------------------------------------- Atexo 公共采购平台(摩洛哥门户)

ATEXO_FORM = """<html><body><form id="f" method="post" action="/index.php?page=entreprise.EntrepriseAdvancedSearch&amp;searchAnnCons">
<input type="hidden" name="PRADO_PAGESTATE" value="state-123"/><input type="hidden" name="PRADO_POSTBACK_TARGET" value=""/>
<input type="text" name="ctl0$menu$quickSearch" value="Recherche rapide"/>
<input type="radio" name="ctl0$CONTENU_PAGE$AdvancedSearch$rechercheFloue" value="floue" checked="checked"/>
<input type="radio" name="ctl0$CONTENU_PAGE$AdvancedSearch$rechercheFloue" value="exact"/>
<select name="ctl0$CONTENU_PAGE$AdvancedSearch$annonceType"><option value="1">A</option><option value="3" selected="selected">C</option></select>
<textarea name="ctl0$CONTENU_PAGE$AdvancedSearch$domaineActivite$idsDomaines"></textarea>
<input type="submit" name="ctl0$CONTENU_PAGE$AdvancedSearch$boutonClear" value="Effacer"/>
<input type="submit" name="ctl0$CONTENU_PAGE$AdvancedSearch$lancerRecherche" value="Lancer la recherche"/>
{results}</form></body></html>"""


def _atexo_row(ref, title, org="o8p", buyer="ONEE - BRANCHE EAU", published="10/09/2026", closing="21/10/2026", place="MAROC, TETOUAN"):
    """与门户结果表同样结构的一行(ID 后缀、悬浮提示里的完整标题与地点)。"""
    return f"""<tr><td class="check-col"></td>
<td><div>AOO</div><div class="info-bulle"><div id="c_{ref}_type_procedure">Appel d'offres ouvert</div></div>
<div id="c_{ref}_panelBlocCategorie">Travaux</div><div>{published}</div></td>
<td><div class="objet-line"><span class="ref">R-{ref}</span></div>
<div class="objet-line" id="c_{ref}_panelBlocObjet"><strong>Objet :</strong> {title[:12]}
<div class="info-bulle" id="c_{ref}_infosBullesObjet"><div>{title}</div></div></div>
<div class="objet-line" id="c_{ref}_panelBlocDenomination"><strong>Acheteur public :</strong> {buyer}</div></td>
<td><div id="c_{ref}_panelBlocLieuxExec">MAROC<br/><div class="info-bulle" id="c_{ref}_infosLieuExecution"><div>{place}</div></div></div></td>
<td><div class="cloture-line">{closing}<br/>10:00</div></td>
<td><a href="?page=entreprise.EntrepriseDetailConsultation&amp;refConsultation={ref}&amp;orgAcronyme={org}">Détail</a></td></tr>"""


def _atexo_results(n, rows, state):
    """结果页:检索条件存在页面状态里(没有检索表单了),另有"每页条数"下拉框。"""
    head = f"Nombre de résultats : {n}" if n else "Aucun résultat"
    return (f"<html><body><form method='post' action='/index.php?page=entreprise.EntrepriseAdvancedSearch&amp;searchAnnCons'>"
            f"<input type='hidden' name='PRADO_PAGESTATE' value='{state}'/><input type='hidden' name='PRADO_POSTBACK_TARGET' value=''/>"
            f"<div>{head}</div><select name='ctl0$CONTENU_PAGE$resultSearch$listePageSizeTop'>"
            f"<option value='10' selected='selected'>10</option><option value='500'>500</option></select>"
            f"<table>{''.join(rows)}</table></form></body></html>")


class FakeSession:
    """假会话:按请求返回预设页面,记下每次请求与提交的表单。"""

    def __init__(self, respond):
        self.respond, self.calls = respond, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def get(self, url):
        self.calls.append(("GET", url, None))
        return self.respond("GET", url, None)

    def post(self, url, data):
        self.calls.append(("POST", url, dict(data)))
        return self.respond("POST", url, data)


def test_atexo_search_collector(monkeypatch):
    dom_field, btn = "ctl0$CONTENU_PAGE$AdvancedSearch$domaineActivite$idsDomaines", "ctl0$CONTENU_PAGE$AdvancedSearch$lancerRecherche"
    rows_121 = [_atexo_row(1000 + i, f"Travaux hydrauliques lot {i} du barrage Oulad Talha") for i in range(12)]
    pages = {
        "1.21": _atexo_results(12, rows_121[:10], "res-1.21"),  # 默认每页 10 条
        "res-1.21": _atexo_results(12, rows_121, "res-1.21-all"),
        "1.13": _atexo_results(2, [rows_121[0], _atexo_row(2001, "Renforcement de l'AEP de Tanger", org="d4q")], "res-1.13"),
        "9.99": _atexo_results(0, [], "res-9.99"),
        "kw:STEP boues": _atexo_results(2, [rows_121[1], _atexo_row(3001, "Travaux de réalisation d'une STEP à boues activées à Taza",
                                                                   org="g3h", buyer="SRM FES - MEKNES")], "res-kw"),
        "kw:barrage": _atexo_results(600, rows_121[:3], "res-kw-big"),  # 结果超过每页上限
        "res-kw-big": _atexo_results(600, rows_121[:3], "res-kw-big-all"),
    }

    def respond(method, url, data):
        base = "https://mp.test/index.php?page=entreprise.EntrepriseAdvancedSearch&searchAnnCons"
        if method == "GET":
            return fetch.Page(url, 200, ATEXO_FORM.format(results="").encode(), "text/html; charset=utf-8")
        assert url == base
        if data["PRADO_PAGESTATE"] == "state-123":  # 检索页上点"检索"
            kw = data.get("ctl0$CONTENU_PAGE$AdvancedSearch$keywordSearch")
            if kw:
                assert data[dom_field] == ""  # 关键词检索不限领域
            return fetch.Page(base, 200, pages[f"kw:{kw}" if kw else data[dom_field]].encode(), "text/html; charset=utf-8")
        assert data["PRADO_POSTBACK_TARGET"] == "ctl0$CONTENU_PAGE$resultSearch$listePageSizeTop"  # 结果页上改每页条数
        return fetch.Page(base, 200, pages[data["PRADO_PAGESTATE"]].encode(), "text/html; charset=utf-8")

    ses = FakeSession(respond)
    monkeypatch.setattr(fetch, "session", lambda *a, **k: ses)
    src = {"id": "ma", "url": "https://mp.test/index.php?page=entreprise.EntrepriseAdvancedSearch&AllCons&EnCours",
           "domains": ["1.21", "1.13"], "lang": "fr"}
    ctx = Ctx(iso3="", names=[], wb_code="", since=date(2026, 8, 1), today=TODAY)
    items = KINDS["atexo_search"](src, ctx)
    # 1.21:打开检索页 → 点检索 → 改每页 500 条;1.13 结果不到 10 条,不用翻页
    assert [(m, u.rsplit("&", 1)[-1]) for m, u, _ in ses.calls] == [
        ("GET", "domaineActivite=1.21"), ("POST", "searchAnnCons"), ("POST", "searchAnnCons"),
        ("GET", "domaineActivite=1.13"), ("POST", "searchAnnCons")]
    search = ses.calls[1][2]  # 与浏览器提交的一样:默认值、选中的单选与下拉项,只带"检索"按钮
    assert search[dom_field] == "1.21" and search[btn] == "Lancer la recherche" and "ctl0$CONTENU_PAGE$AdvancedSearch$boutonClear" not in search
    assert search["ctl0$CONTENU_PAGE$AdvancedSearch$rechercheFloue"] == "floue" and search["ctl0$CONTENU_PAGE$AdvancedSearch$annonceType"] == "3"
    assert ses.calls[2][2]["ctl0$CONTENU_PAGE$resultSearch$listePageSizeTop"] == "500"
    assert len(items) == 13 and ctx.warnings == []  # 两个领域都有的同一公告只算一次
    it = next(i for i in items if i.key == "atexo:o8p:1003")
    assert it.url == "https://mp.test/index.php?page=entreprise.EntrepriseDetailConsultation&refConsultation=1003&orgAcronyme=o8p"
    assert it.title == "Travaux hydrauliques lot 3 du barrage Oulad Talha"  # 悬浮提示里的完整标题,不是截断的
    assert (it.published, it.fields["deadline"], it.fields["domain"]) == ("2026-09-10", "2026-10-21", "1.21")
    assert {k: it.fields[k] for k in ("reference", "buyer", "procedure", "category", "place")} == {
        "reference": "R-1003", "buyer": "ONEE - BRANCHE EAU", "procedure": "Appel d'offres ouvert", "category": "Travaux",
        "place": "MAROC, TETOUAN"}
    assert next(i for i in items if i.key == "atexo:d4q:2001").fields["domain"] == "1.13"
    # 没有在招公告不算错;页面结构变了(找不到结果数)要报错,不能悄悄当成 0 条
    assert KINDS["atexo_search"]({**src, "domains": ["9.99"]}, ctx) == []
    pages["1.13"] = ATEXO_FORM.format(results="<table></table>")
    with pytest.raises(RuntimeError, match="结果数"):
        KINDS["atexo_search"]({**src, "domains": ["1.13"]}, ctx)
    # 关键词检索(一组词任一命中):补上只标了顶层大类的公告;从带领域参数的地址进入检索页,再清空领域
    ses.calls.clear()
    items = KINDS["atexo_search"]({**src, "domains": ["1.21"], "keywords": ["STEP boues"]}, ctx)
    assert [u.rsplit("&", 1)[-1] for m, u, _ in ses.calls if m == "GET"] == ["domaineActivite=1.21", "domaineActivite=1.21"]
    assert ses.calls[-1][2]["ctl0$CONTENU_PAGE$AdvancedSearch$keywordSearch"] == "STEP boues"
    step = next(i for i in items if i.key == "atexo:g3h:3001")
    assert "domain" not in step.fields and len(items) == 13  # 与领域结果重复的只算一次
    assert next(i for i in items if i.key == "atexo:o8p:1001").fields["domain"] == "1.21"
    ctx.warnings.clear()
    KINDS["atexo_search"]({**src, "domains": [], "keywords": ["barrage"]}, ctx)
    assert ctx.warnings and "超过每页上限" in ctx.warnings[0]


def test_follow_cuts_contacts_and_min_amount(env):
    """跟进详情页:经办人联系方式截掉不入库;预估金额低于门槛的只记为已见;金额进结构化字段,没模型结果时列表也显示。"""
    fp = Path(env.country.watch)
    wc = yaml.safe_load(fp.read_text(encoding="utf-8"))
    wc["sources"][0].update(follow={"selector": ".body", "cut": ["Contact Administratif"]},
                            min_amount={"label": "Estimation (en Dhs TTC)", "value": 20_000_000, "currency": "MAD"})
    fp.write_text(yaml.safe_dump(wc, allow_unicode=True), encoding="utf-8")
    env.web.pages["https://ex.test/list"] = LIST_HTML.format(
        extra="<tr><td>22/09/2026</td><td><a href='/n/7'>Travaux d’adduction d’eau potable du douar X</a></td>"
              "<td>Testland</td><td>15/10/2026</td></tr>")
    env.web.pages["https://ex.test/n/1"] = ("<div class='body'>Station d'épuration. Estimation (en Dhs TTC) * : 118 200 000,00 @@@@ "
                                            "Contact Administratif : Jane DOE jane@example.test +212500000000</div>")
    env.web.pages["https://ex.test/n/7"] = "<div class='body'>Estimation (en Dhs TTC) * : 321 252,00</div>"
    by_key = {s["key"]: s for s in _run(env).signals}
    assert set(by_key) == {"https://ex.test/n/1", "https://ex.test/n/3"}  # n/7 低于门槛;n/3 非水务不跟进详情,照常存
    s1 = by_key["https://ex.test/n/1"]
    assert s1["fields"]["estimate"] == "118,200,000 MAD" and "Jane" not in s1["excerpt"] and "Contact" not in s1["excerpt"]
    assert "Jane" not in env.store.doc_path("https://ex.test/n/1").read_text(encoding="utf-8")  # 缓存里也没有
    assert env.store.seen("notices", "https://ex.test/n/7") is not None  # 记为已见,下次不再打开详情页
    assert row(s1, TODAY, {})["amount"] == "118,200,000 MAD"


def test_rss_and_worldbank_collectors(monkeypatch):
    rss = """<?xml version="1.0"?><rss><channel>
      <item><title>Irrigation canal loan signed</title><link>https://news.test/1</link>
        <pubDate>Wed, 23 Sep 2026 10:00:00 +0400</pubDate><description>&lt;p&gt;Loan for canal&lt;/p&gt;</description></item>
      <item><title></title><link>https://news.test/2</link></item>
    </channel></rss>"""
    wb = {"procnotices": [{"id": "OP00123", "notice_type": "Invitation for Bids", "project_id": "P500001",
                           "bid_description": "Water supply works", "noticedate": "24-Aug-2026",
                           "submission_deadline_date": "2026-10-30T00:00:00Z", "notice_text": "<p>Bids are invited</p>"}]}
    web = FakeWeb({"https://news.test/rss": rss})

    def get(url, params=None, **kw):
        if "procnotices" in url:
            assert params["project_ctry_code_exact"] == "ZZ"
            return fetch.Page(url, 200, json.dumps(wb).encode(), "application/json")
        return web.get(url)

    monkeypatch.setattr(fetch, "get", get)
    ctx = Ctx(iso3="ZZZ", names=["Testland"], wb_code="ZZ", since=date(2026, 7, 26), today=TODAY)
    items = KINDS["rss"]({"id": "r", "url": "https://news.test/rss", "lang": "en"}, ctx)
    assert [(i.url, i.published, i.text) for i in items] == [("https://news.test/1", "2026-09-23", "Loan for canal")]
    n, = KINDS["wb_procnotices"]({"id": "wb"}, ctx)
    assert n.key == "wb:OP00123" and n.published == "2026-08-24" and n.fields["deadline"] == "2026-10-30"
    assert n.title == "Invitation for Bids:Water supply works" and n.text == "Bids are invited"
