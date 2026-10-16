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

from core import country_facts, geo, tstore
from core.watch import reporting
from core.zh_name import FUNDERS, TERMS_PATH

from . import reports as R
from .common import Ctx
from .tender_monitor import _Refs

# 章节目录:(章, [(节, 这一节放什么)]);和成稿一样「第 N 章」+「N.M 节」。改章节只改这里,模板跟着重新生成。
OUTLINE: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("研究背景、目的与范围", (("范围与口径", "国别、编制日期、版本(内部版 / 公开版)与统计口径"),
                            ("数据来源与方法", "本报告用到的数据及其来源、更新时间"))),
    ("国家概况与宏观经济环境", (("基本情况", "国名、区域、首都、语言、货币、收入组与贷款类别、我方在当地"),
                               ("关键指标", "8 项关键指标卡,超过警戒线的标红"),
                               ("宏观经济", "经济、财政与外部指标表(含人均 GDP(PPP))+ 分析"),
                               ("国别备注与近期动态", "国别表的备注:近期动态、情报与待办(只进内部版)"))),
    ("人口、城镇化与用水需求", (("人口与城镇化", "人口、城镇化指标表 + 分析"),
                               ("主要城市", "主要城市的人口、经济、水风险与水厂"),
                               ("水务片区", "人划定的大城市圈:包含城市、合计人口、服务水司、主要水源与挂上的商机"))),
    ("水资源与气候条件", (("水资源与用水结构", "可再生淡水、取水量与结构、淡水压力"),
                          ("水风险", "WRI Aqueduct 总体水风险、用水压力、干旱、洪水 + 分析"))),
    ("水务行业体制、法规与规划", (("管理机构", "水务管理机构与职责"), ("水务国企", "供排水运营企业"),
                                 ("法律法规", "相关法律法规;收了原文的法规列文号、颁布日期与现行状态"),
                                 ("排放标准", "从法规原文摘出的排放限值,角标回到条文"), ("本国定义", "通用概念在该国法规里的定义与差别"),
                                 ("水务大事记", "项目批准与关闭、机构改革、规划政策、水价调整、工程投运"),
                                 ("水价", "主要城市现行水价"), ("体制要点", "分析"))),
    ("全国供水现状", (("覆盖率与服务水平", "饮水服务覆盖率"), ("水厂", "库里登记的自来水厂"), ("现状分析", "分析"))),
    ("全国污水与排水现状", (("收集与处理", "卫生设施覆盖率"), ("污水处理厂", "库里登记的污水厂"), ("现状分析", "分析"))),
    ("首都水务专题", (("概况", "首都的人口、经济、水风险、水价与水厂"), ("分析", "首都市场的机会与难点"))),
    ("投资规划与资金来源", (("资金方分布", "商机的出资方与融资方式"), ("已识别项目的投资规模", "商机的投资规模(折合人民币)"),
                            ("国际金融机构涉水项目", "世行项目库统计"), ("分析", "资金来源与投资节奏"))),
    ("竞争格局", (("商机参与方", "商机上记录的业主、咨询、承包商与同业动向"),
                  ("近期中标项目", "已中标项目:近三年的中标记录、中标单位与国别、中标价,角标回原公告"),
                  ("主要中标单位", "按中标数与金额排的承包商,以及中标单位国别分布"), ("分析", "业主、承包商与竞争态势"))),
    ("市场准入与商务环境", (("准入与竞争要点", "国别表「市场准入与竞争」"),
                            ("营商环境(世界银行 B-READY)", "B-READY 总分、供水与公共采购得分、商业水价、外企参标限制、授标与回款天数、供水与采购细项"),
                            ("分析", "招标采购、资质与本地化要求"))),
    ("机会识别与进入策略", (("已立项跟踪的商机", "人工确认、正在跟踪或已结束的商机及阶段"), ("AI 建议的商机", "机器建议、待人决定跟不跟的商机及理由"),
                            ("未来时间节点", "这些商机未来 30 天的截止与节点"), ("重点机会研判", "分析"), ("行动建议", "分析"))),
    ("风险分析与建议", (("需要留意的指标", "超过警戒线的指标"), ("风险提示", "分析"), ("结论与建议", "分析"))),
)
APPENDIX = (("数据来源登记", "各项数据的来源与时间"), ("术语与缩略语", "报告里出现的机构缩写与中文全称"),
            ("参考研究资料", "库里登记的该国公开研究资料(项目评估、完成报告、行业研究),可点到原文"), ("原文链接", "正文角标对应的原公告与原文,一号一条"))

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
    "governance": "体制与法规要点:150–300 字,管理机构与国企的分工、近期改革(见水务大事记)、法规与排放标准对外资承包商的影响",
}
FIELDS_B = {
    "supply": "全国供水现状分析:150–300 字,覆盖率与服务水平、水厂情况、主要短板",
    "sewage": "全国污水与排水现状分析:150–300 字,卫生设施覆盖、污水厂规模与处理级别、主要短板",
    "capital": "首都水务专题分析:150–300 字,首都的需求、在建与规划项目、机会与难点",
    "funding": "投资规划与资金来源分析:150–300 字,主要出资方、贷款类别、已识别项目规模与投资节奏",
    "competition": "竞争格局分析:150–300 字,业主与采购方式、近期中标的承包商与国别(用「近期中标项目」「主要中标单位」的数据)、中国企业的份额、竞争态势",
}
FIELDS_C = {
    "access": "市场准入与商务环境分析:150–300 字,招标采购流程、资质与本地化、对我方的要求;有世界银行 B-READY 数据的,结合公共采购得分、外企参标限制、采购细项、授标与回款天数写",
    "opportunities": "重点机会研判:最值得跟进的 3–8 个机会,Markdown 列表;每个写明是什么、为什么值得跟进、关键日期、资格限制或风险",
    "actions": "行动建议:按紧迫程度排序的具体行动,Markdown 列表,写明建议完成时间",
    "risks": "风险提示:4–6 条,Markdown 列表,每条标明风险高低(高 / 中 / 低),涉及政治、经济与支付、法律合规、执行",
    "conclusion": "结论与建议:3–5 条,Markdown 列表,给管理层的明确建议",
}
NO_DATA = "本系统暂无该项数据,需人工补充。"
CITY_ROWS = 15
PLANT_ROWS = 10
AWARD_ROWS = 20   # 近期中标项目列几条
AWARD_YEARS = 3   # 「近期」= 近几年
EVENT_ROWS = 25   # 水务大事记列几条
STUDY_ROWS = 30   # 附录里的研究资料列几条


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


def _bready(g, name: str, built: bool) -> str:
    """营商环境(世界银行 B-READY,v2.23):一句话 + 小表,偏弱的写明;字段没建(built 假)时整节不出,这一期没覆盖的写明。"""
    if not built:
        return ""
    n = lambda p: _num(g(p))  # noqa: E731
    if n("bready_total") is None:
        return f"世界银行 B-READY 2025 这一期没有覆盖{name}(评了 101 个经济体,下一期计划扩大到约 180 个)。\n"
    usd, loc, cur = n("water_tariff_usd"), n("water_tariff"), str(g("water_tariff_cur") or "")
    rows = [("营商环境总分", _fmt(n("bready_total"), 1, "分"), "三大支柱平均,0–100"), ("供水得分", _fmt(n("bready_water"), 1, "分"), "水价监管、漏损目标、接水与供水可靠性"),
            ("公共采购得分", _fmt(n("bready_procure"), 1, "分"), "招投标规则、电子采购、授标与付款"), ("纠纷解决得分", _fmt(n("bready_dispute"), 1, "分"), "商事纠纷解决"),
            ("商业水价", (f"约 {usd:.2f} 美元/m³" + (f"({loc:g} {cur}/m³)" if loc is not None else "")) if usd is not None else "", "水费 + 污水费,每月 1000 m³ 的商业用户"),
            ("外企参标限制", str(g("foreign_bid") or ""), "法规是否限制外国企业参加公共招标"),
            ("大型工程授标天数", _fmt(n("award_days"), 0, "天"), "国际招标门槛以上的工程,从发标到授标(专家估计)"),
            ("政府合同回款天数", _fmt(n("gov_pay_days"), 0, "天"), "企业收到政府合同款的平均天数"),
            ("企业供水可靠比例", (f"{n('water_reliable'):.1f}%" if n("water_reliable") is not None else ""), "没遇到缺水的企业占比"), ("接水天数", _fmt(n("water_conn_days"), 0, "天"), "企业新接供水的天数")]
    body = []
    for k, v, why in rows:
        if not v:
            continue
        bad = country_facts.tone(k, v) or ("所有公共招标都对外企设限" if k == "外企参标限制" and "全部" in v else
                                           "部分招标对外企设限,投标前要核实" if k == "外企参标限制" and ("部分" in v or "说法不一" in v) else None)
        body.append([k, R._md_text(v) + (f"(超警戒线:{bad})" if bad else ""), why])
    weak = [r[0] for r in body if "超警戒线" in r[1]]
    head = (f"世界银行 B-READY 2025 评{name}的营商环境总分 {_fmt(n('bready_total'), 1)} 分"
            + (f";需要留意的是{'、'.join(weak)}" if weak else ",各项未越过警戒线") + "。分数测的是最大的商业城市,世行不给国家排名。\n\n")
    return head + R._table(["项目", "数值", "说明"], body) + f"\n来源:世界银行 Business Ready(<https://www.worldbank.org/en/businessready>),数据:{R._md_text(str(g('bready_edition') or ''))}。\n"


def _cny(w: float | None) -> str:
    """折合人民币(库里存万元):1 亿以上写亿元,其余写万元。"""
    if w is None:
        return ""
    return f"{w / 1e4:,.2f} 亿元" if w >= 1e4 else f"{w:,.0f} 万元"


def _rows_in(b, t: str, crid: str) -> list[dict]:
    """某张表里挂在这个国家下的行;表没建或没有「所属国别」字段时为空(老库不报错)。"""
    return b.rows(t, filter=b.filter(b.cond(t, "country", "is", crid))) if t in b.t and b.has(t, "country") else []


def _awards_block(b, awards: list[dict], refs: _Refs, today) -> tuple[str, str]:
    """→ (近期中标项目, 主要中标单位)。近几年的按中标价排前 AWARD_ROWS 条,每条角标回原公告;单位按中标数与金额排,国别分布另表。"""
    if not awards:
        return "", ""
    g = lambda r, p: b.get("award", r, p) if b.has("award", p) else None  # noqa: E731
    day = lambda r: tstore.day(g(r, "awarded"), b.tz) or ""  # noqa: E731
    since = f"{today.year - AWARD_YEARS}-01-01"
    recent = [r for r in awards if day(r) >= since]
    pool = recent or awards
    years = sorted({d[:4] for d in (day(r) for r in awards) if d})
    cny = [x for x in (_num(g(r, "price_cny")) for r in recent) if x]
    cn = [r for r in recent if "中国" in str(g(r, "winner_country") or "")]
    head = (f"库里登记该国已中标项目 {len(awards)} 份" + (f"({years[0]}–{years[-1]} 年)" if years else "")
            + (f",其中近 {AWARD_YEARS} 年({since[:4]} 年起){len(recent)} 份" + (f",写明中标价的合计约 {_cny(sum(cny))}" if cny else "")
               + (f";中国企业中标 {len(cn)} 份,占 {len(cn) / len(recent) * 100:.0f}%" if recent else "") if recent else f";近 {AWARD_YEARS} 年没有记录,列最近的")
            + f"。按中标价列前 {min(len(pool), AWARD_ROWS)} 条,角标点开是原公告:\n\n")
    top = sorted(pool, key=lambda r: (-(_num(g(r, "price_cny")) or 0), day(r)), reverse=False)[:AWARD_ROWS]
    body = []
    for r in top:
        name = R._md_text(str(g(r, "name_zh") or g(r, "name") or "")[:60]) + refs.mark(str(g(r, "url") or ""), str(g(r, "name_zh") or ""), str(g(r, "name") or ""))
        kind = "、".join(x for x in (str(g(r, "sector") or ""), str(g(r, "asset") or ""), str(g(r, "delivery") or "")) if x)
        winner = R._md_text(str(g(r, "winner") or "")[:60]) + (f"({R._md_text(str(g(r, 'winner_country')))})" if g(r, "winner_country") else "")
        body.append([name, kind, winner, _cny(_num(g(r, "price_cny"))), day(r), R._md_text(str(g(r, "buyer") or g(r, "funder") or "")[:40])])
    table = head + R._table(["项目", "类别", "中标单位(国别)", "中标价(折合)", "中标时间", "采购方或资金"], body) \
        + "\n中标价按入库当日汇率折合人民币;联合体按整体记一份,没写明金额的不计入合计。\n"
    # 主要中标单位:按中标数、金额排;国别分布
    by_w: dict[str, list] = {}
    for r in pool:
        w = str(g(r, "winner") or "").strip()
        if w:
            by_w.setdefault(w, [0, 0.0, str(g(r, "winner_country") or "")])
            by_w[w][0] += 1
            by_w[w][1] += _num(g(r, "price_cny")) or 0
    firms = sorted(by_w.items(), key=lambda kv: (-kv[1][0], -kv[1][1]))[:12]
    ctry: Counter = Counter()
    for r in pool:  # 联合体的每一国各计一次
        for k in dict.fromkeys(x.strip() for x in re.split(r"[、,,/;;]", str(g(r, "winner_country") or "")) if x.strip()) or ["未注明"]:
            ctry[k] += 1
    firms_md = (f"近 {AWARD_YEARS} 年" if recent else "库里") + f"中标单位 {len(by_w)} 家,按中标数列前 {len(firms)} 家:\n\n" \
        + R._table(["中标单位", "国别", "中标数", "中标总额(折合)"], [[R._md_text(w[:60]), R._md_text(c), n, _cny(v) if v else ""] for w, (n, v, c) in firms]) \
        + "\n中标单位国别分布(占这些中标项目的比例,联合体的每一国各计一次):\n\n" \
        + R._table(["国别", "中标数", "占比"], [[R._md_text(k), v, f"{v / len(pool) * 100:.0f}%"] for k, v in ctry.most_common(10)])
    return table, firms_md


def _events_block(b, events: list[dict], refs: _Refs) -> str:
    """水务大事记:按日期从新到旧列 EVENT_ROWS 条,出处角标。"""
    if not events:
        return ""
    g = lambda r, p: b.get("water_event", r, p) if b.has("water_event", p) else None  # noqa: E731
    raw = lambda r: str(g(r, "日期原文") or "").strip() or (tstore.day(g(r, "day"), b.tz) or "")  # noqa: E731
    rows = sorted(events, key=lambda r: tstore.day(g(r, "day"), b.tz) or "", reverse=True)[:EVENT_ROWS]
    body = [[raw(r), str(g(r, "kind") or ""), R._md_text(str(g(r, "title") or "")[:90]) + refs.mark(str(g(r, "url") or ""), str(g(r, "title") or "")),
             R._cell(g(r, "city")), R._md_text(str(g(r, "source") or "")[:40])] for r in rows]
    return (f"库里登记该国水务事件 {len(events)} 件,列最近 {len(rows)} 件(项目批准与关闭来自世界银行项目库,其余摘自机构文件原文):\n\n"
            + R._table(["日期", "类别", "事件", "城市", "出处"], body))


def _laws_block(b, laws: list[dict], refs: _Refs) -> str:
    """收了原文的法规(「法规」表):名称、类别、文号、颁布日期、现行状态,角标到原文。"""
    if not laws:
        return ""
    g = lambda r, p: b.get("law", r, p) if b.has("law", p) else None  # noqa: E731
    body = [[R._md_text(str(g(r, "name") or "")) + refs.mark(str(g(r, "url") or ""), str(g(r, "name") or ""), str(g(r, "name_orig") or "")),
             str(g(r, "kind") or ""), R._md_text(str(g(r, "number") or "")), tstore.day(g(r, "issued"), b.tz) or "", str(g(r, "status") or ""),
             R._md_text(str(g(r, "summary") or "").split("\n")[0][:80])] for r in sorted(laws, key=lambda r: tstore.day(g(r, "issued"), b.tz) or "", reverse=True)]
    return f"\n收了原文的法规 {len(laws)} 部(条文中文与原文在系统里对照可查):\n\n" + R._table(["法规", "类别", "文号", "颁布", "状态", "要点"], body)


def _law_ref(b, laws: list[dict], v: object, refs: _Refs) -> str:
    """排放限值、本国定义上的「所属法规」→ 法规名 + 原文角标。"""
    rid = (v[0] if isinstance(v, list) and v else v) if v else None
    rid = rid.get("id") if isinstance(rid, dict) else rid
    r = next((x for x in laws if x["id"] == rid), None)
    if not r:
        return R._cell(v)
    return R._md_text(str(b.get("law", r, "name") or "")[:40]) + refs.mark(str(b.get("law", r, "url") or ""), str(b.get("law", r, "name") or ""))


def _limits_block(b, limits: list[dict], laws: list[dict], refs: _Refs) -> str:
    if not limits:
        return ""
    g = lambda r, p: b.get("discharge_limit", r, p) if b.has("discharge_limit", p) else None  # noqa: E731
    order = {"出水浓度上限": 0, "排入管网上限": 1, "去除率下限": 2, "受纳水体允许浓度": 3}
    rows = sorted(limits, key=lambda r: (order.get(str(g(r, "basis") or ""), 9), str(g(r, "receiving") or ""), str(g(r, "param") or "")))
    body = [[str(g(r, "param") or g(r, "param_orig") or ""), (str(g(r, "value_text") or "") or _fmt(_num(g(r, "value")), 2)) + (f" {g(r, 'unit')}" if g(r, "unit") else ""),
             str(g(r, "basis") or ""), str(g(r, "receiving") or ""), R._md_text(str(g(r, "scope") or "")[:50]), _law_ref(b, laws, g(r, "law"), refs),
             str(g(r, "verified") or "")] for r in rows]
    unv = sum(1 for r in rows if str(g(r, "verified") or "") != "已核实")
    return (f"从法规原文摘出的排放限值 {len(limits)} 条;同一国不同口径(出水浓度上限、去除率、受纳水体允许浓度)分开列,不能直接比;角标回到法规原文。"
            + (f"其中 {unv} 条还没人工核实,引用前先核。" if unv else "") + "\n\n" + R._table(["指标", "限值", "口径", "排到哪里", "适用范围", "法规", "核实"], body))


def _terms_block(b, terms: list[dict], laws: list[dict], refs: _Refs) -> str:
    if not terms:
        return ""
    g = lambda r, p: b.get("local_term", r, p) if b.has("local_term", p) else None  # noqa: E731
    body = [[R._md_text(str(g(r, "concept") or "")), R._md_text(" / ".join(x for x in (str(g(r, "term_zh") or ""), str(g(r, "term_orig") or "")) if x)),
             str(g(r, "relation") or ""), R._md_text((str(g(r, "difference") or "") or str(g(r, "definition") or ""))[:120]), _law_ref(b, laws, g(r, "law"), refs),
             str(g(r, "verified") or "")] for r in sorted(terms, key=lambda r: str(g(r, "concept") or ""))]
    return ("通用概念在该国法规里怎么定义、和通用说法差在哪;报告与助手回答该国问题时按这里的定义。\n\n"
            + R._table(["通用概念", "本国说法 / 原文用词", "关系", "差别或定义", "出处法规", "核实"], body))


def _areas_block(b, areas: list[dict], cities: list[dict], opps: list[dict]) -> str:
    """水务片区:包含城市、合计人口、服务水司、主要水源、挂上的商机数。"""
    if not areas:
        return ""
    g = lambda r, p: b.get("water_area", r, p) if b.has("water_area", p) else None  # noqa: E731
    pop = {r["id"]: _num(b.get("city", r, "population")) or 0 for r in cities}
    ids = lambda v: [x.get("id") if isinstance(x, dict) else x for x in (v if isinstance(v, list) else ([v] if v else []))]  # noqa: E731
    n_opp = Counter()
    if b.has("opportunity", "area"):
        for o in opps:
            for a in ids(b.get("opportunity", o, "area")):
                n_opp[a] += 1
    body = [[R._md_text(str(g(r, "name") or "")), R._cell(g(r, "cities")), _fmt(sum(pop.get(i, 0) for i in ids(g(r, "cities")))), R._md_text(str(g(r, "utility") or "")[:40]),
             R._md_text(str(g(r, "sources") or "").split("\n")[0][:60]), n_opp.get(r["id"], 0) or ""] for r in areas]
    return (f"划定水务片区 {len(areas)} 片(共用水司、水源与干管的连片城市,是小国市场的主要分析单位):\n\n"
            + R._table(["片区", "包含城市", "合计人口", "服务水司", "主要水源", "挂上的商机"], body))


def _studies_block(b, studies: list[dict], refs: _Refs) -> str:
    """附录:研究资料清单,新的在前,角标到原文。"""
    if not studies:
        return ""
    g = lambda r, p: b.get("study", r, p) if b.has("study", p) else None  # noqa: E731
    rows = sorted(studies, key=lambda r: tstore.day(g(r, "published"), b.tz) or "", reverse=True)[:STUDY_ROWS]
    body = [[R._md_text(str(g(r, "name_zh") or g(r, "name") or "")[:70]) + refs.mark(str(g(r, "url") or ""), str(g(r, "name_zh") or ""), str(g(r, "name") or "")),
             str(g(r, "kind") or ""), R._md_text(str(g(r, "publisher") or "")[:30]), tstore.day(g(r, "published"), b.tz) or "", R._md_text(str(g(r, "project") or "")[:40])]
            for r in rows]
    return f"库里登记该国研究资料 {len(studies)} 份,列最近 {len(rows)} 份:\n\n" + R._table(["标题", "类型", "发布机构", "发布", "相关项目"], body)


def _bready_items(g) -> str:
    """营商环境细项(bready_items,一行「[供水|采购] 项目:答案」):采购细项在前;对外企投标不利的标「超警戒线」。"""
    items = [x.strip() for x in str(g("bready_items") or "").splitlines() if x.strip()]
    if not items:
        return ""
    bad = re.compile(r"外企参标或授标受限|外企须|专留给本地企业")
    out = []
    for grp, label in (("采购", "公共采购细项"), ("供水", "供水细项")):
        rows = []
        for x in items:
            if not x.startswith(f"[{grp}]"):
                continue
            t = re.sub(r"^\[[^\]]+\]\s*", "", x)
            i = max(t.rfind(":"), t.rfind("："))
            k, v = (t[:i].strip(), t[i + 1:].strip()) if i > 0 else (t, "")
            flag = bool(bad.search(k)) and bool(v) and not v.startswith(("否", "说法不一"))
            rows.append([R._md_text(k), R._md_text(v) + (f"({R.BAD_LABEL}:对外企投标不利)" if flag else "")])
        if rows:
            out.append(f"{label}:\n\n" + R._table(["项目", "答案"], rows))
    return "\n".join(out)


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
    # 国别概况页其余页签的数据(2026-10-16 使用方要:页面有的报告也要有):已中标项目、水务事件、法规原文与排放标准、本国定义、水务片区、研究资料
    awards, events, laws = _rows_in(b, "award", crid), _rows_in(b, "water_event", crid), _rows_in(b, "law", crid)
    limits, lterms, areas, studies = _rows_in(b, "discharge_limit", crid), _rows_in(b, "local_term", crid), _rows_in(b, "water_area", crid), _rows_in(b, "study", crid)
    refs = _Refs()  # 角标:同一网址一个编号,附录里列全

    # ---- 数据块
    sec: dict[str, dict[str, str]] = {}
    sec["研究背景、目的与范围"] = {
        "范围与口径": (f"本报告分析{name}的城镇供水、污水与排水市场,面向海外业务,版本为{'内部版' if internal else '公开版'},编制日期 {ctx.today.isoformat()}。"
                    f"商机口径:已立项跟踪的 {len(own_opps)} 个、AI 建议的 {len(ai_opps)} 个(标为不跟进的不计)。"
                    "金额按入库当日汇率折合人民币。\n"),
        "数据来源与方法": "\n".join(f"- {x}" for x in (
            "宏观与水务指标:世界银行 WDI、IMF《世界经济展望》(每月 1 日更新)",
            "水风险:WRI Aqueduct 4.0;人均 GDP(PPP):Kummu 等;世行涉水项目:世行项目库",
            "营商环境:世界银行 B-READY(专家问卷与企业调查,测最大商业城市)",
            "城市:GeoNames;水厂与污水厂:HydroWASTE 等公开数据集与人工补录;水务片区:人工划定",
            "机构、国企与法规:人工核实的国别资料(附来源);法规原文、排放限值与本国定义:从法规原文按条摘出(角标回条文)",
            "已中标项目与中标单位:开发银行与各国采购网公开的中标记录;水务事件:世行项目库与机构文件;研究资料:开发银行文件库",
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
    econ = [r for r in _rows_of(ind, "经济") if id(r) not in used]
    if _num(g("gdp_pc")) is not None and not any("PPP" in r["k"] for r in ind):  # 人均 GDP(PPP):概况页「水风险与经济」里的那项
        econ.append({"k": "人均 GDP(PPP)", "v": f"{_num(g('gdp_pc')):,.0f} 美元(2021 年国际美元)", "year": "2024", "src": "Kummu 等(2025)",
                     "url": "https://doi.org/10.1038/s41597-025-04487-x", "bad": None})
    sec["国家概况与宏观经济环境"] = {
        "基本情况": R._table(["项目", "内容", "项目", "内容"], pair) if pair else "",
        "关键指标": R.kpi_table(kp),
        "宏观经济": "\n".join(x for x in (_ind_table(econ), _ind_table([r for r in _rows_of(ind, "财政与外部") if id(r) not in used])) if x),
        "国别备注与近期动态": R._bullets(g("note")) if internal and g("note") else "",
    }
    city_rows = []
    for r in cities[:CITY_ROWS]:
        city_rows.append([cg(r, "name_zh") or cg(r, "name_en"), _fmt(_num(cg(r, "population"))), _fmt(_num(cg(r, "gdp")), 1),
                          _risk(_num(cg(r, "water_stress"))), _fmt(_num(cg(r, "supply_count"))), _fmt(_num(cg(r, "sewage_count")))])
    sec["人口、城镇化与用水需求"] = {
        "人口与城镇化": _ind_table(_rows_of(ind, "人口与国土")),
        "主要城市": (f"库里登记该国城市 {len(cities)} 座,按人口列前 {min(len(cities), CITY_ROWS)} 座。\n\n"
                    + R._table(["城市", "人口", "地区生产总值(亿美元)", "用水压力", "自来水厂", "污水厂"], city_rows)) if cities else "",
        "水务片区": _areas_block(b, areas, cities, opps),
    }
    water_keys = ("人均境内可再生淡水", "年淡水取用量", "淡水压力", "取水中")
    pct = lambda p: f"{_num(g(p)):.1f}%" if _num(g(p)) is not None else ""  # noqa: E731
    risk_rows = [[h, v] for h, v in (("总体水风险", _risk(_num(g("water_risk")))), ("用水压力(当前)", _risk(_num(g("water_stress")))),
                                    ("用水压力(2030 年)", _risk(_num(g("water_stress_2030")))), ("用水压力(2050 年)", _risk(_num(g("water_stress_2050")))),
                                    ("干旱风险", _risk(_num(g("drought")))), ("河流洪水风险", _risk(_num(g("flood")))),
                                    ("无安全饮用水人口占比", pct("no_drinking")), ("无卫生设施人口占比", pct("no_sanitation")), ("未处理污水占比", pct("untreated_ww"))) if v]
    sec["水资源与气候条件"] = {
        "水资源与用水结构": _ind_table([r for r in ind if r["k"].startswith(water_keys)]),
        "水风险": (R._table(["项目", "分数(0–5)或占比"], risk_rows) + f"\n来源:{R._md_text(str(g('risk_source') or 'WRI Aqueduct 4.0'))}。分数 4 以上极高,3–4 高;模型估计,作趋势参考。\n") if risk_rows else "",
    }
    tariffs = [[cg(r, "name_zh"), _fmt(_num(cg(r, "tariff")), 2), cg(r, "tariff_currency") or "", tstore.day(cg(r, "tariff_changed"), b.tz) or "",
                R._md_text(R._cell(cg(r, "tariff_note")))[:80]] for r in cities if _num(cg(r, "tariff")) is not None]
    sec["水务行业体制、法规与规划"] = {
        "管理机构": R._dots_table(g("water_agencies"), ["机构", "英文名", "职责", "网站"]),
        "水务国企": R._dots_table(g("water_soes"), ["企业", "英文名", "说明", "网站"]),
        "法律法规": R._dots_table(g("laws"), ["法规", "领域", "要点", "链接"]) + _laws_block(b, laws, refs),
        "排放标准": _limits_block(b, limits, laws, refs),
        "本国定义": _terms_block(b, lterms, laws, refs),
        "水务大事记": _events_block(b, events, refs),
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
    aw_table, aw_firms = _awards_block(b, awards, refs, ctx.today)
    sec["竞争格局"] = {"商机参与方": R._table(["编号", "名称", "参与方"], party_rows) if party_rows else "", "近期中标项目": aw_table, "主要中标单位": aw_firms}
    bready = _bready(g, name, b.has("country", "bready_edition"))
    sec["市场准入与商务环境"] = {"准入与竞争要点": R._bullets(g("access")) if g("access") else "",
                             "营商环境(世界银行 B-READY)": bready + ("\n" + _bready_items(g) if bready and _num(g("bready_total")) is not None else "")}
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
    if studies:
        appendix.append("## 附录 C 参考研究资料\n\n" + _studies_block(b, studies, refs))
    if refs.urls:
        appendix.append("## 附录 D 原文链接\n\n正文角标的号对应这里的原公告与原文,一号一条:\n\n" + refs.chapter())
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
        lines.append(f"## 附录 {'ABCD'[i]} {s}\n\n({what})\n")
    return "\n".join(lines)
