"""助手(门户「助手」页的后端):把一句中文提问解析成页面能执行的「查询计划」。

只做理解,不碰数据:计划里是对象(线索 / 商机 / 时间节点 / 报告)、意图(数一下 / 列清单 / 分组统计 / 要报告)、
筛选条件(国别、领域、类别、资金来源、我方阶段、处理状态、时间范围、关键词)与分组维度;页面拿着计划、带着登录人的 cookie
去查表格并呈现,数字全部来自表格,模型不写数字、也看不到表里的数据(送给模型的只有问题本身)。

两种解析:
- 规则:词表(本体的枚举值 + 口语同义词)、国名(reference/countries.yaml)、时间短语。没配模型密钥时只有它;配了也先跑一遍兜底。
- 模型:DeepSeek 或 Claude(与采集判定共用 core.watch.llm 的接入;profile 的 assistant.llm 可单独指定,缺省沿用 watch.llm 的
  provider 与 model,但关掉思考模式、输出很短,一问几百 token)。模型输出的每个值都按词表校验,认不出的丢进 unresolved 让人看见。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

import yaml

from core import geo

ROOT = Path(__file__).resolve().parent.parent
ONTO_PATH = ROOT / "docs" / "ontology" / "ontology.yaml"

OBJECTS = ("lead", "opportunity", "milestone", "report")
OBJECT_ZH = {"lead": "线索", "opportunity": "商机", "milestone": "时间节点", "report": "报告"}
INTENTS = ("count", "list", "chart", "report", "help")
GROUPS = {"lead": ("country", "category", "sectors", "funder", "day", "status", "source"),
          "opportunity": ("country", "stage", "sectors", "funder"),
          "milestone": ("country", "kind", "day"), "report": ("kind", "country")}
GROUP_ZH = {"country": "国别", "category": "类别", "sectors": "领域", "funder": "资金来源", "day": "按天", "status": "处理状态",
            "source": "信源", "stage": "我方阶段", "kind": "类型"}
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
            "未来 30 天有哪些投标截止", "海水淡化的线索按资金来源统计", "最近的周报"]


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
    terms: list[dict] = field(default_factory=list)
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
    v = Vocab(
        sectors=[x["v"] for x in _values(tax["lead_sectors"])], sectors_fine=fine,
        categories=[x["v"] for x in _values(tax["lead_category"])],
        funders=[g["v"] for g in fund["groups"]], funder_other=fund["other"], funder_empty=fund["empty"],
        stages=[x["v"] for x in _values(tax["stage"])], tracked=list(o["glossary"]["tracked"]["stages"]),
        statuses=[x["v"] for x in _values(tax["lead_status"])],
        milestone_kinds=[x["v"] for x in _values(tax["milestone_kind"])],
        deadline_kinds=[x["v"] for x in tax["milestone_kind"]["groups"][0]["values"]],
        report_kinds=[x["v"] for x in _values(tax["report_kind"])],
        terms=[x for x in (make_term(t) for t in o.get("terms") or []) if x],
        not_collected=list((o.get("data_scope") or {}).get("not_collected") or []),
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
    }
    # 太短、太泛的英文词不要(「plan」「law」会误伤),资金来源里长度 ≤ 2 的拉丁缩写(WB、EU)只在整词时才认,见 _hit
    for dim in v.words.values():
        for val, ws in dim.items():
            dim[val] = [w for w in ws if not (w.isascii() and len(w) <= 3 and w not in ("eoi", "spn", "gpn", "adb", "eib", "afd", "kfw", "idb", "dfc", "pq", "wb", "eu", "ida"))]
    return v


def empty_plan() -> dict:
    return {"intent": "list", "object": "lead",
            "filters": {"countries": [], "regions": [], "sectors": [], "sectors_fine": [], "categories": [], "funders": [], "stages": [],
                        "tracked": None, "status": [], "relevant": None, "days": None, "from": None, "to": None, "future_days": None,
                        "kinds": [], "keywords": [], "terms": []},
            "group_by": None, "limit": DEFAULT_LIMIT, "understood": [], "unresolved": [], "parsed_by": "rules", "model": None, "note": ""}


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
    """时间短语 → {days | from,to | future_days},附带给人看的说明。先看相对窗口,再看本周 / 上周 / 本月 / 上月 / 今年 / 某月。"""
    m = re.search(r"(最近|近|过去|这)\s*(\d+|[一二两三四五六七八九十]+|半)\s*(个月|天|日|周|星期|礼拜|月|年)", text)
    if m:
        n = _num(m.group(2))
        if n:
            days = max(1, round(n * UNIT_DAYS[m.group(3)]))
            return {"days": days}, f"最近 {days} 天"
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


def rules_parse(q: str, today: date) -> dict:
    v = vocab()
    text = _norm(q)
    p = empty_plan()
    f = p["filters"]
    # 对象
    if re.search(r"商机|项目机会|在跟|跟踪|投标的|中标的", text) and "线索" not in text:
        p["object"] = "opportunity"
    elif re.search(r"节点|开标|截止日|截止|到期|时间表|日程", text) and "线索" not in text and "商机" not in text:
        p["object"] = "milestone"
    elif re.search(r"周报|月报|日报|国别报告|项目报告|报告", text) and not re.search(r"生成|出一份|做一份|写一份|来一份|给我一份", text):
        p["object"] = "report"
    # 意图
    if re.search(r"生成|出一份|做一份|写一份|来一份|给我一份", text) and "报告" in text:
        p["intent"] = "report"
        p["object"] = "report"
    elif re.search(r"多少|几条|几个|几份|数量|总数|计数|有没有|共有", text):
        p["intent"] = "count"
    elif re.search(r"按|分布|统计|占比|趋势|走势|分组|排名|排行|最多|图|哪些国家|哪个国家|各国", text):
        p["intent"] = "chart"
    # 分组维度
    gb = None
    if re.search(r"按(国家|国别|国)|分国|各国|哪些国家|哪个国家", text):
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
    if gb and gb in GROUPS[p["object"]]:
        p["group_by"] = gb
        p["intent"] = "chart"
    elif p["intent"] == "chart" and not gb:
        p["group_by"] = "country" if "country" in GROUPS[p["object"]] else GROUPS[p["object"]][0]
    # 国别(几个都认;国名也可能是「中标」之类词的一部分,geo 只认国名词表)
    for iso in geo.detect(q):
        f["countries"].append({"iso3": iso, "name": geo.name_zh(iso)})
    for x in v.not_collected:  # 「国内」「我国」这类口语指本体 data_scope 里写的国家
        if not f["countries"] and not re.search(r"国内外|国外", text) and any(w in text for w in x.get("words") or []):
            f["countries"].append({"iso3": x["iso3"], "name": geo.name_zh(x["iso3"])})
    # 领域 / 类别 / 资金来源 / 阶段 / 状态 / 节点与报告类型
    f["sectors"] = _pick(text, v.words["sectors"])
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
    # 时间
    t, label = parse_time(text, today)
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
                f["days"], f["from"], f["future_days"], f["kinds"], f["keywords"], f["terms"], p["group_by"]]) \
            and not re.search(r"线索|商机|节点|报告|全部|所有|都|清单|列表|多少", text):
        p["intent"] = "help"
    return finish(p)


# ---------------------------------------------------------------- 模型解析


SYSTEM = """你是「全球水务开源情报」系统首页的提问解析器。用户用中文(偶尔夹英文)提问,你只负责把问题翻译成一个查询计划(JSON),不回答问题、不编造数据。

系统里有四类对象:
- lead 线索:从公开信源采集的公告与新闻,字段有国别、领域(粗分)、类别、出资方、发现时间、处理状态、是否相关。
- opportunity 商机:人在跟的项目,字段有国别、领域(细分)、我方阶段、资金。
- milestone 时间节点:投标截止、开标、授标等日期,挂在商机或线索下。
- report 报告:国别报告、项目报告、周报、月报、日报。

字段取值(只能用这些,别的词放进 unresolved):
- sectors(领域,粗分):{sectors}
- categories(线索类别):{categories}
- funders(资金来源归组):{funders}
- stages(商机我方阶段):{stages};「在跟踪 / 在跟的商机」= tracked: true(阶段 ∈ {tracked});「AI 推荐」= 阶段「建议」
- status(线索处理状态):{statuses}
- kinds:时间节点类型 {milestone_kinds};报告类型 {report_kinds}
- countries:国名用中文或 ISO3 三字母代码,可多个
- 时间:相对窗口用 days(最近 N 天,按发现时间 / 日期)或 future_days(未来 N 天,只对时间节点有意义);绝对区间用 from / to(YYYY-MM-DD)。今天是 {today}。
- keywords:用户明确要在标题里找的词(机构名、项目名、地名),不要把领域 / 类别词重复放进来。
- 行业术语(跨领域的词,按这里理解;提问里出现时不要据此填 sectors,也不要放进 keywords,系统按术语自己的相关词去找):{terms}
- intent:count 问数量;list 要清单(缺省);chart 要分组统计 / 分布 / 趋势 / 哪些国家最多;report 要生成一份新报告;help 看不懂或与系统无关。
- group_by:chart 时的分组维度,线索可选 country / category / sectors / funder / day / status / source,商机 country / stage / sectors / funder,时间节点 country / kind / day,报告 kind / country。
- limit:要几条(1–200),没说就 50。

{guide}"""

GUIDE = """输出格式:只输出一个 JSON 对象,不要任何其他文字。字段与示例:
{"object": "lead", "intent": "chart", "countries": ["埃及"], "sectors": ["供水"], "categories": ["招标"], "funders": ["世界银行"],
 "stages": [], "tracked": null, "status": [], "relevant": null, "days": 30, "from": null, "to": null, "future_days": null,
 "kinds": [], "keywords": [], "group_by": "country", "limit": 50, "unresolved": []}
没有的字段给空数组或 null;object 只能取 lead / opportunity / milestone / report;intent 只能取 count / list / chart / report / help。"""

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
        "group_by": {"anyOf": [{"type": "string"}, {"type": "null"}]}, "limit": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "unresolved": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["object", "intent", "countries", "sectors", "categories", "funders", "stages", "tracked", "status", "relevant", "days", "from",
                 "to", "future_days", "kinds", "keywords", "group_by", "limit", "unresolved"],
    "additionalProperties": False,
}


def system_prompt(today: date) -> str:
    v = vocab()
    return SYSTEM.format(sectors="、".join(v.sectors), categories="、".join(v.categories), funders="、".join(v.funders), stages="、".join(v.stages),
                         tracked="、".join(v.tracked), statuses="、".join(v.statuses), milestone_kinds="、".join(v.milestone_kinds),
                         report_kinds="、".join(v.report_kinds), terms="、".join(_term_brief(t) for t in terms()) or "无",
                         today=today.isoformat(), guide=GUIDE)


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
    said = _pick(rest, v.words["sectors"])
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


def from_model(data: dict, today: date, q: str = "") -> dict:
    """模型输出 → 计划:每个值按词表校验,认不出的进 unresolved;对象与意图不合法的回缺省;q 是原提问(按本体的行业术语纠正)。"""
    v = vocab()
    p = empty_plan()
    f = p["filters"]
    p["parsed_by"] = "model"
    p["model"] = data.get("_model")
    p["object"] = data.get("object") if data.get("object") in OBJECTS else "lead"
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
    f["sectors"] = keep(_strs(data.get("sectors")), v.sectors, v.words["sectors"])
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
    if _iso(data.get("from")) or _iso(data.get("to")):
        f["from"], f["to"] = _iso(data.get("from")), _iso(data.get("to"))
        f["days"] = None
    if p["object"] == "milestone" and not (f["days"] or f["from"] or f["future_days"]):
        f["future_days"] = DEFAULT_FUTURE_DAYS
    f["keywords"] = [k[:60] for k in _strs(data.get("keywords"))][:5]
    gb = data.get("group_by")
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
    p["unresolved"] = list(dict.fromkeys(u for u in unresolved if u))[:8]
    apply_terms(p, q)
    return finish(p)


# ---------------------------------------------------------------- 整理与说明


def finish(p: dict) -> dict:
    """补「理解为」的说明(页面原样显示,让人一眼核对条件)。"""
    f = p["filters"]
    v = vocab()
    u = [x for x in p["understood"] if x[0] != "对象"]
    u.insert(0, ["对象", OBJECT_ZH[p["object"]]])
    if f["countries"]:
        u.append(["国别", "、".join(c["name"] for c in f["countries"])])
    if f["regions"]:
        u.append(["区域", "、".join(f["regions"])])
    if f["sectors"]:
        u.append(["领域", "、".join(f["sectors"])])
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
    if f["keywords"]:
        u.append(["标题或摘要含", "、".join(f["keywords"])])
    if not any(x[0] == "时间" for x in u):
        if f["days"]:
            u.append(["时间", f"最近 {f['days']} 天"])
        elif f["from"] or f["to"]:
            u.append(["时间", f"{f['from'] or '…'} 至 {f['to'] or '…'}"])
        elif f["future_days"]:
            u.append(["时间", f"未来 {f['future_days']} 天"])
    if p["group_by"]:
        u.append(["分组", GROUP_ZH[p["group_by"]]])
    u.append(["呈现", {"count": "数一下", "list": f"清单(最多 {p['limit']} 条)", "chart": "分组统计", "report": "生成报告", "help": "帮助"}[p["intent"]]])
    p["understood"] = u
    scope = {x["iso3"]: x["note"] for x in v.not_collected}
    p["scope_note"] = "".join(dict.fromkeys(scope[c["iso3"]] for c in f["countries"] if c["iso3"] in scope))
    return p


def plan(q: str, today: date, clf=None) -> dict:
    """一句提问 → 计划。配了模型就用模型解析(规则结果兜底:模型没结果、出错或认不出任何条件时用规则的),没配就只用规则。"""
    q = (q or "").strip()[:MAX_Q]
    if not q:
        p = empty_plan()
        p["intent"] = "help"
        return finish(p)
    rules = rules_parse(q, today)
    if clf is None or not getattr(clf, "enabled", False):
        rules["note"] = "" if clf is None else (getattr(clf, "fatal", "") or getattr(clf, "note", "") or "未配置模型密钥,按词表解析")
        return rules
    out = clf.ask(system_prompt(today), q, SCHEMA, GUIDE)
    if not out or "_error" in out:
        rules["note"] = "模型没有返回可用结果,按词表解析" + (f"({out['_error']})" if out and out.get("_error") else "")
        return rules
    p = from_model(out, today, q)
    if p["intent"] == "help" and rules["intent"] != "help":
        p = rules
        p["note"] = "模型没看懂,按词表解析"
    return p
