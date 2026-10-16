"""国别报告(A14 的「国别报告」类型):章节与写法照阿塞拜疆成稿(private/materials/AZE/…成稿,15 章 72 节、4.8 万字)。

2026-10-16 使用方:成稿 60 页、内容翔实,程序出的只有 20 页、像数据表——选定「照成稿 72 节写」:
每一节都由模型写成完整段落(有库里数据的按数据写,没有的按模型确知的公开资料写并逐句标「(公开资料,待核实)」),
库里的表格放在段落后面作支撑;市场规模测算之类库里没有依据的,写明方法与假设,给不出的数写「需人工补充」。
章节目录 OUTLINE 是唯一的来源:报告按它出,报告模板里的章节目录也由它生成(scripts/make_report_template.py)。
没配模型时只出数据表与要点,分析段落从略(报告会短得多,附录 A 写明)。
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

# 章节目录:(章, [(节, 这一节写什么)]);照成稿「第 N 章」+「N.M 节」。节名里的「首都」「我国」在出报告时换成实际的首都名、投标人国籍。
# 每节的「写什么」同时是给模型的指令(配了模型时)和模板目录里的括号说明。
OUTLINE: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("研究背景、目的与范围", (
        ("研究背景", "为什么此时研究这国水务市场:三个因素(投资周期、政策与机构变化、我方契机),每个因素一段"),
        ("研究目的与读者", "面向管理层的用途:是否进入、以何种方式进入、优先争取哪些项目;报告以决策所需的事实与判断为主"),
        ("范围界定", "业务范围(城镇供水与污水为核心,海水淡化、雨水排涝、水利灌溉作延伸)、地域范围、时间范围、商机口径(程序填)"),
        ("研究方法与数据来源", "公开来源研究;数据来源清单与更新时间(程序填);数据冲突时的取舍原则"))),
    ("国家概况与宏观经济环境", (
        ("地理、行政区划与经济区", "位置、国土、邻国与跨境河流、行政区划层级、主要经济区及其在水务上的含义"),
        ("政治体制与政策稳定性", "政体、政府换届节奏、政策连续性,对长周期基础设施投资的含义"),
        ("宏观经济", "经济规模与增速、结构、通胀、财政与债务,对水务投资支付能力的含义(按关键指标与宏观经济表写)"),
        ("外汇、汇率与主权评级", "汇率制度与走势、外汇储备、外债、主权评级与利率,对合同币种与回款的含义"),
        ("外资与营商环境", "外资法律框架、投资优惠、世界银行 B-READY 得分与细项的含义、外企参标限制"),
        ("我国与该国经贸关系与我国企业现状", "双边关系定位、贸易与投资规模、我国承包商在当地的业绩与存在(按已中标项目里本国企业的记录写)"),
        ("国别备注与近期动态", "国别表的备注:近期动态、情报与待办(只进内部版,程序填)"))),
    ("人口、城镇化与用水需求", (
        ("全国人口与预测", "人口总量、增速、年龄结构、人口预测,对用水需求的含义"),
        ("城镇体系", "首位城市与大城市圈、规模以上城市分布(按主要城市表与水务片区写),各城市在水务市场里的分量"),
        ("城镇化与住房建设", "城镇化率与趋势、住房与固定资产投资,供排水派生需求"),
        ("用水需求现状与预测", "人均用水、户规模、生活用水量估算与增长判断;给不出的数写明需人工补充"))),
    ("水资源与气候条件", (
        ("水资源总量与跨境依赖", "人均可再生淡水、跨境来水比例、主要河流与含水层"),
        ("主要水源工程", "水库、引水工程、地下水源及其供水作用;库里没有的写明"),
        ("用水结构与水资源压力", "取水总量与农业/工业/生活结构、淡水压力、Aqueduct 用水压力与趋势(按水风险表写)"),
        ("气候变化与灾害风险", "干旱、洪水、径流变化与极端天气,对项目类型(蓄水、海水淡化、排涝)的含义"))),
    ("水务行业体制、法规与规划", (
        ("机构体系与近期改革", "主管部门、监管机构、运营国企的分工与近年改革(按管理机构、水务国企表与大事记里的机构改革写);我方应对接谁"),
        ("法律法规", "水法、供排水法、PPP 法、采购法、环评法的要点与对外资承包商的影响(按法律法规表写)"),
        ("国家战略与规划", "国家水战略、部门规划、目标年份与指标(按大事记里的规划与政策写)"),
        ("重点专项计划", "首都或重点区域的专项投资计划、十年目标、分阶段安排;库里没有的写明"),
        ("水价体制与财务可持续性", "水价水平与调价机制、成本回收、运营企业财务状况(按水价表与大事记里的水价调整写)"),
        ("技术标准与审批", "饮用水与排放标准、设计规范、环评与审批流程(按排放标准、本国定义表写)"),
        ("国际合作格局", "各开发银行与双边机构在该国水务的历史项目与在手管道(按世行项目库、大事记里的项目批准、研究资料写)"))),
    ("全国供水现状", (
        ("覆盖率与服务水平", "安全管理饮水覆盖率、城乡差异、供水连续性与水质(按覆盖率指标表写)"),
        ("水源与水厂", "地表水与地下水比例、主要水厂与规模(按自来水厂表写)"),
        ("输配水管网与漏损", "管网长度、管龄、漏损率、计量,改造需求"),
        ("区域城镇供水项目进展", "区域中心城镇供水项目的历史与当前进展(按大事记里的工程投运写)"),
        ("供水侧问题诊断", "分层次归纳供水侧的主要问题,每条给依据"))),
    ("全国污水与排水现状", (
        ("收集率与处理率", "污水收集、处理比例与城乡差异(按卫生设施指标表写)"),
        ("污水处理厂", "污水厂数量、规模、处理级别与运行状态(按污水处理厂表写)"),
        ("污泥处置与再生水利用", "污泥处置方式、再生水规定与利用;库里没有的写明"),
        ("工业废水与雨水排涝", "工业废水来源与处理、雨水排涝问题与项目"),
        ("环境影响与排放标准", "受纳水体污染、排放限值与执行情况(按排放标准表写)"),
        ("污水侧问题诊断", "归纳污水侧主要问题,每条给依据"))),
    ("首都及大首都水务专题", (
        ("概况", "首都及其都市圈的人口、经济、水风险、水价、水厂(按首都表与水务片区写)"),
        ("供水系统", "水源构成、水厂、管网、供水可靠性与短板"),
        ("污水系统", "覆盖率、污水厂、排放去向与短板"),
        ("管网改造与智慧水务", "管网更新、漏损探测、计量与智慧水务计划"),
        ("海水淡化与非常规水源", "海水淡化、再生水等项目与规划;没有的写明"),
        ("首都在建与规划项目清单", "挂在首都或其片区的商机(程序填)"),
        ("首都市场的机会与难点", "机会集中在哪、难点是什么,每条给依据"))),
    ("区域城镇与重点地区", (
        ("区域城镇的投资模式", "首都以外城镇供排水的资金与实施模式、在手项目(按首都以外的商机写)"),
        ("重点地区与特殊区域", "有专项政策或特殊地位的地区(重建区、自治区、飞地、经济特区)的水务需求与项目;没有的写明"))),
    ("投资规划与资金来源", (
        ("投资规模的核实", "业内流传的投资总量说法与可核实的依据对照(按已识别商机规模与规划目标写),给出可信区间"),
        ("国内资金来源", "国家预算、主权基金、运营企业自筹、地方资金"),
        ("国际金融机构项目管道", "各出资方的在手与管道项目(按资金方分布、各商机融资情况、世行项目库写)"),
        ("公私合作前景", "PPP 法律框架、已有案例、政府倾向(按法律法规表写)"),
        ("我国融资渠道的可行性", "政策性金融、商业银行、出口信贷、丝路基金等对该国的适用性与案例"))),
    ("市场规模测算与预测", (
        ("测算方法与假设", "口径(工程承包合同额)、期限、币种、关键假设;只用报告里的数据与规划目标"),
        ("测算结果", "分供水、污水、海水淡化等的规模区间;给不出的写需人工补充,不编数"),
        ("自上而下校验", "用预算占比、运营企业投资计划、人均投资等交叉核对"),
        ("设备与材料市场", "泵阀管材膜等的进口规模与我国份额;没有数据写明"),
        ("运营服务市场", "按计费水量与水价估算运营市场规模"),
        ("我国企业可及市场", "按采购渠道与资格限制估算我方可参与的份额(按已中标项目里本国企业份额写)"),
        ("敏感性分析", "影响测算的关键变量与区间"))),
    ("竞争格局", (
        ("业主与采购模式", "业主结构、采购方式(国际招标、国内招标、PPP)、评标习惯(按商机参与方写)"),
        ("本地承包商", "主要本地承包商、体量与能力(按主要中标单位里本国企业写)"),
        ("外国承包商与服务商", "在该国中标的外国承包商、咨询与运营商(按主要中标单位与近期中标项目写)"),
        ("设备与材料供应商", "主要设备来源国与品牌、我国设备地位"),
        ("我国企业在该国业绩与经验教训", "我国企业中标记录、合同额、执行中的问题(按近期中标项目里本国企业的记录写)"),
        ("竞争态势总结", "分层概括竞争格局,我方处于哪一层"))),
    ("市场准入与商务环境", (
        ("招标采购流程", "国内预算项目与国际金融机构项目各自的采购流程与平台(按市场准入与竞争要点、公共采购细项写)"),
        ("资质、注册与本地化", "承包商资质、本地注册、本地成分要求与办理周期"),
        ("税务、劳工与签证", "企业所得税、增值税、常设机构、外籍劳工配额与签证"),
        ("合同条件、付款与担保", "合同范本、付款周期(按政府合同回款天数写)、担保与保函"),
        ("汇率与资金汇出", "汇率风险、利润汇出限制(按汇率指标写)"))),
    ("机会识别与进入策略", (
        ("项目机会清单", "已立项跟踪与 AI 建议的商机及未来时间节点(程序填)"),
        ("重点机会研判", "最值得跟进的 3–8 个机会,每个写明是什么、为什么、关键日期、资格限制或风险,句末标商机编号"),
        ("目标细分与业务组合", "按业务线(工程承包、设备、运营、投资)的优先级与组合"),
        ("进入路径", "本地伙伴、国际金融机构项目、PPP、分包等路径的利弊"),
        ("SWOT 分析", "优势、劣势、机会、威胁各 3–5 条"),
        ("近 6 至 12 个月行动计划", "分阶段的具体行动,写明建议完成时间与负责部门"))),
    ("风险分析与建议", (
        ("政治与地缘风险", "风险等级(高/中/低)与依据"),
        ("经济与支付风险", "风险等级与依据(按越过警戒线的指标写)"),
        ("法律与合规风险", "风险等级与依据"),
        ("执行风险", "风险等级与依据:资质、本地化、劳工、物流、气候"),
        ("结论与建议", "给管理层的 3–5 条明确建议"))),
)
APPENDIX = (("数据来源登记", "各项数据的来源与时间"), ("项目管道清单", "该国全部商机(含不跟进的)"), ("机构与企业名录", "管理机构、水务国企、中标单位"),
            ("水务设施清单", "库里登记的自来水厂与污水厂"), ("术语与缩略语", "报告里出现的机构缩写与中文全称"),
            ("参考研究资料", "库里登记的该国公开研究资料,可点到原文"), ("原文链接", "正文角标对应的原公告与原文,一号一条"))
PROGRAM_SECTIONS = {"范围界定", "研究方法与数据来源", "国别备注与近期动态", "首都在建与规划项目清单", "项目机会清单"}  # 这几节只放数据,不请模型写

COUNTRY_SYSTEM = """你是投标人国籍为 {bidder} 的水务工程承包与投资企业的市场分析师,为管理层撰写《{country}水务市场分析报告》。
写法照专业咨询报告:先给判断,再给依据;简洁、专业的中文书面语,不写客套话,不用 Markdown 标题;每节 300–800 字,可用列表。
下面「报告数据」里的数字、项目、机构与日期可直接使用;引用具体商机时在句末用方括号注明商机编号,如 [UZB-001]。
报告数据里没有的,可以写你确知的公开背景与事实(机构沿革、法律、政策、重大项目、宏观数据),但这样的句子句末必须标注「(公开资料,待核实)」,
拿不准的不写;给不出可靠数字的地方直接写「需人工补充」。不要重复报告数据里表格的全部内容,表格会放在你的段落后面。"""
SUMMARY_FIELD = {"summary": "摘要:4–6 段,每段 2–4 句;依次写市场总体判断、最重要的机构与政策变化、最值得跟进的机会与时间、资金来源特点、主要风险与建议"}
BATCH = 6  # 每次请模型写几节

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

    def __init__(self, bidder_zh: str = "我国", capital: str = "") -> None:
        self.parts: list[str] = []
        self.n = 0
        self.bidder_zh, self.capital = bidder_zh, capital

    def chapter(self, i: int, sections: dict[str, str]) -> None:
        title, secs = OUTLINE[i]
        body = [(name, sections.get(name, "").strip()) for name, _ in secs]
        body = [(name, txt) for name, txt in body if txt]
        if not body:
            return
        self.n += 1
        self.parts.append(f"# 第 {self.n} 章 {_title(title, self.bidder_zh, self.capital)}\n")
        for m, (name, txt) in enumerate(body, 1):
            self.parts.append(f"## {self.n}.{m} {_title(name, self.bidder_zh, self.capital)}\n\n{txt}\n")


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


def _section_fields(bidder: str, capital: str) -> dict[str, tuple[str, str, str]]:
    """{字段名: (章, 节, 写什么)}:给模型写的节(程序填的除外)。"""
    out = {}
    for ci, (ch, secs) in enumerate(OUTLINE):
        for si, (sec, what) in enumerate(secs):
            if sec in PROGRAM_SECTIONS:
                continue
            out[f"s{ci + 1}_{si + 1}"] = (ch, sec, what)
    return out


def _title(sec: str, bidder_zh: str, capital: str) -> str:
    """节名里的「我国」「首都」换成实际说法。"""
    return sec.replace("我国与该国", f"{bidder_zh}与该国").replace("我国", bidder_zh).replace("首都及大首都", f"{capital}及大{capital}" if capital else "首都及大首都") \
        .replace("首都", capital or "首都")


def _write_sections(ctx: Ctx, country: str, data_md: str, fields: dict[str, tuple[str, str, str]], known: set[str]) -> tuple[dict, list[str]]:
    """分批请模型写各节(每批 BATCH 节);→ ({字段: 文本}, 错误)。"""
    clf = ctx.clf
    if clf is None or not getattr(clf, "enabled", False) or not hasattr(clf, "write"):
        return {}, []
    system = COUNTRY_SYSTEM.format(bidder=(ctx.profile.bidder or {}).get("nationality") or "未设", country=country)
    keys = list(fields)
    ana: dict = {}
    errs: list[str] = []
    batches = [dict(SUMMARY_FIELD)] + [{k: f"第{fields[k][0]}章「{fields[k][1]}」:{fields[k][2]}" for k in keys[i:i + BATCH]} for i in range(0, len(keys), BATCH)]
    for fs in batches:
        out = clf.write(system, data_md, fs)
        if not out or out.get("_error"):
            errs.append((out or {}).get("_error") or "模型未返回")
            continue
        out, _ = reporting._check_cites(out, known)
        ana.update(out)
    return ana, errs


def build_country(ctx: Ctx, iso: str, internal: bool = True) -> tuple[str, set[str], set[str], str]:
    """→ (Markdown, 引用的线索记录编号, 写进的商机记录编号, ISO3)。"""
    b = ctx.base
    crid = ctx.country_rid(iso)
    c = b.record("country", crid)
    g = lambda p: b.get("country", c, p) if b.has("country", p) else None  # noqa: E731
    name = str(g("name_zh") or geo.name_zh(iso) or iso)
    bidder_iso = (ctx.profile.bidder or {}).get("nationality") or ""
    bidder_zh = geo.name_zh(bidder_iso) or "我国"
    ind, basics = R.fact_rows(g("indicators")), R.fact_rows(g("basics"))
    kp = R._kpis(ind)

    # ---- 取数:商机、城市、水厂(使用方 2026-10-10:国别报告不放线索,只写人工确认与 AI 建议的商机;「不跟进」的只进附录 B)
    all_opps = R._opps(ctx, iso)
    og = lambda r, p: b.get("opportunity", r, p) if b.has("opportunity", p) else None  # noqa: E731
    opps = [r for r in all_opps if str(og(r, "stage") or "建议") != "不跟进"]
    order = {s: i for i, s in enumerate(R.STAGE_ORDER)}
    opps.sort(key=lambda r: (order.get(str(og(r, "stage")), 99), str(og(r, "code"))))
    code_of = {r["id"]: str(og(r, "code") or "") for r in opps}
    ms = R._milestones(ctx, ctx.today, ctx.today + timedelta(days=R.UPCOMING_DAYS), set(code_of))
    ai_opps = [r for r in opps if str(og(r, "stage") or "建议") == "建议"]
    own_opps = [r for r in opps if r not in ai_opps]
    cities = b.rows("city", filter=b.filter(b.cond("city", "country", "is", crid))) if "city" in b.t and b.has("city", "country") else []
    cities.sort(key=lambda r: -(_num(b.get("city", r, "population")) or 0))
    plants = {t: (b.rows(t, filter=b.filter(b.cond(t, "country", "is", crid))) if t in b.t and b.has(t, "country") else [])
              for t in ("supply_plant", "sewage_plant")}
    cg = lambda r, p: b.get("city", r, p)  # noqa: E731
    awards, events, laws = _rows_in(b, "award", crid), _rows_in(b, "water_event", crid), _rows_in(b, "law", crid)
    limits, lterms, areas, studies = _rows_in(b, "discharge_limit", crid), _rows_in(b, "local_term", crid), _rows_in(b, "water_area", crid), _rows_in(b, "study", crid)
    refs = _Refs()  # 角标:同一网址一个编号,附录里列全
    eg = lambda r, p: b.get("water_event", r, p) if b.has("water_event", p) else None  # noqa: E731
    events_of = lambda *kinds: _events_block(b, [r for r in events if str(eg(r, "kind") or "") in kinds], refs)  # noqa: E731

    # ---- 数据块:{(章, 节): Markdown},放在模型段落后面
    D: dict[tuple[str, str], str] = {}
    D["研究背景、目的与范围", "范围界定"] = (
        f"本报告分析{name}的城镇供水、污水与排水市场,面向海外业务,版本为{'内部版' if internal else '公开版'},编制日期 {ctx.today.isoformat()}。"
        f"商机口径:已立项跟踪的 {len(own_opps)} 个、AI 建议的 {len(ai_opps)} 个(标为不跟进的只进附录 B)。金额按入库当日汇率折合人民币。\n")
    D["研究背景、目的与范围", "研究方法与数据来源"] = "\n".join(f"- {x}" for x in (
        "宏观与水务指标:世界银行 WDI、IMF《世界经济展望》(每月 1 日更新)",
        "水风险:WRI Aqueduct 4.0;人均 GDP(PPP):Kummu 等;世行涉水项目:世行项目库",
        "营商环境:世界银行 B-READY(专家问卷与企业调查,测最大商业城市)",
        "城市:GeoNames;水厂与污水厂:HydroWASTE 等公开数据集与人工补录;水务片区:人工划定",
        "机构、国企与法规:人工核实的国别资料(附来源);法规原文、排放限值与本国定义:从法规原文按条摘出(角标回条文)",
        "已中标项目与中标单位:开发银行与各国采购网公开的中标记录;水务事件:世行项目库与机构文件;研究资料:开发银行文件库",
        "商机:人工立项跟踪的商机与 AI 建议的商机(公开事实由机器从采购公告与新闻整理,我方判断由人填写)",
        "分析段落:模型据以上数据撰写;引用商机处标编号;库里没有、模型按公开资料补写的句子标「(公开资料,待核实)」,以原文为准",
    )) + "\n"
    base_rows = [(h, R._md_text(str(v))) for h, v in (("国名", name), ("大洲与区域", g("region")), ("市场分级", g("tier")),
                                                     ("收入组与贷款类别", g("income_group")), ("经营区域", g("market")),
                                                     ("我方在当地", R._cell(g("presence")) if internal else None)) if v]
    seen = {h for h, _ in base_rows}
    base_rows += [(r["k"], R._val_md(r) + (f"({r['year']})" if r["year"] else "") + (f" {R._src_md(r)}" if R._src_md(r) else "")) for r in basics
                  if r["k"] and r["k"] not in seen and not (r["k"] == "世行收入组" and "收入组与贷款类别" in seen)]
    half = (len(base_rows) + 1) // 2
    pair = [list(base_rows[i]) + (list(base_rows[i + half]) if i + half < len(base_rows) else ["", ""]) for i in range(half)]
    D["国家概况与宏观经济环境", "地理、行政区划与经济区"] = R._table(["项目", "内容", "项目", "内容"], pair) if pair else ""
    used = {id(r) for _, r in kp}
    econ = [r for r in _rows_of(ind, "经济") if id(r) not in used]
    if _num(g("gdp_pc")) is not None and not any("PPP" in r["k"] for r in ind):  # 人均 GDP(PPP):概况页「水风险与经济」里的那项
        econ.append({"k": "人均 GDP(PPP)", "v": f"{_num(g('gdp_pc')):,.0f} 美元(2021 年国际美元)", "year": "2024", "src": "Kummu 等(2025)",
                     "url": "https://doi.org/10.1038/s41597-025-04487-x", "bad": None})
    D["国家概况与宏观经济环境", "宏观经济"] = "\n".join(x for x in (R.kpi_table(kp), _ind_table(econ)) if x)
    D["国家概况与宏观经济环境", "外汇、汇率与主权评级"] = _ind_table([r for r in _rows_of(ind, "财政与外部") if id(r) not in used])
    bready = _bready(g, name, b.has("country", "bready_edition"))
    D["国家概况与宏观经济环境", "外资与营商环境"] = bready
    D["国家概况与宏观经济环境", "国别备注与近期动态"] = R._bullets(g("note")) if internal and g("note") else ""
    city_rows = [[cg(r, "name_zh") or cg(r, "name_en"), _fmt(_num(cg(r, "population"))), _fmt(_num(cg(r, "gdp")), 1),
                  _risk(_num(cg(r, "water_stress"))), _fmt(_num(cg(r, "supply_count"))), _fmt(_num(cg(r, "sewage_count")))] for r in cities[:CITY_ROWS]]
    D["人口、城镇化与用水需求", "全国人口与预测"] = _ind_table(_rows_of(ind, "人口与国土"))
    D["人口、城镇化与用水需求", "城镇体系"] = ((f"库里登记该国城市 {len(cities)} 座,按人口列前 {min(len(cities), CITY_ROWS)} 座。\n\n"
                                           + R._table(["城市", "人口", "地区生产总值(亿美元)", "用水压力", "自来水厂", "污水厂"], city_rows)) if cities else "") \
        + ("\n" + _areas_block(b, areas, cities, opps) if areas else "")
    water_keys = ("人均境内可再生淡水", "年淡水取用量", "淡水压力", "取水中")
    pct = lambda p: f"{_num(g(p)):.1f}%" if _num(g(p)) is not None else ""  # noqa: E731
    risk_rows = [[h, v] for h, v in (("总体水风险", _risk(_num(g("water_risk")))), ("用水压力(当前)", _risk(_num(g("water_stress")))),
                                    ("用水压力(2030 年)", _risk(_num(g("water_stress_2030")))), ("用水压力(2050 年)", _risk(_num(g("water_stress_2050")))),
                                    ("干旱风险", _risk(_num(g("drought")))), ("河流洪水风险", _risk(_num(g("flood")))),
                                    ("无安全饮用水人口占比", pct("no_drinking")), ("无卫生设施人口占比", pct("no_sanitation")), ("未处理污水占比", pct("untreated_ww"))) if v]
    D["水资源与气候条件", "水资源总量与跨境依赖"] = _ind_table([r for r in ind if r["k"].startswith(water_keys[:1])])
    D["水资源与气候条件", "用水结构与水资源压力"] = _ind_table([r for r in ind if r["k"].startswith(water_keys[1:])]) \
        + ((R._table(["项目", "分数(0–5)或占比"], risk_rows) + f"\n来源:{R._md_text(str(g('risk_source') or 'WRI Aqueduct 4.0'))}。分数 4 以上极高,3–4 高;模型估计,作趋势参考。\n") if risk_rows else "")
    tariffs = [[cg(r, "name_zh"), _fmt(_num(cg(r, "tariff")), 2), cg(r, "tariff_currency") or "", tstore.day(cg(r, "tariff_changed"), b.tz) or "",
                R._md_text(R._cell(cg(r, "tariff_note")))[:80]] for r in cities if _num(cg(r, "tariff")) is not None]
    D["水务行业体制、法规与规划", "机构体系与近期改革"] = "\n".join(x for x in (
        R._dots_table(g("water_agencies"), ["机构", "英文名", "职责", "网站"]), R._dots_table(g("water_soes"), ["企业", "英文名", "说明", "网站"]), events_of("机构改革")) if x)
    D["水务行业体制、法规与规划", "法律法规"] = R._dots_table(g("laws"), ["法规", "领域", "要点", "链接"]) + _laws_block(b, laws, refs)
    D["水务行业体制、法规与规划", "国家战略与规划"] = events_of("规划与政策")
    D["水务行业体制、法规与规划", "水价体制与财务可持续性"] = "\n".join(x for x in (
        (R._table(["城市", "现行水价", "货币", "最近调价", "说明"], tariffs) if tariffs else ""), events_of("水价调整")) if x)
    D["水务行业体制、法规与规划", "技术标准与审批"] = "\n".join(x for x in (_limits_block(b, limits, laws, refs), _terms_block(b, lterms, laws, refs)) if x)
    wb_line = (f"世行项目库 2020 年 10 月至 2024 年:该国涉水项目 {_fmt(_num(g('wb_water_n')))} 个,承诺额 {_fmt(_num(g('wb_water_musd')))} 百万美元。\n"
               if _num(g("wb_water_n")) is not None else "")
    D["水务行业体制、法规与规划", "国际合作格局"] = "\n".join(x for x in (wb_line, events_of("项目批准", "项目完成")) if x)

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

    D["全国供水现状", "覆盖率与服务水平"] = _ind_table([r for r in ind if re.search(r"饮水", r["k"])])
    D["全国供水现状", "水源与水厂"] = plant_block("supply_plant", "自来水厂")
    D["全国供水现状", "区域城镇供水项目进展"] = events_of("工程投运")
    D["全国污水与排水现状", "收集率与处理率"] = _ind_table([r for r in ind if re.search(r"卫生", r["k"])])
    D["全国污水与排水现状", "污水处理厂"] = plant_block("sewage_plant", "污水处理厂")
    D["全国污水与排水现状", "环境影响与排放标准"] = _limits_block(b, limits, laws, refs)
    cap_name = _capital(basics)
    cap_city = next((r for r in cities if cap_name and str(cg(r, "name_zh") or "") == cap_name), cities[0] if cities else None)
    cap_ids: set[str] = set()
    if cap_city:
        cap_name = str(cg(cap_city, "name_zh") or cap_name)
        cap_ids = {cap_city["id"]}
        ag = lambda r, p: b.get("water_area", r, p)  # noqa: E731
        for a in areas:  # 首都所在的片区:片区里的城市都算首都圈
            ids = {x.get("id") if isinstance(x, dict) else x for x in (ag(a, "cities") or [])}
            if cap_city["id"] in ids:
                cap_ids |= ids
        cp = [[h, v] for h, v in (("人口", _fmt(_num(cg(cap_city, "population"))) + (f"({cg(cap_city, 'pop_year')})" if cg(cap_city, "pop_year") else "")),
                                 ("地区生产总值(亿美元)", _fmt(_num(cg(cap_city, "gdp")), 1)), ("用水压力", _risk(_num(cg(cap_city, "water_stress")))),
                                 ("干旱风险", _risk(_num(cg(cap_city, "drought")))), ("无安全饮用水人口占比", _fmt(_num(cg(cap_city, "no_drinking")), 1, "%")),
                                 ("未处理污水占比", _fmt(_num(cg(cap_city, "untreated_ww")), 1, "%")),
                                 ("现行水价", (_fmt(_num(cg(cap_city, "tariff")), 2) + " " + str(cg(cap_city, "tariff_currency") or "")).strip()),
                                 ("自来水厂 / 污水厂", f"{_fmt(_num(cg(cap_city, 'supply_count'))) or 0} / {_fmt(_num(cg(cap_city, 'sewage_count'))) or 0} 座")) if v]
        D["首都及大首都水务专题", "概况"] = R._table(["项目", cap_name], cp)
    opp_city = lambda r: {x.get("id") if isinstance(x, dict) else x for x in (og(r, "city") or [])} if b.has("opportunity", "city") else set()  # noqa: E731
    cap_opps = [r for r in opps if opp_city(r) & cap_ids]
    other_opps = [r for r in opps if r not in cap_opps]
    opp_table = lambda rows: R._table(["编号", "名称", "我方阶段", "公开进度", "下一节点", "资金"], [[_strip_refs(R._cell(v)) for v in x[:2] + x[3:]] for x in R._opp_rows(ctx, rows)])  # noqa: E731
    D["首都及大首都水务专题", "首都在建与规划项目清单"] = (f"挂在{cap_name}及其片区的商机 {len(cap_opps)} 个:\n\n" + opp_table(cap_opps)) if cap_opps else ""
    D["区域城镇与重点地区", "区域城镇的投资模式"] = (f"{cap_name or '首都'}以外的商机 {len(other_opps)} 个:\n\n" + opp_table(other_opps)) if other_opps and cap_ids else ""
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
    D["投资规划与资金来源", "投资规模的核实"] = ((f"写明规模的商机 {len(osz)} 个,合计约 {_fmt(sum(v for _, v in osz) / 1e4, 1)} 亿元人民币:\n\n"
                                            + R._table(["编号", "名称", "折合人民币(亿元)"], [[og(r, "code"), R._md_text(str(og(r, "name_zh") or "")), _fmt(v / 1e4, 2)] for r, v in osz])) if osz else "")
    D["投资规划与资金来源", "国际金融机构项目管道"] = "\n".join(x for x in (
        ((f"写明资金来源的商机 {len(fin_rows)} 个,按出资方计:\n\n" + R._table(["出资方", "商机数"], fund_rows)
          + "\n各商机的融资情况:\n\n" + R._table(["编号", "名称", "我方阶段", "资金"], fin_rows)) if fund_rows else ""), wb_line) if x)
    party_rows = []
    for r in opps:
        lines = [_strip_refs(x) for x in str(og(r, "parties") or "").split("\n") if x.strip()]
        owner = str(og(r, "owner_org") or "").strip()
        if owner:
            lines.insert(0, "业主与执行机构:" + _strip_refs(owner))
        if lines:
            party_rows.append([og(r, "code"), R._md_text(str(og(r, "name_zh") or "")), ";".join(R._md_text(x)[:160] for x in lines[:5])])
    aw_table, aw_firms = _awards_block(b, awards, refs, ctx.today)
    D["竞争格局", "业主与采购模式"] = R._table(["编号", "名称", "参与方"], party_rows) if party_rows else ""
    D["竞争格局", "外国承包商与服务商"] = "\n".join(x for x in (aw_table, aw_firms) if x)
    D["竞争格局", f"{bidder_zh}企业在该国业绩与经验教训"] = _awards_block(b, [r for r in awards if bidder_zh and bidder_zh in str(b.get("award", r, "winner_country") or "")], refs, ctx.today)[0] \
        .replace("库里登记该国已中标项目", f"库里登记{bidder_zh}企业在该国的已中标项目") if awards and bidder_zh else ""
    D["市场准入与商务环境", "招标采购流程"] = "\n".join(x for x in ((R._bullets(g("access")) if g("access") else ""),
                                                             (_bready_items(g) if bready and _num(g("bready_total")) is not None else "")) if x)
    D["机会识别与进入策略", "项目机会清单"] = (
        "已立项跟踪的商机:\n\n" + (opp_table(own_opps) if own_opps else "该国还没有人工立项跟踪的商机。\n")
        + ("\nAI 建议的商机:\n\n" + R._table(["编号", "名称", "公开进度", "建议理由"],
                                         [[og(r, "code"), R._md_text(str(og(r, "name_zh") or "")), og(r, "public_stage") or "",
                                           R._md_text(_strip_refs(str(og(r, "ai_reason") or "").split("\n")[0]))[:200]] for r in ai_opps])
           + "\n机器建议的商机未经人工确认,跟不跟由业务人员在「线索处理 · AI 推荐的商机」里决定。\n" if ai_opps else "")
        + f"\n未来 {R.UPCOMING_DAYS} 天的时间节点:\n\n"
        + (R._table(["日期", "类型", "商机", "状态", "说明"], [x[:3] + x[4:] for x in R._ms_rows(ctx, ms, code_of)]) if ms else f"未来 {R.UPCOMING_DAYS} 天没有时间节点。\n"))
    bad = [r for r in ind if r["bad"]]
    D["风险分析与建议", "经济与支付风险"] = ("以下指标越过了警戒线(警戒线写在括号里):\n\n"
                                      + "\n".join(f"- {R._md_text(r['k'])}:{R._md_text(r['v'])}({r['year']}),警戒线:{r['bad']}" for r in bad) + "\n") if bad else ""

    # ---- 分析段落:把全部数据交给模型,按节分批写
    known = R._known(ctx, [], opps)
    data_md = "\n\n".join(f"## {ch}\n\n" + "\n\n".join(f"### {s}\n\n{t}" for (c2, s), t in D.items() if c2 == ch and t and s != "国别备注与近期动态")
                          for ch, _ in OUTLINE[1:] if any(c2 == ch and t for (c2, _), t in D.items()))
    pts = R.key_points(g, ind)
    data_md = (f"# 报告数据:{name}(ISO3 {iso}),投标人国籍 {bidder_zh}\n\n要点:\n" + "\n".join(f"- {x}" for x in pts) + "\n\n" + data_md)[:80000]
    fields = _section_fields(bidder_zh, cap_name)
    ana, errs = _write_sections(ctx, name, data_md, fields, known)
    used_model = bool(ana)

    # ---- 成书:封面、目录、摘要、各章、附录
    book = _Book(bidder_zh, cap_name)
    for ci, (ch, secs) in enumerate(OUTLINE):
        sections = {}
        for si, (sec, _) in enumerate(secs):
            prose = _para(ana.get(f"s{ci + 1}_{si + 1}"))
            data = D.get((ch, sec), "") or D.get((ch, _title(sec, bidder_zh, cap_name)), "")
            sections[sec] = (prose + ("\n" if prose and data else "") + data).strip()
        book.chapter(ci, sections)
    chapters = [p for p in book.parts if p.startswith("# 第 ")]
    month = f"{ctx.today.year} 年 {ctx.today.month} 月"
    cover = (f"---\ntitle: \"{name}水务市场分析报告\"\nsubtitle: \"{'内部资料　仅供参考' if internal else '公开版'}\"\ndate: \"{month}\"\n---\n\n"
             + "**目　录**\n\n" + "\\\n".join(["摘要"] + [p[2:].strip() for p in chapters] + ["附录"]) + "\n")
    summary = _para(ana.get("summary")) if ana.get("summary") else (
        f"{name}:已立项跟踪的商机 {len(own_opps)} 个,AI 建议的商机 {len(ai_opps)} 个;未来 {R.UPCOMING_DAYS} 天时间节点 {len(ms)} 个。\n\n"
        + "\n".join(f"- {R._md_text(x)}" for x in pts) + "\n")
    note = (f"分析段落由模型({ctx.clf.label})按阿塞拜疆成稿的章节逐节撰写:方括号内为商机编号;标「(公开资料,待核实)」的句子是模型按公开资料补写的,须核实后再引用;"
            "写「需人工补充」的地方库里没有依据。" if used_model else "本报告未使用模型,只列数据;各节的分析段落需人工补充,所以比成稿短得多。")
    if errs:
        note += f"其中 {len(errs)} 批段落模型没写成({errs[0]}),已从略。"
    names = json.loads(TERMS_PATH.read_text(encoding="utf-8"))["terms"]
    body_text = "\n".join(book.parts)
    terms = [[a, names[a]] for a in FUNDERS if a in names and re.search(r"(?<![A-Za-z])" + re.escape(a) + r"(?![A-Za-z])", body_text)]
    pipe = R._table(["编号", "名称", "我方阶段", "公开进度", "下一节点", "资金"], [[_strip_refs(R._cell(v)) for v in x[:2] + x[3:]] for x in R._opp_rows(ctx, all_opps)]) if all_opps else ""
    orgs = "\n".join(x for x in (R._dots_table(g("water_agencies"), ["机构", "英文名", "职责", "网站"]), R._dots_table(g("water_soes"), ["企业", "英文名", "说明", "网站"]), aw_firms) if x)
    facilities = "\n".join(x for x in (plant_block("supply_plant", "自来水厂"), plant_block("sewage_plant", "污水处理厂")) if x)
    appendix = ["# 附录\n", "## 附录 A 数据来源登记\n\n" + D["研究背景、目的与范围", "研究方法与数据来源"]
                + f"\n{note}数据取自部门共用表格(国别、城市、水厂、商机、已中标项目、法规、水务事件、研究资料),生成于 {R.now_iso()}。\n"]
    for letter, (title, text) in zip("BCDEFG", (("项目管道清单", pipe), ("机构与企业名录", orgs), ("水务设施清单", facilities),
                                                 ("术语与缩略语", R._table(["缩写", "中文全称"], terms) if terms else ""),
                                                 ("参考研究资料", _studies_block(b, studies, refs)),
                                                 ("原文链接", ("正文角标的号对应这里的原公告与原文,一号一条:\n\n" + refs.chapter()) if refs.urls else ""))):
        if text:
            appendix.append(f"## 附录 {letter} {title}\n\n{text}")
    md = cover + "\n# 摘要\n\n" + summary + "\n" + "\n".join(book.parts) + "\n" + "\n".join(appendix)
    cl, co = R._cites(ctx, [], opps)
    return md, cl, co, iso


def outline_md() -> str:
    """报告模板的正文:章节目录与每节放什么(scripts/make_report_template.py 写进模板,让模板一打开就看得到报告长什么样)。"""
    lines = ["# 摘要\n", "(市场总体判断、最重要的机构与政策变化、最值得跟进的机会与时间、资金来源特点、主要风险与建议)\n"]
    for n, (title, secs) in enumerate(OUTLINE, 1):
        lines.append(f"# 第 {n} 章 {title}\n")
        for m, (s, what) in enumerate(secs, 1):
            lines.append(f"## {n}.{m} {s}\n\n({what})\n")
    lines.append("# 附录\n")
    for i, (s, what) in enumerate(APPENDIX):
        lines.append(f"## 附录 {'ABCDEFG'[i]} {s}\n\n({what})\n")
    return "\n".join(lines)
