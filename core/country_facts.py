"""国别概况:把各来源合成一国一份,国别概况页、导出与报告共用。

- 静态资料 reference/country_basics.yaml:首都、官方语言、货币、中英文维基百科条目(条目即出处);
- 世行国别元数据(收入组、世行区域、贷款类别)与指标快照:世行 WDI、IMF《世界经济展望》(含预测),
  来自最近一期 ingest 快照(每月 1 日更新);每个数值带数据年份与来源链接;
- 使用方市场分布(core.markets):所在区域,当地机构、在建项目、常驻人员与托管关系;
- 水务管理机构、水务国企与相关法律法规 reference/country_water.yaml:每条带官网或法律原文链接与来源(注明怎么核实的)。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

import yaml

from core import geo
from core.ledger import ROOT
from core.markets import MarketCountry, Markets

BASICS_PATH = ROOT / "reference" / "country_basics.yaml"
WATER_PATH = ROOT / "reference" / "country_water.yaml"
WATER_PARTS = (("institutions", "水务管理机构"), ("enterprises", "水务国企"), ("laws", "相关法律法规"))
LAW_KIND_ZH = {"water": "水法与水资源", "wss": "供水、排水与污水", "ppp": "PPP 与特许经营", "procurement": "公共采购",
               "env": "环境与环评", "other": "其他"}
VIA_ZH = {"primary_read": "官方原文", "secondary": "权威二手来源", "search_snippet": "仅见检索摘要,待核实"}
# 负面指标的门槛(国别概况页 portal/portal.js 的 TONES 是同一张表,改一处要改另一处;tests/test_country_facts.py 核对两边一致)
# (指标名正则, 判断, 原因):数值落在判断为真的一侧时标红并写明原因
TONES: tuple[tuple[str, object, str], ...] = (
    (r"实际增速|GDP 增速", lambda v: v < 0, "负增长"), (r"通胀|CPI", lambda v: v >= 10, "通胀 ≥ 10%"), (r"政府总债务", lambda v: v >= 70, "债务 ≥ 70% GDP"),
    (r"净借贷", lambda v: v <= -5, "赤字 ≥ 5% GDP"), (r"经常账户", lambda v: v <= -5, "逆差 ≥ 5% GDP"), (r"外债/GNI", lambda v: v >= 60, "外债 ≥ 60% GNI"),
    (r"直接投资净流入", lambda v: v < 0, "净流出"), (r"人口增速", lambda v: v < 0, "人口减少"),
    (r"基本饮水|基本卫生", lambda v: v < 80, "覆盖率 < 80%"), (r"安全管理饮水|安全管理卫生", lambda v: v < 50, "覆盖率 < 50%"),
    (r"淡水压力", lambda v: v >= 75, "取水 ≥ 75% 可再生量"), (r"无安全饮用水|无卫生设施", lambda v: v >= 20, "≥ 20% 人口"), (r"未处理污水", lambda v: v >= 50, "≥ 50%"),
    (r"水风险|用水压力|干旱风险|洪水风险", lambda v: v >= 3, "风险高(≥ 3 分)"),
    (r"营商环境总分|供水得分|公共采购得分|纠纷解决得分", lambda v: v < 50, "低于 50 分,偏弱"), (r"企业供水可靠", lambda v: v < 80, "不到 80%,缺水的企业多"),
    (r"授标天数", lambda v: v > 180, "超过 180 天,授标慢"), (r"回款天数", lambda v: v > 90, "超过 90 天,回款慢"),
)


def lead_number(text: str) -> float | None:
    """文字开头的数(认 −、–、千分位):「-1.7%」→ -1.7;没有数时 None。"""
    m = re.match(r"\s*(-?\d[\d,]*(?:\.\d+)?)", str(text or "").replace("−", "-").replace("–", "-"))
    return float(m.group(1).replace(",", "")) if m else None


def tone(label: str, value: str) -> str | None:
    """指标是不是「负面」:是 → 原因(如「负增长」),否 → None。"""
    v = lead_number(value)
    if v is None:
        return None
    for pat, bad, why in TONES:
        if re.search(pat, label):
            return why if bad(v) else None
    return None


SRC_ZH = {"WB-WDI": "世行 WDI", "IMF-WEO": "IMF WEO"}
INCOME_ZH = {"Low income": "低收入", "Lower middle income": "中等偏下收入", "Upper middle income": "中等偏上收入", "High income": "高收入"}
LENDING_ZH = {"IDA": "IDA(优惠贷款)", "IBRD": "IBRD(非优惠贷款)", "Blend": "IDA 与 IBRD 混合", "Not classified": "不适用"}
REGION_WB_ZH = {"Europe & Central Asia": "欧洲与中亚", "Sub-Saharan Africa": "撒哈拉以南非洲", "Latin America & Caribbean": "拉美与加勒比",
                "East Asia & Pacific": "东亚与太平洋", "South Asia": "南亚", "Middle East & North Africa": "中东与北非",
                "Middle East, North Africa, Afghanistan & Pakistan": "中东、北非、阿富汗与巴基斯坦", "North America": "北美"}


@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    group: str
    wdi: str | None = None  # 世行 WDI 代码(取最新实际值)
    imf: str | None = None  # IMF WEO 代码(没有 WDI 时取其最新实际值;另给后续预测)
    imf_scale: float = 1.0  # IMF 数值换成与 WDI 相同的单位
    unit: str = "%"  # 显示:% | 亿美元 | 美元 | 万人 | 万 km² | 人/km² | m³ | 亿 m³ | 汇率


METRICS: tuple[Metric, ...] = (
    Metric("gdp", "GDP(现价)", "经济", "NY.GDP.MKTP.CD", "NGDPD", 1e9, "亿美元"),
    Metric("gdp_pc", "人均 GDP", "经济", "NY.GDP.PCAP.CD", "NGDPDPC", 1.0, "美元"),
    Metric("gni_pc", "人均 GNI(Atlas 法)", "经济", "NY.GNP.PCAP.CD", None, 1.0, "美元"),
    Metric("growth", "GDP 实际增速", "经济", "NY.GDP.MKTP.KD.ZG", "NGDP_RPCH"),
    Metric("inflation", "CPI 通胀", "经济", "FP.CPI.TOTL.ZG", "PCPIPCH"),
    Metric("agr", "农林渔业增加值/GDP", "经济", "NV.AGR.TOTL.ZS"),
    Metric("ind", "工业增加值/GDP", "经济", "NV.IND.TOTL.ZS"),
    Metric("srv", "服务业增加值/GDP", "经济", "NV.SRV.TOTL.ZS"),
    Metric("gov_debt", "政府总债务/GDP", "财政与外部", None, "GGXWDG_NGDP"),
    Metric("gov_balance", "政府净借贷/GDP(负为赤字)", "财政与外部", None, "GGXCNL_NGDP"),
    Metric("current_account", "经常账户/GDP", "财政与外部", None, "BCA_NGDPD"),
    Metric("ext_debt", "外债总额", "财政与外部", "DT.DOD.DECT.CD", None, 1.0, "亿美元"),
    Metric("ext_debt_gni", "外债/GNI", "财政与外部", "DT.DOD.DECT.GN.ZS"),
    Metric("reserves", "外汇储备(含黄金)", "财政与外部", "FI.RES.TOTL.CD", None, 1.0, "亿美元"),
    Metric("fdi", "外国直接投资净流入", "财政与外部", "BX.KLT.DINV.CD.WD", None, 1.0, "亿美元"),
    Metric("fx", "官方汇率(本币/美元,年均)", "财政与外部", "PA.NUS.FCRF", None, 1.0, "汇率"),
    Metric("pop", "总人口", "人口与国土", "SP.POP.TOTL", "LP", 1e6, "万人"),
    Metric("pop_growth", "人口增速", "人口与国土", "SP.POP.GROW"),
    Metric("urban", "城镇化率", "人口与国土", "SP.URB.TOTL.IN.ZS"),
    Metric("urban_growth", "城镇人口增速", "人口与国土", "SP.URB.GROW"),
    Metric("area", "国土面积", "人口与国土", "AG.SRF.TOTL.K2", None, 1.0, "万 km²"),
    Metric("density", "人口密度", "人口与国土", "EN.POP.DNST", None, 1.0, "人/km²"),
    Metric("water_basic", "基本饮水服务覆盖率", "水资源与供排水", "SH.H2O.BASW.ZS"),
    Metric("water_safe", "安全管理饮水覆盖率", "水资源与供排水", "SH.H2O.SMDW.ZS"),
    Metric("san_basic", "基本卫生设施覆盖率", "水资源与供排水", "SH.STA.BASS.ZS"),
    Metric("san_safe", "安全管理卫生设施覆盖率", "水资源与供排水", "SH.STA.SMSS.ZS"),
    Metric("freshwater_pc", "人均境内可再生淡水", "水资源与供排水", "ER.H2O.INTR.PC", None, 1.0, "m³"),
    Metric("water_stress", "淡水压力(取水/可再生资源)", "水资源与供排水", "ER.H2O.FWST.ZS"),
    Metric("withdrawal", "年淡水取用量", "水资源与供排水", "ER.H2O.FWTL.K3", None, 1.0, "亿 m³"),
    Metric("w_agr", "取水中农业占比", "水资源与供排水", "ER.H2O.FWAG.ZS"),
    Metric("w_ind", "取水中工业占比", "水资源与供排水", "ER.H2O.FWIN.ZS"),
    Metric("w_dom", "取水中生活占比", "水资源与供排水", "ER.H2O.FWDM.ZS"),
)
GROUPS = ("经济", "财政与外部", "人口与国土", "水资源与供排水")
BY_KEY = {m.key: m for m in METRICS}


def _num(x: float, nd: int) -> str:
    s = f"{x:,.{nd}f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def fmt(v: float | None, unit: str, currency: str = "") -> str:
    """按指标单位写成中文读法:GDP 1,149 亿美元、人口 3,640 万人、面积 44.9 万 km²。"""
    if v is None:
        return "—"
    if unit == "%":
        return f"{v:.1f}%"
    if unit == "亿美元":
        a = v / 1e8
        return f"{_num(a, 0 if abs(a) >= 100 else 1 if abs(a) >= 1 else 2)} 亿美元"
    if unit == "美元":
        return f"{_num(v, 0)} 美元"
    if unit == "万人":
        return f"{_num(v / 1e4, 0 if v >= 1e6 else 1)} 万人"
    if unit == "万 km²":
        return f"{_num(v / 1e4, 1 if v >= 1e4 else 2)} 万 km²"
    if unit == "人/km²":
        return f"{_num(v, 0 if v >= 10 else 1)} 人/km²"
    if unit == "m³":
        return f"{_num(v, 0)} m³"
    if unit == "亿 m³":
        return f"{_num(v, 1 if v < 100 else 0)} 亿 m³"
    if unit == "汇率":
        return f"{_num(v, 2 if v < 10 else 0)}" + (f" {currency}/USD" if currency else "")
    return _num(v, 2)


@dataclass
class Value:
    metric: Metric
    value: float
    year: int
    source: str  # WB-WDI | IMF-WEO
    url: str
    forecasts: list[tuple[int, float]] = field(default_factory=list)  # IMF 预测(最新实际值之后的年份)
    forecast_url: str = ""  # 预测的出处(IMF 数据页)

    def text(self, currency: str = "") -> str:
        return f"{fmt(self.value, self.metric.unit, currency)}({self.year})"

    def forecast_text(self, n: int = 3, currency: str = "") -> str:
        return "、".join(f"{y} {fmt(v, self.metric.unit, currency)}" for y, v in self.forecasts[:n])


@dataclass
class CountryFacts:
    iso3: str
    iso2: str
    name_zh: str
    name_en: str
    basics: dict  # reference/country_basics.yaml 里这一国(可能为空)
    meta: dict  # 世行国别元数据(可能为空)
    values: dict[str, Value]
    market: MarketCountry | None = None
    as_of: str = ""  # 指标快照日期
    org: str = "本单位"  # 使用方简称(市场分布表的 org)
    region_label: str = "所在区域"  # 市场分布表的 label,如"经营区域"
    water: dict = field(default_factory=dict)  # reference/country_water.yaml 里这一国:institutions、enterprises、laws、notes
    water_checked: str = ""  # 这份资料的核实日期

    def water_items(self) -> list[tuple[str, dict]]:
        """水务管理机构、国企、相关法律法规 → [(类别名, 条目)],按这个顺序。"""
        return [(label, e) for part, label in WATER_PARTS for e in (self.water.get(part) or [])]

    def get(self, key: str) -> Value | None:
        return self.values.get(key)

    @property
    def currency_code(self) -> str:
        return str(self.basics.get("currency_code") or "")

    def _meta(self, key: str, zh: dict) -> str:
        v = str(self.meta.get(key) or "").strip()  # 世行接口的个别取值带尾随空格,如 "Sub-Saharan Africa "
        return zh.get(v, v or "—")

    def income(self) -> str:
        return self._meta("income_group", INCOME_ZH)

    def region_wb(self) -> str:
        return self._meta("region_wb", REGION_WB_ZH)

    def lending(self) -> str:
        return self._meta("lending_type", LENDING_ZH)

    def links(self) -> list[tuple[str, str]]:
        """参考链接:维基百科(中、英)、世界银行国别数据、IMF 国别页。"""
        b = self.basics
        zh = (f"https://zh.wikipedia.org/zh-cn/{quote(b['wiki_zh'])}" if b.get("wiki_zh")
              else f"https://zh.wikipedia.org/w/index.php?search={quote(self.name_zh)}")
        en = (f"https://en.wikipedia.org/wiki/{quote(b['wiki_en'])}" if b.get("wiki_en")
              else f"https://en.wikipedia.org/w/index.php?search={quote(self.name_en)}")
        out = [("中文维基百科", zh), ("English Wikipedia", en)]
        if self.iso2:
            out.append(("世界银行国别数据", f"https://data.worldbank.org/country/{self.iso2}"))
        out.append(("IMF 国别页", f"https://www.imf.org/en/Countries/{self.iso3}"))
        return out

    def basics_rows(self, internal: bool = True) -> list[tuple[str, str, str, str]]:
        """→ [(项目, 内容, 来源名, 来源链接)]:首都、语言、货币、面积、收入组、世行区域与贷款类别;internal 时另加使用方在该国的情况。"""
        wiki = self.links()[0][1]
        rows = []
        for k, label in (("capital", "首都"), ("languages", "官方语言"), ("currency", "货币")):
            v = self.basics.get(k)
            if v:
                if k == "currency" and self.currency_code:
                    v = f"{v}({self.currency_code})"
                rows.append((label, str(v), "维基百科", wiki))
        if (a := self.get("area")) is not None:
            rows.append(("国土面积", a.text(), SRC_ZH[a.source], a.url))
        if self.meta:
            wb = f"https://data.worldbank.org/country/{self.iso2}" if self.iso2 else ""
            rows.append(("世行收入组", self.income(), "世界银行国别分类", wb))
            rows.append(("世行区域 / 贷款类别", f"{self.region_wb()} / {self.lending()}", "世界银行国别分类", wb))
        if self.market is not None and internal:
            rows.append((self.region_label, self.market.region, "市场分布表", ""))
            rows.append((f"{self.org}在该国", self.market.presence(), "市场分布表", ""))
        return rows


@lru_cache(maxsize=4)
def load_water(path: str = str(WATER_PATH)) -> dict:
    """水务管理机构、国企与法律法规 → {checked: 核实日期, countries: {ISO3: {institutions, enterprises, laws, notes}}};没有文件时为空。"""
    fp = Path(path)
    raw = (yaml.safe_load(fp.read_text(encoding="utf-8")) or {}) if fp.exists() else {}
    return {"checked": str(raw.get("checked") or ""), "countries": {str(k).upper(): v or {} for k, v in (raw.get("countries") or {}).items()}}


def check_water(raw: dict) -> list[str]:
    """校验 country_water.yaml 的内容(测试里跑):每条有中文名与至少一个 http(s) 来源,来源注明怎么核实的;
    官网、原文网址是 http(s) 或空;法规有类别、年份是整数。→ 问题清单,没有问题时为空。"""
    errs = []
    for iso3, c in (raw.get("countries") or {}).items():
        if not (isinstance(iso3, str) and len(iso3) == 3 and iso3.isupper()):
            errs.append(f"{iso3}: 国别码应为 3 位大写字母")
        for part, _ in WATER_PARTS:
            for i, e in enumerate((c or {}).get(part) or []):
                where = f"{iso3}.{part}[{i}]"
                if not str(e.get("name_zh") or "").strip():
                    errs.append(f"{where}: 没有中文名")
                if e.get("url") is not None and not str(e["url"]).startswith(("https://", "http://")):
                    errs.append(f"{where}: 官网/原文网址不是 http(s):{e['url']!r}")
                srcs = e.get("src") or []
                if not srcs:
                    errs.append(f"{where}: 没有来源")
                for s in srcs:
                    if not str(s.get("url") or "").startswith(("https://", "http://")):
                        errs.append(f"{where}: 来源网址不是 http(s):{s.get('url')!r}")
                    if s.get("via") not in VIA_ZH:
                        errs.append(f"{where}: 来源的 via 应为 {'、'.join(VIA_ZH)}:{s.get('via')!r}")
                if part == "laws":
                    if e.get("kind") not in LAW_KIND_ZH:
                        errs.append(f"{where}: 法规类别 kind 应为 {'、'.join(LAW_KIND_ZH)}:{e.get('kind')!r}")
                    if e.get("year") is not None and not isinstance(e["year"], int):
                        errs.append(f"{where}: 年份 year 应为整数:{e['year']!r}")
    return errs


@lru_cache(maxsize=1)
def load_basics(path: str = str(BASICS_PATH)) -> dict[str, dict]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return {str(k).upper(): v for k, v in (raw.get("countries") or {}).items()}


def _latest(data_dir: Path, table: str) -> Path | None:
    snaps = sorted((Path(data_dir) / "snapshots").glob(f"*/{table}.parquet"))
    return snaps[-1] if snaps else None


def snapshot_date(data_dir: Path) -> str:
    fp = _latest(data_dir, "country_indicator")
    return fp.parent.name if fp else ""


def _rows(fp: Path | None, iso3s: list[str] | None) -> list[dict]:
    if fp is None:
        return []
    import duckdb

    where = ""
    if iso3s is not None:
        where = " where iso3 in (" + ",".join("'" + c.replace("'", "") + "'" for c in iso3s) + ")"
    rel = duckdb.sql(f"select * from '{fp}'{where}")
    cols = rel.columns
    return [dict(zip(cols, r)) for r in rel.fetchall()]


def _values(rows: list[dict], iso2: str, iso3: str) -> dict[str, Value]:
    series: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        series.setdefault((r["source"], r["indicator"]), []).append(r)
    out: dict[str, Value] = {}
    for m in METRICS:
        base = sorted(series.get(("WB-WDI", m.wdi)) or [], key=lambda r: r["year"]) if m.wdi else []
        imf = sorted(series.get(("IMF-WEO", m.imf)) or [], key=lambda r: r["year"]) if m.imf else []
        wdi_act = [r for r in base if not r["is_forecast"]]
        imf_act = [r for r in imf if not r["is_forecast"]]
        # 取最新实际值:两家都有时取年份新的(世行某些国别的数据停在多年前,如刚果(金)的 CPI);同年以世行为准
        if wdi_act and (not imf_act or wdi_act[-1]["year"] >= imf_act[-1]["year"]):
            last, src = wdi_act[-1], "WB-WDI"
        elif imf_act:
            last, src = imf_act[-1], "IMF-WEO"
        else:
            continue
        scale = m.imf_scale if src == "IMF-WEO" else 1.0
        imf_url = f"https://www.imf.org/external/datamapper/{m.imf}@WEO/{iso3}" if m.imf else ""
        url = f"https://data.worldbank.org/indicator/{m.wdi}?locations={iso2}" if src == "WB-WDI" else imf_url
        fc = [(int(r["year"]), float(r["value"]) * m.imf_scale) for r in imf if r["is_forecast"] and r["year"] > last["year"]]
        out[m.key] = Value(m, float(last["value"]) * scale, int(last["year"]), src, url, fc, imf_url if fc else "")
    return out


def load_facts(data_dir: Path, markets: Markets | None = None, iso3s: list[str] | None = None) -> dict[str, CountryFacts]:
    """→ {ISO3: 国别概况}。iso3s 不给时取市场分布表里的国别(没有市场表时取快照里有的全部国别)。"""
    fp_ind, fp_meta = _latest(data_dir, "country_indicator"), _latest(data_dir, "country")
    if iso3s is None:
        iso3s = list(markets.iso3s()) if markets else sorted({r["iso3"] for r in _rows(fp_meta, None)})
    ind = _rows(fp_ind, iso3s)
    meta = {r["iso3"]: r for r in _rows(fp_meta, iso3s)}
    basics = load_basics()
    wdata = load_water(str(WATER_PATH))
    water = wdata["countries"]
    as_of = fp_ind.parent.name if fp_ind else ""
    out = {}
    for iso3 in iso3s:
        g = geo.countries().get(iso3, {})
        iso2 = g.get("iso2") or (meta.get(iso3) or {}).get("iso2") or ""
        out[iso3] = CountryFacts(
            iso3=iso3, iso2=iso2, name_zh=g.get("name_zh") or iso3, name_en=g.get("name_en") or (meta.get(iso3) or {}).get("name_en") or iso3,
            basics=basics.get(iso3) or {}, meta=meta.get(iso3) or {}, values=_values([r for r in ind if r["iso3"] == iso3], iso2, iso3),
            market=markets.get(iso3) if markets else None, as_of=as_of, org=markets.org if markets else "本单位",
            region_label=markets.label if markets else "所在区域", water=water.get(iso3) or {}, water_checked=wdata["checked"])
    return out


def water_desc(e: dict) -> str:
    """一条的说明:法规写类别、年份与说明,机构与国企写职能。"""
    if "kind" in e:
        head = LAW_KIND_ZH.get(e.get("kind"), "") + (f" · {e['year']} 年" if e.get("year") else "")
        return head + (f";{e['note']}" if e.get("note") else "")
    return str(e.get("role") or "")


# ---------- 报告与模型用 ----------

def background_md(f: CountryFacts, table, internal: bool = True) -> list[str]:
    """报告"国家概况与宏观经济"一节:基本资料表 + 分组指标表(最新值、IMF 预测、来源链接)+ 参考链接。
    table 为报告的表格函数(core.report.table),单元格里可以放 Markdown 链接;公开版(internal=False)不写使用方在该国的情况。"""
    out = []
    rows = f.basics_rows(internal)
    if rows:
        out += ["**基本资料**\n", table(["项目", "内容", "来源"], [[a, b, f"[{s}]({u})" if u else s] for a, b, s, u in rows]), ""]
    body = []
    for grp in GROUPS:
        for m in METRICS:
            v = f.get(m.key)
            if m.group != grp or v is None or m.key == "area":
                continue
            src = f"[{SRC_ZH[v.source]}]({v.url})" + (f"、[IMF WEO]({v.forecast_url})" if v.forecast_url and v.source != "IMF-WEO" else "")
            body.append([grp, m.label, v.text(f.currency_code), v.forecast_text(3, f.currency_code) or "—", src])
    if body:
        out += ["**经济、人口与水资源指标**\n", table(["类别", "指标", "最新值(年份)", "IMF 预测", "来源"], body), ""]
    if items := f.water_items():
        rows = [[label, e["name_zh"] + (f"({e['name']})" if e.get("name") else ""), water_desc(e) or "—",
                 f"[{'原文' if 'kind' in e else '官网'}]({e['url']})" if e.get("url") else "—",
                 "、".join(f"[来源{i}]({x['url']})" for i, x in enumerate(e.get("src") or [], 1))] for label, e in items]
        out += ["**水务管理机构、国企与相关法律法规**\n", table(["类别", "名称", "职能 / 说明", "官网 / 原文", "来源"], rows),
                f"(据各自官网、法律原文与权威资料整理,核实于 {f.water_checked or '—'};机构改组频繁,以官网为准。)\n"]
    out.append("参考链接:" + "、".join(f"[{t}]({u})" for t, u in f.links()) + "。"
               f"指标取自 {f.as_of or '最近一期'} 的数据快照(世行 WDI、IMF《世界经济展望》接口,每月 1 日更新),括号内为数据年份;"
               "首都、语言、货币据维基百科条目整理。\n")
    return out


def facts_line(f: CountryFacts, keys: tuple[str, ...] = ("pop", "gdp", "gdp_pc", "growth", "inflation", "gov_debt", "urban",
                                                        "water_basic", "san_basic")) -> str:
    """给模型的一行:国名(区域;本单位情况):各项最新值(年份),增速附 IMF 预测。不用方括号,以免与线索号混淆。"""
    parts = []
    for k in keys:
        v = f.get(k)
        if v is None:
            continue
        s = f"{BY_KEY[k].label} {v.text(f.currency_code)}"
        if k == "growth" and v.forecasts:
            s += f",IMF 预测 {v.forecast_text(2)}"
        parts.append(s)
    head = f.name_zh + (f"({f.market.region};{f.org}{f.market.presence()})" if f.market else "")
    return f"{head}:" + ";".join(parts) + (f";收入组 {f.income()}" if f.meta else "")


def overview_md(facts: list[CountryFacts], table) -> str:
    """多国一览(简报、专题报告的附录):人口、GDP、人均 GDP、增速与 IMF 预测、通胀、城镇化率、基本饮水,带数据年份。"""
    body = []
    for f in facts:
        g = f.get("growth")
        body.append([f.name_zh, f.market.region if f.market else "—", *(f.get(k).text(f.currency_code) if f.get(k) else "—"
                                                                        for k in ("pop", "gdp", "gdp_pc")),
                     (g.text() + (f";{g.forecast_text(1)}" if g.forecasts else "")) if g else "—",
                     *(f.get(k).text() if f.get(k) else "—" for k in ("inflation", "urban", "water_basic"))])
    return table(["国别", facts[0].region_label if facts else "区域", "人口", "GDP", "人均 GDP", "增速;IMF 预测", "通胀", "城镇化率",
                  "基本饮水"], body)
