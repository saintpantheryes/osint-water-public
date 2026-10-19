"""助手的「看一条链接」:用户把别处搜到的网址贴进提问框,这里读原文、看信源可信度、在库里找同一项目、到公开渠道找旁证,
再请模型给简要分析和入库建议。只读、不写表格:入库由页面带着登录人的身份建一行(来源方式「用户上传」),后台的补全任务照旧接手。

    check(url, note, today, clf, db) → {
        "url", "final_url", "domain", "title", "published",
        "source": {"kind", "label", "level", "known"},            # 网站类型(规则)+ 是不是库里已有的信源
        "analysis": {...} | None,                                  # 模型的分析(没配模型或出错时为 None)
        "same_url": [线索], "related_leads": [线索], "related_opps": [商机],
        "outside": [{"kind", "title", "url", "date"}],            # 公开渠道的旁证(世行检索、GDELT 近三个月新闻)
        "advice": {"action": 入库 | 先核实 | 不入库 | 已在库, "reason"},
        "draft": {...},                                            # 页面「入库」按钮要写的值
        "notes": [...],                                            # 哪一步没做成
    }

安全:只抓公网地址(每次请求、包括跳转,都先解析主机,内网、本机、保留地址一律拒绝),限 8 MB、25 秒。
"""
from __future__ import annotations

import html
import ipaddress
import json
import re
import socket
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from itertools import islice
from urllib.parse import urlparse

import httpx

from core import geo
from core.review import material_of
from core.file_text import FileError, file_text
from core.storage import USER_AGENT
from core.watch.fetch import Page
from core.watch.run import doc_text
from core.watch.text import parse_date

URL_RE = re.compile(r"(?i:https?)://[^\s<>\"'\u0080-\U0010ffff]+")  # 网址只认 ASCII(浏览器复制出来的都是编码过的),后面紧跟的中文不算;不整体忽略大小写:那样 s、k 会被当成 ſ、K 排除
MAX_BYTES = 8_000_000
TIMEOUT = 25.0
DOC_CHARS = 6_000  # 给模型看的正文长度
UPLOAD_CHARS = 12_000  # 用户上传的文件多给模型看一些(清单型的材料一页就是七八条)
ITEM_MAX = 15  # 清单型材料最多逐条看几条
ITEM_WORKERS = 4  # 逐条找原公告、查库时并行几条
PASTED_MIN = 200  # 链接后面跟着这么多字,算是用户贴来的正文(网站不让程序读时用它)

# 网站类型:按域名后缀认。官方与开发银行的公告可信度高;新闻媒体看是不是转述;其余(企业、展会、博客、聚合站)先核实
MDB = {
    "worldbank.org": "世界银行", "adb.org": "亚洲开发银行", "aiib.org": "亚投行", "ebrd.com": "欧洲复兴开发银行",
    "isdb.org": "伊斯兰开发银行", "afdb.org": "非洲开发银行", "iadb.org": "美洲开发银行", "eib.org": "欧洲投资银行",
    "opecfund.org": "欧佩克基金", "undp.org": "联合国开发署", "ungm.org": "联合国采购网", "kfw.de": "德国复兴信贷银行",
    "afd.fr": "法国开发署", "jica.go.jp": "日本国际协力机构", "ndb.int": "新开发银行", "sfd.gov.sa": "沙特发展基金",
    "kuwait-fund.org": "科威特基金", "adfd.ae": "阿布扎比发展基金",
}
MEDIA = {
    "reuters.com": "路透社", "bloomberg.com": "彭博社", "ft.com": "金融时报", "meed.com": "MEED", "zawya.com": "Zawya",
    "agbi.com": "AGBI", "globalwaterintel.com": "Global Water Intelligence", "waterworld.com": "WaterWorld",
    "smartwatermagazine.com": "Smart Water Magazine", "devex.com": "Devex", "gazeta.uz": "Gazeta.uz", "kun.uz": "Kun.uz",
    "uza.uz": "乌兹别克国家通讯社", "daryo.uz": "Daryo", "trend.az": "Trend", "apa.az": "APA", "report.az": "Report.az",
    "arabnews.com": "Arab News", "thenationalnews.com": "The National", "argaam.com": "Argaam", "spa.gov.sa": "沙特通讯社",
    "wam.ae": "阿联酋通讯社", "ahram.org.eg": "金字塔报", "xinhuanet.com": "新华网", "news.cn": "新华网",
}
GOV = re.compile(r"(^|\.)(gov|mil|int)$|(^|\.)(gov|gob|gouv|go|govt|gv|mil)\.[a-z]{2}$|(^|\.)admin\.ch$")
LEVEL = {"官方": "高", "开发银行与国际组织": "高", "新闻媒体": "中", "其他网站": "待核实"}

ACTIONS = ("入库", "先核实", "不入库")
STAGES = ("立项或规划", "融资", "招标", "授标", "在建", "建成", "搁置后重启", "其他")
CATEGORIES = ("招标", "意向书", "资格预审", "总采购公告", "特定采购公告", "授标或短名单", "融资", "立项或规划", "项目动态", "政策", "其他")
SECTORS = ("供水", "污水", "水利", "海水淡化", "雨水排涝", "水环境", "其他")

GUIDE = """
输出格式:只输出一个 JSON 对象,不要任何其他文字。字段:
- "title_zh":原文标题的中文译名(一行)
- "summary_zh":中文摘要,两三句,写清项目是什么、在哪、规模、谁出钱或谁做、进展到哪一步
- "project_name":项目的中文名称(简短);"names":原文里出现的项目名、地名、机构名的原文写法(数组,3~6 个,每个至少 4 个字母,用来在库里搜同一项目)
- "country":项目所在国的 ISO3;"city":城市或地区(原文写法)
- "funder":出资方 / 投资方(原文怎么写就怎么写,注明国别);"amount":金额或规模(原文写法,如 1.5 million m3/day、USD 200 million)
- "info_date":这篇报道或公告本身的日期 YYYY-MM-DD,看不出来写 null
- "stage":进展,取 立项或规划 / 融资 / 招标 / 授标 / 在建 / 建成 / 搁置后重启 / 其他
- "category":取 招标 / 意向书 / 资格预审 / 总采购公告 / 特定采购公告 / 授标或短名单 / 融资 / 立项或规划 / 项目动态 / 政策 / 其他
- "sectors":领域数组,取 供水 / 污水 / 水利 / 海水淡化 / 雨水排涝 / 水环境 / 其他
- "credibility":{"level": "高" | "中" | "低", "reason": 一句话:谁发的、是一手还是转述(转述的写出原始出处)、有没有具体的人名机构数字}
- "freshness":一句话说时效:信息是哪年的、现在可能到了哪一步、要不要查后续
- "mismatch":用户附言和原文对不上的地方(如出资方、规模、地点),一句话;都对得上写 ""
- "news_query":用来检索后续新闻的英文检索式(2~4 个英文词,项目专名放引号里,如 "Surum" Tashkent wastewater)
- "advice":{"action": "入库" | "先核实" | "不入库", "reason": 一句话}。和水务(供水、污水、水利、海水淡化、排涝、水环境)无关的写不入库
- "verify":要核实的要点,数组,最多 3 条,每条一句
- "deadline":这篇公告的投标(或意向书、资格预审)截止日 YYYY-MM-DD,没写的写 null;"jv":公告对联合体的说法,取 允许 / 不允许,没写的 null
- "pros":对投标人有利的因素(资金已落实、采购方式开放、规模与我方匹配等),数组,最多 4 条;"cons":不利因素与风险(工期、本地化要求、付款、竞争等),数组,最多 4 条;都只写原文有依据的
- "question":用户附言里问了什么:{"kind": "资格对照" | "其他" | null, "text": 问题原话}。问「我们的业绩 / 资质能不能满足」「有没有资格投」这类写 资格对照;
  附言只是说明来源、没有问问题写 null
- "requirements":这篇公告写的投标人资格门槛(没写的字段填 null):{"similar_usd": 单个类似合同最低金额(美元,数字), "turnover_usd": 年均营业额最低(美元,数字),
  "financial_usd": 财务资源 / 流动资金最低(美元,数字), "years": 类似业绩要在近几年内(数字), "specific": 特定资格(工程量、人员、设备)一句话}。
  欧元等其他币种按原文给的汇率或大致汇率折成美元
- "items":这份材料如果是几条互不相同的招标或项目的清单(同事整理的商机清单、招标监测、月报里的项目表),逐条列出,最多 15 条;
  只讲一个项目的材料写 []。每条:{"ref": 公告或标段编号(原文写法,如 NRWWP-4;没有写 null), "title": 原文名称, "title_zh": 中文名,
  "country": ISO3, "funder": 出资方, "amount": 金额或规模(原文写法), "stage": 进展(取值同上), "deadline": 投标截止日 YYYY-MM-DD(没有写 null),
  "url": 材料里给出的这一条的网址(没有写 null), "query": 到世界银行等采购网站找这条原公告用的英文检索词(编号加 2~3 个项目词),
  "names": 在库里搜同一项目用的原文词(2~4 个), "sectors": 这一条的领域数组(取值同上;兽医站、学校、道路这类跟水务无关的写 ["其他"]),
  "requirements": 这一条的资格门槛,结构同上面的 requirements(材料里没写的填 null)}
"""

_S = {"type": "string"}
_N = {"anyOf": [{"type": "string"}, {"type": "null"}]}
_NUM = {"anyOf": [{"type": "number"}, {"type": "null"}]}
_REQ = {"anyOf": [{"type": "object", "properties": {"similar_usd": _NUM, "turnover_usd": _NUM, "financial_usd": _NUM, "years": _NUM, "specific": _N},
                   "required": ["similar_usd", "turnover_usd", "financial_usd", "years", "specific"], "additionalProperties": False}, {"type": "null"}]}
QUESTION_KINDS = ("资格对照", "其他")
SCHEMA = {
    "type": "object",
    "properties": {
        "title_zh": _S, "summary_zh": _S, "project_name": _N, "names": {"type": "array", "items": _S}, "country": _N, "city": _N,
        "funder": _N, "amount": _N, "info_date": _N, "stage": {"type": "string", "enum": list(STAGES)},
        "category": {"type": "string", "enum": list(CATEGORIES)}, "sectors": {"type": "array", "items": {"type": "string", "enum": list(SECTORS)}},
        "credibility": {"type": "object", "properties": {"level": {"type": "string", "enum": ["高", "中", "低"]}, "reason": _S},
                        "required": ["level", "reason"], "additionalProperties": False},
        "freshness": _S, "mismatch": _S, "news_query": _N,
        "advice": {"type": "object", "properties": {"action": {"type": "string", "enum": list(ACTIONS)}, "reason": _S},
                   "required": ["action", "reason"], "additionalProperties": False},
        "verify": {"type": "array", "items": _S},
        "deadline": _N, "jv": {"anyOf": [{"type": "string", "enum": ["允许", "不允许"]}, {"type": "null"}]},
        "pros": {"type": "array", "items": _S}, "cons": {"type": "array", "items": _S},
        "question": {"anyOf": [{"type": "object", "properties": {"kind": _N, "text": _N}, "required": ["kind", "text"], "additionalProperties": False}, {"type": "null"}]},
        "requirements": _REQ,
        "items": {"type": "array", "items": {
            "type": "object",
            "properties": {"ref": _N, "title": _S, "title_zh": _S, "country": _N, "funder": _N, "amount": _N, "stage": _N, "deadline": _N,
                           "url": _N, "query": _N, "names": {"type": "array", "items": _S}, "sectors": {"type": "array", "items": _S}, "requirements": _REQ},
            "required": ["ref", "title", "title_zh", "country", "funder", "amount", "stage", "deadline", "url", "query", "names", "sectors", "requirements"],
            "additionalProperties": False,
        }},
    },
    "additionalProperties": False,
}
SCHEMA["required"] = list(SCHEMA["properties"])


class LinkError(ValueError):
    """网址不对、抓不到、不让抓:原因给页面直接显示。"""


def find_url(text: str | None) -> str | None:
    m = URL_RE.search(text or "")
    return m.group(0).rstrip(".,;:!?") if m else None


def _host_ok(host: str | None) -> None:
    """主机必须解析到公网地址:挡住内网服务(表格、数据库、本机)和云主机的元数据地址。"""
    if not host:
        raise LinkError("网址里没有主机名")
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError) as e:
        raise LinkError(f"找不到这个网站:{host}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global or ip.is_multicast:
            raise LinkError("只能看公网上的网页")


def fetch_page(url: str, transport: httpx.BaseTransport | None = None, check_host=_host_ok) -> Page:
    """抓一个公网网页(跟随跳转,每一跳都查主机),限大小与时间。"""
    def guard(req: httpx.Request) -> None:
        if req.url.scheme not in ("http", "https"):
            raise LinkError("网址要以 http:// 或 https:// 开头")
        check_host(req.url.host)
    try:
        with httpx.Client(follow_redirects=True, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}, transport=transport,
                          event_hooks={"request": [guard]}, max_redirects=5) as c, c.stream("GET", url) as r:
            if r.status_code >= 400:
                raise LinkError(f"网站回了 {r.status_code}" + (",可能不让程序访问;可以把链接和正文一起贴给我" if r.status_code in (401, 403, 429) else ""))
            buf = bytearray()
            for chunk in r.iter_bytes():
                buf += chunk
                if len(buf) > MAX_BYTES:
                    raise LinkError("网页太大(超过 8 MB)")
            return Page(str(r.url), r.status_code, bytes(buf), r.headers.get("content-type", ""))
    except httpx.TimeoutException as e:
        raise LinkError("网站太久没有回应") from e
    except httpx.HTTPError as e:
        raise LinkError(f"打不开这个网址:{type(e).__name__}") from e


def _domain(url: str) -> str:
    h = (urlparse(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def _suffix_in(host: str, table: dict[str, str]) -> str | None:
    return next((v for k, v in table.items() if host == k or host.endswith("." + k)), None)


def source_kind(url: str, known: dict[str, str] | None = None) -> dict:
    """网站类型(按域名)→ {kind, label, level, known}:known 是库里「信源」表已有的信源名(没有为 "")。"""
    host = _domain(url)
    known_name = next((n for d, n in (known or {}).items() if d and (host == d or host.endswith("." + d))), "")
    if (name := _suffix_in(host, MDB)):
        kind, label = "开发银行与国际组织", name
    elif GOV.search(host):
        kind, label = "官方", "政府或公共机构网站"
    elif (name := _suffix_in(host, MEDIA)):
        kind, label = "新闻媒体", name
    else:
        kind, label = "其他网站", "企业、展会、行业协会或转载站"
    return {"kind": kind, "label": label, "level": LEVEL[kind], "known": known_name, "domain": host}


def _title_of(page: Page, body: str) -> str:
    from core.watch.manual import _title_of as t
    try:
        return t(page, body)
    except Exception:  # noqa: BLE001  标题取不到不影响
        return next((ln for ln in body.splitlines() if len(ln.strip()) >= 8), page.url)[:300]


def _published(page: Page) -> str | None:
    """网页上写的发布日期(meta 标签);取不到为 None。"""
    m = re.search(rb'(?:article:published_time|datePublished|pubdate|publish-date)["\']?\s*(?:content|:)\s*=?\s*["\']([^"\']{8,30})', page.content[:300_000], re.IGNORECASE)
    if not m:
        return None
    d = parse_date(m.group(1).decode("ascii", "ignore"))
    return d.isoformat() if isinstance(d, date) else (str(d) if d else None)


def system_prompt(today: date) -> str:
    return (f"你是水务项目情报分析员,今天是 {today.isoformat()}。用户从别的渠道找到一条网页,贴给你看。"
            "根据网页正文判断它讲的是什么水务项目、信源靠不靠得住、信息新不新,给出要不要收进线索库的建议。"
            "只依据正文和用户附言,不要编造正文里没有的事实;正文没写的字段填 null 或空。用中文写。")


def _clean(out: dict | None) -> dict | None:
    if not out or "_error" in out:
        return None
    s = lambda k, n=300: (str(out.get(k) or "").strip()[:n] or None)  # noqa: E731
    cred = out.get("credibility") if isinstance(out.get("credibility"), dict) else {}
    adv = out.get("advice") if isinstance(out.get("advice"), dict) else {}
    lst = lambda k: [str(x).strip() for x in (out.get(k) or []) if str(x).strip()] if isinstance(out.get(k), list) else []  # noqa: E731
    iso = geo.iso3(out.get("country")) if out.get("country") else None
    info = out.get("info_date")
    info = info if isinstance(info, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", info) else None
    return {
        "title_zh": s("title_zh"), "summary_zh": s("summary_zh", 600), "project_name": s("project_name", 120),
        "names": [n for n in lst("names") if len(re.sub(r"\W", "", n)) >= 4][:6], "country": iso, "city": s("city", 80),
        "funder": s("funder", 200), "amount": s("amount", 120), "info_date": info,
        "stage": out.get("stage") if out.get("stage") in STAGES else None,
        "category": out.get("category") if out.get("category") in CATEGORIES else None,
        "sectors": [x for x in lst("sectors") if x in SECTORS],
        "credibility": {"level": cred.get("level") if cred.get("level") in ("高", "中", "低") else None, "reason": str(cred.get("reason") or "")[:300]},
        "freshness": s("freshness"), "mismatch": s("mismatch"), "news_query": s("news_query", 120),
        "advice": {"action": adv.get("action") if adv.get("action") in ACTIONS else None, "reason": str(adv.get("reason") or "")[:300]},
        "verify": lst("verify")[:3], "model": out.get("_model"),
        "deadline": out.get("deadline") if isinstance(out.get("deadline"), str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", out.get("deadline")) else None,
        "jv": out.get("jv") if out.get("jv") in ("允许", "不允许") else None, "pros": lst("pros")[:4], "cons": lst("cons")[:4],
        "question": _clean_question(out.get("question")), "requirements": _clean_req(out.get("requirements")),
        "items": [it for it in (_clean_item(x) for x in (out.get("items") or []) if isinstance(x, dict)) if it][:ITEM_MAX] if isinstance(out.get("items"), list) else [],
    }


def _clean_question(q: object) -> dict | None:
    if not isinstance(q, dict) or q.get("kind") not in QUESTION_KINDS:
        return None
    return {"kind": q["kind"], "text": str(q.get("text") or "").strip()[:200]}


def _clean_req(r: object) -> dict | None:
    """资格门槛:四个数字(美元 / 年)+ 一句特定资格;全空 → None。"""
    if not isinstance(r, dict):
        return None
    out = {}
    for k in ("similar_usd", "turnover_usd", "financial_usd", "years"):
        v = r.get(k)
        try:
            out[k] = float(v) if v is not None and float(v) > 0 else None
        except (TypeError, ValueError):
            out[k] = None
    out["specific"] = str(r.get("specific") or "").strip()[:300] or None
    return out if any(out.values()) else None


def _clean_item(x: dict) -> dict | None:
    """清单里的一条(模型给的)→ 归一;没有名称的丢掉。"""
    s = lambda k, n=200: (str(x.get(k) or "").strip()[:n] or None)  # noqa: E731
    if not s("title") and not s("title_zh"):
        return None
    dl = x.get("deadline")
    url = s("url", 500)
    names = [str(n).strip() for n in (x.get("names") or []) if str(n).strip()] if isinstance(x.get("names"), list) else []
    return {"ref": s("ref", 40), "title": s("title", 300) or s("title_zh"), "title_zh": s("title_zh") or s("title", 300),
            "country": geo.iso3(x.get("country")) if x.get("country") else None, "funder": s("funder"), "amount": s("amount", 120),
            "stage": x.get("stage") if x.get("stage") in STAGES else None,
            "deadline": dl if isinstance(dl, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", dl) else None,
            "url": url if url and URL_RE.fullmatch(url) else None, "query": s("query", 120),
            "names": [n for n in names if len(re.sub(r"\W", "", n)) >= 4][:4],
            "sectors": [v for v in (x.get("sectors") if isinstance(x.get("sectors"), list) else []) if v in SECTORS], "requirements": _clean_req(x.get("requirements"))}


# ---------- 库里找 ----------

LEAD_PROPS = ["key", "title", "url", "country", "found_at", "summary_zh", "project_name", "opportunity", "status"]


def _lead_row(db, r: dict) -> dict:
    g = lambda p: db.get("lead", r, p)  # noqa: E731
    link = lambda v: (v[0] if isinstance(v, list) and v else v) if v else None  # noqa: E731
    opp = link(g("opportunity"))
    return {"id": r.get("id"), "key": g("key"), "title": g("title"), "url": g("url"), "summary_zh": g("summary_zh"),
            "project_name": g("project_name"), "found_at": str(g("found_at") or "")[:10], "status": g("status"),
            "country": (link(g("country")) or {}).get("title") if isinstance(link(g("country")), dict) else None,
            "opportunity": opp.get("title") if isinstance(opp, dict) else None, "opportunity_id": opp.get("id") if isinstance(opp, dict) else None}


def _take(db, oid: str, flt: dict, props: list[str], n: int) -> list[dict]:
    proj = [db.fid(oid, p) for p in props if db.has(oid, p)]
    return list(islice(db.api.iter_records(db.t[oid], filter=flt, projection=proj, take=n), n))


def search_library(db, urls: list[str], words: list[str], iso: str | None) -> dict:
    """同一网址的线索;标题、中文摘要或项目名称含任一检索词的线索(有国别时限本国);名称、原文名称、别名含检索词的商机。"""
    out: dict = {"same_url": [], "related_leads": [], "related_opps": []}
    urls = [u for u in dict.fromkeys(urls) if u]
    if urls:
        flt = db.filter(*[db.cond("lead", "url", "is", u) for u in urls], conjunction="or")
        out["same_url"] = [_lead_row(db, r) for r in _take(db, "lead", flt, LEAD_PROPS, 5)]
    if not words:
        return out
    crid = None
    if iso:
        rows = _take(db, "country", db.filter(db.cond("country", "key", "is", iso)), ["key"], 1)
        crid = rows[0]["id"] if rows else None
    text = [db.cond("lead", p, "contains", w) for w in words for p in ("title", "summary_zh", "project_name") if db.has("lead", p)]
    conds = [db.filter(*text, conjunction="or")] + ([db.cond("lead", "country", "isAnyOf", [crid])] if crid else [])
    same = {r["id"] for r in out["same_url"]}
    out["related_leads"] = [_lead_row(db, r) for r in _take(db, "lead", db.filter(*conds), LEAD_PROPS, 12) if r.get("id") not in same][:10]
    oprops = [p for p in ("name_zh", "name_local", "aliases") if db.has("opportunity", p)]
    if oprops:
        otext = [db.cond("opportunity", p, "contains", w) for w in words for p in oprops]
        oconds = [db.filter(*otext, conjunction="or")] + ([db.cond("opportunity", "country", "isAnyOf", [crid])] if crid else [])
        for r in _take(db, "opportunity", db.filter(*oconds), ["code", "name_zh", "stage"], 5):
            g = lambda p: db.get("opportunity", r, p) if db.has("opportunity", p) else None  # noqa: E731
            out["related_opps"].append({"id": r.get("id"), "code": g("code"), "name_zh": g("name_zh"), "stage": g("stage")})
    return out


def known_sources(db) -> dict[str, str]:
    """「信源」表里有网址的信源:域名 → 信源名。"""
    out = {}
    for r in db.rows("source", props=[p for p in ("name", "url") if db.has("source", p)]):
        u = db.get("source", r, "url")
        if u:
            out.setdefault(_domain(str(u)), str(db.get("source", r, "name") or ""))
    return out


# ---------- 公开渠道的旁证 ----------

GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"


def search_outside(query: str | None, iso: str | None, get=None) -> tuple[list[dict], list[str]]:
    """世行采购公告与项目(按检索式和国别)、GDELT 近三个月的新闻:各取几条,出错只记一句,不影响其余。"""
    if not query:
        return [], []
    get = get or (lambda u, params: httpx.get(u, params=params, timeout=15, headers={"User-Agent": USER_AGENT}))
    found: list[dict] = []
    notes: list[str] = []

    def wb():
        from core.watch.manual import search_worldbank
        return [{"kind": x["kind"], "title": x["title"], "url": x["url"], "date": str(x["date"] or "")[:10]} for x in search_worldbank(query.replace('"', ""), iso, rows=5)][:5]

    def gd():
        r = get(GDELT, {"query": query, "mode": "artlist", "format": "json", "maxrecords": 8, "timespan": "3m", "sort": "datedesc"})
        data = json.loads(r.text or "{}") if r.status_code == 200 and r.text.strip().startswith("{") else {}
        return [{"kind": "新闻(近三个月)", "title": a.get("title") or a["url"], "url": a["url"], "date": str(parse_date(a.get("seendate")) or "")[:10]}
                for a in data.get("articles") or [] if a.get("url")][:6]

    with ThreadPoolExecutor(2) as ex:
        futs = {"世行检索": ex.submit(wb), "新闻检索": ex.submit(gd)}
        for name, f in futs.items():
            try:
                found += f.result(timeout=25)
            except Exception as e:  # noqa: BLE001
                notes.append(f"{name}没做成({type(e).__name__})")
    return found, notes



# ---------- 清单型材料:逐条找原公告、查库 ----------

ITEM_STATUS = ("已在库", "建议入库", "先核实", "非水务")
WATER = frozenset(SECTORS) - {"其他"}
SCAN_ROWS = 500  # 核「在不在世行采购网」:整国最近这么多条公告按正文比对(2026-10-16 使用方:先核实的要助手自己核,不能只让人选)
SCAN_KEEP = 3600  # 整国公告列表的进程内缓存(秒)
_scan_cache: dict[str, tuple[float, list[dict]]] = {}
# 出资方的招标页检索链接(服务器抓不到这些脚本页,给人一个点开就是检索结果的链接)
FUNDER_LINKS = {
    "亚行": "https://www.adb.org/projects/tenders?keywords={q}",
    "法开署": "https://afd.dgmarket.com/tenders/list.do?keywords={q}",
    "欧洲复兴开发银行": "https://www.ebrd.com/work-with-us/procurement/project-procurement.html?keywords={q}",
    "世行": "https://projects.worldbank.org/en/projects-operations/procurement?srce=both&qterm={q}",
}
SEARCH_LINK = "https://www.bing.com/search?q=%22{q}%22"


def _norm(text: str | None) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", (text or "").lower())


def search_notice(query: str | None, iso: str | None) -> list[dict]:
    """到世界银行采购公告与项目里找一条原公告(缺省的找法;别的渠道以后加在这里)→ [{kind, title, url, date, deadline, text}]。"""
    if not query:
        return []
    from core.watch.manual import search_worldbank
    return [{"kind": x["kind"], "title": x["title"], "url": x["url"], "date": str(x["date"] or "")[:10],
             "deadline": str(x.get("deadline") or "")[:10] or None, "text": x.get("text") or ""} for x in search_worldbank(query.replace('"', ""), iso, rows=8)]


def _ref_in(ref: str | None, *texts: str | None) -> bool:
    r = _norm(ref)
    return bool(r) and len(r) >= 4 and any(r in _norm(t) for t in texts)


def _name_in(names: list[str], *texts: str | None) -> bool:
    blob = " ".join(_norm(t) for t in texts)
    return any(_norm(n) and _norm(n) in blob for n in names)


def funder_guess(item: dict) -> str | None:
    """清单写的出资方 + 编号、名称里的线索 → 亚行 / 法开署 / 欧洲复兴开发银行 / 世行。「贷款 4207-UZB」这种四位数-国别码是亚行的贷款号(清单常误写成世行)。"""
    blob = " ".join(str(item.get(k) or "") for k in ("funder", "title", "title_zh", "ref"))
    if re.search(r"\b\d{4}-[A-Z]{3}\b", blob) or re.search(r"\bADB\b|Asian Development|亚行|亚洲开发银行", blob):
        return "亚行"
    if re.search(r"\bAFD\b|法开署|法国开发署|Agence Fran", blob):
        return "法开署"
    if re.search(r"\bEBRD\b|欧洲复兴", blob):
        return "欧洲复兴开发银行"
    if re.search(r"\bIDA\b|\bIBRD\b|World Bank|世行|世界银行", blob):
        return "世行"
    return None


def wb_scan(iso: str | None) -> list[dict]:
    """一国最近 SCAN_ROWS 条世行采购公告(进程内缓存一小时)。"""
    import time
    if not iso:
        return []
    hit = _scan_cache.get(iso)
    if hit and time.time() - hit[0] < SCAN_KEEP:
        return hit[1]
    from core.watch.manual import wb_country_notices
    rows = wb_country_notices(iso, rows=SCAN_ROWS)
    _scan_cache[iso] = (time.time(), rows)
    return rows


def verify_outside(item: dict, iso: str | None, scan=wb_scan, outside=search_outside) -> dict:
    """没找到原公告的那条,助手再核一遍:整国世行公告按正文比对编号与名字;新闻里找旁证;猜出资方,给可点开的检索链接。
    → {original, sure, candidates, wb_scanned, wb_since, news, funder, links, notes}"""
    ref, names = item.get("ref"), item.get("names") or []
    out: dict = {"original": None, "sure": False, "candidates": [], "wb_scanned": 0, "wb_since": None, "news": [], "funder": funder_guess(item),
                 "links": [], "notes": []}
    try:
        rows = scan(iso)
        out["wb_scanned"] = len(rows)
        out["wb_since"] = min((str(r.get("date") or "")[:10] for r in rows if r.get("date")), default=None)
        exact = [r for r in rows if _ref_in(ref, r.get("title"), r.get("text"))]
        close = [r for r in rows if r not in exact and _name_in(names, r.get("title"), r.get("text"))]
        if exact:
            out["original"], out["sure"] = {k: exact[0].get(k) for k in ("kind", "title", "url", "date", "deadline")}, True
        elif close:
            out["original"], out["candidates"] = {k: close[0].get(k) for k in ("kind", "title", "url", "date", "deadline")}, [{k: c.get(k) for k in ("kind", "title", "url", "date", "deadline")} for c in close[:2]]
    except Exception as e:  # noqa: BLE001
        out["notes"].append(f"整国核对世行公告没做成({type(e).__name__})")
    if not out["sure"]:
        try:
            found, more = outside(item.get("query") or " ".join([w for w in [ref, *names] if w][:4]), iso)
            out["news"] = [f for f in found if str(f.get("kind") or "").startswith("新闻")][:4]
            out["notes"] += more
        except Exception as e:  # noqa: BLE001
            out["notes"].append(f"新闻检索没做成({type(e).__name__})")
        q = ref or (names[0] if names else "") or str(item.get("title") or "")[:60]
        from urllib.parse import quote
        links = []
        if out["funder"] and out["funder"] in FUNDER_LINKS:
            links.append({"name": f"到{out['funder']}招标页找", "url": FUNDER_LINKS[out["funder"]].format(q=quote(q))})
        if out["funder"] != "世行":
            links.append({"name": "到世行采购网找", "url": FUNDER_LINKS["世行"].format(q=quote(q))})
        links.append({"name": "搜索引擎查这个编号", "url": SEARCH_LINK.format(q=quote(q))})
        out["links"] = links
    return out


def check_item(item: dict, db, today: date, find_original=search_notice, verify=verify_outside, scope=None, records: dict | None = None) -> dict:
    """清单里的一条:跟水务无关的直接判「非水务」;库里有没有(编号对得上就算同一条;同项目别的标段只列作相关)、原公告找到没有;
    没找到的再核一遍(verify:整国世行公告、新闻、出资方链接),给入库建议和要写的值。"""
    ref, names, iso = item.get("ref"), item.get("names") or [], item.get("country")
    words = [w for w in [ref, *names] if w]
    base = {**{k: item.get(k) for k in ("ref", "title", "title_zh", "funder", "amount", "stage", "deadline", "sectors", "requirements")},
            "country": {"iso3": iso, "name": geo.name_zh(iso)} if iso else None, "original": None, "sure": False, "candidates": [], "in_library": [],
            "related": [], "checked": None, "draft": None, "notes": []}
    if item.get("sectors") and not (set(item["sectors"]) & WATER):
        out = {**base, "status": "非水务", "why": f"不是水务项目(领域:{'、'.join(item['sectors'])}),不入库。"}
        return {**out, "bid": item_bid(out, today, scope, records)}
    lib = {"same_url": [], "related_leads": [], "related_opps": []}
    notes: list[str] = []
    if db is not None:
        try:
            lib = search_library(db, [item["url"]] if item.get("url") else [], words, iso)
        except Exception as e:  # noqa: BLE001
            notes.append(f"查库出错({type(e).__name__})")
    have = lib["same_url"] + [r for r in lib["related_leads"] if _ref_in(ref, r.get("title"), r.get("url"), r.get("project_name"), r.get("summary_zh"))]
    related = [r for r in lib["related_leads"] if r not in have][:3]
    original, candidates, sure = None, [], False
    if item.get("url"):
        original, sure = {"kind": source_kind(item["url"])["label"], "title": item["title"], "url": item["url"], "date": None, "deadline": None}, True
    elif not have:
        try:
            hits = find_original(item.get("query") or " ".join(words[:4]), iso)
        except Exception as e:  # noqa: BLE001
            hits, notes = [], notes + [f"找原公告出错({type(e).__name__})"]
        exact = [h for h in hits if _ref_in(ref, h.get("title"), h.get("text"))]
        close = [h for h in hits if h not in exact and _name_in(names, h.get("title"), h.get("text"))]
        if exact:
            original, sure = exact[0], True
        elif close:
            original, candidates = close[0], close[:2]
    checked = None
    if not have and not sure and verify is not None:  # 没找到:助手自己再核一遍,不把判断推给人
        try:
            checked = verify(item, iso)
        except Exception as e:  # noqa: BLE001
            checked, notes = None, notes + [f"再核一遍出错({type(e).__name__})"]
        if checked and checked.get("sure"):
            original, sure, candidates = checked["original"], True, []
        elif checked and checked.get("original") and not original:
            original, candidates = checked["original"], checked.get("candidates") or []
    if have:
        status, why = "已在库", "库里已有这一条(编号对得上)" + (f",已归入商机「{have[0]['opportunity']}」" if have[0].get("opportunity") else "") + "。"
    elif original and sure:
        status, why = "建议入库", f"找到原公告({original['kind']}),按原公告入库,信源可信。"
    else:
        status, why = "先核实", _checked_why(checked, original, iso)
    if related and status != "已在库":
        why += f" 库里有 {len(related)} 条同项目别的标段。"
    draft = {"title": (original["title"] if original and sure else item["title"])[:300], "origin": "用户上传", "summary_zh": item.get("title_zh"),
             "project_name": item.get("title_zh")}
    if original and sure:
        draft["url"] = original["url"]
    if iso:
        draft["country_iso3"] = iso
    if original and sure and original.get("date"):
        draft["published"] = original["date"]
    if item.get("funder"):
        draft["funder"] = item["funder"]
    if item.get("amount"):
        draft["amount"] = item["amount"]
    draft["note"] = (f"助手预判({today.isoformat()}):{status}。{why} 来自上传的清单"
                     + (f",编号 {ref}" if ref else "") + (f",截止 {item['deadline']}" if item.get("deadline") else "") + "。")[:2000]
    out = {**base, "status": status, "why": why, "original": original, "sure": sure, "candidates": candidates, "in_library": have[:3], "related": related,
           "checked": checked, "draft": draft, "notes": notes}
    return {**out, "bid": item_bid(out, today, scope, records)}


def item_bid(it: dict, today: date, scope, records: dict | None) -> dict:
    """清单里的一条 → 投标评估(core.bid_check.assess):信源按找到的原公告或清单自带的网址算,旁证取再核时找到的新闻。"""
    from core import bid_check
    url = (it["original"] or {}).get("url") if it.get("sure") and it.get("original") else None
    src = source_kind(url) if url else {"kind": "其他网站", "label": "清单里的一条(没找到原公告)", "level": "待核实", "known": "", "domain": ""}
    ck = it.get("checked") or {}
    facts = {"title": it.get("title_zh") or it.get("title"), "url": url, "country": (it.get("country") or {}).get("iso3"), "sectors": it.get("sectors") or [],
             "stage": it.get("stage"), "category": "招标" if it.get("deadline") else None, "deadline": it.get("deadline"), "requirements": it.get("requirements"),
             "credibility": {}, "source": src, "original": it.get("original"), "sure": it.get("sure"), "outside": ck.get("news") or [],
             "verify_links": ck.get("links") or [], "pros": [], "cons": []}
    lib = {"in_library": it.get("in_library") or [], "related": it.get("related") or [], "related_opps": []}
    return bid_check.assess(facts, today, records, scope, lib)


def _checked_why(checked: dict | None, original: dict | None, iso: str | None) -> str:
    """先核实那条的结论:助手核了什么、结果如何、下一步点哪里。"""
    if not checked:
        return "在世界银行采购网站没找到原公告,再核一遍也没做成;可点下面的链接到出资方网站找。" if original is None else "找到一条像是同一项目的公告,但编号对不上;对得上就按它入库。"
    parts = []
    if checked.get("wb_scanned"):
        parts.append(f"助手核过:世行采购网{geo.name_zh(iso) or ''}最近 {checked['wb_scanned']} 条公告(自 {checked.get('wb_since') or '?'})里没有这个编号")
    else:
        parts.append("助手核过:世行采购网关键词检索没找到" + ("(整国核对没做成)" if checked.get("notes") else ""))
    if original:
        parts.append("有一条像是同一项目的公告但编号对不上(下面列出)")
    parts.append(f"新闻里有 {len(checked['news'])} 条相关报道" if checked.get("news") else "近三个月新闻里没有相关报道")
    f = checked.get("funder")
    if f and f != "世行":
        parts.append(f"按编号、出资方看像是{f}的项目,服务器打不开{f}的网页,请点「到{f}招标页找」看原公告")
    elif f == "世行":
        parts.append("清单写的是世行项目但世行网上没有,可能是国内采购网的公告或清单有误")
    return ";".join(parts) + "。暂不入库;要收也可以点「仍收进」(信源记为待核实)。"


def check_list(items: list[dict], db, today: date, find_original=search_notice, step=lambda stage: None, verify=verify_outside, scope=None, records: dict | None = None) -> dict:
    """清单的每一条并行找原公告、查库、再核;进度写「找原公告 3/7」。"""
    import threading
    done, lock, n = 0, threading.Lock(), len(items)
    step(f"找原公告 0/{n}")

    def one(it: dict) -> dict:
        nonlocal done
        try:
            out = check_item(it, db, today, find_original, verify, scope, records)
        except Exception as e:  # noqa: BLE001
            out = {**check_item({**it, "url": None, "query": None, "names": [], "sectors": []}, None, today, lambda q, i: [], None, scope, records), "status": "先核实",
                   "why": f"这一条查找出错({type(e).__name__}),请人看。", "notes": [f"{type(e).__name__}: {e}"[:200]]}
        with lock:
            done += 1
            step(f"找原公告 {done}/{n}")
        return out

    with ThreadPoolExecutor(ITEM_WORKERS) as ex:
        rows = list(ex.map(one, items))
    counts = {k: sum(1 for r in rows if r["status"] == k) for k in ITEM_STATUS}
    from core.bid_check import VERDICTS
    verdicts = {v: sum(1 for r in rows if (r.get("bid") or {}).get("verdict") == v) for v in VERDICTS}
    return {"items": rows, "n": n, "counts": counts, "verdicts": verdicts}


# ---------- 资格对照:清单的门槛 vs 我方业绩(2026-10-16 使用方:附言问「我们的业绩能满足吗」,要按问题答,不能只给线索) ----------

_USD = re.compile(r"(折\s*)?([\d][\d,]*(?:\.\d+)?)\s*(亿|万)?\s*美元")
VERDICTS = ("满足", "不满足", "不确定")


def usd_of(text: object) -> float | None:
    """公司资料「金额」文字 → 美元数:「折 1511.5万美元」「1.2亿美元」「52万美元」;多段取「折」后面那个,没有「折」取第一个。"""
    ms = list(_USD.finditer(str(text or "")))
    if not ms:
        return None
    m = next((m for m in ms if m.group(1)), ms[0])
    return float(m.group(2).replace(",", "")) * {"亿": 1e8, "万": 1e4}.get(m.group(3) or "", 1)


def our_records(db) -> dict[str, list[dict]]:
    """公司资料里的业绩 → {"本单位": [...], "兄弟单位": [...]}(合作方不算);每条 {name, year, usd, amount, sectors}。
    兄弟单位的只能另列作参考,不能当本单位业绩(使用方 2026-10-15)。"""
    out: dict[str, list[dict]] = {"本单位": [], "兄弟单位": []}
    if db is None or "company" not in db.t:
        return out
    props = [p for p in ("name", "kind", "owner", "year", "amount", "sectors") if db.has("company", p)]
    for r in db.rows("company", props=props):
        g = lambda p: db.get("company", r, p) if p in props else None  # noqa: E731
        if str(g("kind") or "") != "业绩":
            continue
        owner = str(g("owner") or "本单位")
        if owner not in out:
            continue
        try:
            year = int(g("year")) if g("year") else None
        except (TypeError, ValueError):
            year = None
        out[owner].append({"id": r["id"], "name": str(g("name") or ""), "year": year, "usd": usd_of(g("amount")), "amount": str(g("amount") or ""),
                           "sectors": list(g("sectors") or [])})
    return out


def _wan(x: float) -> str:
    return f"{x / 1e4:,.0f} 万美元"


def qualify(req: dict | None, sectors: list[str], records: dict[str, list[dict]], today: date) -> dict:
    """一条公告的门槛 vs 我方业绩 → {verdict, why, records(本单位同领域前几条), siblings(兄弟单位够门槛的), gaps(系统答不了的)}。
    只比「单个类似合同金额」(有年限的只看年限内的;年份不详的算在内但注明);营业额、财务资源系统里没有,写明要财务部出数。"""
    want = {x for x in sectors if x in WATER}
    fit = lambda r: not want or not r["sectors"] or bool(set(r["sectors"]) & want)  # noqa: E731
    years = req.get("years") if req else None
    since = today.year - int(years) if years else None
    in_win = lambda r: since is None or r["year"] is None or r["year"] >= since  # noqa: E731
    ours = sorted((r for r in records.get("本单位") or [] if fit(r) and in_win(r) and r["usd"]), key=lambda r: -r["usd"])
    need = req.get("similar_usd") if req else None
    sibs = sorted((r for r in records.get("兄弟单位") or [] if fit(r) and in_win(r) and r["usd"] and (not need or r["usd"] >= need)), key=lambda r: -r["usd"])[:2]
    top = ours[:3]
    win = f"(近 {int(years)} 年内)" if years else ""
    yr = lambda r: f"({r['year']})" if r["year"] else "(年份不详,请核)"  # noqa: E731
    if not need:
        verdict = "不确定"
        why = "材料没写单个类似合同的金额门槛" + (f";我方同领域业绩最大的是「{top[0]['name']}」{_wan(top[0]['usd'])}{yr(top[0])}" if top else ";我方资料里没有同领域业绩") + "。"
    elif top and top[0]["usd"] >= need:
        verdict = "满足"
        why = f"类似合同要 {_wan(need)}以上{win},我方「{top[0]['name']}」{_wan(top[0]['usd'])}{yr(top[0])},够。"
    else:
        verdict = "不满足"
        why = (f"类似合同要 {_wan(need)}以上{win}" + (f",我方同领域最大的「{top[0]['name']}」只有 {_wan(top[0]['usd'])}{yr(top[0])}" if top else ",我方资料里没有同领域(或年限内)的业绩") + "。"
               + (f"兄弟单位有够门槛的(仅供参考,不能当本单位业绩):{'、'.join(r['name'] for r in sibs)}。" if sibs else ""))
    gaps = []
    if req and req.get("turnover_usd"):
        gaps.append(f"年均营业额要 {_wan(req['turnover_usd'])}:系统里没有营业额数据,要财务部出数")
    if req and req.get("financial_usd"):
        gaps.append(f"财务资源要 {_wan(req['financial_usd'])}:系统里没有,要财务部出数")
    if req and req.get("specific"):
        gaps.append(f"特定资格要对照招标文件:{req['specific']}")
    return {"verdict": verdict, "why": why, "records": top, "siblings": sibs, "gaps": gaps}


def answer_question(q: dict | None, a: dict | None, items: list[dict] | None, db, today: date) -> dict | None:
    """附言问「能不能满足」:清单逐条对照(非水务的不评),单篇按整篇的门槛对照 → {kind, text, summary, per_item, qualify?}。
    清单的对照结果写进每条的 qualify。"""
    if not q or q.get("kind") != "资格对照" or not a:
        return None
    records = our_records(db)
    n_ours = len(records["本单位"])
    if items:
        counts = {v: 0 for v in VERDICTS}
        for it in items:
            if it.get("status") == "非水务":
                it["qualify"] = None
                continue
            it["qualify"] = qualify(it.get("requirements"), it.get("sectors") or [], records, today)
            counts[it["qualify"]["verdict"]] += 1
        judged = sum(counts.values())
        summary = (f"按我方资料里 {n_ours} 条本单位业绩对照(只比单个类似合同金额):{judged} 条水务项目里,业绩够门槛的 {counts['满足']} 条、不够的 {counts['不满足']} 条、"
                   f"材料没写门槛的 {counts['不确定']} 条。营业额、财务资源系统里没有,要财务部出数;特定资格要对照招标文件。")
        return {"kind": q["kind"], "text": q.get("text") or "", "summary": summary, "per_item": True}
    qa = qualify(a.get("requirements"), a.get("sectors") or [], records, today)
    return {"kind": q["kind"], "text": q.get("text") or "", "summary": f"按我方资料里 {n_ours} 条本单位业绩对照:{qa['verdict']}。{qa['why']}", "per_item": False, "qualify": qa}


# ---------- 合起来 ----------

def _advice(src: dict, a: dict | None, lib: dict) -> dict:
    if lib["same_url"]:
        r = lib["same_url"][0]
        return {"action": "已在库", "reason": "库里已经有这个网址的线索" + (f",已归入商机「{r['opportunity']}」" if r.get("opportunity") else "") + "。"}
    if a and a["advice"]["action"]:
        act, why = a["advice"]["action"], a["advice"]["reason"]
    else:
        act = "先核实" if src["level"] != "高" else "入库"
        why = "没有模型分析,只按网站类型判断" + ("(官方或开发银行网站)" if src["level"] == "高" else ",建议先看原文再定") + "。"
    if act == "入库" and lib["related_leads"]:
        why = why if not why or why.endswith(("。", "!", "!", ".")) else why + "。"
        why += f"库里已有 {len(lib['related_leads'])} 条相关线索,入库后请挂到同一商机。"
    return {"action": act, "reason": why.strip()}


UPLOAD_SOURCE = {"kind": "其他网站", "label": "上传的文件(没给网址,信源要人看)", "level": "待核实", "known": "", "domain": ""}


def check(url: str, note: str, today: date, clf=None, db=None, *, fetch=fetch_page, outside=search_outside,
          upload: bytes | None = None, progress=None, find_original=search_notice, verify=verify_outside, scope=None) -> dict:
    """看一条链接;upload 是用户传来的文件(PDF、Word、截图):有文件就按文件看,不去抓网页,网址(可不给)只用来认信源、查库。
    progress(阶段名) 每进一步叫一次(助手接口用它告诉页面走到哪了);不给就不叫。
    材料是几条项目的清单(同事整理的商机清单、招标监测)时,逐条找原公告、查库里有没有,给每条的入库建议,由人决定(check_list)。
    scope 是市场国别范围(core.bid_check.Scope);单条和清单的每一条都附投标评估 bid(八项检查 → 建议投标 / 有条件可投 / 不建议投标)。"""
    step = progress or (lambda stage: None)
    url = (url or "").strip()
    notes: list[str] = []
    if upload is not None:
        step("读文件")
        try:
            body, how = file_text(upload)
        except FileError as e:
            raise LinkError(str(e)) from e
        url = url or find_url(body) or ""
        if url and not URL_RE.fullmatch(url):
            raise LinkError("这不是一个完整的网址")
        page = Page(url, 0, html.escape(body).encode(), "text/plain")
        note = (note or "")[:500]
        notes.append("按你上传的文件分析" + (f"({how})" if how else ""))
        body_ok = True
    else:
        if not URL_RE.fullmatch(url):
            raise LinkError("这不是一个完整的网址")
        body_ok = False
    pasted = (note or "").strip()
    if not body_ok:
        step("读原文")
        try:
            page = fetch(url)
            body, _ = doc_text(page)
            note = (note or "")[:500]  # 读到了网页,附言只是附言
        except LinkError:
            if len(pasted) < PASTED_MIN:
                raise
            page, body = None, ""
    if not body_ok and len(body.strip()) < 40:
        if len(pasted) < PASTED_MIN:
            raise LinkError("没有读到正文(网页可能要登录或靠脚本生成);可以把链接和正文一起贴给我")
        # 网站不让程序读、或读不到正文:按用户一起贴来的正文看(网址照旧用来认信源、查库)
        page = Page(url, 0, html.escape(pasted).encode(), "text/plain")
        body, note = pasted, ""
        notes.append("网站没让程序读到正文,按你贴的正文分析")
    title = _title_of(page, body)
    known: dict[str, str] = {}
    if db is not None:
        try:
            known = known_sources(db)
        except Exception as e:  # noqa: BLE001
            notes.append(f"读信源表出错({type(e).__name__})")
    src = source_kind(page.url, known) if page.url else dict(UPLOAD_SOURCE)
    if page.url and _domain(page.url) != _domain(url):
        src["redirected_from"] = _domain(url)
    a = None
    if clf is not None and getattr(clf, "enabled", False):
        step("模型分析")
        user = (f"用户附言:{note.strip() or '(无)'}\n网址:{page.url}\n网站:{src['domain']}({src['label']})\n网页标题:{title}\n\n正文:\n{body[:UPLOAD_CHARS if upload is not None else DOC_CHARS]}")
        out = clf.ask(system_prompt(today), user, SCHEMA, GUIDE)
        a = _clean(out)
        if a is None:
            notes.append("模型没有给出分析" + (f"({out['_error']})" if out and out.get("_error") else ""))
    else:
        notes.append("没有配置模型,只做了网站类型判断和库里比对")
    iso = (a or {}).get("country") or next(iter(geo.detect(f"{title}\n{body[:4000]}")), None)
    records = None
    if db is not None:
        try:
            records = our_records(db)
        except Exception as e:  # noqa: BLE001
            notes.append(f"读公司资料出错({type(e).__name__})")
    if a and len(a.get("items") or []) >= 2:  # 清单型材料:逐条看,不做整篇的查库、旁证
        for it in a["items"]:
            it["country"] = it["country"] or iso
        step("逐条评估")
        lst = check_list(a["items"], db, today, find_original, step, verify, scope, records)
        c = lst["counts"]
        adv = {"action": "逐条看", "reason": f"这是 {lst['n']} 条项目的清单:{c['已在库']} 条已在库,{c['建议入库']} 条找到了原公告、建议入库,"
                                            f"{c['先核实']} 条没找到原公告(助手已核过世行采购网与新闻,结论在每条下面),{c['非水务']} 条不是水务项目。"
                                            "建议入库的已勾上,点一下就收;没找到的暂不入库。"}
        v = lst["verdicts"]
        adv["reason"] = f"投标评估:建议投标 {v['建议投标']} 条、有条件可投 {v['有条件可投']} 条、不建议投标 {v['不建议投标']} 条。" + adv["reason"]
        notes.append("清单型材料:逐条找了原公告、查了库、逐条评估了能不能投")
        if (a.get("question") or {}).get("kind") == "资格对照":
            step("对照我方业绩")
        q = answer_question(a.get("question"), a, lst["items"], db, today)  # 附言问了「能不能满足」:先答问题,再给清单
        if q:
            adv["reason"] = q["summary"] + " " + adv["reason"]
        return {"ok": True, "url": url, "final_url": page.url, "domain": src["domain"], "title": title, "published": (a or {}).get("info_date") or _published(page),
                "country": {"iso3": iso, "name": geo.name_zh(iso)} if iso else None, "source": src, "analysis": a, "same_url": [], "related_leads": [],
                "related_opps": [], "outside": [], "advice": adv, "list": lst, "question": q, "draft": None, "notes": notes}
    words = (a or {}).get("names") or []
    lib = {"same_url": [], "related_leads": [], "related_opps": []}
    step("查库里相关")
    if db is not None:
        try:
            lib = search_library(db, [u for u in (url, page.url) if u], words, iso)
        except Exception as e:  # noqa: BLE001
            notes.append(f"查库出错({type(e).__name__})")
    step("查公开渠道")
    found, more = outside((a or {}).get("news_query"), iso)
    notes += more
    adv = _advice(src, a, lib)
    step("投标评估")
    bid, bid_facts = _bid(a, src, lib, found, iso, url, page.url, title, today, scope, records)
    pub = (a or {}).get("info_date") or _published(page)
    draft = {"title": title, "url": page.url if not src.get("redirected_from") else url, "origin": "用户上传"}
    if a:
        draft.update({k: v for k, v in {"summary_zh": a["summary_zh"], "project_name": a["project_name"], "funder": a["funder"],
                                        "amount": a["amount"], "category": a["category"], "sectors": a["sectors"] or None}.items() if v})
    if pub:
        draft["published"] = pub
    if iso:
        draft["country_iso3"] = iso
    draft["note"] = (f"助手预判({today.isoformat()}):{adv['action']}。{adv['reason']}"
                     + (f" 信源:{src['label']}" + (f",{a['credibility']['reason']}" if a and a['credibility']['reason'] else "") + "。")
                     + (f" 时效:{a['freshness']}" if a and a.get("freshness") else "")
                     + (f" 与附言不符:{a['mismatch']}" if a and a.get("mismatch") else "")
                     + (f" 用户附言:{note.strip()}" if note.strip() else ""))[:2000]
    return {"ok": True, "url": url, "final_url": page.url, "domain": src["domain"], "title": title, "published": pub,
            "country": {"iso3": iso, "name": geo.name_zh(iso)} if iso else None, "source": src, "analysis": a, **lib,
            "outside": found, "advice": adv, "bid": bid, "bid_facts": bid_facts, "material": material_of(url, page.url, upload),
            "question": answer_question((a or {}).get("question"), a, None, db, today), "draft": draft, "notes": notes}


def _bid(a: dict | None, src: dict, lib: dict, found: list[dict], iso: str | None, url: str, final_url: str, title: str, today: date, scope, records) -> tuple[dict, dict]:
    """单条链接或文件 → (投标评估(core.bid_check.assess), 评估依据 facts)。facts 随结果给页面,人点了复核按钮后按它重算(core.review.rebid)。
    原公告:官方或开发银行网站自己就是;媒体转述的看世行渠道里有没有对得上的。"""
    from core import bid_check
    a = a or {}
    names = a.get("names") or []
    wb = [x for x in found if not str(x.get("kind") or "").startswith("新闻") and _name_in(names, x.get("title"))]
    original, sure = None, False
    if src.get("level") == "高" and final_url:
        original, sure = {"kind": src.get("label"), "title": title, "url": final_url}, True
    elif wb:
        original, sure = {"kind": wb[0].get("kind"), "title": wb[0].get("title"), "url": wb[0].get("url")}, True
    facts = {"title": a.get("title_zh") or title, "url": final_url or url, "country": iso, "sectors": a.get("sectors") or [], "stage": a.get("stage"),
             "category": a.get("category"), "deadline": a.get("deadline"), "requirements": a.get("requirements"), "credibility": a.get("credibility") or {},
             "source": src, "original": original, "sure": sure, "outside": found, "verify_links": [], "pros": a.get("pros") or [], "cons": a.get("cons") or []}
    return bid_check.assess(facts, today, records, scope, lib), facts
