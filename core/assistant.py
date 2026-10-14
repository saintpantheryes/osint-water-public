"""助手(门户「助手」页的后端):把一句中文提问解析成页面能执行的「查询计划」。

只做理解,不碰数据:计划里是对象(线索 / 商机 / 时间节点 / 报告 / 已中标项目)、意图(数一下 / 列清单 / 分组统计 / 要报告)、
筛选条件(国别、领域、类别、资金来源、我方阶段、处理状态、时间范围、关键词;已中标项目另有合同类型、承包模式、主体工程、中标单位及其国别、规模、金额)、
分组维度与指标(本体 semantic 节的语义层:数量、中标价、中标价 / 招标估价、评标天数、投标家数,各自的算法与单位);
页面拿着计划、带着登录人的 cookie 去查表格并呈现,数字全部来自表格,模型不写数字、也看不到表里的数据(送给模型的只有问题本身)。
已中标项目的规模表格筛不了:计划里只给下限 / 上限(m³/日),页面读工程量与名称里写明的规模再筛,没写规模的另列。
接着问(页面把上一问的计划带来):没说查什么的沿用上一问的对象;以「那」「其中」开头或很短的「……呢」再沿用上一问的条件与指标,只换这一句说到的。

两种解析:
- 规则:词表(本体的枚举值 + 口语同义词)、国名(reference/countries.yaml)、时间短语。没配模型密钥时只有它;配了也先跑一遍兜底。
- 模型:DeepSeek 或 Claude(与采集判定共用 core.watch.llm 的接入;profile 的 assistant.llm 可单独指定,缺省沿用 watch.llm 的
  provider 与 model,但关掉思考模式、输出很短,一问几百 token)。模型输出的每个值都按词表校验,认不出的丢进 unresolved 让人看见。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

import yaml

from core import fx, geo, law

ROOT = Path(__file__).resolve().parent.parent
ONTO_PATH = ROOT / "docs" / "ontology" / "ontology.yaml"

OBJECTS = ("lead", "opportunity", "milestone", "report", "award", "country", "law")
OBJECT_ZH = {"lead": "线索", "opportunity": "商机", "milestone": "时间节点", "report": "报告", "award": "已中标项目", "country": "国别", "law": "法规"}
INTENTS = ("count", "list", "chart", "report", "help")
GROUPS = {"lead": ("country", "category", "sectors", "funder", "day", "status", "source"),
          "opportunity": ("country", "stage", "sectors", "funder"),
          "milestone": ("country", "kind", "day"), "report": ("kind", "country"),
          "award": ("country", "sector", "contract_type", "delivery", "asset", "winner_country", "funder", "year"),
          "country": (),  # 国别一国一行(营商环境指标),不再分组:页面列国别表
          "law": ()}  # 法规问答(core/law.py):页面按种类列排放限值、本国定义或条文
GROUP_ZH = {"country": "国别", "category": "类别", "sectors": "领域", "funder": "资金来源", "day": "按天", "status": "处理状态",
            "source": "信源", "stage": "我方阶段", "kind": "类型", "sector": "类别", "contract_type": "合同类型", "delivery": "承包模式", "asset": "主体工程", "winner_country": "中标单位国别", "year": "年份"}
GROUP_ALIAS = {"award": {"category": "sector", "sectors": "sector", "kind": "contract_type", "day": "year"}}  # 别的对象的分组说法 → 已中标项目的(它的「类别」就是领域)
MAX_Q = 400
DEFAULT_LIMIT, MAX_LIMIT = 50, 200
DEFAULT_FUTURE_DAYS = 30

# 口语 → 本体值(本体枚举值本身也直接认)。顺序:长词在前,避免「海水淡化」被「淡化」先切掉一半
SECTOR_WORDS = {
    "供水": ["供水", "饮水", "饮用水", "净水", "自来水", "输水", "管网", "水厂", "减漏", "水表", "计量", "water supply", "drinking water"],
    "污水": ["污水", "废水", "污泥", "再生水", "排污", "卫生设施", "wastewater", "sewage", "sludge"],
    "水利": ["水利", "灌溉", "灌区", "水库", "防洪", "调水", "渠道", "大坝", "水坝", "irrigation", "dam", "flood"],
    "海水淡化": ["海水淡化", "海淡", "淡化", "desalination", "desal"],
    "雨水排涝": ["雨水排涝", "雨水", "排涝", "内涝", "排水", "stormwater", "drainage"],
    "水环境": ["水环境", "河道治理", "流域治理", "生态修复", "水环境治理", "river restoration"],
}
CATEGORY_WORDS = {
    "招标": ["招标", "招投标", "招标公告", "tender", "招标文件", "发标"],
    "意向书": ["意向书", "意向征集", "eoi", "reoi", "expression of interest"],
    "资格预审": ["资格预审", "预审", "prequalification", "pq"],
    "总采购公告": ["总采购公告", "gpn"],
    "特定采购公告": ["特定采购公告", "spn"],
    "授标或短名单": ["授标", "短名单", "定标", "中标公告", "中标结果", "合同签署", "award", "shortlist"],
    "融资": ["融资", "贷款", "赠款", "融资类", "loan", "grant", "financing"],
    "立项或规划": ["立项", "规划", "可研", "可行性研究", "总体规划", "plan", "feasibility"],
    "项目动态": ["项目动态", "项目进展", "动态", "进展", "开工", "完工", "news"],
    "政策": ["政策", "法规", "法律", "水价", "policy", "law"],
}
FUNDER_WORDS = {  # 本体 groupings.funders 的 patterns 之外再补中文简称
    "世界银行": ["世行", "世界银行", "world bank"], "亚洲开发银行": ["亚行", "亚开行", "亚洲开发银行", "adb"], "亚投行": ["亚投行", "aiib"],
    "非洲开发银行": ["非行", "非开行", "非洲开发银行", "afdb"], "欧洲复兴开发银行": ["欧复行", "欧洲复兴开发银行", "欧洲复兴", "ebrd"],
    "欧洲投资银行": ["欧投行", "欧洲投资银行", "eib"], "伊斯兰开发银行": ["伊行", "伊斯兰开发银行", "伊斯兰银行", "isdb"],
    "美洲开发银行": ["美洲开发银行", "泛美开发银行", "idb"], "欧佩克基金": ["欧佩克", "opec"], "法国开发署": ["法开署", "法国开发署", "afd"],
    "德国复兴信贷银行": ["德国复兴信贷银行", "德复行", "kfw"], "欧盟": ["欧盟", "european union"], "联合国机构": ["联合国", "undp", "unicef", "unops"],
    "美国开发机构": ["美国国际开发署", "usaid", "美国开发金融公司", "dfc"], "本国政府": ["本国政府", "政府出资", "政府预算", "政府"],
}
STATUS_WORDS = {"待处理": ["待处理", "未处理", "没处理", "没人看"], "已处理": ["已处理", "处理过", "处理了"], "已忽略": ["已忽略", "忽略了", "忽略的"]}
TRACKED_WORDS = ["在跟踪", "跟踪中的商机", "正在跟的", "在跟的", "跟进中的商机", "跟踪的商机"]
SUGGESTED_WORDS = ["ai推荐", "ai 推荐", "推荐的商机", "机器建议", "建议的商机", "待定的商机"]
DEADLINE_WORDS = ["截止", "到期", "deadline", "截标", "收标"]
NUM_ZH = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "十五": 15, "二十": 20, "三十": 30, "半": 0.5}
UNIT_DAYS = {"天": 1, "日": 1, "周": 7, "星期": 7, "礼拜": 7, "个月": 30, "月": 30, "年": 365}
EXAMPLES = ["最近 7 天埃及有哪些新线索", "世界银行出资的供水项目有多少条", "最近 30 天的招标按国家分", "在跟踪的商机有哪些",
            "未来 30 天有哪些投标截止", "海水淡化的线索按资金来源统计", "摩洛哥近三年运营维护的中标信息", "摩洛哥近三年污水处理厂的中标价是招标估价的几成"]

# 已中标项目(本体 award):别人已经拿到的合同(谁中了什么标、中标价多少)。提问里有「中标」「授予」「award」就是它;
# 说的是我方的(我们中标的、中标的商机、阶段)是商机的我方阶段「中标」;说线索、公告的是线索类别「授标或短名单」
AWARD_RE = re.compile(r"中标|中了.{0,6}?标|中的标|授予|已授合同|(?:招标|授标|开标)结果|(?<![a-z])award(?:s|ed)?(?![a-z])")
OURS_RE = re.compile(r"(?<![帮给替为让请])(?:我们|我方|我司|咱们|本公司|我公司)[^,，。;；?？]{0,12}?(?:中标|中了|拿下|赢)|商机|项目机会|在跟|跟踪|跟进|投标的|阶段")
CONTRACT_WORDS = {  # 合同类型(本体 award_contract_type)的口语;按 CONTRACT_ORDER 认,认到的词先划掉:「非咨询」不算咨询,「设计施工」「EPC」算工程
    "非咨询服务": ["非咨询服务", "非咨询", "non-consulting"],
    "运营": ["运营维护", "运维", "运营", "委托运营", "托管运营", "管理合同", "o&m", "o & m", "operation and maintenance", "operation & maintenance", "operations",
             "operation", "affermage"],
    "工程": ["施工", "土建", "工程", "works", "construction"],  # EPC、DBO、设计施工是承包模式(DELIVERY_WORDS)
    "货物": ["货物", "设备", "供货", "物资", "goods", "equipment"],
    "咨询服务": ["咨询服务", "咨询", "设计", "监理", "可研", "可行性研究", "consulting", "consultancy", "consultant", "supervision", "design"],
}
CONTRACT_ORDER = ("非咨询服务", "运营", "工程", "货物", "咨询服务")
DELIVERY_WORDS = {  # 承包模式(本体 award_delivery)的口语;按 DELIVERY_ORDER 认,认到的词先划掉(「设计建设运营」是 DBO,不再算设计施工)
    "DBO": ["dbo", "dbot", "dbom", "设计建造运营", "设计建设运营", "设计施工运营", "建设运营一体", "建设加运营", "建设+运营", "建设和运营", "建设并运营",
            "design build operate", "design-build-operate", "design, build and operate"],
    "BOT/PPP": ["bot", "boot", "boo", "ppp", "公私合营", "政府和社会资本合作", "特许经营", "特许权", "混合年金", "hybrid annuity", "concession"],
    "设计施工": ["epc", "设计施工", "设计建设", "设计建造", "设计采购施工", "工程总承包", "交钥匙", "design build", "design-build", "design and build", "turnkey"],
    "施工": ["纯施工", "单纯施工", "施工总承包"],
}
DELIVERY_ORDER = ("DBO", "BOT/PPP", "设计施工", "施工", "运营")
FIRM_RE = re.compile(r"\s*的?\s*(?:企业|公司|承包商|厂商|单位|企|联合体|团队|集团|firms?|compan(?:y|ies)|contractors?)")  # 国名后面跟这些:说的是中标单位的国别
CN_FIRM_WORDS = ("中资", "中企", "中方", "chinese")  # 不带国名的「中国企业」说法
# 规模(日处理 / 供水能力):提问里的「5000 m³/d 以上」「日处理 5 万吨」→ m³/日。页面(index.html 的 capM3d)按同一套单位读工程量与名称
CAP_NUM = r"(\d+(?:[.,]\d+)*|[一二两三四五六七八九十百千]+)\s*(万|千|k|thousand|million|百万|亿|тыс\.?)?\s*"
CAP_UNITS = [  # 单位 → 折成 m³/日 的倍数;「吨」按水算 1 吨 = 1 m³;只写「m³」不带「/日」的多半是池容、方量,不算(前面写了规模、水厂的除外,见 CAP_DAILY)
    (r"(?:m3|m³|立方米?|方|吨|t|cbm)\s*(?:/|每|per|a)\s*(?:d|day|j|jour|d[ií]a|日|天)(?![a-z])|m3pd|cmd(?![a-z])|cubic met(?:er|re)s? (?:per|a) day|(?:м3|м³)\s*/\s*сут", 1),
    (r"(?:m3|m³|立方米?|方|吨|t)\s*(?:/|每|per)\s*(?:h|hour|小时|时)(?![a-z])", 24),
    (r"(?:m3|m³|立方米?)\s*(?:/|每|per)\s*(?:s|sec|second|秒)(?![a-z])", 86400),
    (r"(?:l|升)\s*(?:/|每|per)\s*(?:s|sec|second|秒)(?![a-z])|lps(?![a-z])", 86.4),
    (r"mld(?![a-z])|ml\s*/\s*d(?:ay)?(?![a-z])|megalit(?:er|re)s? (?:per|a) day", 1000),
    (r"mgd(?![a-z])", 3785.41),
]
CAP_DAILY = r"(?:规模|能力|处理量|供水量|产水量|日处理|日供水|日产水?|水厂|处理厂|淡化厂)[^\d一二两三四五六七八九十]{0,4}?(?:\d+(?:[.,]\d+)*\s*(?:万|千)?\s*(?:-|–|—|~|～|至|到)\s*)?"
CAP_MULT = {"万": 1e4, "千": 1e3, "k": 1e3, "thousand": 1e3, "million": 1e6, "百万": 1e6, "亿": 1e8, "тыс": 1e3, "тыс.": 1e3}
AWARD_GROUPS = [  # 已中标项目的分组说法,先于通用的认(通用的「按类别 / 领域」「按天」再经 GROUP_ALIAS 换成它的)
    (r"(?:按|各|分)(?:中标)?(?:单位|方|企业|公司|承包商)的?(?:国别|国家)|中标(?:单位|方|企业|公司)的?(?:国别|国家)|(?:哪些|哪个|哪国|各国)的?(?:企业|公司|承包商)", "winner_country"),
    (r"按(?:承包)?模式|(?:各|分)(?:承包)?模式|承包模式(?:分|统计|分布)", "delivery"),
    (r"按合同|按类型|(?:各|分)(?:合同)?类型", "contract_type"),
    (r"按主体工程|按工程(?:对象|类别)|(?:各|分)(?:类)?(?:主体工程|工程对象)|(?:水厂|厂)(?:和|与|跟|、)管网(?:分开|分别|各)", "asset"),
    (r"按年|每年|逐年|各年|历年|年度|趋势|走势", "year"),
]


# 语义层(本体 semantic 节,v2.21):指标、算法与分类值的口语都在本体里,这里只认「提问里明说了查什么」与「接着问」的说法
OTHER_OBJECT_RE = re.compile(r"线索|公告|商机|项目机会|在跟|跟踪|跟进|节点|截止日?|到期|日程|时间表|报告|日报|周报|月报")  # 明说了查已中标项目以外的
OBJECT_RE = re.compile(OTHER_OBJECT_RE.pattern + r"|中标|授予|授标|已授|合同|(?<![a-z])awards?(?![a-z])")
FOLLOW_RE = re.compile(r"^\s*(?:那|那么|那就|其中|这些|这里面|这其中|它们|同样|换成|如果是|再看|再按|还是)")  # 接着上一问说的;很短的「……呢」也算(见 _follow_up)
SECTOR_FIX = [("污水处理厂", "污水"), ("污水厂", "污水"), ("污水管网", "污水"), ("污水管", "污水"), ("排水管网", "排水"), ("雨水管网", "雨水")]  # 「污水厂」里的「水厂」、「污水管网」里的「管网」不算供水
INHERIT = ("countries", "sectors", "categories", "funders", "contract_types", "deliveries", "assets", "winner_countries", "winners", "keywords", "price", "capacity",
           "tiers", "foreign", "metric")
INHERIT_ZH = {"countries": "国别", "sectors": "类别或领域", "categories": "类别", "funders": "资金来源", "contract_types": "合同类型", "deliveries": "承包模式",
              "assets": "主体工程", "winner_countries": "中标单位国别", "winners": "中标单位", "keywords": "关键词", "price": "金额", "capacity": "规模",
              "time": "时间", "measures": "指标", "group_by": "分组",
              "tiers": "市场分级", "foreign": "外企参标", "metric": "门槛"}


def _words(x) -> list[str]:
    """表里的长文本(一行一个,或用「、」「,」「;」隔开)或本体里的列表 → 词。"""
    parts = x if isinstance(x, list) else re.split(r"[\n、,，;；]+", str(x or ""))
    return list(dict.fromkeys(w.strip() for w in parts if str(w).strip()))


def make_term(t: dict) -> dict | None:
    """一行行业术语(Teable「行业术语」表的一行,或本体 terms 节的初始词)→ 助手用的形状。拉丁字母的相关词短于 4 个字母的不用(会误中别的词)。"""
    term = str(t.get("term") or "").strip()
    match = [w for w in _words(t.get("match")) if not (w.isascii() and len(w) < 4)]
    if not term or not match or t.get("disabled"):
        return None
    sectors = [s for s in _words(t.get("sectors")) if s != "其他"]
    return {"term": term, "covers": _words(t.get("covers")), "sectors": sectors, "match": match, "means": str(t.get("means") or ""),
            "triggers": sorted({w.lower() for w in [term, *_words(t.get("aliases"))]}, key=len, reverse=True)}


_live_terms: list[dict] | None = None  # 后台任务从 Teable「行业术语」表读到的(assistant_server 每分钟刷新);None = 用本体的初始词


def set_terms(rows: list[dict] | None) -> None:
    """换上表里读到的行业术语(rows 为表的行,字段按本体属性 id);None 回到本体 terms 节的初始词。"""
    global _live_terms
    _live_terms = None if rows is None else [x for x in (make_term(r) for r in rows) if x]
    law.set_concepts(rows)  # 同一张表也是通用概念(语义层):没写相关词、只作法规概念的行也算


def terms() -> list[dict]:
    return _live_terms if _live_terms is not None else vocab().terms


@dataclass
class Vocab:
    """词表:全部来自本体(枚举值、资金来源归组、跟踪口径),再叠口语同义词。"""
    sectors: list[str]
    sectors_fine: dict[str, list[str]]  # 粗分 → 商机表的细分领域
    categories: list[str]
    funders: list[str]
    funder_other: str
    funder_empty: str
    stages: list[str]
    tracked: list[str]
    statuses: list[str]
    milestone_kinds: list[str]
    deadline_kinds: list[str]
    report_kinds: list[str]
    contract_types: list[str] = field(default_factory=list)  # 已中标项目的合同类型(本体 award_contract_type)
    deliveries: list[str] = field(default_factory=list)  # 已中标项目的承包模式(本体 award_delivery)
    assets: list[str] = field(default_factory=list)  # 已中标项目的主体工程(本体 award_asset)
    measures: list[dict] = field(default_factory=list)  # 本体 semantic.measures(words 小写、长的在前)
    aggs: list[dict] = field(default_factory=list)  # 本体 semantic.aggs
    terms: list[dict] = field(default_factory=list)
    market_tiers: list[str] = field(default_factory=list)  # 「市场国别」= 本体 glossary.threshold.tiers(重点、市场)
    not_collected: list[dict] = field(default_factory=list)  # 本体 data_scope.not_collected:不在采集范围的国家(iso3、口语 words、说明 note)  # 本体 terms:行业术语(triggers = 词本身 + 同义说法,小写、长的在前)
    words: dict[str, dict[str, list[str]]] = field(default_factory=dict)  # 维度 → 值 → 触发词(小写)


def _values(tax: dict) -> list[dict]:
    return [v for g in tax["groups"] for v in g["values"]]


@lru_cache(maxsize=1)
def vocab() -> Vocab:
    o = yaml.safe_load(ONTO_PATH.read_text(encoding="utf-8"))
    tax = o["taxonomies"]
    fine: dict[str, list[str]] = {}
    for v in _values(tax["opp_sectors"]):
        fine.setdefault(v["parent"], []).append(v["v"])
    fund = o["groupings"]["funders"]
    sem = o.get("semantic") or {}
    v = Vocab(
        sectors=[x["v"] for x in _values(tax["lead_sectors"])], sectors_fine=fine,
        categories=[x["v"] for x in _values(tax["lead_category"])],
        funders=[g["v"] for g in fund["groups"]], funder_other=fund["other"], funder_empty=fund["empty"],
        stages=[x["v"] for x in _values(tax["stage"])], tracked=list(o["glossary"]["tracked"]["stages"]),
        statuses=[x["v"] for x in _values(tax["lead_status"])],
        milestone_kinds=[x["v"] for x in _values(tax["milestone_kind"])],
        deadline_kinds=[x["v"] for x in tax["milestone_kind"]["groups"][0]["values"]],
        report_kinds=[x["v"] for x in _values(tax["report_kind"])],
        contract_types=[x["v"] for x in _values(tax["award_contract_type"])],
        deliveries=[x["v"] for x in _values(tax["award_delivery"])],
        assets=[x["v"] for x in _values(tax["award_asset"])],
        measures=[{**m, "words": sorted({str(w).lower() for w in m.get("words") or []}, key=len, reverse=True)} for m in sem.get("measures") or []],
        aggs=[{**a, "words": sorted({str(w).lower() for w in a.get("words") or []}, key=len, reverse=True)} for a in sem.get("aggs") or []],
        terms=[x for x in (make_term(t) for t in o.get("terms") or []) if x],
        not_collected=list((o.get("data_scope") or {}).get("not_collected") or []),
        market_tiers=list(o["glossary"]["threshold"].get("tiers") or []),
    )
    def merge(values: list[str], extra: dict[str, list[str]], more: dict[str, list[str]] | None = None) -> dict[str, list[str]]:
        out = {}
        for val in values:
            ws = {val.lower()} | {w.lower() for w in extra.get(val, [])} | {w.lower() for w in (more or {}).get(val, [])}
            out[val] = sorted(ws, key=len, reverse=True)
        return out
    patterns = {g["v"]: list(g["patterns"]) for g in fund["groups"]}
    v.words = {
        "sectors": merge([s for s in v.sectors if s != "其他"], SECTOR_WORDS),
        "categories": merge([c for c in v.categories if c != "其他"], CATEGORY_WORDS),
        "funders": merge(v.funders, FUNDER_WORDS, patterns),
        "stages": merge(v.stages, {}),
        "statuses": merge(v.statuses, STATUS_WORDS),
        "milestone_kinds": merge([k for k in v.milestone_kinds if k != "其他"], {}),
        "report_kinds": merge(v.report_kinds, {}),
        "contract_types": merge(v.contract_types, CONTRACT_WORDS),
        "deliveries": merge([d for d in v.deliveries if d != "运营"], DELIVERY_WORDS),  # 「运营」只在同一句说了别的承包模式时才按承包模式查,见 _or_delivery
        "assets": merge([a for a in v.assets if a != "其他"], (sem.get("words") or {}).get("award_asset") or {}),
    }
    # 太短、太泛的英文词不要(「plan」「law」会误伤),资金来源里长度 ≤ 2 的拉丁缩写(WB、EU)只在整词时才认,见 _hit
    for dim in v.words.values():
        for val, ws in dim.items():
            dim[val] = [w for w in ws if not (w.isascii() and len(w) <= 3 and w not in ("eoi", "spn", "gpn", "adb", "eib", "afd", "kfw", "idb", "dfc", "pq", "wb", "eu", "ida",
                                                                                       "epc", "dbo", "o&m", "bot", "ppp", "boo", "wtp"))]
    return v


def empty_plan() -> dict:
    return {"intent": "list", "object": "lead",
            "filters": {"countries": [], "regions": [], "sectors": [], "sectors_fine": [], "categories": [], "funders": [], "stages": [],
                        "tracked": None, "status": [], "relevant": None, "days": None, "from": None, "to": None, "future_days": None,
                        "kinds": [], "keywords": [], "terms": [],
                        "contract_types": [], "deliveries": [], "winner_countries": [], "winners": [], "capacity": None,
                        "price": None, "assets": [],
                        "tiers": [], "foreign": [], "metric": None},  # 最后三项只对国别:市场分级、外企参标限制、指标门槛 {id, min, max};此前七项只对已中标项目;capacity = {min, max}(m³/日);price = {min, max, currency, rate}(原币,rate = 1 单位折人民币,近似)
            "group_by": None, "limit": DEFAULT_LIMIT, "understood": [], "unresolved": [], "parsed_by": "rules", "model": None, "note": "",
            "measures": [], "order": None}  # order 只对国别:{id, dir: asc|desc}(「最慢」「最高」);问的指标 [{id, agg}](本体 semantic.measures);空 = 只数数量


# ---------------------------------------------------------------- 规则解析


def _norm(q: str) -> str:
    return re.sub(r"\s+", " ", q.strip()).lower()


def _hit(text: str, word: str) -> bool:
    """中文词直接包含;拉丁词按整词(不区分大小写)。"""
    if not word:
        return False
    if word.isascii():
        return re.search(r"(?<![a-z0-9])" + re.escape(word) + r"(?![a-z0-9])", text) is not None
    return word in text


def _pick(text: str, table: dict[str, list[str]], exclude: set[str] | None = None) -> list[str]:
    out = []
    for val, words in table.items():
        if exclude and val in exclude:
            continue
        if any(_hit(text, w) for w in words):
            out.append(val)
    return out


def _num(s: str) -> float | None:
    s = s.strip()
    if s.isdigit():
        return int(s)
    if s in NUM_ZH:
        return NUM_ZH[s]
    if len(s) == 2 and s[0] == "十" and s[1] in NUM_ZH:
        return 10 + NUM_ZH[s[1]]
    if len(s) == 2 and s[1] == "十" and s[0] in NUM_ZH:
        return NUM_ZH[s[0]] * 10
    if len(s) == 3 and s[1] == "十":
        return NUM_ZH.get(s[0], 0) * 10 + NUM_ZH.get(s[2], 0)
    return None


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def parse_time(text: str, today: date) -> tuple[dict, str | None]:
    """时间短语 → {days | from,to | future_days},附带给人看的说明。先看写明的年份(2024 年、2022–2024 年、2023 年以来),
    再看相对窗口,再看本周 / 上周 / 本月 / 上月 / 今年 / 某月。"""
    y4 = r"((?:19|20)\d\d)"
    if m := re.search(y4 + r"\s*年\s*(\d{1,2})\s*月", text):
        y, mo = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12:
            a = date(y, mo, 1)
            return {"from": a.isoformat(), "to": ((a.replace(month=mo + 1) if mo < 12 else date(y + 1, 1, 1)) - timedelta(days=1)).isoformat()}, f"{y} 年 {mo} 月"
    if m := re.search(y4 + r"\s*年?\s*(?:-|–|—|~|～|至|到)\s*" + y4 + r"\s*年", text):
        a, b = sorted((m.group(1), m.group(2)))
        return {"from": f"{a}-01-01", "to": f"{b}-12-31"}, f"{a}–{b} 年"
    if m := re.search(y4 + r"\s*年?\s*(?:以来|至今|以后|之后|起)", text):
        return {"from": f"{m.group(1)}-01-01", "to": today.isoformat()}, f"{m.group(1)} 年以来"
    if m := re.search(y4 + r"\s*年(?!代)", text):
        return {"from": f"{m.group(1)}-01-01", "to": f"{m.group(1)}-12-31"}, f"{m.group(1)} 年"
    m = re.search(r"(最近|近|过去|这)\s*(\d+|[一二两三四五六七八九十]+|半)\s*(个月|天|日|周|星期|礼拜|月|年)", text)
    if m:
        n = _num(m.group(2))
        if n:
            days = max(1, round(n * UNIT_DAYS[m.group(3)]))
            return {"days": days}, (f"最近 {n:g} 年({days} 天)" if m.group(3) == "年" and n == int(n) else f"最近 {days} 天")
    m = re.search(r"(未来|今后|接下来|之后|后|内)\s*(\d+|[一二两三四五六七八九十]+|半)\s*(个月|天|日|周|星期|礼拜|月|年)", text) \
        or re.search(r"(\d+|[一二两三四五六七八九十]+|半)\s*(个月|天|日|周|星期|礼拜|月|年)\s*(内|之内|以内)", text)
    if m:
        g = m.groups()
        n, unit = (_num(g[1]), g[2]) if len(g) == 3 and g[0] in ("未来", "今后", "接下来", "之后", "后", "内") else (_num(g[0]), g[1])
        if n:
            days = max(1, round(n * UNIT_DAYS[unit]))
            return {"future_days": days}, f"未来 {days} 天"
    if "今天" in text or "今日" in text:
        return {"days": 1}, "今天"
    if "昨天" in text:
        d = today - timedelta(days=1)
        return {"from": d.isoformat(), "to": d.isoformat()}, "昨天"
    if "本周" in text or "这周" in text or "这个星期" in text:
        return {"from": _monday(today).isoformat(), "to": today.isoformat()}, "本周"
    if "上周" in text or "上个星期" in text or "上星期" in text:
        a = _monday(today) - timedelta(days=7)
        return {"from": a.isoformat(), "to": (a + timedelta(days=6)).isoformat()}, "上周"
    if "本月" in text or "这个月" in text:
        return {"from": today.replace(day=1).isoformat(), "to": today.isoformat()}, "本月"
    if "上月" in text or "上个月" in text:
        last = today.replace(day=1) - timedelta(days=1)
        return {"from": last.replace(day=1).isoformat(), "to": last.isoformat()}, "上月"
    if "今年" in text:
        return {"from": today.replace(month=1, day=1).isoformat(), "to": today.isoformat()}, "今年"
    if "去年" in text:
        y = today.year - 1
        return {"from": f"{y}-01-01", "to": f"{y}-12-31"}, "去年"
    m = re.search(r"(?<![\d年])(\d{1,2})\s*月(?:份)?(?!\d)", text)
    if m and 1 <= int(m.group(1)) <= 12:
        mo = int(m.group(1))
        y = today.year if mo <= today.month else today.year - 1  # 没说年份:今年还没到的月份按去年
        a = date(y, mo, 1)
        b = (a.replace(month=mo + 1) if mo < 12 else date(y + 1, 1, 1)) - timedelta(days=1)
        return {"from": a.isoformat(), "to": b.isoformat()}, f"{y} 年 {mo} 月"
    return {}, None


# ---------------------------------------------------------------- 已中标项目


def is_award_q(text: str) -> bool:
    """问的是已中标项目(别人拿到的合同):有「中标 / 授予 / award」,不是说我方的(我们中标的、商机、阶段),也不是线索或公告。"""
    t = _norm(text)
    return bool(AWARD_RE.search(t)) and not OURS_RE.search(t) and not re.search(r"线索|公告", t)


def contract_types(text: str) -> list[str]:
    """提问里的合同类型(按本体的顺序);「工程量」不算「工程」。"""
    v = vocab()
    rest, out = _norm(text).replace("工程量", " "), []
    for val in [c for c in CONTRACT_ORDER if c in v.contract_types] + [c for c in v.contract_types if c not in CONTRACT_ORDER]:
        ws = v.words["contract_types"].get(val, [])
        if any(_hit(rest, w) for w in ws):
            out.append(val)
            for w in ws:
                rest = rest.replace(w, " ")
    return [c for c in v.contract_types if c in out]


def _deliveries(text: str) -> tuple[list[str], str]:
    v = vocab()
    rest, out = _norm(text), []
    for val in [d for d in DELIVERY_ORDER if d in v.words["deliveries"]]:
        ws = v.words["deliveries"][val]
        if any(_hit(rest, w) for w in ws):
            out.append(val)
            for w in ws:
                rest = rest.replace(w, " ")
    return [d for d in v.deliveries if d in out], rest


def deliveries(text: str) -> list[str]:
    """提问里的承包模式(DBO、BOT/PPP、设计施工、施工;按本体的顺序)。「运营」见 _or_delivery。"""
    return _deliveries(text)[0]


def contract_types_besides_delivery(text: str) -> list[str]:
    """划掉承包模式的说法后再认合同类型:「设计建设运营」里的「运营」不算合同类型。"""
    return contract_types(_deliveries(text)[1])


def _or_delivery(f: dict) -> None:
    """说了承包模式(DBO、EPC、PPP……)时,同一句里的「运营」也按承包模式查,两样按「或」:「DBO 或者运营项目」是 DBO 的和运营的都要
    (合同类型、承包模式两项都筛就成了「既是又是」,一份也没有);说了承包模式的「工程」不再另筛(DBO、EPC 本来就是工程,亚行有的记成货物)。"""
    if not f.get("deliveries"):
        return
    if "运营" in f.get("contract_types", []):
        f["deliveries"] = [d for d in vocab().deliveries if d in f["deliveries"] or d == "运营"]
    f["contract_types"] = [c for c in f.get("contract_types", []) if c not in ("运营", "工程")]


def _firm_only(q: str, words: list[str]) -> bool:
    """这些词(国名、「国内」)在提问里每一处后面都跟着「企业 / 公司」:说的是中标单位的国别,不是项目所在国。"""
    t = _norm(q)
    ends = [m.end() for w in words if w for m in re.finditer(re.escape(w.lower()), t)]
    return bool(ends) and all(FIRM_RE.match(t, e) for e in ends)


def _names(iso: str) -> list[str]:
    c = geo.countries().get(iso) or {}
    return [n for n in [c.get("name_zh"), c.get("name_en"), *(c.get("aliases") or [])] if n]


def _country_word(w: str) -> str | None:
    """「中国」「China」「中国企业」这类只是国名(加企业、公司)的词 → ISO3;「中国电建」这样的单位名 → None。"""
    iso = geo.iso3(w)
    if not iso:
        return None
    rest = _norm(w)
    for n in sorted(_names(iso), key=len, reverse=True):
        rest = rest.replace(n.lower(), "")
    return iso if not rest.strip() or rest.strip() == iso.lower() or re.fullmatch(FIRM_RE.pattern + r"\s*", rest) else None


def winner_countries(q: str) -> tuple[list[str], set[str]]:
    """提问里的中标单位国别 → (中文国名, 只当中标单位国别讲的 ISO3)。「中国企业 / 中资 / 我国企业」→ 中国;已中标项目不收中国国内的合同,
    提问里的中国除非写明「中国国内 / 在中国」,都当中标单位的国别。"""
    t = _norm(q)
    out, only = [], set()
    for iso in geo.detect(q):
        firm = _firm_only(q, _names(iso)) or (iso == "CHN" and not re.search(r"中国(?:国内|境内|大陆)|在中国", t))
        if firm:
            out.append(geo.name_zh(iso))
            only.add(iso)
    if any(w in t for w in CN_FIRM_WORDS) or any(_firm_only(q, [w]) for x in vocab().not_collected if x["iso3"] == "CHN" for w in x.get("words") or []):
        if "中国" not in out:
            out.insert(0, "中国")
    return out, only


def _cap_num(s: str) -> float | None:
    """「5,000」「1.5」「10.000.000」「五千」→ 数。"""
    if re.fullmatch(r"\d{1,3}(?:,\d{3})+", s):
        s = s.replace(",", "")
    elif re.fullmatch(r"\d{1,3}(?:\.\d{3}){2,}", s):
        s = s.replace(".", "")
    try:
        return float(s.replace(",", "."))
    except ValueError:
        pass
    total, cur = 0, 0
    for ch in s:
        if ch in "十百千":
            total, cur = total + (cur or 1) * {"十": 10, "百": 100, "千": 1000}[ch], 0
        elif ch in NUM_ZH:
            cur = NUM_ZH[ch]
        else:
            return None
    return float(total + cur) or None


def capacity_hits(text: str) -> list[tuple[int, int, float, float]]:
    """一段文字里写明的规模 → [(数字起点, 终点, m³/日, 单位倍数(不含「万」这类数量级))],按位置排。"""
    t = str(text or "").lower()
    out: dict[int, tuple[int, int, float, float]] = {}
    pats = [(CAP_NUM + "(?:" + u + ")", k) for u, k in CAP_UNITS] + [(CAP_DAILY + CAP_NUM + r"(?:吨|立方米?|方|m3|m³)(?!\s*(?:/|每|per))", 1)]
    for pat, k in pats:
        for m in re.finditer(pat, t):
            v = _cap_num(m.group(1))
            if v:
                out.setdefault(m.start(1), (m.start(1), m.end(), v * CAP_MULT.get(m.group(2) or "", 1) * k, k))
    return sorted(out.values())


def parse_capacity(text: str) -> dict | None:
    """提问里的规模条件 → {"min", "max"}(m³/日,没有的为 None):「以上 / 超过 / 不低于」是下限,「以下 / 不超过」是上限,
    「3–5 万吨/日」是区间,只写一个数按下限。没写带单位的规模 → None。"""
    t = _norm(text)
    hits = capacity_hits(t)
    if not hits:
        return None
    a, b, v, k = hits[0]
    before, after = t[max(0, a - 10):a], t[b:b + 4]
    m = re.search(r"(\d+(?:[.,]\d+)*|[一二两三四五六七八九十百千]+)\s*(万|千|k)?\s*(?:-|–|—|~|～|至|到)\s*$", before)
    if m and _cap_num(m.group(1)):  # 区间:前一个数没写「万」的跟后一个数走(3–5 万吨/日)
        lo = _cap_num(m.group(1)) * k * (CAP_MULT[m.group(2)] if m.group(2) else v / k / _cap_num(re.match(CAP_NUM, t[a:]).group(1)))
        return {"min": round(min(lo, v), 2), "max": round(max(lo, v), 2)}
    if re.match(r"\s*(?:及|或)?(?:以下|以内|之内)", after) or re.search(r"(?:(?<!不)(?:小于|低于|少于)|不超过|不到|不足|最多|≤|<=|<)\s*$", before):
        return {"min": None, "max": round(v, 2)}
    return {"min": round(v, 2), "max": None}  # 「以上 / 超过 / 不低于」与只写一个数的,都按下限


def cap_label(c: dict | None) -> str:
    """{min, max} → 「≥ 5000 m³/日」「3 万–5 万 m³/日」。"""
    if not c:
        return ""
    n = lambda x: f"{round(x / 1e4, 2):g} 万" if x >= 1e4 else f"{round(x, 1):g}"  # noqa: E731
    if c.get("min") and c.get("max"):
        return f"{n(c['min'])}–{n(c['max'])} m³/日"
    return (f"≥ {n(c['min'])}" if c.get("min") else f"≤ {n(c['max'])}") + " m³/日"


# 金额(中标价):「中标金额大于 5000 万迪拉姆」「1 亿元以上」「100 万美元以下」「1000–5000 万元」→ {min, max, currency, rate}(原币)。
# 要写了币种,或数前面有「金额 / 中标价 / 合同额」之类的字样,免得把规模、年份当金额;没写币种的按人民币。页面按原币比同币种的,
# 别的币种按 rate(当天汇率,见 _rate)折人民币比折合人民币(万元)
PRICE_CUR = [("USD", r"美元|美金|usd|us\$|\$"), ("EUR", r"欧元|eur|€"), ("MAD", r"迪拉姆|dirhams?|mad(?![a-z])|dh(?![a-z])"), ("KGS", r"索姆|kgs"),
             ("AZN", r"马纳特|manats?|azn"), ("KZT", r"坚戈|tenge|kzt"), ("UZS", r"苏姆|uzs"), ("EGP", r"埃及镑|egp"), ("INR", r"卢比|inr"),
             ("CNY", r"人民币|rmb|cny|元")]
PRICE_MULT = {"千万": 1e7, "百万": 1e6, "万": 1e4, "亿": 1e8, "千": 1e3, "million": 1e6, "mn": 1e6, "m": 1e6, "billion": 1e9, "bn": 1e9, "k": 1e3}
_PRICE_NUM = r"(\d+(?:[.,]\d+)*)\s*(千万|百万|万|亿|千|million|mn|billion|bn|m(?![a-z³3²])|k(?![a-z]))?\s*"
_PRICE_RE = re.compile(_PRICE_NUM + r"(" + "|".join(p for _, p in PRICE_CUR) + r")?")
_PRICE_WORD = r"(?:金额|中标价|中标额|合同额|合同金额|合同价|造价|标价|报价|价格|价值|价)"


RATES_FILE: Path | None = None  # 后台存的当天汇率(core.fx 的缓存 fx.json);助手服务启动时设,没有就用 core.fx 的近似表
_rates: dict = {}


def _rate(cur: str) -> float:
    """1 单位外币折多少人民币:读后台的汇率缓存(不联网,文件变了才重读),读不到用近似表。"""
    if cur == "CNY":
        return 1.0
    try:
        if RATES_FILE is not None and RATES_FILE.exists():
            mt = RATES_FILE.stat().st_mtime
            if _rates.get("_mt") != mt:
                _rates.clear()
                _rates.update(json.loads(RATES_FILE.read_text(encoding="utf-8")).get("rates") or {})
                _rates["_mt"] = mt
            if _rates.get(cur):
                return float(_rates[cur])
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return fx.STATIC.get(cur, 1.0)


def parse_price(text: str) -> dict | None:
    """提问里的金额条件(见上);没有 → None。「以上 / 超过 / 大于」是下限,「以下 / 不到 / 小于」是上限,「A 到 B」是区间,只写一个数按下限。"""
    t = _norm(text)
    for m in _PRICE_RE.finditer(t):
        cur_txt, a, b = m.group(3), m.start(), m.end()
        before = t[max(0, a - 12):a]
        if not cur_txt and not re.search(_PRICE_WORD + r"[^\d]{0,8}$", before):
            continue
        if not cur_txt and re.match(r"\s*(?:年|天|月|个|份|家|座|条|km|公里|米|吨|m3|m³|立方|方|升|l/s|mld)", t[b:]):
            continue
        if re.match(r"\s*(?:-|–|—|~|～|至|到)\s*\d", t[b:]):  # 区间的前一个数:由后一个数一起读
            continue
        num = lambda x, u: float(x.replace(",", "")) * PRICE_MULT.get(u or "", 1)  # noqa: E731
        v = num(m.group(1), m.group(2))
        cur = next((c for c, p in PRICE_CUR if cur_txt and re.fullmatch(p, cur_txt)), "CNY")
        rate = _rate(cur)
        out = {"min": None, "max": None, "currency": cur, "rate": rate}
        lo = re.search(r"(\d+(?:[.,]\d+)*)\s*(千万|百万|万|亿|千)?\s*(?:-|–|—|~|～|至|到)\s*$", before)
        after = t[b:b + 6]
        if lo:  # 区间:前一个数没写「万」的跟后一个数走(1000–5000 万元)
            out["min"], out["max"] = sorted([num(lo.group(1), lo.group(2) or m.group(2)), v])
        elif re.match(r"\s*(?:及|或)?(?:以下|以内|之内)", after) or re.search(r"(?:(?<!不)(?:小于|低于|少于)|不超过|不到|不足|最多|≤|<=|<)\s*" + _PRICE_WORD + r"?\s*$", before):
            out["max"] = v
        else:
            out["min"] = v
        return out
    return None


def price_label(p: dict | None) -> str:
    """{min, max, currency, rate} → 「≥ 5,000 万迪拉姆(约 3,700 万元)」。"""
    if not p:
        return ""
    zh = {"CNY": "元", "USD": "美元", "EUR": "欧元", "MAD": "迪拉姆", "KGS": "索姆", "AZN": "马纳特", "KZT": "坚戈", "UZS": "苏姆", "EGP": "埃及镑", "INR": "卢比"}
    def n(x: float, cur: str) -> str:
        s = f"{x / 1e8:,.4g} 亿" if x >= 1e8 else f"{x / 1e4:,.4g} 万" if x >= 1e4 else f"{x:,.4g} "
        return s + zh.get(cur, cur)
    def both(x: float) -> str:
        return n(x, p["currency"]) + ("" if p["currency"] == "CNY" else f"(约 {n(x * p['rate'], 'CNY')})")
    if p.get("min") and p.get("max"):
        return f"{both(p['min'])} 到 {both(p['max'])}"
    return ("≥ " + both(p["min"])) if p.get("min") else ("≤ " + both(p["max"]))


# ---------------------------------------------------------------- 语义层(本体 semantic 节)


def _strike(text: str, table: dict[str, list[str]]) -> list[tuple[int, str]]:
    """词表里的词在 text 里出现在哪:长词先认,认到的划掉(「污水处理厂」不再算「水处理厂」,「平均中标价」不再算「中标价」)→ [(位置, 值)],按位置排。"""
    hits, t = [], text
    for w, val in sorted(((w, val) for val, ws in table.items() for w in ws if w), key=lambda x: -len(x[0])):
        while True:
            m = re.search((r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])") if w.isascii() else re.escape(w), t)
            if not m:
                break
            hits.append((m.start(), val))
            t = t[:m.start()] + " " * len(w) + t[m.end():]
    return sorted(hits)


def measure_of(mid: str | None) -> dict | None:
    return next((m for m in vocab().measures if m["id"] == mid), None)


def agg_of(aid: str | None) -> dict | None:
    return next((a for a in vocab().aggs if a["id"] == aid), None)


def parse_measures(text: str) -> list[dict]:
    """提问里问的指标与算法(本体 semantic)→ [{id, agg}],按出现的先后,最多 3 个;算法只取这个指标能用的,没说的用它的缺省(aggs 的第一个)。没问指标 → []。"""
    v, t = vocab(), _norm(text)
    ids = list(dict.fromkeys(val for _, val in _strike(t, {m["id"]: m["words"] for m in v.measures if m["words"]})))[:3]
    said = [val for _, val in _strike(t, {a["id"]: a["words"] for a in v.aggs if a["words"]})]
    out = []
    for mid in ids:
        m = measure_of(mid)
        out.append({"id": mid, "agg": next((a for a in said if a in m["aggs"]), m["aggs"][0])})
    return out


def measure_label(ms: list[dict]) -> str:
    """[{id, agg}] → 「评标天数(平均)、投标家数(中位数)」。"""
    return "、".join(f"{measure_of(x['id'])['name']}({agg_of(x['agg'])['name']})" for x in ms if measure_of(x["id"]) and agg_of(x["agg"]))


def assets(text: str) -> list[str]:
    """提问里的主体工程(污水厂、净水厂、海水淡化厂、管网……;本体 semantic.words.award_asset),按本体的顺序。"""
    v = vocab()
    got = {val for _, val in _strike(_norm(text), v.words["assets"])}
    return [a for a in v.assets if a in got]


def _sector_text(text: str) -> str:
    for a, b in SECTOR_FIX:
        text = text.replace(a, b)
    return text


def _measure_object(ms: list[dict], q: str, current: str) -> str:
    """问了指标(评标天数、招标估价……)而对象不对的:提问没明说查别的,就改查指标所属的对象(语义层 measures.of 的第一个)。"""
    for x in ms:
        m = measure_of(x["id"])
        if m and x["id"] != "count" and current not in m["of"] and not OTHER_OBJECT_RE.search(_norm(q)) and not (m["of"][0] == "country" and NEWS_RE.search(_norm(q))):
            return m["of"][0]
    return current


def _award_noise(k: str) -> bool:
    """模型放进关键词的、其实是已中标项目别的条件的词(合同类型、规模、类别、主体工程、指标、「中标信息」这类):不当名称关键词。"""
    v, t = vocab(), _norm(k)
    return bool(contract_types(t) or deliveries(t) or capacity_hits(t) or parse_price(t) or _pick(t, v.words["sectors"]) or AWARD_RE.search(t) or "规模" in t
                or assets(t) or parse_measures(t) or re.fullmatch(r"的?(?:项目|信息|记录|结果|情况|合同)", t))


# ---------------------------------------------------------------- 国别(营商环境,世界银行 B-READY;v2.23)
# 2026-10-14 使用方要的问法:「供水得分低于 50 的市场国别」「哪些国家不限制外企参标」「摩洛哥商业水价多少」「哪些国家政府回款最慢」

COUNTRY_RE = re.compile(r"营商环境|b-?ready|business ready|外企参标|外企.{0,4}(?:限制|设限|受限)|(?:限制|限)外企|外国企业.{0,4}限制|限制外国企业|对外企")
NEWS_RE = re.compile(r"改革|调整|上涨|上调|涨价|下调|降价|新闻|动态|消息|项目|公告|政策|计划|规划|报道")  # 「埃及水价改革」「水价调整」说的是线索,不是 B-READY 的数
TIER_WORDS = [("重点", ["重点国别", "重点国家", "重点市场"]), ("市场", ["市场国别", "市场国家", "市场级国别", "「市场」"])]
FOREIGN_NONE = ["不限制外企", "不限外企", "外企不受限", "外企不受限制", "对外企没有限制", "对外企无限制", "没有外企限制", "外企无限制", "外企没有限制", "不限制外国企业",
                "外企可以投标", "外企能投标", "外企能参标", "外企可以参标", "允许外企", "对外企开放"]
FOREIGN_ALL = ["全部招标都限制外企", "所有招标都限制外企", "全部限制外企", "全部招标有限制", "全都限制外企"]
FOREIGN_ANY = ["限制外企", "外企受限", "外企参标限制", "对外企设限", "外企有限制", "限制外国企业", "外企不能投标", "外企.{0,4}限制", "外企设限"]
ORDER_HIGH = r"最高|最大|最多|最长|最贵|最慢|最久|最晚"  # 数越大排越前
ORDER_LOW = r"最低|最小|最少|最短|最便宜|最快|最早"
ORDER_BEST = r"最好|最强|最优|最佳|最容易|最方便"  # 按指标的 better 读
ORDER_WORST = r"最差|最弱|最糟|最难|最不方便"
_TH_NUM = r"(\d+(?:\.\d+)?)\s*(?:分|美元|%|％|天|个百分点)?"
TH_MAX = [r"(?<!不)(?:低于|小于|少于|不到|不足|未达|<|＜)\s*" + _TH_NUM, _TH_NUM + r"\s*(?:以下|以内|之内)"]
TH_MIN = [r"(?:不低于|不少于|至少|起码|>=|≥)\s*" + _TH_NUM, r"(?:超过|高于|大于|多于|>|＞)\s*" + _TH_NUM, _TH_NUM + r"\s*以上"]


def _country_signal(text: str, ms: list[dict], current: str) -> bool:
    """问的是国别的营商环境:提到营商环境、外企参标,或问了只有国别才有的指标(供水得分、商业水价、回款天数……)。"""
    return bool(COUNTRY_RE.search(text)) or _measure_object(ms, text, current) == "country"


def _thresholds(text: str) -> list[tuple[int, str, float]]:
    out = []
    for kind, pats in (("max", TH_MAX), ("min", TH_MIN)):
        for pat in pats:
            for m in re.finditer(pat, text):
                if not any(a <= m.start() < b for a, b, *_ in out):
                    out.append((m.start(), m.end(), kind, float(m.group(1))))
    return [(a, k, v) for a, _, k, v in sorted(out)]


def country_filters(text: str, ms: list[dict]) -> tuple[dict, dict | None]:
    """国别的条件:市场分级、外企参标限制、指标门槛(「低于 50」「90 天以上」;按离它最近的、在它前面的指标)→ (条件, 排序)。"""
    v = vocab()
    f = {"tiers": [], "foreign": [], "metric": None}
    for tier, ws in TIER_WORDS:
        if any(w in text for w in ws):
            f["tiers"] = ["重点"] if tier == "重点" else list(v.market_tiers)
            break
    if any(w in text for w in FOREIGN_NONE):
        f["foreign"] = ["无限制"]
    elif any(w in text for w in FOREIGN_ALL):
        f["foreign"] = ["全部招标有限制"]
    elif any(re.search(w, text) for w in FOREIGN_ANY):
        f["foreign"] = ["部分招标有限制", "全部招标有限制"]
    pos = _strike(text, {m["id"]: m["words"] for m in v.measures if "country" in m["of"] and m["words"]})
    for at, kind, num in _thresholds(text):
        before = [mid for p, mid in pos if p < at]
        mid = before[-1] if before else (ms[0]["id"] if ms else None)
        if not mid:
            continue
        cur = f["metric"] if f["metric"] and f["metric"]["id"] == mid else {"id": mid, "min": None, "max": None}
        cur[kind] = num
        f["metric"] = cur
    order = None
    mid = (ms[0]["id"] if ms else None) or (f["metric"] or {}).get("id")
    m = measure_of(mid) if mid else None
    if m:
        hi = "desc" if m.get("better") != "low" else "asc"
        lo = "asc" if hi == "desc" else "desc"
        if re.search(ORDER_HIGH, text):
            order = {"id": mid, "dir": "desc"}
        elif re.search(ORDER_LOW, text):
            order = {"id": mid, "dir": "asc"}
        elif re.search(ORDER_BEST, text):
            order = {"id": mid, "dir": hi}
        elif re.search(ORDER_WORST, text):
            order = {"id": mid, "dir": lo}
    return f, order


def country_plan(p: dict, text: str, ms: list[dict]) -> dict:
    """对象是国别时的计划:国名、市场分级、外企参标、门槛、排序;指标只留国别有的,没问指标的给缺省(问外企参标的看公共采购得分,其余看总分)。"""
    f = p["filters"]
    p["object"] = "country"
    for k in ("sectors", "sectors_fine", "categories", "funders", "stages", "status", "kinds", "keywords", "terms", "contract_types", "deliveries",
              "winner_countries", "winners", "assets"):
        f[k] = []
    f["tracked"] = f["relevant"] = f["capacity"] = f["price"] = None
    f["days"] = f["from"] = f["to"] = f["future_days"] = None  # B-READY 一期一个数,不按时间筛
    p["understood"] = [x for x in p["understood"] if x[0] != "时间"]
    p["group_by"] = None
    p["measures"] = [x for x in ms if "country" in measure_of(x["id"])["of"]]
    cf, p["order"] = country_filters(text, p["measures"])
    f.update(cf)
    if not p["measures"]:
        mid = (f["metric"] or {}).get("id") or ("bready_procure" if f["foreign"] else "bready_total")
        p["measures"] = [{"id": mid, "agg": measure_of(mid)["aggs"][0]}]
        p["measure_default"] = True  # 没问指标、给的缺省:接着问时上一问的指标优先
        if not p["order"]:
            p["order"] = country_filters(text, p["measures"])[1]
    if f["countries"]:  # 点了国名的问的是这几国本身(「阿塞拜疆外企参标有限制吗」):不再按外企参标、门槛、分级筛掉它们,页面逐国作答
        f["tiers"], f["foreign"], f["metric"], p["order"] = [], [], None, None
    p["intent"] = "count" if re.search(r"几个|多少个|多少国|几国|有多少", text) and not f["countries"] else "list"
    m = re.search(r"(?:前|最\S{1,3}的)\s*(\d+|[一二两三四五六七八九十]+)\s*(?:个|国|名)", text)
    if m and _num(m.group(1)):
        p["limit"] = max(1, min(MAX_LIMIT, int(_num(m.group(1)))))
    elif not f["countries"]:
        p["limit"] = MAX_LIMIT  # 国别不多,缺省全列
    return p


def rules_parse(q: str, today: date, force_object: str | None = None) -> dict:
    """force_object:接着上一问、这一句没说查什么时,沿用上一问的对象(见 plan)。"""
    v = vocab()
    text = _norm(q)
    p = empty_plan()
    f = p["filters"]
    ms = parse_measures(text)
    # 对象:已中标项目先认(「中标信息」不是商机阶段;「我们中标的商机」仍是商机,见 is_award_q);问了只有已中标项目才有的指标(评标天数、招标估价)的也是它
    if force_object == "country" or (COUNTRY_RE.search(text) and not re.search(r"线索|商机", text)) or _measure_object(ms, q, force_object if force_object in OBJECTS else "lead") == "country":
        p["object"] = "country"
    elif force_object in OBJECTS:
        p["object"] = force_object
    elif is_award_q(text) or _measure_object(ms, q, "lead") == "award":
        p["object"] = "award"
    elif re.search(r"商机|项目机会|在跟|跟踪|投标的|中标的", text) and "线索" not in text:
        p["object"] = "opportunity"
    elif re.search(r"节点|开标|截止日|截止|到期|时间表|日程", text) and "线索" not in text and "商机" not in text:
        p["object"] = "milestone"
    elif re.search(r"周报|月报|日报|国别报告|项目报告|报告", text) and not re.search(r"生成|出一份|做一份|写一份|来一份|给我一份", text):
        p["object"] = "report"
    if p["object"] == "country":  # 国别(营商环境):国名、市场分级、外企参标、门槛,不走下面线索与已中标项目的条件
        for iso in geo.detect(q):
            f["countries"].append({"iso3": iso, "name": geo.name_zh(iso)})
        for x in v.not_collected:
            if not f["countries"] and not re.search(r"国内外|国外", text) and any(w in text for w in x.get("words") or []):
                f["countries"].append({"iso3": x["iso3"], "name": geo.name_zh(x["iso3"])})
        return finish(country_plan(p, text, ms))
    # 意图
    if re.search(r"生成|出一份|做一份|写一份|来一份|给我一份", text) and "报告" in text:
        p["intent"] = "report"
        p["object"] = "report"
    elif re.search(r"多少|几条|几个|几份|数量|总数|计数|有没有|共有", text):
        p["intent"] = "count"
    elif re.search(r"按|分布|统计|占比|趋势|走势|分组|排名|排行|最多|图|哪些国家|哪个国家|各国", text):
        p["intent"] = "chart"
    # 分组维度
    gb = next((g for pat, g in AWARD_GROUPS if re.search(pat, text)), None) if p["object"] == "award" else None
    if gb:
        pass  # 已中标项目自己的说法(按中标单位国别、按合同类型、按年)
    elif re.search(r"按(国家|国别|国)|分国|各国|哪些国家|哪个国家", text):
        gb = "country"
    elif re.search(r"按(类别|类型|种类)|分类别|各类别", text):
        gb = "kind" if p["object"] in ("milestone", "report") else "category"
    elif re.search(r"按领域|分领域|各领域", text):
        gb = "sectors"
    elif re.search(r"按(资金|出资|出资方|资金来源|机构)|分资金|各资金|各出资方", text):
        gb = "funder"
    elif re.search(r"按(天|日)|每天|每日|逐日|趋势|走势", text):
        gb = "day"
    elif re.search(r"按阶段|分阶段|各阶段", text):
        gb = "stage"
    elif re.search(r"按(处理)?状态", text):
        gb = "status"
    elif re.search(r"按信源|各信源|按来源", text):
        gb = "source"
    gb = GROUP_ALIAS.get(p["object"], {}).get(gb, gb)
    if gb and gb in GROUPS[p["object"]]:
        p["group_by"] = gb
        p["intent"] = "chart"
    elif p["intent"] == "chart" and not gb:
        p["group_by"] = "country" if "country" in GROUPS[p["object"]] else GROUPS[p["object"]][0]
    # 国别(几个都认;国名也可能是「中标」之类词的一部分,geo 只认国名词表)。已中标项目:「中国企业」「土耳其公司」说的是中标单位的国别
    award = p["object"] == "award"
    win, win_iso = winner_countries(q) if award else ([], set())
    for iso in geo.detect(q):
        if iso not in win_iso:
            f["countries"].append({"iso3": iso, "name": geo.name_zh(iso)})
    for x in v.not_collected:  # 「国内」「我国」这类口语指本体 data_scope 里写的国家(「国内企业中标」除外)
        ws = [w for w in x.get("words") or [] if w in text]
        if not f["countries"] and not re.search(r"国内外|国外", text) and ws and not (award and _firm_only(q, ws)):
            f["countries"].append({"iso3": x["iso3"], "name": geo.name_zh(x["iso3"])})
    # 领域 / 类别 / 资金来源 / 阶段 / 状态 / 节点与报告类型
    f["sectors"] = _pick(_sector_text(text), v.words["sectors"])
    if p["object"] == "lead" or p["object"] == "milestone":
        excl = set()
        if p["object"] == "milestone":
            excl = set(v.categories)  # 节点的「投标截止」不算类别
        f["categories"] = _pick(text, v.words["categories"], excl)
    f["funders"] = _pick(text, v.words["funders"])
    if "政府" in f["funders"] or "本国政府" in f["funders"]:
        if not re.search(r"政府出资|政府预算|本国政府|政府资金", text):
            f["funders"] = [x for x in f["funders"] if x != "本国政府"]
    if p["object"] == "opportunity":
        f["stages"] = _pick(text, v.words["stages"])
        if any(w in text for w in TRACKED_WORDS) or (re.search(r"跟踪|在跟", text) and not f["stages"]):
            f["tracked"] = True
        if any(w in text for w in SUGGESTED_WORDS) and "建议" not in f["stages"]:
            f["stages"].append("建议")
        f["sectors_fine"] = [x for s in f["sectors"] for x in v.sectors_fine.get(s, [s])]
    if p["object"] == "lead":
        f["status"] = _pick(text, v.words["statuses"])
        if re.search(r"不相关|无关|不算相关", text):
            f["relevant"] = False
        elif re.search(r"相关", text):
            f["relevant"] = True
        if re.search(r"新线索|新增", text) and not re.search(r"最近|近|过去|本周|上周|本月|上月|今天|昨天|今年", text):
            f["days"] = 7
            p["understood"].append(["时间", "最近 7 天(「新」按一周算)"])
    if p["object"] == "milestone":
        f["kinds"] = _pick(text, v.words["milestone_kinds"])
        if not f["kinds"] and any(_hit(text, w) for w in DEADLINE_WORDS):
            f["kinds"] = list(v.deadline_kinds)
    if p["object"] == "report":
        f["kinds"] = _pick(text, v.words["report_kinds"])
    if award:  # 合同类型、中标单位(「中标单位是 X」)、规模(页面读工程量与名称再筛)
        f["contract_types"] = contract_types_besides_delivery(text)
        f["winner_countries"] = win
        f["winners"] = [w for w in re.findall(r"中标(?:单位|方|企业|公司|人)\s*(?:是|为|含|包括|有|:|：)\s*[「“\"']?([^\s,，。;；、的」”\"'?？]{2,40})", q) if not _country_word(w)]
        f["capacity"] = parse_capacity(text)
        f["deliveries"] = deliveries(text)
        f["price"] = parse_price(text)
        f["assets"] = assets(text)
        _or_delivery(f)
    # 指标(语义层):只留这个对象有的;别的对象的指标说法放进「没认出来」
    p["measures"] = [x for x in ms if p["object"] in measure_of(x["id"])["of"]]
    p["unresolved"] += [measure_of(x["id"])["name"] for x in ms if p["object"] not in measure_of(x["id"])["of"] and "country" not in measure_of(x["id"])["of"]]  # 「水价调整的线索」里的「水价」不算没认出来
    # 时间
    t, label = parse_time(text, today)
    if t and award and t.get("future_days"):  # 「三年内」对已中标项目说的是过去
        t, label = {"days": t["future_days"]}, label.replace("未来", "最近")
    if t:
        f.update(t)
        p["understood"].append(["时间", label])
    if p["object"] == "milestone" and not (f["days"] or f["from"] or f["future_days"]):
        f["future_days"] = DEFAULT_FUTURE_DAYS
        p["understood"].append(["时间", f"未来 {DEFAULT_FUTURE_DAYS} 天(缺省)"])
    # 关键词:引号里的原样当标题关键词
    f["keywords"] = [k.strip() for k in re.findall(r"[\"“「『]([^\"”」』]{2,40})[\"”」』]", q) if k.strip()]
    # 条数
    m = re.search(r"(?:前|最新|最近的)\s*(\d+)\s*(?:条|个|份)", text) or re.search(r"(\d+)\s*(?:条|个|份)(?!天|月)", text)
    explicit = False
    if m and p["intent"] in ("list", "count"):
        p["limit"] = max(1, min(MAX_LIMIT, int(m.group(1))))
        explicit = True
        if p["intent"] == "count" and re.search(r"前\s*\d+", text):
            p["intent"] = "list"
    if p["object"] == "report" and p["intent"] == "list" and not explicit:
        p["limit"] = min(p["limit"], 20)  # 报告不多,清单缺省 20 份
    if p["intent"] == "count" and p["group_by"]:
        p["intent"] = "chart"
    apply_terms(p, q)
    if not any([f["countries"], f["sectors"], f["categories"], f["funders"], f["stages"], f["tracked"], f["status"], f["relevant"] is not None,
                f["days"], f["from"], f["future_days"], f["kinds"], f["keywords"], f["terms"], p["group_by"], award]) \
            and not re.search(r"线索|商机|节点|报告|全部|所有|都|清单|列表|多少", text):
        p["intent"] = "help"
    return finish(p)


# ---------------------------------------------------------------- 模型解析


SYSTEM = """你是「全球水务开源情报」系统首页的提问解析器。用户用中文(偶尔夹英文)提问,你只负责把问题翻译成一个查询计划(JSON),不回答问题、不编造数据。

系统里有七类对象:
- lead 线索:从公开信源采集的公告与新闻,字段有国别、领域(粗分)、类别、出资方、发现时间、处理状态、是否相关。
- opportunity 商机:我方在跟的项目,字段有国别、领域(细分)、我方阶段、资金。「我们 / 我方中标的」「中标的商机」= 商机 + 阶段「中标」。
- milestone 时间节点:投标截止、开标、授标等日期,挂在商机或线索下。
- report 报告:国别报告、项目报告、周报、月报、日报。
- award 已中标项目:开发银行与各国采购网公开的中标记录(一份已授出的合同:谁中了什么标、中标价多少),字段有国别、类别(即领域粗分)、合同类型、
  中标价、中标时间、工程量(含规模,如「净水厂 5 万吨/日」)、中标单位及其国别、资金来源。问「中标信息 / 中标记录 / 中标结果 / 谁中标 / 中标单位 /
  中标价 / 合同授予 / awards」的都是它,不是商机(除非明说是我方的)。
- country 国别:一国一行的营商环境(世界银行 B-READY 2025):营商环境总分、供水得分、公共采购得分、商业水价、外企参标限制、大型工程授标天数、
  政府合同回款天数等。问「营商环境 / 外企参标 / 哪些国家水价最高 / 回款最慢」的是它;门槛(低于 50 分)、外企参标、市场分级、排序系统按提问自己读,你只填 object、countries、measure。
- law 法规:收了原文的各国水务相关法规(水法、供排水与污水、排放标准、PPP 与特许经营、公共采购、环评)的条文、排放限值、本国对某个概念的定义。
  问「法律 / 法规怎么规定」「特许经营最长几年」「污水排放标准」「某国法律里污水怎么定义」的是它;你只填 object、countries、keywords(要在条文里找的中文词)。

字段取值(只能用这些,别的词放进 unresolved):
- sectors(领域,粗分):{sectors}
- categories(线索类别):{categories}
- funders(资金来源归组):{funders}
- stages(商机我方阶段):{stages};「在跟踪 / 在跟的商机」= tracked: true(阶段 ∈ {tracked});「AI 推荐」= 阶段「建议」
- status(线索处理状态):{statuses}
- kinds:时间节点类型 {milestone_kinds};报告类型 {report_kinds}
- countries:项目所在国,国名用中文或 ISO3 三字母代码,可多个
- 已中标项目另有:contract_types(合同类型){contract_types}(运营维护、运维、委托运营、O&M → 运营;施工、土建 → 工程;设备、供货 → 货物;
  咨询、设计、监理 → 咨询服务);deliveries(承包模式){deliveries}(DBO、DBOT、设计建造运营、建设加运营 → DBO;BOT、PPP、特许经营、混合年金 → BOT/PPP;
  EPC、设计施工、交钥匙 → 设计施工;说了承包模式又说了「运营」的,如「DBO 或运营项目」,运营也放进 deliveries,两样按「或」查);winner_countries(中标单位国别,中文国名;「中国企业中标 / 中资」= ["中国"],这时中国不进 countries);
  assets(主体工程){assets}(污水厂、污水处理站、STEP → 污水处理厂;净水厂、自来水厂 → 供水厂;淡化厂 → 海水淡化厂;管道、管线 → 管网);
  winners(中标单位名称里要含的词);capacity_min / capacity_max(规模即日处理或供水能力的下限 / 上限,一律折成 m³/日:1 万吨/日 = 10000,1 MLD = 1000,
  1 L/s = 86.4;「5000m3/d 以上」= capacity_min 5000);sectors 就是它的「类别」;时间按中标时间。合同类型、承包模式、规模、金额不要放进 keywords
  (金额条件系统按提问自己读)。
- 指标(measure,语义层;只能取这些 id,只问数量、清单的给 count):{measures}。问这些指标的一律是括号里写的对象(除非明说查线索、商机)。
- 算法(agg):{aggs};没说的给 null(系统用指标的缺省)。「平均评标时间」= measure eval_days、agg mean;「中标金额与招标金额什么比例」= price_ratio。
- 给了「上一个问题」时:这一个没说查什么的,对象沿用上一个;以「那」「其中」开头或「……呢」这样的短句,条件沿用上一个,只换这一句说到的(系统会补,你只按这一句填)。
- 时间:相对窗口用 days(最近 N 天,按发现时间 / 日期;「近三年」= 1095)或 future_days(未来 N 天,只对时间节点有意义);绝对区间用 from / to(YYYY-MM-DD)。今天是 {today}。
- keywords:用户明确要在标题(已中标项目是名称)里找的词(机构名、项目名、地名),不要把领域 / 类别词重复放进来。
- 行业术语(跨领域的词,按这里理解;提问里出现时不要据此填 sectors,也不要放进 keywords,系统按术语自己的相关词去找):{terms}
- intent:count 问数量;list 要清单(缺省);chart 要分组统计 / 分布 / 趋势 / 哪些国家最多;report 要生成一份新报告;help 看不懂或与系统无关。
- group_by:chart 时的分组维度,线索可选 country / category / sectors / funder / day / status / source,商机 country / stage / sectors / funder,时间节点 country / kind / day,报告 kind / country,
  已中标项目 country / sector / contract_type / delivery / asset / winner_country / funder / year。
- limit:要几条(1–200),没说就 50。

{guide}"""

GUIDE = """输出格式:只输出一个 JSON 对象,不要任何其他文字。字段与示例:
{"object": "lead", "intent": "chart", "countries": ["埃及"], "sectors": ["供水"], "categories": ["招标"], "funders": ["世界银行"],
 "stages": [], "tracked": null, "status": [], "relevant": null, "days": 30, "from": null, "to": null, "future_days": null,
 "kinds": [], "keywords": [], "contract_types": [], "deliveries": [], "assets": [], "winner_countries": [], "winners": [], "capacity_min": null, "capacity_max": null,
 "measure": "count", "agg": null, "group_by": "country", "limit": 50, "unresolved": []}
没有的字段给空数组或 null;object 只能取 lead / opportunity / milestone / report / award / country / law;intent 只能取 count / list / chart / report / help。"""

SCHEMA = {
    "type": "object",
    "properties": {
        "object": {"type": "string", "enum": list(OBJECTS)}, "intent": {"type": "string", "enum": list(INTENTS)},
        "countries": {"type": "array", "items": {"type": "string"}}, "sectors": {"type": "array", "items": {"type": "string"}},
        "categories": {"type": "array", "items": {"type": "string"}}, "funders": {"type": "array", "items": {"type": "string"}},
        "stages": {"type": "array", "items": {"type": "string"}}, "tracked": {"anyOf": [{"type": "boolean"}, {"type": "null"}]},
        "status": {"type": "array", "items": {"type": "string"}}, "relevant": {"anyOf": [{"type": "boolean"}, {"type": "null"}]},
        "days": {"anyOf": [{"type": "integer"}, {"type": "null"}]}, "from": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "to": {"anyOf": [{"type": "string"}, {"type": "null"}]}, "future_days": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "kinds": {"type": "array", "items": {"type": "string"}}, "keywords": {"type": "array", "items": {"type": "string"}},
        "contract_types": {"type": "array", "items": {"type": "string"}}, "deliveries": {"type": "array", "items": {"type": "string"}},
        "assets": {"type": "array", "items": {"type": "string"}}, "winner_countries": {"type": "array", "items": {"type": "string"}},
        "measure": {"anyOf": [{"type": "string"}, {"type": "null"}]}, "agg": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "winners": {"type": "array", "items": {"type": "string"}}, "capacity_min": {"anyOf": [{"type": "number"}, {"type": "null"}]},
        "capacity_max": {"anyOf": [{"type": "number"}, {"type": "null"}]},
        "group_by": {"anyOf": [{"type": "string"}, {"type": "null"}]}, "limit": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "unresolved": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["object", "intent", "countries", "sectors", "categories", "funders", "stages", "tracked", "status", "relevant", "days", "from",
                 "to", "future_days", "kinds", "keywords", "contract_types", "deliveries", "assets", "winner_countries", "winners", "capacity_min", "capacity_max",
                 "measure", "agg", "group_by", "limit", "unresolved"],
    "additionalProperties": False,
}


def system_prompt(today: date) -> str:
    v = vocab()
    return SYSTEM.format(sectors="、".join(v.sectors), categories="、".join(v.categories), funders="、".join(v.funders), stages="、".join(v.stages),
                         tracked="、".join(v.tracked), statuses="、".join(v.statuses), milestone_kinds="、".join(v.milestone_kinds),
                         report_kinds="、".join(v.report_kinds), contract_types="、".join(v.contract_types),
                         deliveries="、".join(v.deliveries), assets="、".join(v.assets), terms="、".join(_term_brief(t) for t in terms()) or "无",
                         measures=";".join(f"{m['id']} {m['name']}({'、'.join(OBJECT_ZH[o] for o in m['of'])}" + (";说法:" + "、".join(m["words"][:8]) if m["words"] else "") + ")"
                                           for m in v.measures),
                         aggs="、".join(f"{a['id']} {a['name']}" for a in v.aggs), today=today.isoformat(), guide=GUIDE)


def _term_brief(t: dict) -> str:
    scope = "只在" + "、".join(t["sectors"]) + "里找" if t["sectors"] else "不限领域"
    return f"{t['term']}(跨{'、'.join(t['covers'])},{scope})" if t["covers"] else f"{t['term']}({scope})"


def apply_terms(p: dict, q: str) -> dict:
    """提问里出现本体 terms 里的行业术语时按本体理解,不靠模型自觉:领域只留提问里另外明说的,再加术语自己的 sectors
    (跨领域的术语 sectors 为空 = 不限领域);术语按它的相关词去标题与中文摘要里找;模型把术语当成关键词的去掉。"""
    v = vocab()
    text = _norm(q)
    hits = [t for t in terms() if any(_hit(text, w) for w in t["triggers"])]
    if not hits:
        return p
    f = p["filters"]
    rest = text
    for t in hits:
        for w in t["triggers"]:
            rest = rest.replace(w, " ")
    said = _pick(_sector_text(rest), v.words["sectors"])
    f["sectors"] = [s for s in v.sectors if s in said or any(s in t["sectors"] for t in hits)]
    if p["object"] == "opportunity":
        f["sectors_fine"] = [x for s in f["sectors"] for x in v.sectors_fine.get(s, [s])]
    own = {w.lower() for t in hits for w in t["triggers"] + t["match"]}
    is_term = lambda k: k.lower() in own or any(w in k.lower() for t in hits for w in t["triggers"])
    f["keywords"] = [k for k in f["keywords"] if not is_term(k)]
    p["unresolved"] = [u for u in p["unresolved"] if not is_term(u)]
    f["terms"] = [{"term": t["term"], "match": t["match"], "covers": t["covers"], "sectors": t["sectors"]} for t in hits]
    return p


def _strs(x) -> list[str]:
    if isinstance(x, str):
        x = [x]
    return [str(s).strip() for s in (x or []) if isinstance(s, (str, int, float)) and str(s).strip()] if isinstance(x, list) else []


def _iso(x) -> str | None:
    try:
        return date.fromisoformat(str(x).strip()[:10]).isoformat()
    except (ValueError, TypeError):
        return None


def from_model(data: dict, today: date, q: str = "", force_object: str | None = None) -> dict:
    """模型输出 → 计划:每个值按词表校验,认不出的进 unresolved;对象与意图不合法的回缺省;q 是原提问(按本体的行业术语、语义层纠正)。
    force_object:接着上一问、这一句没说查什么时沿用的对象(见 plan)。"""
    v = vocab()
    p = empty_plan()
    f = p["filters"]
    p["parsed_by"] = "model"
    p["model"] = data.get("_model")
    p["object"] = data.get("object") if data.get("object") in OBJECTS else "lead"
    if p["object"] in ("lead", "opportunity") and is_award_q(q):  # 模型把「中标信息」当成商机阶段「中标」(10-13 用户反映 0 条):按提问纠正
        p["object"] = "award"
    # 指标以提问里的说法为准(语义层的词表),说法里没有的才用模型给的
    ms = parse_measures(q)
    mm = measure_of(data.get("measure")) if isinstance(data.get("measure"), str) else None
    if not ms and mm and mm["id"] != "count":
        ms = [{"id": mm["id"], "agg": data.get("agg") if data.get("agg") in mm["aggs"] else mm["aggs"][0]}]
    p["object"] = force_object if force_object in OBJECTS else _measure_object(ms, q, p["object"])  # 问评标天数、招标估价的,模型当成线索也改查已中标项目(10-13 用户反映)
    if p["object"] == "country" or rules_parse(q, today, force_object)["object"] == "country":  # 国别(营商环境)的条件按提问读(门槛、外企参标、排序),模型多给的国名补上
        cp = rules_parse(q, today, "country")
        cp["parsed_by"], cp["model"] = "model", p["model"]
        for c in _strs(data.get("countries")):
            iso = geo.iso3(c)
            if iso and all(x["iso3"] != iso for x in cp["filters"]["countries"]):
                cp["filters"]["countries"].append({"iso3": iso, "name": geo.name_zh(iso)})
        if cp["filters"]["countries"] and cp["intent"] == "count":
            cp["intent"] = "list"
        return finish(cp)
    award = p["object"] == "award"
    p["intent"] = data.get("intent") if data.get("intent") in INTENTS else "list"
    unresolved = _strs(data.get("unresolved"))

    def keep(values: list[str], allowed: list[str], aliases: dict[str, list[str]] | None = None) -> list[str]:
        out = []
        for s in values:
            hit = s if s in allowed else next((val for val, ws in (aliases or {}).items() if s.lower() in ws), None)
            if hit and hit not in out:
                out.append(hit)
            elif not hit:
                unresolved.append(s)
        return out

    for c in _strs(data.get("countries")):
        iso = geo.iso3(c)
        if iso and all(x["iso3"] != iso for x in f["countries"]):
            f["countries"].append({"iso3": iso, "name": geo.name_zh(iso)})
        elif not iso:
            unresolved.append(c)
    if award:  # 已中标项目:合同类型、中标单位及其国别、规模;模型漏了的按提问补(规模以提问里写的为准,不靠模型换算)
        win, win_iso = winner_countries(q)
        f["countries"] = [c for c in f["countries"] if c["iso3"] not in win_iso]
        f["contract_types"] = keep(_strs(data.get("contract_types")), v.contract_types, v.words["contract_types"]) or contract_types_besides_delivery(q)
        f["deliveries"] = [d for d in keep(_strs(data.get("deliveries")), v.deliveries, v.words["deliveries"]) if d != "运营"] or deliveries(q)
        if "运营" in _strs(data.get("deliveries")) and f["deliveries"] and "运营" not in f["contract_types"]:
            f["contract_types"].append("运营")
        _or_delivery(f)
        f["price"] = parse_price(q)  # 金额以提问里写的为准(币种、折算由这里定,不靠模型)
        f["assets"] = assets(q) or keep(_strs(data.get("assets")), v.assets, v.words["assets"])
        named = _strs(data.get("winners"))
        for c in _strs(data.get("winner_countries")) + win + [w for w in named if _country_word(w)]:  # 模型把「中国企业」写进 winners 的也算国别
            iso = geo.iso3(c)
            if iso and geo.name_zh(iso) not in f["winner_countries"]:
                f["winner_countries"].append(geo.name_zh(iso))
            elif not iso:
                unresolved.append(c)
        f["winners"] = [k[:60] for k in named if not _country_word(k)][:5]
        num = lambda x: float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and 0 < x < 1e9 else None  # noqa: E731
        lo, hi = num(data.get("capacity_min")), num(data.get("capacity_max"))
        f["capacity"] = parse_capacity(q) or ({"min": lo, "max": hi} if lo or hi else None)
    f["sectors"] = keep(_strs(data.get("sectors")), v.sectors, v.words["sectors"])
    if award and f["assets"]:  # 「污水厂」里有「水厂」:模型多给的「供水」以提问里的说法为准
        said = _pick(_sector_text(_norm(q)), v.words["sectors"])
        f["sectors"] = [s for s in f["sectors"] if s in said] or said if said else f["sectors"]
    f["categories"] = keep(_strs(data.get("categories")), v.categories, v.words["categories"]) if p["object"] == "lead" else []
    f["funders"] = keep(_strs(data.get("funders")), v.funders, v.words["funders"])
    if p["object"] == "opportunity":
        f["stages"] = keep(_strs(data.get("stages")), v.stages)
        f["tracked"] = True if data.get("tracked") is True else None
        f["sectors_fine"] = [x for s in f["sectors"] for x in v.sectors_fine.get(s, [s])]
    if p["object"] == "lead":
        f["status"] = keep(_strs(data.get("status")), v.statuses, v.words["statuses"])
        f["relevant"] = data.get("relevant") if isinstance(data.get("relevant"), bool) else None
    if p["object"] == "milestone":
        f["kinds"] = keep(_strs(data.get("kinds")), v.milestone_kinds)
    if p["object"] == "report":
        f["kinds"] = keep(_strs(data.get("kinds")), v.report_kinds)
    if isinstance(data.get("days"), (int, float)) and data["days"] > 0:
        f["days"] = int(min(data["days"], 3660))
    if isinstance(data.get("future_days"), (int, float)) and data["future_days"] > 0 and p["object"] == "milestone":
        f["future_days"] = int(min(data["future_days"], 3660))
    elif isinstance(data.get("future_days"), (int, float)) and data["future_days"] > 0 and award and not f["days"]:  # 「三年内」对已中标项目说的是过去
        f["days"] = int(min(data["future_days"], 3660))
    if _iso(data.get("from")) or _iso(data.get("to")):
        f["from"], f["to"] = _iso(data.get("from")), _iso(data.get("to"))
        f["days"] = None
    if p["object"] == "milestone" and not (f["days"] or f["from"] or f["future_days"]):
        f["future_days"] = DEFAULT_FUTURE_DAYS
    f["keywords"] = [k[:60] for k in _strs(data.get("keywords")) if not (award and (_award_noise(k) or _country_word(k)))][:5]
    if award:
        unresolved = [u for u in unresolved if not _award_noise(u)]
    gb = GROUP_ALIAS.get(p["object"], {}).get(data.get("group_by"), data.get("group_by"))
    if gb in GROUPS[p["object"]]:
        p["group_by"] = gb
        if p["intent"] in ("list", "count"):
            p["intent"] = "chart"
    elif p["intent"] == "chart":
        p["group_by"] = "country" if "country" in GROUPS[p["object"]] else GROUPS[p["object"]][0]
    if isinstance(data.get("limit"), (int, float)) and data["limit"] > 0:
        p["limit"] = int(min(MAX_LIMIT, data["limit"]))
    if p["intent"] == "report":
        p["object"] = "report"
    p["measures"] = [x for x in ms if p["object"] in measure_of(x["id"])["of"]]
    unresolved += [measure_of(x["id"])["name"] for x in ms if p["object"] not in measure_of(x["id"])["of"]]
    named = {w for x in p["measures"] for w in measure_of(x["id"])["words"]} | {w for a in v.aggs for w in a["words"]}
    unresolved = [u for u in unresolved if not (parse_measures(u) or _norm(u) in named or (award and assets(u)))]  # 模型认不出、语义层认得的不算没认出来
    p["unresolved"] = list(dict.fromkeys(u for u in unresolved if u))[:8]
    apply_terms(p, q)
    return finish(p)


# ---------------------------------------------------------------- 整理与说明


FINISH_KEYS = {"对象", "国别", "区域", "类别", "领域", "资金来源", "我方阶段", "处理状态", "是否相关", "类型", "术语", "合同类型", "承包模式", "主体工程", "中标价",
               "中标单位国别含", "中标单位含", "规模", "名称或中标单位含", "标题或摘要含", "分组", "呈现", "指标",
               "市场分级", "外企参标", "门槛", "排序"}


def finish(p: dict) -> dict:
    """补「理解为」的说明(页面原样显示,让人一眼核对条件)。可以重跑(接着问补了条件后再整理一次):这里加的几项先去掉再加。"""
    f = p["filters"]
    v = vocab()
    u = [["时间", x[1]] if x[0] == "中标时间" else x for x in p["understood"] if x[0] not in FINISH_KEYS]
    u.insert(0, ["对象", OBJECT_ZH[p["object"]]])
    if f["countries"]:
        u.append(["国别", "、".join(c["name"] for c in f["countries"])])
    if f["regions"]:
        u.append(["区域", "、".join(f["regions"])])
    award = p["object"] == "award"
    if f["sectors"]:
        u.append(["类别" if award else "领域", "、".join(f["sectors"])])  # 已中标项目的「类别」就是领域(本体 award.sector)
    if f["categories"]:
        u.append(["类别", "、".join(f["categories"])])
    if f["funders"]:
        u.append(["资金来源", "、".join(f["funders"])])
    if f["tracked"]:
        u.append(["我方阶段", "在跟踪(" + "、".join(v.tracked) + ")"])
    elif f["stages"]:
        u.append(["我方阶段", "、".join(f["stages"])])
    if f["status"]:
        u.append(["处理状态", "、".join(f["status"])])
    if f["relevant"] is not None:
        u.append(["是否相关", "相关" if f["relevant"] else "不相关"])
    if f["kinds"]:
        u.append(["类型", "、".join(f["kinds"])])
    for t in f.get("terms") or []:
        span = "只看" + "、".join(t["sectors"]) if t["sectors"] else ("跨" + "、".join(t["covers"]) + "," if t["covers"] else "") + "不限领域"
        u.append(["术语", f"{t['term']}({span};按 {len(t['match'])} 个相关词查标题和中文摘要)"])
    if f.get("contract_types"):
        u.append(["合同类型", "、".join(f["contract_types"])])
    if f.get("deliveries"):
        u.append(["承包模式", "、".join(f["deliveries"]) + ("(几样都算)" if len(f["deliveries"]) > 1 else "")])
    if f.get("assets"):
        u.append(["主体工程", "、".join(f["assets"]) + ("(几样都算)" if len(f["assets"]) > 1 else "")])
    if f.get("price"):
        u.append(["中标价", price_label(f["price"])])
    if f.get("winner_countries"):
        u.append(["中标单位国别含", "、".join(f["winner_countries"])])
    if f.get("winners"):
        u.append(["中标单位含", "、".join(f["winners"])])
    if f.get("capacity"):
        u.append(["规模", cap_label(f["capacity"]) + "(按工程量与名称里写明的日处理能力比;没写规模的另列)"])
    if f["keywords"]:
        u.append(["名称或中标单位含" if award else "标题或摘要含", "、".join(f["keywords"])])
    if not any(x[0] == "时间" for x in u):
        if f["days"]:
            u.append(["时间", f"最近 {f['days'] // 365} 年({f['days']} 天)" if f["days"] % 365 == 0 else f"最近 {f['days']} 天"])
        elif f["from"] or f["to"]:
            u.append(["时间", f"{f['from'] or '…'} 至 {f['to'] or '…'}"])
        elif f["future_days"]:
            u.append(["时间", f"未来 {f['future_days']} 天"])
    if award:  # 已中标项目的时间是中标时间
        u = [["中标时间", x[1]] if x[0] == "时间" else x for x in u]
    if p["object"] == "country":
        if f.get("tiers"):
            u.append(["市场分级", "、".join(f["tiers"])])
        if f.get("foreign"):
            u.append(["外企参标", "无限制" if f["foreign"] == ["无限制"] else "、".join(f["foreign"])])
        if f.get("metric") and measure_of(f["metric"]["id"]):
            m, x = measure_of(f["metric"]["id"]), f["metric"]
            num = lambda n: f"{n:g}" + ("%" if m["unit"] == "%" else " " + m["unit"])  # noqa: E731
            u.append(["门槛", m["name"] + ("在 " + num(x["min"]) + " 至 " + num(x["max"]) + " 之间" if x["min"] is not None and x["max"] is not None
                                          else "低于 " + num(x["max"]) if x["max"] is not None else "高于 " + num(x["min"]))])
        if p.get("measures"):
            u.append(["指标", "、".join(measure_of(x["id"])["name"] for x in p["measures"])])
        o = p.get("order")
        if o and measure_of(o["id"]):
            u.append(["排序", measure_of(o["id"])["name"] + ("从高到低" if o["dir"] == "desc" else "从低到高")])
        u.append(["呈现", "单国营商环境" if len(f["countries"]) == 1 else ("数一下,附国别表" if p["intent"] == "count" else "国别表")
                  + "(世界银行 B-READY 2025;没覆盖的国家写明)"])
        p["understood"] = u
        p["scope_note"] = ""
        return p
    if p.get("measures"):
        u.append(["指标", measure_label(p["measures"])])
    if p["group_by"]:
        u.append(["分组", GROUP_ZH[p["group_by"]]])
    if p.get("measures") and p["intent"] not in ("report", "help"):
        u.append(["呈现", "指标卡片" + ("与按" + GROUP_ZH[p["group_by"]] + "分的柱图" if p["group_by"] else "")])
    else:
        u.append(["呈现", {"count": "数一下", "list": f"清单(最多 {p['limit']} 条)", "chart": "分组统计", "report": "生成报告", "help": "帮助"}[p["intent"]]])
    p["understood"] = u
    scope = {x["iso3"]: x["note"] for x in v.not_collected}
    p["scope_note"] = "".join(dict.fromkeys(scope[c["iso3"]] for c in f["countries"] if c["iso3"] in scope))
    return p


def _prev(prev) -> dict | None:
    """页面带来的上一问(它自己的计划:对象、条件、指标、分组;外面传来的,逐项按词表校验)→ 干净的一份;不合格 → None。"""
    if not isinstance(prev, dict) or prev.get("object") not in OBJECTS:
        return None
    v, pf = vocab(), prev.get("filters") if isinstance(prev.get("filters"), dict) else {}
    lst = lambda k, allowed: [x for x in _strs(pf.get(k)) if x in allowed]  # noqa: E731
    num = lambda x: float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and 0 <= x < 1e13 else None  # noqa: E731
    f: dict = {"countries": [{"iso3": c["iso3"], "name": geo.name_zh(c["iso3"])} for c in (pf.get("countries") or [])[:10]
                             if isinstance(c, dict) and isinstance(c.get("iso3"), str) and geo.iso3(c["iso3"]) == c["iso3"]],
               "sectors": lst("sectors", v.sectors), "categories": lst("categories", v.categories), "funders": lst("funders", v.funders),
               "contract_types": lst("contract_types", v.contract_types), "deliveries": lst("deliveries", v.deliveries), "assets": lst("assets", v.assets),
               "winner_countries": [geo.name_zh(geo.iso3(c)) for c in _strs(pf.get("winner_countries")) if geo.iso3(c)],
               "winners": [w[:60] for w in _strs(pf.get("winners"))][:5], "keywords": [w[:60] for w in _strs(pf.get("keywords"))][:5],
               "days": int(pf["days"]) if num(pf.get("days")) and 0 < pf["days"] <= 3660 else None, "from": _iso(pf.get("from")), "to": _iso(pf.get("to")),
               "price": None, "capacity": None,
               "tiers": lst("tiers", v.market_tiers), "foreign": lst("foreign", ["无限制", "部分招标有限制", "全部招标有限制", "说法不一"]), "metric": None}
    mt = pf.get("metric") if isinstance(pf.get("metric"), dict) else {}
    if prev["object"] == "country" and measure_of(mt.get("id")) and (num(mt.get("min")) is not None or num(mt.get("max")) is not None):
        f["metric"] = {"id": mt["id"], "min": num(mt.get("min")), "max": num(mt.get("max"))}
    pr, cap = pf.get("price") if isinstance(pf.get("price"), dict) else {}, pf.get("capacity") if isinstance(pf.get("capacity"), dict) else {}
    cur = pr.get("currency") if pr.get("currency") in [c for c, _ in PRICE_CUR] else None
    if cur and (num(pr.get("min")) or num(pr.get("max"))):
        f["price"] = {"min": num(pr.get("min")), "max": num(pr.get("max")), "currency": cur, "rate": _rate(cur)}
    if num(cap.get("min")) or num(cap.get("max")):
        f["capacity"] = {"min": num(cap.get("min")), "max": num(cap.get("max"))}
    ms = []
    for x in prev.get("measures") or []:
        m = measure_of(x.get("id")) if isinstance(x, dict) else None
        if m and prev["object"] in m["of"] and x.get("agg") in m["aggs"]:
            ms.append({"id": m["id"], "agg": x["agg"]})
    gb = prev.get("group_by") if prev.get("group_by") in GROUPS[prev["object"]] else None
    od = prev.get("order") if isinstance(prev.get("order"), dict) else {}
    order = {"id": od["id"], "dir": od["dir"]} if prev["object"] == "country" and measure_of(od.get("id")) and od.get("dir") in ("asc", "desc") else None
    out = {"object": prev["object"], "q": str(prev.get("q") or "")[:MAX_Q], "filters": f, "measures": ms[:3], "group_by": gb, "order": order}
    pl = prev.get("law") if isinstance(prev.get("law"), dict) else {}
    if prev["object"] == "law":  # 法规问答的种类、指标、概念与关键词(逐项校验)
        out["law"] = {"kind": pl.get("kind") if pl.get("kind") in law.KINDS else "limit", "params": [x for x in _strs(pl.get("params")) if x in law.words().params],
                      "concepts": [x[:30] for x in _strs(pl.get("concepts"))][:5], "keywords": [x[:40] for x in _strs(pl.get("keywords"))][:8]}
    return out


def _follow_up(q: str) -> bool:
    """接着上一问说的:以「那」「其中」「这些」开头,或很短的「……呢」(「那吉尔吉斯斯坦呢」「按年份呢」)。"""
    t = q.strip()
    return bool(FOLLOW_RE.search(t)) or (len(t) <= 16 and re.search(r"呢\s*[?？]?\s*$", t) is not None)


def _inherit(p: dict, prev: dict, q: str) -> None:
    """接着问:这一句没说的条件、指标、分组沿用上一问的(同一个对象时),「理解为」里写明沿用了哪几项。"""
    f, pf, got = p["filters"], prev["filters"], []
    for k in INHERIT:
        if k in ("tiers", "foreign", "metric") and f.get("countries"):  # 「那吉尔吉斯斯坦呢」问的是这一国本身,不按上一问的筛
            continue
        if k == "countries" and p["object"] == "country" and (f.get("tiers") or f.get("foreign") or f.get("metric")):  # 「那市场国别呢」换成按条件列国别
            continue
        if not f.get(k) and pf.get(k) and (p["object"] == "award" or k not in ("contract_types", "deliveries", "assets", "winner_countries", "winners", "price", "capacity")):
            f[k] = pf[k]
            got.append(INHERIT_ZH[k])
    if not (f.get("days") or f.get("from") or f.get("to") or f.get("future_days")) and (pf.get("days") or pf.get("from") or pf.get("to")):
        f["days"], f["from"], f["to"] = pf.get("days"), pf.get("from"), pf.get("to")
        got.append(INHERIT_ZH["time"])
    if (not p["measures"] or p.pop("measure_default", False)) and prev["measures"]:
        p["measures"] = prev["measures"]
        got.append(INHERIT_ZH["measures"])
    if p["object"] == "country" and not p.get("order") and prev.get("order") and not f.get("countries"):
        p["order"] = prev["order"]
        got.append("排序")
    if not p["group_by"] and prev["group_by"] and not re.search(r"不分|不用分|合起来|总的", q):
        p["group_by"] = prev["group_by"]
        got.append(INHERIT_ZH["group_by"])
    if got:
        p["understood"].append(["沿用上一问", "、".join(got)])


def law_plan(q: str, lw: dict, by: str, model: str | None = None) -> dict:
    """law.detect 的结果 → 计划(对象「法规」)。"""
    p = empty_plan()
    p["object"], p["intent"], p["parsed_by"], p["model"] = "law", "list", by, model
    p["filters"]["countries"] = [{"iso3": c, "name": geo.name_zh(c)} for c in lw["countries"]]
    p["law"] = {k: lw[k] for k in ("kind", "params", "concepts", "keywords")}
    p["law"]["inherited"] = lw.get("inherited") or []
    p["understood"] = law.understood(p)
    scope = {x["iso3"]: x["note"] for x in vocab().not_collected}
    p["scope_note"] = "".join(dict.fromkeys(scope[c] for c in lw["countries"] if c in scope))
    return p


def plan(q: str, today: date, clf=None, prev=None) -> dict:
    """一句提问 → 计划。配了模型就用模型解析(规则结果兜底:模型没结果、出错或认不出任何条件时用规则的),没配就只用规则。
    prev:页面带来的上一问的计划(接着问):这一句没明说查什么的沿用它的对象(问了别的对象才有的指标的除外);像追问的再沿用条件、指标与分组。"""
    q = (q or "").strip()[:MAX_Q]
    if not q:
        p = empty_plan()
        p["intent"] = "help"
        return finish(p)
    prev = _prev(prev)
    lw = law.detect(q, prev)
    if lw:  # 法规问题(排放标准、本国定义、条文):不走线索 / 中标的解析
        return law_plan(q, lw, "rules")
    force = None
    if prev and not OBJECT_RE.search(_norm(q)):
        force = _measure_object(parse_measures(q), q, prev["object"])
    rules = rules_parse(q, today, force)
    out = None
    if clf is None or not getattr(clf, "enabled", False):
        rules["note"] = "" if clf is None else (getattr(clf, "fatal", "") or getattr(clf, "note", "") or "未配置模型密钥,按词表解析")
        p = rules
    else:
        ask = q if not prev or not prev["q"] else f"上一个问题(查的是{OBJECT_ZH[prev['object']]}):{prev['q']}\n这一个问题:{q}"
        out = clf.ask(system_prompt(today), ask, SCHEMA, GUIDE)
        if not out or "_error" in out:
            rules["note"] = "模型没有返回可用结果,按词表解析" + (f"({out['_error']})" if out and out.get("_error") else "")
            p = rules
        else:
            p = from_model(out, today, q, force)
            if p["intent"] == "help" and rules["intent"] != "help":
                p = rules
                p["note"] = "模型没看懂,按词表解析"
    if p["object"] == "law":  # 模型认作法规问题、提问里又没有法规说法的(「摩洛哥特许经营最长多少年」):按条文查
        cs = [c["iso3"] for c in p["filters"]["countries"]] or list(dict.fromkeys(geo.detect(q)))
        kw = [k for k in p["filters"]["keywords"] if k] or law.keywords(_norm(q), cs)
        return law_plan(q, {"kind": "text" if kw else "list", "params": [], "concepts": law.concepts_in(_norm(q)), "keywords": kw[:8], "countries": cs, "inherited": []},
                        "model", p.get("model"))
    if force and force == prev["object"] == p["object"] and rules_parse(q, today)["object"] != force:  # 不沿用也会是这个对象的,不写
        p["understood"].append(["沿用上一问", "对象(" + OBJECT_ZH[force] + ")"])
    if prev and prev["object"] == p["object"] and _follow_up(q):
        _inherit(p, prev, q)
        if p["intent"] == "help":
            p["intent"] = "list"
    if any(x[0] == "沿用上一问" for x in p["understood"]):  # 两条并成一条
        said = [x[1] for x in p["understood"] if x[0] == "沿用上一问"]
        p["understood"] = [x for x in p["understood"] if x[0] != "沿用上一问"] + [["沿用上一问", "、".join(said)]]
        finish(p)
    return p
