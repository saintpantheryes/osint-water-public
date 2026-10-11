"""采集器:按信源类型复用,由 countries/{ISO3}/watch.yaml 的配置驱动。

每个采集器:fn(src: dict, ctx: Ctx) -> list[Item]。只取公开页面与官方接口;抓取纪律见 fetch.py。
类型:
- html_list      列表页(公告栏、招标表格):CSS 选择器取标题、链接、日期与其他字段,可按国别过滤
- rss            RSS/Atom
- wb_procnotices 世行采购公告接口(按世行国别码)
- wb_projects    世行项目接口(新项目与状态/日期/金额变化)
- page           单页变化监测(项目页、PPP 清单页):正文指纹变了就出一条"页面变化"
- links          任意网页的链接列表(用户添加的网站用):新出现的链接即新条目
- aiib           亚投行采购公告与项目清单(页面内嵌 JS 数据文件)
- ckan           CKAN 开放数据接口(美洲开发银行采购公告等)
- gdelt          GDELT 新闻接口(可选;限流即停)
- atexo_search   Atexo 公共采购平台(摩洛哥公共采购门户等)的在线检索:按活动领域取"在招"公告,要经页面表单回发
- iati           国际援助透明度(IATI)数据,经 d-portal 接口按发布机构与行业代码取项目(亚洲开发银行的项目管线等)
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from core.sources.http import SourceBlocked
from core.watch import fetch
from core.watch.store import Item, sha1
from core.watch.text import html_text, norm_lines, parse_date, parse_date_infer_year, region_text, soup


@dataclass
class Ctx:
    iso3: str
    names: list[str]  # 国名的各种写法,用于在多国列表里过滤本国条目
    wb_code: str
    since: date  # 按时间窗取数的接口(GDELT)从这天起
    today: date = field(default_factory=date.today)  # 运行日(补年份、算时间窗用;回放测试时可指定)
    warnings: list[str] = field(default_factory=list)  # 部分失败(如多页监测里某一页取不到)但信源整体可用


Collector = Callable[[dict, Ctx], list[Item]]
KINDS: dict[str, Collector] = {}


def kind(name: str):
    def deco(fn: Collector) -> Collector:
        KINDS[name] = fn
        return fn

    return deco


def _pick(el, spec: str | None) -> str | None:
    """选择器取值:'css' 取文本,'css@attr' 取属性,'@attr' 取元素自身属性。"""
    if not spec or el is None:
        return None
    css, _, attr = spec.partition("@")
    node = el.select_one(css) if css else el
    if node is None:
        return None
    if attr:
        v = node.get(attr)
        return str(v).strip() if v else None
    return norm_lines(node.get_text(" ")).replace("\n", " ") or None


def _mentions(text: str | None, names: list[str]) -> bool:
    t = (text or "").casefold()
    return any(n.casefold() in t for n in names)


def _fingerprint(it: Item, fields: list[str] | None) -> str:
    if not fields:
        return ""
    return sha1(json.dumps({k: it.fields.get(k) for k in fields}, ensure_ascii=False, sort_keys=True))


@kind("html_list")
def html_list(src: dict, ctx: Ctx) -> list[Item]:
    out: list[Item] = []
    for url in [src["url"], *src.get("more_pages", [])]:
        page = fetch.get(url)
        s = soup(page.content)
        for el in s.select(src["item"]):
            title = _pick(el, src["title"])
            for pat in src.get("title_remove", []):
                title = re.sub(pat, "", title or "").strip() or None
            spec = src.get("link") or src["title"]
            href = _pick(el, spec if "@" in spec else spec + "@href")
            if not title or not href:
                continue  # 表头行等
            link = urljoin(page.url, href)
            fields = {k: _pick(el, spec) for k, spec in (src.get("fields") or {}).items()}
            if src.get("country_in"):
                where = title if src["country_in"] == "title" else fields.get(src["country_in"])
                if not _mentions(where, ctx.names):
                    continue
            if src.get("country_from"):  # 全球信源:国别从标题前缀或某个字段里用正则取,如 {field: title, regex: '^([^:]+):'}
                cf = src["country_from"]
                m = re.search(cf.get("regex", "(.+)"), (title if cf.get("field", "title") == "title" else fields.get(cf["field"])) or "")
                fields["country"] = m.group(1).strip() if m else None
            if src.get("deadline_field"):
                fields["deadline"] = parse_date(fields.get(src["deadline_field"]))
            text = " | ".join(f"{k}: {v}" for k, v in fields.items() if v)
            raw_date = _pick(el, src.get("date"))
            published = parse_date_infer_year(raw_date, ctx.today) if src.get("date_infer_year") else parse_date(raw_date)
            it = Item(key=link, url=link, title=title, published=published,
                      text=text, fields=fields, lang=src.get("lang"))
            it.fingerprint = _fingerprint(it, src.get("track_changes"))
            out.append(it)
    if not out and not src.get("may_be_empty"):
        raise RuntimeError(f"{src['id']}: 列表解析为 0 条——页面结构可能改了,检查选择器")
    return out


_ATEXO_DETAIL = re.compile(r"EntrepriseDetailConsultation&refConsultation=(\d+)&orgAcronyme=(\w+)")
_ATEXO_CONTROLS = {  # Atexo 检索页的控件名(摩洛哥公共采购门户实测);别的 Atexo 平台不同时在信源配置 controls 里覆盖
    "domain": "ctl0$CONTENU_PAGE$AdvancedSearch$domaineActivite$idsDomaines",
    "search": "ctl0$CONTENU_PAGE$AdvancedSearch$lancerRecherche",
    "page_size": "ctl0$CONTENU_PAGE$resultSearch$listePageSizeTop",
    "keyword": "ctl0$CONTENU_PAGE$AdvancedSearch$keywordSearch",
}
_DMY = re.compile(r"\b\d{2}/\d{2}/\d{4}\b")


def _form(page: fetch.Page, button: str | None = None) -> tuple[str, dict[str, str]]:
    """页面表单的提交地址与默认值,与浏览器提交时相同:文本框、隐藏域、文本区、选中的单选/复选框与下拉项;
    按钮只带 button 指定的那一个(相当于点了它)。"""
    f = soup(page.content).find("form")
    if f is None:
        raise RuntimeError("检索页里没有表单——页面结构可能改了")
    data: dict[str, str] = {}
    for el in f.find_all(["input", "select", "textarea"]):
        name = el.get("name")
        t = (el.get("type") or el.name).lower()
        if not name:
            continue
        if t in ("submit", "image", "button", "reset", "file"):
            if name == button:
                data[name] = el.get("value", "")
        elif t in ("radio", "checkbox"):
            if el.has_attr("checked"):
                data[name] = el.get("value", "on")
        elif el.name == "select":
            opt = el.find("option", selected=True)
            if opt is not None:
                data[name] = opt.get("value", opt.get_text(strip=True))
        elif el.name == "textarea":
            data[name] = el.get_text()
        else:
            data[name] = el.get("value", "")
    if button and button not in data:
        raise RuntimeError(f"检索页里没有按钮 {button}——页面结构可能改了")
    return urljoin(page.url, f.get("action") or page.url), data


def _text(el) -> str | None:
    if el is None:
        return None
    return norm_lines(el.get_text(" ")).replace("\n", " ") or None


def _after_label(el) -> str | None:
    """"<strong>Objet :</strong> 正文"这类块里标签后面的文字。"""
    if el is None:
        return None
    for lab in el.find_all("strong"):
        lab.extract()
    return _text(el)


def _atexo_count(page: fetch.Page) -> int | None:
    t = soup(page.content).get_text(" ")
    if re.search(r"Aucun r[ée]sultat", t):
        return 0
    m = re.search(r"Nombre de r[ée]sultats\s*:\s*(\d+)", t)
    return int(m.group(1)) if m else None


def _atexo_items(page: fetch.Page, src: dict) -> list[Item]:
    """检索结果页的每一行:标题取悬浮提示里的完整"Objet"(列表里的可能截断),另取编号、采购方、程序、类别、地点、截止。"""
    out = []
    for a in soup(page.content).find_all("a", href=_ATEXO_DETAIL):
        tr = a.find_parent("tr")
        tds = tr.find_all("td", recursive=False) if tr else []
        if len(tds) < 5:
            continue
        ref_cons, org = _ATEXO_DETAIL.search(a["href"]).groups()
        head, body, place, closing = tds[1], tds[2], tds[3], tds[4]
        title = _text(body.select_one("[id$=infosBullesObjet]")) or _after_label(body.select_one("[id$=panelBlocObjet]"))
        if not title:
            continue
        lieu = place.select_one("[id$=panelBlocLieuxExec]")
        fields = {
            "reference": _text(body.select_one("span.ref")),
            "buyer": _after_label(body.select_one("[id$=panelBlocDenomination]")),
            "procedure": _text(head.select_one("[id$=type_procedure]")),
            "category": _text(head.select_one("[id$=panelBlocCategorie]")),
            "place": _text(place.select_one("[id$=infosLieuExecution]"))  # 悬浮提示里是完整的地点列表
                     or (norm_lines(lieu.get_text("\n")).split("\n")[0] if lieu is not None else None),
            "closing": _text(closing.select_one(".cloture-line")),
        }
        fields["deadline"] = parse_date(fields["closing"])
        m = _DMY.search(head.get_text(" "))
        link = urljoin(page.url, a["href"])
        out.append(Item(key=f"atexo:{org}:{ref_cons}", url=link, title=title, published=parse_date(m.group(0)) if m else None,
                        text=" | ".join(f"{k}: {v}" for k, v in fields.items() if v), fields=fields, lang=src.get("lang")))
    return out


@kind("atexo_search")
def atexo_search(src: dict, ctx: Ctx) -> list[Item]:
    """Atexo(Local Trust MPE)公共采购平台的在线检索,如摩洛哥公共采购门户:取"在招"公告。
    - domains:按活动领域检索(水利、供排水工程领域里的公告标题常不带水务词,整个领域都要);
    - keywords:按关键词检索,一组词为"任一词命中";补上只标了顶层大类(如大型污水厂只标"工程")、
      或分在别的领域(运营服务、设备供货)里的水务公告。
    结果要经页面表单回发才出,与在网页上点"检索"相同:打开检索页 → 填条件、点检索 → 结果多于 10 条时改为每页 500 条。
    每次检索 2–3 次请求;只做公开检索,不登录、不下载招标文件。"""
    ctl = {**_ATEXO_CONTROLS, **(src.get("controls") or {})}
    doms = src.get("domains") or []
    searches = [("domain", d) for d in doms] + [("keywords", k) for k in src.get("keywords") or []]
    entry = doms[0] if doms else "1"  # 检索表单要从带领域参数的地址进入(参数为空时直接出结果页)
    out: dict[str, Item] = {}
    with fetch.session() as ses:
        for how, value in searches:
            page = ses.get(src["url"] + f"&domaineActivite={value if how == 'domain' else entry}")
            action, data = _form(page, ctl["search"])
            data[ctl["domain"]] = value if how == "domain" else ""
            if how == "keywords":
                data[ctl["keyword"]] = value
            page = ses.post(action, data)
            label = f"领域 {value}" if how == "domain" else f"关键词组({value.split()[0]} 等)"
            n = _atexo_count(page)
            if n is None:
                raise RuntimeError(f"{src['id']}: {label}的检索结果页里没有结果数——页面结构可能改了")
            if n > 10:  # 默认每页 10 条:回发"每页条数"改成 500
                action, data = _form(page)
                data.update({ctl["page_size"]: "500", "PRADO_POSTBACK_TARGET": ctl["page_size"]})
                page = ses.post(action, data)
            items = _atexo_items(page, src)
            if n and not items:
                raise RuntimeError(f"{src['id']}: {label}结果 {n} 条但一条也没解析出来——页面结构可能改了")
            if n > 500:
                ctx.warnings.append(f"{label}:结果 {n} 条,超过每页上限 500,只取了前 500 条——把这组词拆开")
            elif len(items) < n:
                ctx.warnings.append(f"{label}:结果 {n} 条,只解析出 {len(items)} 条")
            for it in items:
                if how == "domain":
                    it.fields["domain"] = value
                out.setdefault(it.key, it)  # 同一公告可能同时在几个领域、几组关键词里
    return list(out.values())


@kind("rss")
def rss(src: dict, ctx: Ctx) -> list[Item]:
    page = fetch.get(src["url"])
    s = BeautifulSoup(page.content, "xml")
    out = []
    for el in s.find_all(["item", "entry"]):
        title = (el.find("title").get_text(" ", strip=True) if el.find("title") else "").strip()
        ln = el.find("link")
        link = (ln.get("href") or ln.get_text(strip=True)) if ln else ""
        if not title or not link:
            continue
        pub = el.find(["pubDate", "published", "updated", "dc:date"])
        desc = el.find(["description", "summary", "content"])
        text = html_text(desc.get_text(), drop_chrome=False) if desc else ""
        out.append(Item(key=link, url=link, title=title, published=parse_date(pub.get_text() if pub else None),
                        text=text[:2000], lang=src.get("lang")))
    return out


WB_NOTICES = "https://search.worldbank.org/api/v2/procnotices"
WB_NOTICE_URL = "https://projects.worldbank.org/en/projects-operations/procurement-detail/{id}"


@kind("wb_procnotices")
def wb_procnotices(src: dict, ctx: Ctx) -> list[Item]:
    params = {"format": "json", "srt": "noticedate", "order": "desc", "rows": min(src.get("rows", 50), 1000), "fl": "*"}
    if src.get("notice_types"):  # 接口里 ^ 表示"或"
        params["notice_type_exact"] = "^".join(src["notice_types"])
    code = src.get("country_code") or ctx.wb_code
    if code:  # 全球范围不按国别过滤,逐条记下国别
        params["project_ctry_code_exact"] = code
    if src.get("qterm"):
        params["qterm"] = src["qterm"]
    data = json.loads(fetch.get(WB_NOTICES, params=params).content)
    out = []
    for n in data.get("procnotices") or []:
        nid = str(n.get("id") or "")
        if not nid:
            continue
        fields = {
            "country": n.get("project_ctry_code") or n.get("project_ctry_name"),
            "notice_type": n.get("notice_type"), "project_id": n.get("project_id"), "project_name": n.get("project_name"),
            "procurement_method": n.get("procurement_method_name"), "deadline": parse_date(n.get("submission_deadline_date")),
        }
        body = html_text(n.get("notice_text") or "", drop_chrome=False)
        out.append(Item(key=f"wb:{nid}", url=WB_NOTICE_URL.format(id=nid),
                        title=f"{n.get('notice_type') or '采购公告'}:{n.get('bid_description') or n.get('project_name') or nid}",
                        published=parse_date(n.get("noticedate")), text=body[:6000], fields=fields, lang="en"))
    return out


WB_PROJECTS = "https://search.worldbank.org/api/v3/projects"
WB_PROJECT_URL = "https://projects.worldbank.org/en/projects-operations/project-detail/{pid}"
_WB_FL = "id,project_name,status,boardapprovaldate,closingdate,totalamt,last_stage_reached_name,impagency,pdo"


@kind("wb_projects")
def wb_projects(src: dict, ctx: Ctx) -> list[Item]:
    params = {"format": "json", "rows": src.get("rows", 100), "fl": _WB_FL + ",countrycode", "srt": "boardapprovaldate", "order": "desc"}
    code = src.get("country_code") or ctx.wb_code
    if code:
        params["countrycode_exact"] = code
    if src.get("qterm"):
        params["qterm"] = src["qterm"]
    data = json.loads(fetch.get(WB_PROJECTS, params=params).content)
    out = []
    for p in (data.get("projects") or {}).values():
        if p.get("status") not in (src.get("statuses") or ["Pipeline", "Active"]):
            continue
        cc = p.get("countrycode")
        fields = {"country": (cc[0] if isinstance(cc, list) and cc else cc) or None,
                  "status": p.get("status"), "stage": p.get("last_stage_reached_name"),
                  "board": parse_date(p.get("boardapprovaldate")), "closing": parse_date(p.get("closingdate")),
                  "amount_usd": p.get("totalamt"), "agency": (p.get("impagency") or "").strip() or None}
        it = Item(key=f"wb:{p['id']}", url=WB_PROJECT_URL.format(pid=p["id"]), title=f"{p['id']} {p.get('project_name')}",
                  published=fields["board"], text=(p.get("pdo") or "")[:2000], fields=fields, lang="en")
        it.fingerprint = _fingerprint(it, src.get("track_changes") or ["status", "stage", "board", "closing", "amount_usd"])
        out.append(it)
    return out


@kind("page")
def page(src: dict, ctx: Ctx) -> list[Item]:
    """单页变化监测:正文(去掉 ignore 里的易变行)算指纹;变没变由运行逻辑对比已见指纹判断。
    某一页取不到只记警告、跳过,不影响其余页面;全部取不到才算信源失败。"""
    out, errors = [], []
    for pg in src["pages"]:
        try:
            p = fetch.get(pg["url"])
        except (fetch.FetchError, SourceBlocked, httpx.HTTPError) as e:
            errors.append(e)
            ctx.warnings.append(f"{pg.get('label') or pg['url']}:{type(e).__name__}: {e}"[:200])
            continue
        text = region_text(p.content, pg.get("selector"))
        for pat in pg.get("ignore", []) + src.get("ignore", []):
            text = re.sub(pat, "", text)
        text = norm_lines(text)
        title = pg.get("label") or _pick(soup(p.content), "title") or pg["url"]
        out.append(Item(key=pg["url"], url=pg["url"], title=title, text=text, fields={"item": pg.get("item")},
                        lang=pg.get("lang"), fingerprint=sha1(text)))
    if errors and not out:
        raise errors[0]
    return out


def _unmojibake(s: str) -> str:
    """把按 latin-1 读错的 UTF-8(如 "TÃ¼rkiye")还原。"""
    if "Ã" in s or "Â" in s:
        try:
            return s.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return s
    return s


@kind("aiib")
def aiib(src: dict, ctx: Ctx) -> list[Item]:
    """亚投行页面内嵌的 JS 数据文件:dataset=ppo 为采购公告(ppo-data-all.js),dataset=projects 为项目清单
    (all-projects-data.js,项目状态或金额变了出一条"变化")。sectors 给出时只收这些行业。"""
    page = fetch.get(src["url"])
    js = page.text
    body = js[js.index("["): js.rindex("]") + 1]
    dataset = src.get("dataset", "ppo")
    if dataset == "ppo":  # 键没加引号
        body = re.sub(r'(?<=[{,])\s*(id|cd|mb|pj|ds|cr|sd|pc|st|ct|tp|dc):"', r'"\1":"', body)
    body = re.sub(r",\s*\]$", "]", body)  # 末尾多余的逗号
    rows = json.loads(body)
    keep = {x.casefold() for x in src.get("sectors") or []}
    out = []
    for r in rows:
        r = {k: _unmojibake(str(v).strip()) for k, v in r.items()}
        if dataset == "ppo":
            if keep and r.get("st", "").casefold() not in keep:
                continue
            fields = {"country": r.get("mb"), "sector": r.get("st"), "notice_type": r.get("tp"), "category": r.get("ct"),
                      "deadline": parse_date(r.get("cd")), "contractor": r.get("cr") or None, "price": r.get("pc") or None}
            published = parse_date(re.sub(r"\s*,\s*", ", ", r.get("id", "")))
            out.append(Item(key=f"aiib:{r.get('dc')}|{r.get('id')}|{r.get('tp')}", url=urljoin("https://www.aiib.org", r.get("dc") or "/"),
                            title=f"{r.get('tp') or r.get('ct')}: {r.get('pj')}", published=published, text=r.get("ds") or "",
                            fields=fields, lang="en"))
        else:
            if keep and r.get("sector", "").casefold() not in keep:
                continue
            fields = {"country": r.get("economy"), "sector": r.get("sector"), "status": r.get("status"),
                      "financing": r.get("financing_type"),
                      "funding": r.get("proposed_funding") or r.get("approved_funding") or r.get("committed_funding") or None}
            it = Item(key=f"aiib:{r.get('path')}", url=urljoin("https://www.aiib.org", r.get("path") or "/"), title=r.get("name") or "",
                      text=f"{r.get('status')} · {r.get('financing_type')} · {fields['funding'] or ''}", fields=fields, lang="en")
            it.fingerprint = _fingerprint(it, ["status", "funding"])
            out.append(it)
    return out


@kind("ckan")
def ckan(src: dict, ctx: Ctx) -> list[Item]:
    """CKAN 开放数据的 DataStore 接口(如美洲开发银行采购公告):按 filters 取最新 limit 条;map 把记录字段对到条目。
    map 的键:key、title、url、published、country、deadline、text;fields 另存其他字段。"""
    params = {"resource_id": src["resource_id"], "limit": src.get("limit", 500)}
    if src.get("sort"):
        params["sort"] = src["sort"]
    if src.get("filters"):
        params["filters"] = json.dumps(src["filters"], ensure_ascii=False)
    data = json.loads(fetch.get(src["url"], params=params).content)
    if not data.get("success", True):
        raise RuntimeError(f"{src['id']}: {data.get('error')}")
    m = src["map"]
    out = []
    for r in (data.get("result") or {}).get("records") or []:
        def g(k: str, r=r) -> str | None:
            v = r.get(m.get(k, ""))
            return str(v).strip() if v not in (None, "") else None

        fields = {k: r.get(v) for k, v in (src.get("fields") or {}).items()}
        fields.update(country=g("country"), deadline=parse_date(g("deadline")))
        out.append(Item(key=f"{src['id']}:{g('key')}", url=g("url") or src.get("home") or "", title=g("title") or "",
                        published=parse_date((g("published") or "")[:10]), text=(g("text") or "")[:4000], fields=fields,
                        lang=src.get("lang")))
    return out


IATI_QUERY = "https://d-portal.org/q.json"
IATI_STATUS = {1: "Pipeline", 2: "Active", 3: "Finalisation", 4: "Closed", 5: "Cancelled", 6: "Suspended"}  # IATI 活动状态代码
_EPOCH = date(1970, 1, 1).toordinal()


@kind("iati")
def iati(src: dict, ctx: Ctx) -> list[Item]:
    """IATI 数据(经 d-portal 的公开查询接口):reporting_ref 是发布机构(亚行 XM-DAC-46004),sectors 是 DAC 行业代码。
    同一项目的几笔贷款、赠款(IATI 里各是一条活动)按项目编号并成一条;状态或金额变了出一条"变化"。
    project_url 里的 {project} 换成项目编号(活动编号去掉发布机构前缀和末段贷款号,如 56139-001)。"""
    params = {"from": "act,sector,country", "reporting_ref": src["reporting_ref"], "orderby": "day_start-",
              "select": "aid,title,description,status_code,day_start,commitment,sector_code,country_code",
              "limit": src.get("rows", 300)}
    if src.get("sectors"):
        params["sector_code"] = "|".join(str(x) for x in src["sectors"])
    data = json.loads(fetch.get(src.get("api", IATI_QUERY), params=params).content)
    keep = set(src.get("statuses") or [1, 2])
    prefix = src["reporting_ref"] + "-"
    projects: dict[str, dict] = {}
    for r in data.get("rows") or []:
        aid = str(r.get("aid") or "")
        if not aid.startswith(prefix) or r.get("status_code") not in keep:
            continue
        rest = aid[len(prefix):]
        pid = rest.rsplit("-", 1)[0] if rest.count("-") >= 2 else rest
        p = projects.setdefault(pid, {"aids": {}, "sectors": [], "countries": [], "title": r.get("title"),
                                      "text": r.get("description") or "", "status": r["status_code"], "start": r.get("day_start")})
        p["aids"][aid] = r.get("commitment") or 0  # 一笔活动按行业、国别各出一行,金额只算一次
        for k, v in (("sectors", r.get("sector_code")), ("countries", r.get("country_code"))):
            if v and v not in p[k]:
                p[k].append(v)
        p["status"] = min(p["status"], r["status_code"])  # 有一笔还在筹备,项目就算筹备
        if r.get("day_start") and (not p["start"] or r["day_start"] < p["start"]):
            p["start"] = r["day_start"]
    out = []
    for pid, p in projects.items():
        start = date.fromordinal(_EPOCH + p["start"]).isoformat() if p["start"] else None
        amount = sum(p["aids"].values()) or None
        fields = {"country": p["countries"][0] if p["countries"] else None, "countries": ", ".join(p["countries"]) or None,
                  "status": IATI_STATUS.get(p["status"]), "start": start, "amount_usd": amount,
                  "sectors": ", ".join(p["sectors"]), "project_id": pid}
        it = Item(key=f"{src['id']}:{pid}", url=src["project_url"].format(project=pid), title=f"{pid} {p['title'] or ''}".strip(),
                  published=start, text=p["text"][:4000], fields=fields, lang="en")
        it.fingerprint = _fingerprint(it, ["status", "amount_usd"])
        out.append(it)
    return out


_NAV_TEXT = re.compile(r"^(home|next|prev|previous|more|read more|login|sign in|contact|about|search|menu|ana səhifə|daha çox|"
                       r"главная|подробнее|далее|accueil|suivant|inicio|siguiente|首页|更多|下一页)$", re.IGNORECASE)


def _other_items(block, a, min_chars: int) -> bool:
    """块里有没有指向别处、文字够长(像另一条目)的链接;附件、"更多"之类的短链接不算。"""
    own = a["href"].strip()
    return any(x["href"].strip() != own and len(x.get_text(" ", strip=True)) >= min_chars for x in block.select("a[href]"))


@kind("links")
def links(src: dict, ctx: Ctx) -> list[Item]:
    """任意网页的链接列表:取 selector 区域(默认去掉导航、页头页脚后的全页)里文字够长的链接,新出现的链接就是新条目。
    用于用户添加的网站(公告栏、新闻栏);可选 include/exclude 正则(对链接地址或文字)。日期取链接所在块里能认出的第一个日期。"""
    page = fetch.get(src["url"])
    s = soup(page.content)
    for t in s(["script", "style", "noscript", "nav", "header", "footer", "aside", "form"]):
        t.decompose()
    roots = s.select(src["selector"]) if src.get("selector") else [s]
    if not roots:
        raise RuntimeError(f"{src['id']}: 选择器 {src['selector']!r} 在页面上没找到")
    inc = re.compile(src["include"], re.IGNORECASE) if src.get("include") else None
    exc = re.compile(src["exclude"], re.IGNORECASE) if src.get("exclude") else None
    out, seen = [], set()
    for a in (x for root in roots for x in root.select("a[href]")):
        title = norm_lines(a.get_text(" ")).replace("\n", " ")
        href = urljoin(page.url, a["href"].strip())
        if (len(title) < src.get("min_chars", 20) or _NAV_TEXT.match(title) or not href.startswith(("http://", "https://"))
                or href.split("#")[0] == page.url.split("#")[0] or href in seen):
            continue
        hay = f"{href} {title}"
        if (inc and not inc.search(hay)) or (exc and exc.search(hay)):
            continue
        seen.add(href)
        published = None
        for i, block in enumerate(a.parents):  # 往上找几层,取链接所在块里的第一个日期
            if i >= 4 or block.name in ("body", "html") or _other_items(block, a, src.get("min_chars", 20)):
                break  # 到了装着别的条目的容器就停,免得拿到别条的日期
            published = parse_date_infer_year(norm_lines(block.get_text(" "))[:400], ctx.today)
            if published:
                break
        out.append(Item(key=href, url=href, title=title[:300], published=published, lang=src.get("lang")))
    if not out and not src.get("may_be_empty"):
        raise RuntimeError(f"{src['id']}: 页面上没有找到可用的链接——检查网址或选择器")
    return out[: src.get("max_items", 200)]


GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"


@kind("gdelt")
def gdelt(src: dict, ctx: Ctx) -> list[Item]:
    days = max(1, min(90, (ctx.today - ctx.since).days))
    params = {"query": src["query"], "mode": "artlist", "format": "json", "maxrecords": src.get("max", 75),
              "timespan": f"{days}d", "sort": "datedesc"}
    # GDELT 对每个来源限速「每 5 秒一次」,忙的时候第一次常回 429:隔 10 秒再试,最多两次
    data = json.loads(fetch.get(GDELT, params=params, timeout=90, wait_429=10, retries_429=2).content or b"{}")
    out = []
    for a in data.get("articles") or []:
        if not a.get("url"):
            continue
        out.append(Item(key=a["url"], url=a["url"], title=a.get("title") or a["url"], published=parse_date(a.get("seendate")),
                        fields={"domain": a.get("domain"), "language": a.get("language")},
                        lang=(a.get("language") or "")[:2].lower() or None))
    return out
