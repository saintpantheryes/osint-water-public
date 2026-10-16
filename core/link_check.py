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
from core.file_text import FileError, file_text
from core.storage import USER_AGENT
from core.watch.fetch import Page
from core.watch.run import doc_text
from core.watch.text import parse_date

URL_RE = re.compile(r"(?i:https?)://[^\s<>\"'\u0080-\U0010ffff]+")  # 网址只认 ASCII(浏览器复制出来的都是编码过的),后面紧跟的中文不算;不整体忽略大小写:那样 s、k 会被当成 ſ、K 排除
MAX_BYTES = 8_000_000
TIMEOUT = 25.0
DOC_CHARS = 6_000  # 给模型看的正文长度
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
"""

_S = {"type": "string"}
_N = {"anyOf": [{"type": "string"}, {"type": "null"}]}
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
    }


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
          upload: bytes | None = None, progress=None) -> dict:
    """看一条链接;upload 是用户传来的文件(PDF、Word、截图):有文件就按文件看,不去抓网页,网址(可不给)只用来认信源、查库。
    progress(阶段名) 每进一步叫一次(助手接口用它告诉页面走到哪了);不给就不叫。"""
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
        user = (f"用户附言:{note.strip() or '(无)'}\n网址:{page.url}\n网站:{src['domain']}({src['label']})\n网页标题:{title}\n\n正文:\n{body[:DOC_CHARS]}")
        out = clf.ask(system_prompt(today), user, SCHEMA, GUIDE)
        a = _clean(out)
        if a is None:
            notes.append("模型没有给出分析" + (f"({out['_error']})" if out and out.get("_error") else ""))
    else:
        notes.append("没有配置模型,只做了网站类型判断和库里比对")
    iso = (a or {}).get("country") or next(iter(geo.detect(f"{title}\n{body[:4000]}")), None)
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
            "outside": found, "advice": adv, "draft": draft, "notes": notes}
