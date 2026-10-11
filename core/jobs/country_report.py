"""国别报告(A14 的「国别报告」类型):章节照阿塞拜疆成稿(private/materials/AZE/…成稿)的结构写,数据从库里取,分析段落由模型按章写。

成稿是人工调研的长篇报告;这里能自动做到的是同一套章节与版式:每章先放库里有的数(指标卡、表格),再放模型据这些数写的分析。
库里没有、模型也不该编的(如市场规模测算、合同条件)写明「需人工补充」,不凭空给数。
章节目录 OUTLINE 是唯一的来源:报告按它出,报告模板里的章节目录也由它生成(scripts/make_report_template.py)。
没配模型时只出数据与要点,分析段落从略。
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import timedelta
from functools import lru_cache

from core import geo, tstore
from core.watch import reporting
from core.zh_name import FUNDERS, TERMS_PATH

from . import reports as R
from .common import Ctx

# 章节目录:(章, [(节, 这一节放什么)]);和成稿一样「第 N 章」+「N.M 节」。改章节只改这里,模板跟着重新生成。
OUTLINE: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("研究背景、目的与范围", (("范围与口径", "国别、编制日期、版本(内部版 / 公开版)与统计口径"),
                            ("数据来源与方法", "本报告用到的数据及其来源、更新时间"))),
    ("国家概况与宏观经济环境", (("基本情况", "国名、区域、首都、语言、货币、收入组与贷款类别、我方在当地"),
                               ("关键指标", "8 项关键指标卡,超过警戒线的标红"),
                               ("宏观经济", "经济、财政与外部指标表 + 分析"))),
    ("人口、城镇化与用水需求", (("人口与城镇化", "人口、城镇化指标表 + 分析"),
                               ("主要城市", "主要城市的人口、经济、水风险与水厂"))),
    ("水资源与气候条件", (("水资源与用水结构", "可再生淡水、取水量与结构、淡水压力"),
                          ("水风险", "WRI Aqueduct 用水压力、干旱、洪水 + 分析"))),
    ("水务行业体制、法规与规划", (("管理机构", "水务管理机构与职责"), ("水务国企", "供排水运营企业"),
                                 ("法律法规", "相关法律法规"), ("水价", "主要城市现行水价"), ("体制要点", "分析"))),
    ("全国供水现状", (("覆盖率与服务水平", "饮水服务覆盖率"), ("水厂", "库里登记的自来水厂"), ("现状分析", "分析"))),
    ("全国污水与排水现状", (("收集与处理", "卫生设施覆盖率"), ("污水处理厂", "库里登记的污水厂"), ("现状分析", "分析"))),
    ("首都水务专题", (("概况", "首都的人口、经济、水风险、水价与水厂"), ("分析", "首都市场的机会与难点"))),
    ("投资规划与资金来源", (("资金方分布", "商机的出资方与融资方式"), ("已识别项目的投资规模", "商机的投资规模(折合人民币)"),
                            ("国际金融机构涉水项目", "世行项目库统计"), ("分析", "资金来源与投资节奏"))),
    ("竞争格局", (("商机参与方", "商机上记录的业主、咨询、承包商与同业动向"), ("分析", "业主、承包商与竞争态势"))),
    ("市场准入与商务环境", (("准入与竞争要点", "国别表「市场准入与竞争」"), ("分析", "招标采购、资质与本地化要求"))),
    ("机会识别与进入策略", (("已立项跟踪的商机", "人工确认、正在跟踪或已结束的商机及阶段"), ("AI 建议的商机", "机器建议、待人决定跟不跟的商机及理由"),
                            ("未来时间节点", "这些商机未来 30 天的截止与节点"), ("重点机会研判", "分析"), ("行动建议", "分析"))),
    ("风险分析与建议", (("需要留意的指标", "超过警戒线的指标"), ("风险提示", "分析"), ("结论与建议", "分析"))),
)
APPENDIX = (("数据来源登记", "各项数据的来源与时间"), ("术语与缩略语", "报告里出现的机构缩写与中文全称"))

COUNTRY_SYSTEM = """你是投标人国籍为 {bidder} 的水务工程承包与投资企业的市场分析师,为管理层撰写《{country}水务市场分析报告》中的分析段落。
写法参照专业咨询报告:先给判断,再给依据;用简洁、专业的中文书面语,不写客套话,不用 Markdown 标题。
数字、项目、机构与日期只用下面给出的数据;引用具体商机时在句末用方括号注明商机编号,如 [UZB-001]。
可以补充你确知的公开背景(如机构沿革、重大政策方向),这类句子句末标注「(公开资料,待核实)」,且不得给出数据里没有的具体数字和日期。
数据不足以下结论时,直接写「本系统暂无该项数据,需人工补充」。"""

FIELDS_A = {
    "summary": "摘要:3–5 段,每段 2–4 句;依次写市场总体判断、最值得跟进的机会与时间、资金来源特点、主要风险与建议",
    "macro": "宏观经济与营商环境分析:200–400 字,经济规模与增速、财政与外债、汇率与支付能力,对水务投资的含义",
    "population": "人口、城镇化与用水需求分析:150–300 字,人口与城镇化趋势、主要城市分布,对供排水需求的含义",
    "water": "水资源压力与气候风险分析:150–300 字,用水压力、取水结构、干旱与洪水风险,对项目类型的含义",
    "governance": "体制与法规要点:150–300 字,管理机构与国企的分工、近期改革、法规对外资承包商的影响",
}
FIELDS_B = {
    "supply": "全国供水现状分析:150–300 字,覆盖率与服务水平、水厂情况、主要短板",
    "sewage": "全国污水与排水现状分析:150–300 字,卫生设施覆盖、污水厂规模与处理级别、主要短板",
    "capital": "首都水务专题分析:150–300 字,首都的需求、在建与规划项目、机会与难点",
    "funding": "投资规划与资金来源分析:150–300 字,主要出资方、贷款类别、已识别项目规模与投资节奏",
    "competition": "竞争格局分析:150–300 字,业主与采购方式、近期中标的承包商与国别、竞争态势",
}
FIELDS_C = {
    "access": "市场准入与商务环境分析:150–300 字,招标采购流程、资质与本地化、对我方的要求",
    "opportunities": "重点机会研判:最值得跟进的 3–8 个机会,Markdown 列表;每个写明是什么、为什么值得跟进、关键日期、资格限制或风险",
    "actions": "行动建议:按紧迫程度排序的具体行动,Markdown 列表,写明建议完成时间",
    "risks": "风险提示:4–6 条,Markdown 列表,每条标明风险高低(高 / 中 / 低),涉及政治、经济与支付、法律合规、执行",
    "conclusion": "结论与建议:3–5 条,Markdown 列表,给管理层的明确建议",
}
NO_DATA = "本系统暂无该项数据,需人工补充。"
CITY_ROWS = 15
PLANT_ROWS = 10


def _num(v: object) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _fmt(v: float | None, nd: int = 0, unit: str = "") -> str:
    if v is None:
        return ""
    s = f"{v:,.{nd}f}"
    return s + (f" {unit}" if unit else "")


def _risk(v: float | None) -> str:
    if v is None:
        return ""
    lv = "极高" if v >= 4 else "高" if v >= 3 else "中到高" if v >= 2 else "低到中" if v >= 1 else "低"
    return f"{v:.1f} 分({lv})" + (f" {R.BAD_LABEL}(≥ 3 分)" if v >= 3 else "")


def _rows_of(rows: list[dict], group: str) -> list[dict]:
    return [r for r in rows if R.GROUP_OF.get(r["k"], "其他") == group]


def _ind_table(rows: list[dict]) -> str:
    return R._table(["指标", "数值", "年份", "来源"], [[R._md_text(r["k"]), R._val_md(r), r["year"], R._src_md(r)] for r in rows]) if rows else ""


def _para(text: str | None, fallback: str = "") -> str:
    t = reporting._plain(text) if text else ""
    return (t or fallback).strip() + "\n" if (t or fallback) else ""


class _Book:
    """按 OUTLINE 编号:章用「# 第 N 章 名」,节用「## N.M 名」;节名取自 OUTLINE,内容为空的节整节不出。"""

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.n = 0

    def chapter(self, i: int, sections: dict[str, str]) -> None:
        title, secs = OUTLINE[i]
        body = [(name, sections.get(name, "").strip()) for name, _ in secs]
        body = [(name, txt) for name, txt in body if txt]
        if not body:
            return
        self.n += 1
        self.parts.append(f"# 第 {self.n} 章 {title}\n")
        for m, (name, txt) in enumerate(body, 1):
            self.parts.append(f"## {self.n}.{m} {name}\n\n{txt}\n")


def _write(ctx: Ctx, country: str, dataset: str, fields: dict[str, str], known: set[str]) -> tuple[dict, str | None]:
    clf = ctx.clf
    if clf is None or not getattr(clf, "enabled", False) or not hasattr(clf, "write"):
        return {}, None
    system = COUNTRY_SYSTEM.format(bidder=(ctx.profile.bidder or {}).get("nationality") or "未设", country=country)
    out = clf.write(system, dataset, fields)
    if not out or out.get("_error"):
        return {}, (out or {}).get("_error") or "模型未返回"
    out, _ = reporting._check_cites(out, known)
    return out, None


@lru_cache(maxsize=1)
def _funder_names() -> tuple[tuple[re.Pattern, str], ...]:
    names = json.loads(TERMS_PATH.read_text(encoding="utf-8"))["terms"]
    out = []
    for a in FUNDERS:
        if a in names:
            out.append((re.compile(r"(?<![A-Za-z])" + re.escape(a) + r"(?![A-Za-z])"), f"{names[a]}({a})"))
            out.append((re.compile(re.escape(names[a])), f"{names[a]}({a})"))
    for en, zh in names.items():  # 全称写法(Asian Infrastructure Investment Bank、World Bank……)
        if (len(en) > 6 and any(zh == names.get(a) for a in FUNDERS)) or en == "World Bank":
            out.append((re.compile(re.escape(en), re.IGNORECASE), next((f"{zh}({a})" for a in FUNDERS if names.get(a) == zh), zh)))
    return tuple(out)


def _funder(text: str) -> str:
    """出资方的写法统一:「AIIB(亚投行)」「Asian Infrastructure Investment Bank (AIIB)」→「亚投行(AIIB)」;认不出的原样。"""
    for pat, name in _funder_names():
        if pat.search(text):
            return name
    return text


def _strip_refs(text: str) -> str:
    """去掉出处线索号「[4d3f6f4e]」「〔R110〕」:国别报告不列线索。"""
    return re.sub(r"\s*[\[〔][0-9A-Za-z]{3,10}[\]〕]", "", text).strip()


def _capital(basics: list[dict]) -> str:
    r = next((x for x in basics if x["k"] in ("首都", "首都或主要城市")), None)
    return re.sub(r"[((].*$", "", r["v"]).strip() if r else ""


def build_country(ctx: Ctx, iso: str, internal: bool = True) -> tuple[str, set[str], set[str], str]:
    """→ (Markdown, 引用的线索记录编号, 写进的商机记录编号, ISO3)。"""
    b = ctx.base
    crid = ctx.country_rid(iso)
    c = b.record("country", crid)
    g = lambda p: b.get("country", c, p) if b.has("country", p) else None  # noqa: E731
    name = str(g("name_zh") or geo.name_zh(iso) or iso)
    ind, basics = R.fact_rows(g("indicators")), R.fact_rows(g("basics"))
    kp = R._kpis(ind)

    # ---- 取数:商机、城市、水厂(使用方 2026-10-10:国别报告不放线索,只写人工确认与 AI 建议的商机;「不跟进」的不写)
    opps = [r for r in R._opps(ctx, iso) if str(b.get("opportunity", r, "stage") or "建议") != "不跟进"]
    order = {s: i for i, s in enumerate(R.STAGE_ORDER)}
    opps.sort(key=lambda r: (order.get(str(b.get("opportunity", r, "stage")), 99), str(b.get("opportunity", r, "code"))))
    code_of = {r["id"]: str(b.get("opportunity", r, "code") or "") for r in opps}
    ms = R._milestones(ctx, ctx.today, ctx.today + timedelta(days=R.UPCOMING_DAYS), set(code_of))
    og = lambda r, p: b.get("opportunity", r, p) if b.has("opportunity", p) else None  # noqa: E731
    ai_opps = [r for r in opps if str(og(r, "stage") or "建议") == "建议"]
    own_opps = [r for r in opps if r not in ai_opps]
    cities = b.rows("city", filter=b.filter(b.cond("city", "country", "is", crid))) if "city" in b.t and b.has("city", "country") else []
    cities.sort(key=lambda r: -(_num(b.get("city", r, "population")) or 0))
    plants = {t: (b.rows(t, filter=b.filter(b.cond(t, "country", "is", crid))) if t in b.t and b.has(t, "country") else [])
              for t in ("supply_plant", "sewage_plant")}
    cg = lambda r, p: b.get("city", r, p)  # noqa: E731

    # ---- 数据块
    sec: dict[str, dict[str, str]] = {}
    sec["研究背景、目的与范围"] = {
        "范围与口径": (f"本报告分析{name}的城镇供水、污水与排水市场,面向海外业务,版本为{'内部版' if internal else '公开版'},编制日期 {ctx.today.isoformat()}。"
                    f"商机口径:已立项跟踪的 {len(own_opps)} 个、AI 建议的 {len(ai_opps)} 个(标为不跟进的不计)。"
                    "金额按入库当日汇率折合人民币。\n"),
        "数据来源与方法": "\n".join(f"- {x}" for x in (
            "宏观与水务指标:世界银行 WDI、IMF《世界经济展望》(每月 1 日更新)",
            "水风险:WRI Aqueduct 4.0;人均 GDP(PPP):Kummu 等;世行涉水项目:世行项目库",
            "城市:GeoNames;水厂与污水厂:HydroWASTE 等公开数据集与人工补录",
            "机构、国企与法规:人工核实的国别资料(附来源)",
            "商机:人工立项跟踪的商机与 AI 建议的商机(公开事实由机器从采购公告与新闻整理,我方判断由人填写)",
            "分析段落:模型据以上数据撰写,引用处标商机编号,以原文为准",
        )) + "\n",
    }
    base_rows = [(h, R._md_text(str(v))) for h, v in (("国名", name), ("大洲与区域", g("region")), ("市场分级", g("tier")),
                                                     ("收入组与贷款类别", g("income_group")), ("经营区域", g("market")),
                                                     ("我方在当地", R._cell(g("presence")) if internal else None)) if v]
    seen = {h for h, _ in base_rows}
    base_rows += [(r["k"], R._val_md(r) + (f"({r['year']})" if r["year"] else "") + (f" {R._src_md(r)}" if R._src_md(r) else "")) for r in basics
                  if r["k"] and r["k"] not in seen and not (r["k"] == "世行收入组" and "收入组与贷款类别" in seen)]
    half = (len(base_rows) + 1) // 2
    pair = [list(base_rows[i]) + (list(base_rows[i + half]) if i + half < len(base_rows) else ["", ""]) for i in range(half)]
    used = {id(r) for _, r in kp}
    sec["国家概况与宏观经济环境"] = {
        "基本情况": R._table(["项目", "内容", "项目", "内容"], pair) if pair else "",
        "关键指标": R.kpi_table(kp),
        "宏观经济": "\n".join(x for x in (_ind_table([r for r in _rows_of(ind, "经济") if id(r) not in used]),
                                         _ind_table([r for r in _rows_of(ind, "财政与外部") if id(r) not in used])) if x),
    }
    city_rows = []
    for r in cities[:CITY_ROWS]:
        city_rows.append([cg(r, "name_zh") or cg(r, "name_en"), _fmt(_num(cg(r, "population"))), _fmt(_num(cg(r, "gdp")), 1),
                          _risk(_num(cg(r, "water_stress"))), _fmt(_num(cg(r, "supply_count"))), _fmt(_num(cg(r, "sewage_count")))])
    sec["人口、城镇化与用水需求"] = {
        "人口与城镇化": _ind_table(_rows_of(ind, "人口与国土")),
        "主要城市": (f"库里登记该国城市 {len(cities)} 座,按人口列前 {min(len(cities), CITY_ROWS)} 座。\n\n"
                    + R._table(["城市", "人口", "地区生产总值(亿美元)", "用水压力", "自来水厂", "污水厂"], city_rows)) if cities else "",
    }
    water_keys = ("人均境内可再生淡水", "年淡水取用量", "淡水压力", "取水中")
    risk_rows = [[h, v] for h, v in (("用水压力(当前)", _risk(_num(g("water_stress")))), ("用水压力(2030 年)", _risk(_num(g("water_stress_2030")))),
                                    ("用水压力(2050 年)", _risk(_num(g("water_stress_2050")))), ("干旱风险", _risk(_num(g("drought")))),
                                    ("河流洪水风险", _risk(_num(g("flood"))))) if v]
    sec["水资源与气候条件"] = {
        "水资源与用水结构": _ind_table([r for r in ind if r["k"].startswith(water_keys)]),
        "水风险": (R._table(["项目", "分数(0–5)"], risk_rows) + f"\n来源:{R._md_text(str(g('risk_source') or 'WRI Aqueduct 4.0'))}。分数 4 以上极高,3–4 高。\n") if risk_rows else "",
    }
    tariffs = [[cg(r, "name_zh"), _fmt(_num(cg(r, "tariff")), 2), cg(r, "tariff_currency") or "", tstore.day(cg(r, "tariff_changed"), b.tz) or "",
                R._md_text(R._cell(cg(r, "tariff_note")))[:80]] for r in cities if _num(cg(r, "tariff")) is not None]
    sec["水务行业体制、法规与规划"] = {
        "管理机构": R._dots_table(g("water_agencies"), ["机构", "英文名", "职责", "网站"]),
        "水务国企": R._dots_table(g("water_soes"), ["企业", "英文名", "说明", "网站"]),
        "法律法规": R._dots_table(g("laws"), ["法规", "领域", "要点", "链接"]),
        "水价": R._table(["城市", "现行水价", "货币", "最近调价", "说明"], tariffs) if tariffs else "",
    }
    supply_ind = [r for r in ind if re.search(r"饮水", r["k"])]
    sewer_ind = [r for r in ind if re.search(r"卫生", r["k"])]

    def plant_block(t: str, label: str) -> str:
        rows = plants[t]
        if not rows:
            return ""
        pg = lambda r, p: b.get(t, r, p)  # noqa: E731
        cap = [x for x in (_num(pg(r, "capacity")) for r in rows) if x is not None]
        lv = Counter(str(pg(r, "level") or "") for r in rows if b.has(t, "level") and pg(r, "level"))
        top = sorted(rows, key=lambda r: -(_num(pg(r, "capacity")) or 0))[:PLANT_ROWS]
        head = (f"库里登记{label} {len(rows)} 座" + (f",合计规模约 {_fmt(sum(cap), 1)} 万吨/日" if cap else "")
                + ((",处理级别:" + "、".join(f"{k} {v} 座" for k, v in lv.most_common())) if lv else "") + "。按规模列前几座:\n\n")
        body = [[pg(r, "name_zh") or pg(r, "name") or "", R._cell(pg(r, "city")), _fmt(_num(pg(r, "capacity")), 2), _fmt(_num(pg(r, "pop_served"))),
                 pg(r, "status") or "", (pg(r, "level") if b.has(t, "level") else pg(r, "process")) or ""] for r in top]
        return head + R._table(["名称", "城市", "规模(万吨/日)", "服务人口", "状态", "处理级别" if t == "sewage_plant" else "工艺"], body) \
            + "\n数据多来自公开数据集,名称、建成年份与工艺多有缺项,以现场核实为准。\n"

    sec["全国供水现状"] = {"覆盖率与服务水平": _ind_table(supply_ind), "水厂": plant_block("supply_plant", "自来水厂")}
    sec["全国污水与排水现状"] = {"收集与处理": _ind_table(sewer_ind), "污水处理厂": plant_block("sewage_plant", "污水处理厂")}
    cap_name = _capital(basics)
    cap_city = next((r for r in cities if cap_name and str(cg(r, "name_zh") or "") == cap_name), cities[0] if cities else None)
    if cap_city:
        cap_name = str(cg(cap_city, "name_zh") or cap_name)
        cp = [[h, v] for h, v in (("人口", _fmt(_num(cg(cap_city, "population"))) + (f"({cg(cap_city, 'pop_year')})" if cg(cap_city, "pop_year") else "")),
                                 ("地区生产总值(亿美元)", _fmt(_num(cg(cap_city, "gdp")), 1)), ("用水压力", _risk(_num(cg(cap_city, "water_stress")))),
                                 ("干旱风险", _risk(_num(cg(cap_city, "drought")))), ("无安全饮用水人口占比", _fmt(_num(cg(cap_city, "no_drinking")), 1, "%")),
                                 ("未处理污水占比", _fmt(_num(cg(cap_city, "untreated_ww")), 1, "%")),
                                 ("现行水价", (_fmt(_num(cg(cap_city, "tariff")), 2) + " " + str(cg(cap_city, "tariff_currency") or "")).strip()),
                                 ("自来水厂 / 污水厂", f"{_fmt(_num(cg(cap_city, 'supply_count'))) or 0} / {_fmt(_num(cg(cap_city, 'sewage_count'))) or 0} 座")) if v]
        sec["首都水务专题"] = {"概况": R._table(["项目", cap_name], cp)}
    else:
        sec["首都水务专题"] = {}
    funders: Counter = Counter()
    fin_rows = []
    for r in opps:
        lines = [_strip_refs(x) for x in str(og(r, "financing") or "").split("\n") if x.strip()]
        for f in {_funder(x.split(" · ")[0].strip()) for x in lines}:
            funders[f] += 1
        if lines:
            fin_rows.append([og(r, "code"), R._md_text(str(og(r, "name_zh") or "")), og(r, "stage") or "建议", ";".join(R._md_text(x) for x in lines[:3])])
    fund_rows = [[R._md_text(k), v] for k, v in funders.most_common(12)]
    osz = sorted([(r, v) for r, v in ((r, _num(og(r, "size_cny"))) for r in opps) if v], key=lambda x: -x[1])
    sec["投资规划与资金来源"] = {
        "资金方分布": ((f"写明资金来源的商机 {len(fin_rows)} 个,按出资方计:\n\n" + R._table(["出资方", "商机数"], fund_rows)
                       + "\n各商机的融资情况:\n\n" + R._table(["编号", "名称", "我方阶段", "资金"], fin_rows)) if fund_rows else ""),
        "已识别项目的投资规模": ((f"写明规模的商机 {len(osz)} 个,合计约 {_fmt(sum(v for _, v in osz) / 1e4, 1)} 亿元人民币:\n\n"
                                  + R._table(["编号", "名称", "折合人民币(亿元)"], [[og(r, "code"), R._md_text(str(og(r, "name_zh") or "")), _fmt(v / 1e4, 2)]
                                                                         for r, v in osz])) if osz else ""),
        "国际金融机构涉水项目": (f"世行项目库 2020 年 10 月至 2024 年:该国涉水项目 {_fmt(_num(g('wb_water_n')))} 个,承诺额 {_fmt(_num(g('wb_water_musd')))} 百万美元。\n"
                                if _num(g("wb_water_n")) is not None else ""),
    }
    party_rows = []
    for r in opps:
        lines = [_strip_refs(x) for x in str(og(r, "parties") or "").split("\n") if x.strip()]
        owner = str(og(r, "owner_org") or "").strip()
        if owner:
            lines.insert(0, "业主与执行机构:" + _strip_refs(owner))
        if lines:
            party_rows.append([og(r, "code"), R._md_text(str(og(r, "name_zh") or "")), ";".join(R._md_text(x)[:160] for x in lines[:5])])
    sec["竞争格局"] = {"商机参与方": R._table(["编号", "名称", "参与方"], party_rows) if party_rows else ""}
    sec["市场准入与商务环境"] = {"准入与竞争要点": R._bullets(g("access")) if g("access") else ""}
    sec["机会识别与进入策略"] = {
        "已立项跟踪的商机": (R._table(["编号", "名称", "我方阶段", "公开进度", "下一节点", "资金"], [[_strip_refs(R._cell(v)) for v in x[:2] + x[3:]] for x in R._opp_rows(ctx, own_opps)])
                         if own_opps else "该国还没有人工立项跟踪的商机。\n"),
        "AI 建议的商机": (R._table(["编号", "名称", "公开进度", "建议理由"],
                                 [[og(r, "code"), R._md_text(str(og(r, "name_zh") or "")), og(r, "public_stage") or "",
                                   R._md_text(_strip_refs(str(og(r, "ai_reason") or "").split("\n")[0]))[:200]] for r in ai_opps])
                       + "\n机器建议的商机未经人工确认,跟不跟由业务人员在「线索处理 · AI 推荐的商机」里决定。\n") if ai_opps else "",
        "未来时间节点": (R._table(["日期", "类型", "商机", "状态", "说明"], [x[:3] + x[4:] for x in R._ms_rows(ctx, ms, code_of)])
                       if ms else f"未来 {R.UPCOMING_DAYS} 天没有时间节点。\n"),
    }
    bad = [r for r in ind if r["bad"]]
    sec["风险分析与建议"] = {"需要留意的指标": ("以下指标越过了警戒线(警戒线写在括号里):\n\n"
                                       + "\n".join(f"- {R._md_text(r['k'])}:{R._md_text(r['v'])}({r['year']}),警戒线:{r['bad']}" for r in bad) + "\n") if bad else ""}

    # ---- 分析段落:把数据交给模型,分三次写(每次 5 段)
    known = R._known(ctx, [], opps)
    data_md = "\n\n".join(f"## {ch}\n\n" + "\n\n".join(f"### {s}\n\n{t}" for s, t in d.items() if t) for ch, d in sec.items()
                          if ch not in ("研究背景、目的与范围",))
    data_md = (f"# {name}(ISO3 {iso})\n\n要点:\n" + "\n".join(f"- {x}" for x in R.key_points(g, ind)) + "\n\n" + data_md)[:60000]
    ana: dict = {}
    errs = []
    for fields in (FIELDS_A, FIELDS_B, FIELDS_C):
        out, err = _write(ctx, name, data_md, fields, known)
        ana.update(out)
        if err:
            errs.append(err)
    used_model = bool(ana)

    sec["国家概况与宏观经济环境"]["宏观经济"] += "\n" + _para(ana.get("macro"))
    sec["人口、城镇化与用水需求"]["人口与城镇化"] += "\n" + _para(ana.get("population"))
    sec["水资源与气候条件"]["水风险"] += "\n" + _para(ana.get("water"))
    sec["水务行业体制、法规与规划"]["体制要点"] = _para(ana.get("governance"))
    sec["全国供水现状"]["现状分析"] = _para(ana.get("supply"))
    sec["全国污水与排水现状"]["现状分析"] = _para(ana.get("sewage"))
    sec["首都水务专题"]["分析"] = _para(ana.get("capital"))
    sec["投资规划与资金来源"]["分析"] = _para(ana.get("funding"))
    sec["竞争格局"]["分析"] = _para(ana.get("competition"))
    sec["市场准入与商务环境"]["分析"] = _para(ana.get("access"), "" if g("access") else NO_DATA)
    sec["机会识别与进入策略"]["重点机会研判"] = _para(ana.get("opportunities"))
    sec["机会识别与进入策略"]["行动建议"] = _para(ana.get("actions"))
    sec["风险分析与建议"]["风险提示"] = _para(ana.get("risks"))
    sec["风险分析与建议"]["结论与建议"] = _para(ana.get("conclusion"))
    for ch in ("全国供水现状", "全国污水与排水现状"):
        if not any(sec[ch].values()):
            sec[ch]["现状分析"] = NO_DATA

    # ---- 成书:封面、目录、摘要、各章、附录
    book = _Book()
    for i, (title, _) in enumerate(OUTLINE):
        book.chapter(i, sec.get(title, {}))
    chapters = [p for p in book.parts if p.startswith("# 第 ")]
    month = f"{ctx.today.year} 年 {ctx.today.month} 月"
    cover = (f"---\ntitle: \"{name}水务市场分析报告\"\nsubtitle: \"{'内部资料　仅供参考' if internal else '公开版'}\"\ndate: \"{month}\"\n---\n\n"
             + "**目　录**\n\n" + "\\\n".join(["摘要"] + [p[2:].strip() for p in chapters] + ["附录"]) + "\n")
    pts = R.key_points(g, ind)
    summary = _para(ana.get("summary")) if ana.get("summary") else (
        f"{name}:已立项跟踪的商机 {len(own_opps)} 个,AI 建议的商机 {len(ai_opps)} 个;未来 {R.UPCOMING_DAYS} 天时间节点 {len(ms)} 个。\n\n"
        + "\n".join(f"- {R._md_text(x)}" for x in pts) + "\n")
    note = (f"分析段落由模型({ctx.clf.label})根据本报告的数据撰写,方括号内为商机编号,标「公开资料,待核实」的句子须核实后再引用。"
            if used_model else "本报告未使用模型,只列数据;分析段落需人工补充。")
    if errs:
        note += f"其中部分段落模型没写成({errs[0]}),已从略。"
    terms = []
    names = json.loads(TERMS_PATH.read_text(encoding="utf-8"))["terms"]
    body_text = "\n".join(book.parts)
    for a in FUNDERS:
        if a in names and re.search(r"(?<![A-Za-z])" + re.escape(a) + r"(?![A-Za-z])", body_text):
            terms.append([a, names[a]])
    appendix = ["# 附录\n", "## 附录 A 数据来源登记\n\n" + sec["研究背景、目的与范围"]["数据来源与方法"]
                + f"\n{note}数据取自部门共用表格(国别、城市、水厂、商机),生成于 {R.now_iso()}。\n"]
    if terms:
        appendix.append("## 附录 B 术语与缩略语\n\n" + R._table(["缩写", "中文全称"], terms))
    md = cover + "\n# 摘要\n\n" + summary + "\n" + "\n".join(book.parts) + "\n" + "\n".join(appendix)
    cl, co = R._cites(ctx, [], opps)
    return md, cl, co, iso


def outline_md() -> str:
    """报告模板的正文:章节目录与每节放什么(scripts/make_report_template.py 写进模板,让模板一打开就看得到报告长什么样)。"""
    lines = ["# 摘要\n", "(市场总体判断、最值得跟进的机会与时间、资金来源特点、主要风险与建议)\n"]
    for n, (title, secs) in enumerate(OUTLINE, 1):
        lines.append(f"# 第 {n} 章 {title}\n")
        for m, (s, what) in enumerate(secs, 1):
            lines.append(f"## {n}.{m} {s}\n\n({what})\n")
    lines.append("# 附录\n")
    for i, (s, what) in enumerate(APPENDIX):
        lines.append(f"## 附录 {'AB'[i]} {s}\n\n({what})\n")
    return "\n".join(lines)
