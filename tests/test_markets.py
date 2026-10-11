"""市场分布(经营区域 → 国别)与国别概况:加载与校验、区域筛选、采集范围、指标取值与来源链接、报告调用、国名别称。
全部离线:市场表与数据快照都在临时目录里现写;国别用真实国别码,数值为虚构。"""
from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import yaml

from core import geo
from core import country_facts
from core.country_facts import (BASICS_PATH, WATER_PATH, background_md, check_water, facts_line, fmt, load_basics, load_facts,
                                load_water, overview_md, snapshot_date)
from core.markets import ALL, MarketsError, load_markets, scope_iso3s
from core.report import table
from core.sources import imf_datamapper as imf
from core.sources import wb_country_meta as meta
from core.sources import wdi_indicators as wdi
from core.watch.query import Filter, apply, geo_options, load_rows
from core.watch.reporting import Spec, build
from tests.test_platform import TODAY, _signal, genv, qstore  # noqa: F401  pytest 夹具

MARKETS = """schema: osint-water/markets@1
name: 测试市场分布
org: 测试公司
label: 测试区域
as_of: 2026-09-26
regions:
- name: 中亚片区
  countries:
  - {iso3: UZB, name: 乌兹别克斯坦, local_entity: true, projects: true, staff: true}
  - {iso3: TJK, name: 塔吉克斯坦, local_entity: false, projects: false, staff: false, note: 由乌兹别克国别管理}
- name: 东非片区
  countries:
  - {iso3: KEN, name: 肯尼亚, local_entity: true, projects: false, staff: true}
"""


@pytest.fixture
def mfile(tmp_path):
    fp = tmp_path / "markets.yaml"
    fp.write_text(MARKETS, encoding="utf-8")
    return fp


def test_load_markets_and_presence(mfile, tmp_path):
    m = load_markets(mfile)
    assert (m.name, m.org, m.label, m.regions) == ("测试市场分布", "测试公司", "测试区域", ("中亚片区", "东非片区"))
    assert m.iso3s() == ("UZB", "TJK", "KEN") and m.members("中亚片区") == ["UZB", "TJK"] and m.members(ALL) == ["UZB", "TJK", "KEN"]
    assert m.region_of("KEN") == "东非片区" and m.region_of("PER") == "" and m.get("PER") is None
    assert m.get("UZB").presence() == "已设当地机构、有在建项目、有常驻人员"
    assert m.get("TJK").presence() == "未设机构、无在建项目与常驻人员(由乌兹别克国别管理)"
    assert load_markets(None) is None and load_markets(tmp_path / "none.yaml") is None
    bad = tmp_path / "bad.yaml"
    bad.write_text(MARKETS.replace("iso3: KEN", "iso3: XXX") + "- name: 重复\n  countries:\n  - {iso3: UZB, name: 乌}\n", encoding="utf-8")
    with pytest.raises(MarketsError) as e:
        load_markets(bad)
    assert "XXX" in str(e.value) and "国别重复:UZB" in str(e.value)
    bad.write_text(MARKETS.replace("markets@1", "markets@0"), encoding="utf-8")
    with pytest.raises(MarketsError, match="schema"):
        load_markets(bad)


def test_real_markets_file_loads():
    """真实的市场分布表(私有 profile 引用):能读、每国都归了区域,国别码都认得。"""
    from core.ledger import ROOT
    fp = ROOT / "private" / "markets.yaml"
    if not fp.exists():
        pytest.skip("私有市场分布表不在(公开版会剔除 private/)")
    m = load_markets(fp)
    assert m.countries and m.regions and all(m.region_of(c) for c in m.iso3s())
    basics = load_basics()
    assert all(c in basics for c in m.iso3s())  # 每个市场国别都有静态资料(首都、语言、货币、维基百科条目)
    water = load_water(str(WATER_PATH))["countries"]
    assert all(water.get(c) for c in m.iso3s())  # 每个市场国别都有水务机构、国企与法规


def test_scope_includes_market_countries(mfile):
    prof = SimpleNamespace(iso3s=("AZE", "UZB"), markets=mfile, known_zero_countries=())
    assert scope_iso3s(prof) == ("AZE", "UZB", "TJK", "KEN")
    assert scope_iso3s(SimpleNamespace(iso3s=("AZE",))) == ("AZE",)


def test_adapters_cover_market_countries(mfile, monkeypatch, capsys):
    """WDI、IMF、世行元数据按 profile 国别 + 市场国别采集;市场国别缺数只提示,不整源失败。"""
    prof = SimpleNamespace(iso3s=("AZE",), markets=mfile, known_zero_countries=(), countries=(
        SimpleNamespace(iso2="AZ", iso3="AZE", wb_country_code="AZ", name_en="Azerbaijan", name_zh="阿塞拜疆", tier="focus"),))
    asked = []

    def wdi_json(url, params=None, **kw):
        asked.append(url)
        return [{"page": 1}, [{"countryiso3code": c, "date": "2025", "value": 1.0} for c in ("AZE", "UZB", "KEN")]]

    monkeypatch.setattr(wdi, "get_json", wdi_json)
    monkeypatch.setattr(wdi, "write_raw", lambda *a, **k: None)
    rows = wdi.run(prof)["country_indicator"]
    assert "AZE;UZB;TJK;KEN" in asked[0] and {r["iso3"] for r in rows} == {"AZE", "UZB", "KEN"}
    assert "TJK" in capsys.readouterr().out
    assert next(r for r in rows if r["indicator"] == "ER.H2O.FWTL.K3")["value"] == 10.0  # 10 亿 m³ → 亿 m³

    series = {"values": {code: {"AZE": {"2025": 1.0}, "UZB": {"2025": 2.0, "2027": 3.0}, "KEN": {"2025": 4.0}} for code in imf.INDICATORS}}
    monkeypatch.setattr(imf, "get_json", lambda url, params=None, **kw: {"values": {url.rsplit("/", 1)[-1]: series["values"]["NGDPD"]}})
    monkeypatch.setattr(imf, "write_raw", lambda *a, **k: None)
    rows = imf.run(prof)["country_indicator"]
    assert {r["iso3"] for r in rows} == {"AZE", "UZB", "KEN"} and any(r["is_forecast"] for r in rows if r["iso3"] == "UZB")

    wb = [{"page": 1}, [{"id": c, "name": n, "region": {"value": "Sub-Saharan Africa"}, "incomeLevel": {"value": "Lower middle income"},
                         "lendingType": {"value": "IDA"}, "capitalCity": "X", "longitude": "1", "latitude": "2"}
                        for c, n in (("AZE", "Azerbaijan"), ("UZB", "Uzbekistan"), ("TJK", "Tajikistan"), ("KEN", "Kenya"))]]
    monkeypatch.setattr(meta, "get_json", lambda url, params=None, **kw: wb)
    monkeypatch.setattr(meta, "write_raw", lambda *a, **k: None)
    rows = meta.run(prof)["country"]
    assert [(r["iso3"], r["tier"]) for r in rows] == [("AZE", "focus"), ("UZB", "market"), ("TJK", "market"), ("KEN", "market")]
    assert next(r for r in rows if r["iso3"] == "KEN")["name_zh"] == "肯尼亚"


def test_rows_and_filter_by_market_region(qstore, mfile):  # noqa: F811
    m = load_markets(mfile)
    rows = load_rows(qstore, TODAY, markets=m.region_map())
    assert {r["no"][:2]: r["market"] for r in rows} == {"a1": "中亚片区", "b2": "东非片区", "c3": "", "d4": "东非片区"}

    def nos(**kw):
        return [r["no"][:2] for r in apply(rows, Filter(days=None, status=[], **kw), TODAY)]

    assert nos(markets=["中亚片区"]) == ["a1"]
    assert nos(markets=[ALL]) == ["a1", "b2"]  # 全部市场国别:秘鲁不在表里
    assert nos(markets=["东非片区", "中亚片区"]) == ["a1", "b2"]
    assert nos() == ["a1", "b2", "c3"]


def _snapshot(data, day="2026-09-26"):
    """临时数据目录里的指标与元数据快照:乌兹别克斯坦有 WDI 与 IMF(含预测);肯尼亚的 WDI 通胀停在 2016 年,IMF 有 2025 年。"""
    d = data / "snapshots" / day
    d.mkdir(parents=True)

    def r(iso3, src, code, year, value, fc=False):
        return {"iso3": iso3, "indicator": code, "indicator_label": code, "unit": "", "year": year, "value": value, "source": src,
                "is_forecast": fc, "source_url": "", "fetched_at": "2026-09-26T00:00:00Z"}

    rows = [r("UZB", "WB-WDI", "SP.POP.TOTL", 2025, 37_050_000), r("UZB", "WB-WDI", "NY.GDP.MKTP.CD", 2025, 147_000_000_000),
            r("UZB", "IMF-WEO", "NGDPD", 2025, 146.0), r("UZB", "IMF-WEO", "NGDPD", 2026, 181.5, True),
            r("UZB", "WB-WDI", "NY.GDP.MKTP.KD.ZG", 2025, 7.7), r("UZB", "IMF-WEO", "NGDP_RPCH", 2026, 6.5, True),
            r("UZB", "IMF-WEO", "NGDP_RPCH", 2027, 5.9, True), r("UZB", "WB-WDI", "AG.SRF.TOTL.K2", 2023, 448_969),
            r("UZB", "WB-WDI", "PA.NUS.FCRF", 2025, 12_579.3), r("UZB", "IMF-WEO", "GGXWDG_NGDP", 2025, 28.6),
            r("KEN", "WB-WDI", "FP.CPI.TOTL.ZG", 2016, 6.3), r("KEN", "IMF-WEO", "PCPIPCH", 2025, 4.1),
            r("KEN", "IMF-WEO", "PCPIPCH", 2026, 5.0, True)]
    pq.write_table(pa.Table.from_pylist(rows), d / "country_indicator.parquet")
    pq.write_table(pa.Table.from_pylist([
        {"iso3": "UZB", "iso2": "UZ", "name_en": "Uzbekistan", "income_group": "Lower middle income", "region_wb": "Europe & Central Asia",
         "lending_type": "Blend"},
        {"iso3": "KEN", "iso2": "KE", "name_en": "Kenya", "income_group": "Lower middle income", "region_wb": "Sub-Saharan Africa ",
         "lending_type": "IDA"}]), d / "country.parquet")


def test_country_facts_values_sources_and_links(tmp_path, mfile):
    _snapshot(tmp_path)
    m = load_markets(mfile)
    facts = load_facts(tmp_path, m)
    assert list(facts) == ["UZB", "TJK", "KEN"] and snapshot_date(tmp_path) == "2026-09-26"
    uz, ke, tj = facts["UZB"], facts["KEN"], facts["TJK"]
    gdp = uz.get("gdp")
    assert (gdp.source, gdp.year, gdp.text()) == ("WB-WDI", 2025, "1,470 亿美元(2025)")
    assert gdp.forecasts == [(2026, 181.5e9)] and gdp.forecast_url.endswith("NGDPD@WEO/UZB")
    assert gdp.url == "https://data.worldbank.org/indicator/NY.GDP.MKTP.CD?locations=UZ"
    assert uz.get("growth").forecast_text(2) == "2026 6.5%、2027 5.9%"
    assert uz.get("pop").text() == "3,705 万人(2025)" and uz.get("area").text() == "44.9 万 km²(2023)"
    assert uz.get("fx").text(uz.currency_code) == "12,579 UZS/USD(2025)"
    assert uz.get("gov_debt").source == "IMF-WEO" and uz.get("gov_debt").url.endswith("GGXWDG_NGDP@WEO/UZB")
    infl = ke.get("inflation")  # 世行停在 2016 年,取 IMF 2025 年的实际值
    assert (infl.source, infl.year, infl.value) == ("IMF-WEO", 2025, 4.1) and infl.forecasts == [(2026, 5.0)]
    assert tj.values == {} and tj.market.note == "由乌兹别克国别管理"
    assert uz.income() == "中等偏下收入" and uz.region_wb() == "欧洲与中亚" and ke.lending() == "IDA(优惠贷款)"
    assert ke.region_wb() == "撒哈拉以南非洲"  # 世行接口原值带尾随空格
    links = dict(uz.links())
    assert links["中文维基百科"] == "https://zh.wikipedia.org/zh-cn/%E4%B9%8C%E5%85%B9%E5%88%AB%E5%85%8B%E6%96%AF%E5%9D%A6"
    assert links["English Wikipedia"] == "https://en.wikipedia.org/wiki/Uzbekistan" and links["IMF 国别页"].endswith("/UZB")
    rows = {a: (b, s) for a, b, s, _ in uz.basics_rows()}
    assert rows["首都"] == ("塔什干", "维基百科") and rows["货币"][0] == "乌兹别克斯坦苏姆(UZS)"
    assert rows["测试区域"] == ("中亚片区", "市场分布表") and rows["测试公司在该国"][0] == "已设当地机构、有在建项目、有常驻人员"
    assert "测试公司在该国" not in {a: b for a, b, _, _ in uz.basics_rows(internal=False)}  # 公开版不写使用方情况
    # 静态资料里没有的国别:维基百科给搜索链接
    other = load_facts(tmp_path, m, ["GHA"])["GHA"]
    assert "search=" in dict(other.links())["中文维基百科"] and other.basics == {}


def test_fmt_units():
    assert [fmt(3.14159, "%"), fmt(1.2e9, "亿美元"), fmt(5.5e7, "亿美元"), fmt(3968.4, "美元"), fmt(839_000, "万人"),
            fmt(28_900, "万 km²"), fmt(81.4, "人/km²"), fmt(468.2, "m³"), fmt(429.0, "亿 m³"), fmt(None, "%")] == [
        "3.1%", "12 亿美元", "0.55 亿美元", "3,968 美元", "83.9 万人", "2.9 万 km²", "81 人/km²", "468 m³", "429 亿 m³", "—"]


def test_background_md_and_lines_for_reports(tmp_path, mfile):
    _snapshot(tmp_path)
    uz = load_facts(tmp_path, load_markets(mfile))["UZB"]
    md = "\n".join(background_md(uz, table))
    assert "**基本资料**" in md and "| 首都 | 塔什干 |" in md and "[维基百科](https://zh.wikipedia.org/zh-cn/" in md
    assert "| 经济 | GDP(现价) | 1,470 亿美元(2025) | 2026 1,815 亿美元 |" in md
    assert "[世行 WDI](https://data.worldbank.org/indicator/NY.GDP.MKTP.CD?locations=UZ)、[IMF WEO](" in md
    assert "参考链接:[中文维基百科]" in md and "2026-09-26 的数据快照" in md
    line = facts_line(uz)
    assert line.startswith("乌兹别克斯坦(中亚片区;测试公司已设当地机构") and "IMF 预测 2026 6.5%" in line and "[" not in line
    assert "| 乌兹别克斯坦 | 中亚片区 | 3,705 万人(2025) |" in overview_md([uz], table)


def test_reports_use_markets_and_country_facts(genv, qstore, mfile):  # noqa: F811
    """配了市场分布:报告能按区域取数、范围说明写区域;国别报告写国家概况;简报附涉及国别概况;专题报告按区域分述。"""
    _snapshot(qstore.root.parent)
    prof = replace(genv.profile, markets=mfile)
    brief = build(prof, qstore, Spec("brief", Filter(days=None, markets=["中亚片区"])), TODAY)
    assert "测试区域:中亚片区" in brief and "[a1a1a1a1]" in brief and "[b2b2b2b2]" not in brief
    assert "## 附录 B 涉及国别概况" in brief and "| 乌兹别克斯坦 | 中亚片区 |" in brief
    country = build(prof, qstore, Spec("country", Filter(days=None), country="UZB"), TODAY)
    assert "# 第 2 章 国家概况与宏观经济" in country and "| 首都 | 塔什干 |" in country and "测试公司在该国" in country
    sector = build(prof, qstore, Spec("sector", Filter(days=None, sectors=["wastewater", "water_supply", "desalination"])), TODAY)
    assert "中亚片区(1 条)" in sector and "东非片区(1 条)" in sector and "市场外国别(1 条)" in sector


@pytest.mark.parametrize("text,expect", [
    ("中国企业在刚果（金）承建水厂", ["CHN", "COD"]), ("刚果金水务", ["COD"]), ("刚果布首都布拉柴维尔", ["COG"]),
    ("波黑电站", ["BIH"]), ("沙特NEOM海水淡化", ["SAU"]), ("阿联酋迪拜", ["ARE"]), ("巴新莫尔斯比港", ["PNG"]),
    ("吉尔吉斯比什凯克供水", ["KGZ"]), ("中非合作论坛", []),
])
def test_geo_chinese_short_names(text, expect):
    assert geo.detect(text) == expect


def test_basics_file_is_complete():
    raw = yaml.safe_load(BASICS_PATH.read_text(encoding="utf-8"))
    assert raw["schema"] == "osint-water/country-basics@1"
    for iso3, v in raw["countries"].items():
        assert iso3 in geo.countries() and set(v) == {"capital", "languages", "currency", "currency_code", "wiki_zh", "wiki_en"}, iso3
        assert len(v["currency_code"]) == 3 and " " not in v["wiki_en"], iso3
    assert date.fromisoformat(str(raw["checked"]))


def test_water_file_is_complete():
    """真实的水务机构、国企与法规资料:格式校验无误,国别码都认得,每国三类里至少有机构和法规。"""
    raw = yaml.safe_load(WATER_PATH.read_text(encoding="utf-8"))
    assert raw["schema"] == "osint-water/country-water@1" and date.fromisoformat(str(raw["checked"]))
    assert check_water(raw) == []
    for iso3, c in raw["countries"].items():
        assert iso3 in geo.countries() and c.get("institutions") and c.get("laws"), iso3


def test_geo_filter_options_cascade():
    """筛选栏地理条件逐级收窄:大洲、经营区域 → 地理分区 → 国别;上级改了,不在范围内的已选项去掉,暂时没线索的保留。"""
    rows = [{"country": c, "region": geo.region(c), "continent": geo.continent(c)} for c in ("MAR", "BRA", "NGA", "KEN", "AZE")]
    rows.append({"country": None, "region": "其他", "continent": "其他"})  # 国别未识别的线索
    regions, countries, keep_r, keep_c = geo_options(rows, [], None, [], [])
    assert {"北非", "南美洲", "西非", "东非", "西亚", "其他"} <= set(regions) and "BRA" in countries
    # 选了北非:国别只列北非的,先选的巴西去掉
    regions, countries, keep_r, keep_c = geo_options(rows, [], None, ["北非"], ["BRA"])
    assert countries == ["MAR"] and keep_r == ["北非"] and keep_c == []
    # 大洲选非洲:分区只列非洲的,之前选的西亚去掉
    regions, countries, keep_r, keep_c = geo_options(rows, ["非洲"], None, ["西亚"], [])
    assert set(regions) == {"北非", "西非", "东非"} and keep_r == [] and set(countries) == {"MAR", "NGA", "KEN"}
    # 经营区域(只有尼日利亚、马里):分区只剩西非;没线索的市场国别也可选,区域外已选的肯尼亚去掉
    regions, countries, keep_r, keep_c = geo_options(rows, [], {"NGA", "MLI"}, [], ["KEN"])
    assert regions == ["西非"] and set(countries) == {"NGA", "MLI"} and keep_c == []
    # 地理上符合、只是还没有线索的已选国别保留(方案里的关注国别不被悄悄放宽)
    regions, countries, keep_r, keep_c = geo_options(rows, ["亚洲"], None, ["中亚"], ["KAZ"])
    assert keep_r == ["中亚"] and keep_c == ["KAZ"] and countries == ["KAZ"]



WATER_SAMPLE = """
schema: osint-water/country-water@1
checked: 2026-09-30
countries:
  UZB:
    notes: 测试用的说明
    institutions:
      - name_zh: 乌兹别克斯坦水利部
        name: O'zbekiston Respublikasi Suv xo'jaligi vazirligi
        role: 水资源管理与灌溉
        url: https://example.uz/water
        src:
          - {title: "About the ministry", url: "https://example.uz/water/about", via: primary_read}
    enterprises:
      - name_zh: 乌兹别克斯坦供水公司
        role: 国有供水运营
        url: null
        src:
          - {title: "World Bank project document", url: "https://documents.worldbank.org/x", via: secondary}
    laws:
      - name_zh: 乌兹别克斯坦《水与用水法》
        name: Suv va suvdan foydalanish to'g'risida
        kind: water
        year: 1993
        note: 规定水资源国家所有与用水许可
        url: https://lex.uz/docs/x
        src:
          - {title: "lex.uz", url: "https://lex.uz/docs/x", via: primary_read}
"""


def test_water_institutions_enterprises_and_laws(tmp_path, mfile, monkeypatch):
    """水务管理机构、国企与法规:按国别读进国别概况,报告"国家概况"一节写成带链接的表;校验挑出缺来源、网址、类别的条目。"""
    fp = tmp_path / "country_water.yaml"
    fp.write_text(WATER_SAMPLE, encoding="utf-8")
    monkeypatch.setattr(country_facts, "WATER_PATH", fp)
    _snapshot(tmp_path)
    facts = load_facts(tmp_path, load_markets(mfile))
    uz, ke = facts["UZB"], facts["KEN"]
    assert [label for label, _ in uz.water_items()] == ["水务管理机构", "水务国企", "相关法律法规"] and uz.water_checked == "2026-09-30"
    assert ke.water == {} and ke.water_items() == []
    md = "\n".join(background_md(uz, table))
    assert "**水务管理机构、国企与相关法律法规**" in md
    assert "| 水务管理机构 | 乌兹别克斯坦水利部(O'zbekiston Respublikasi Suv xo'jaligi vazirligi) | 水资源管理与灌溉 | [官网](https://example.uz/water) |" in md
    assert "| 相关法律法规 | 乌兹别克斯坦《水与用水法》(Suv va suvdan foydalanish to'g'risida) | 水法与水资源 · 1993 年;规定水资源国家所有与用水许可 |" in md
    assert "[来源1](https://documents.worldbank.org/x)" in md and "核实于 2026-09-30" in md
    assert "水务管理机构" not in "\n".join(background_md(ke, table))
    assert check_water(yaml.safe_load(WATER_SAMPLE)) == []
    bad = yaml.safe_load(WATER_SAMPLE)
    bad["countries"]["UZB"]["institutions"][0]["src"] = []
    bad["countries"]["UZB"]["enterprises"][0]["url"] = "example.uz"
    bad["countries"]["UZB"]["laws"][0].update(kind="law", year="1993")
    bad["countries"]["UZB"]["laws"][0]["src"][0]["via"] = "wikipedia"
    errs = check_water(bad)
    assert any("没有来源" in e for e in errs) and any("不是 http(s)" in e for e in errs) and any("kind" in e for e in errs)
    assert any("year" in e for e in errs) and any("via" in e for e in errs)
    assert load_water(str(tmp_path / "none.yaml")) == {"checked": "", "countries": {}}
