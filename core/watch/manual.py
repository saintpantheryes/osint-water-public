"""人工入库:用户提交网址、粘贴文字或上传 PDF/Word,系统取正文、初筛、判定、定国别与领域、挂台账建议,存为线索
(信源"人工提交")。同一网址、同一段文字或同一个文件重复提交时返回已有线索,不重复入库。"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urljoin

import httpx

from core import geo
from core.ledger import load_ledger, load_rules
from core.profile import Profile
from core.sources.http import SourceBlocked
from core.watch import fetch
from core.watch.link import build_index, item_countries, suggest
from core.watch.llm import Classifier
from core.watch.rules import evaluate
from core.watch.run import _kind, _merge_llm, _safe_url, _stamp, doc_text
from core.watch.store import Item, Store, sha1, signal_id
from core.watch.text import docx_text, html_text, norm_lines, pdf_text, scrub_contacts, soup

SOURCE = "manual"
SOURCE_NAME = "人工提交"


class SubmitError(ValueError):
    pass


def merged_ledger(profile: Profile):
    rules = load_rules(profile.rules) if profile.rules else load_rules()
    leds = [load_ledger(c.ledger, rules) for c in profile.countries if c.ledger]
    items = [it for led in leds for it in led.items]
    return SimpleNamespace(items=items, countries={it["id"]: led.country for led in leds for it in led.items}) if items else None


def _title_of(page, body: str) -> str:
    if _kind(page) == "html":
        s = soup(page.content)
        for sel in ("h1", "title", "meta[property='og:title']"):
            el = s.select_one(sel)
            t = (el.get("content") if el is not None and el.name == "meta" else el.get_text(" ") if el is not None else "") or ""
            t = norm_lines(t).replace("\n", " ").strip()
            if len(t) >= 8:
                return t[:300]
    return next((ln for ln in body.splitlines() if len(ln.strip()) >= 8), page.url)[:300]


def _from_url(url: str, store: Store) -> tuple[str, str, list[dict]]:
    """→ (标题, 正文(含附件), docs)。网页上的 PDF/Word 附件最多取 3 个。"""
    page = fetch.get(_safe_url(url))
    body, pages = doc_text(page)
    title = _title_of(page, body)
    store.doc_path(url).write_text(body, encoding="utf-8")
    texts, docs = [body], [{"url": url, "chars": len(body), **({"pages": pages} if pages else {})}]
    if _kind(page) == "html":
        s = soup(page.content)
        links = [urljoin(page.url, a["href"]) for a in s.select("a[href]") if re.search(r"\.(pdf|docx?)(\?|$)", a["href"], re.IGNORECASE)]
        for u in list(dict.fromkeys(links))[:3]:
            try:
                t, pg = doc_text(fetch.get(_safe_url(u), max_bytes=15_000_000))
            except (fetch.FetchError, SourceBlocked, httpx.HTTPError) as e:
                docs.append({"url": u, "error": f"{type(e).__name__}: {e}"[:200]})
                continue
            except Exception as e:  # noqa: BLE001  坏附件不影响提交
                docs.append({"url": u, "error": f"{type(e).__name__}: {e}"[:200]})
                continue
            store.doc_path(u).write_text(t, encoding="utf-8")
            texts.append(f"[附件] {u}\n{t}")
            docs.append({"url": u, "chars": len(t), **({"pages": pg} if pg else {})})
    return title, "\n\n".join(texts), docs


def _from_file(name: str, data: bytes) -> str:
    ext = Path(name).suffix.lower()
    if ext == ".pdf":
        return scrub_contacts(pdf_text(data)[0])
    if ext == ".docx":
        return scrub_contacts(docx_text(data))
    if ext in (".html", ".htm"):
        return scrub_contacts(html_text(data))
    if ext in (".txt", ".md", ".csv", ""):
        return scrub_contacts(norm_lines(data.decode("utf-8", errors="replace")))
    raise SubmitError(f"不支持的文件类型 {ext}:请上传 PDF、Word(.docx)、网页或文本文件")


def submit(profile: Profile, store: Store, clf: Classifier | None, *, url: str = "", text: str = "",
           file: tuple[str, bytes] | None = None, title: str = "", country: str = "", note: str = "",
           today: date | None = None) -> tuple[dict, bool]:
    """→ (线索, 是否新入库)。url / text / file 三选一;同时给了网址和文字时,用给的文字、不再抓取(检索结果入库用)。"""
    url, text = url.strip(), text.strip()
    if url and not url.startswith(("http://", "https://")):
        raise SubmitError("网址要以 http:// 或 https:// 开头")
    if not (url or text or file):
        raise SubmitError("请给出网址、粘贴文字或上传文件")
    if url:
        key = url
    elif file:
        key = f"file:{sha1(file[1].hex())[:16]}"
    else:
        key = f"text:{sha1(text)[:16]}"
    sid = signal_id(SOURCE, key)
    old = store.submitted(sid)
    if old:
        return old, False
    today = today or date.today()
    docs: list[dict] = []
    if url and text:
        body, auto_title = scrub_contacts(text), title or url
        store.doc_path(url).write_text(body, encoding="utf-8")
        docs = [{"url": url, "chars": len(body)}]
    elif url:
        auto_title, body, docs = _from_url(url, store)
    elif file:
        body = _from_file(file[0], file[1])
        auto_title = next((ln for ln in body.splitlines() if len(ln.strip()) >= 8), file[0])[:300]
        store.doc_path(key).write_text(body, encoding="utf-8")
        docs = [{"url": key, "chars": len(body), "name": file[0]}]
    else:
        body = scrub_contacts(text)
        auto_title = next((ln for ln in body.splitlines() if ln.strip()), "粘贴的文字")[:300]
        store.doc_path(key).write_text(body, encoding="utf-8")
        docs = [{"url": key, "chars": len(body)}]
    if len(body.strip()) < 20:
        raise SubmitError("没有读到正文(页面可能要登录或由脚本生成);可以把正文复制后用\"粘贴文字\"提交")
    title = (title or auto_title).strip()
    r = evaluate(Item(key=key, url=url, title=title, text=body[:20_000]), {"topic": "water"}, [])
    r["candidate"] = True  # 人工提交的一律交给模型判定
    led = merged_ledger(profile)
    sig = {
        "id": sid, "country": geo.iso3(country) if country else next(iter(geo.detect(f"{title}\n{body[:5000]}")), None),
        "scope": SOURCE, "source": SOURCE, "source_name": SOURCE_NAME, "kind": "manual", "key": key,
        "url": url or "", "title": title, "published": None, "first_seen": _stamp(today), "change": "new", "lang": None,
        "fields": {k: v for k, v in {"note": note, "file": file[0] if file else None}.items() if v},
        "rules": r, "docs": docs, "llm": None, "links": [], "excerpt": body[:1500],
    }
    if led:
        sig["links"] = suggest("\n".join([title, url, body[:50_000]]), build_index(led), sig["country"])
    if clf is not None:
        system = clf.system("全球各国", (profile.bidder or {}).get("nationality"), led)
        out = clf.classify(system, sig, body)
        if out is not None:
            _merge_llm(sig, out, item_countries(led))
    store.put_submitted(sig)  # 单独文件,不动 state.json 与月度线索文件(那些归定时任务写)
    return sig, True


# ---------- 检索 ----------

def _wb_code(iso3: str) -> str:
    from core.geo import _raw

    rev = {v: k for k, v in (_raw().get("wb_codes") or {}).items()}
    return rev.get(iso3) or geo.countries()[iso3]["iso2"]


def wb_country_notices(iso3: str, rows: int = 500) -> list[dict]:
    """一国最近的世行采购公告(按发布日倒序,最多 rows 条,带正文)→ 与 search_worldbank 同样的字典。
    用来核「某个编号在不在世行采购网」:关键词检索对编号不灵(斜杠、连字符),整国拉下来按正文比对才可靠。"""
    import json

    from core.watch.collectors import WB_NOTICE_URL, WB_NOTICES
    from core.watch.text import parse_date

    params = {"format": "json", "rows": rows, "srt": "noticedate", "order": "desc", "project_ctry_code_exact": _wb_code(iso3),
              "fl": "id,notice_type,noticedate,project_ctry_code,project_name,bid_description,noticetitle,submission_deadline_date,notice_text"}
    out = []
    for n in json.loads(fetch.get(WB_NOTICES, params=params).content).get("procnotices") or []:
        title = f"{n.get('notice_type') or '采购公告'}:{n.get('bid_description') or n.get('noticetitle') or n.get('project_name')}"
        out.append({"kind": "世行采购公告", "title": title, "country": iso3, "date": parse_date(n.get("noticedate")), "url": WB_NOTICE_URL.format(id=n.get("id")),
                    "text": html_text(n.get("notice_text") or "", drop_chrome=False) or title, "deadline": parse_date(n.get("submission_deadline_date"))})
    return out


def search_worldbank(query: str, iso3: str | None = None, rows: int = 20) -> list[dict]:
    """按关键词查世界银行采购公告与项目(官方公开接口,qterm 支持 AND/OR)→ [{kind, title, country, date, url, text}]。
    公告正文随结果一起取回,入库时不必再打开网页(世行公告详情页是脚本页,抓不到正文)。"""
    import json

    from core.watch.collectors import WB_NOTICE_URL, WB_NOTICES, WB_PROJECT_URL, WB_PROJECTS
    from core.watch.text import parse_date

    out = []
    params = {"format": "json", "qterm": query, "rows": rows, "srt": "noticedate", "order": "desc",
              "fl": "id,notice_type,noticedate,project_ctry_code,project_name,bid_description,noticetitle,"
                    "submission_deadline_date,notice_text"}
    if iso3:
        params["project_ctry_code_exact"] = _wb_code(iso3)
    for n in json.loads(fetch.get(WB_NOTICES, params=params).content).get("procnotices") or []:
        title = f"{n.get('notice_type') or '采购公告'}:{n.get('bid_description') or n.get('noticetitle') or n.get('project_name')}"
        out.append({"kind": "世行采购公告", "title": title, "country": geo.iso3(n.get("project_ctry_code")),
                    "date": parse_date(n.get("noticedate")), "url": WB_NOTICE_URL.format(id=n.get("id")),
                    "text": html_text(n.get("notice_text") or "", drop_chrome=False) or title,
                    "deadline": parse_date(n.get("submission_deadline_date"))})
    params = {"format": "json", "qterm": query, "rows": rows, "srt": "boardapprovaldate", "order": "desc",
              "fl": "id,project_name,countrycode,boardapprovaldate,status,totalamt,pdo"}
    if iso3:
        params["countrycode_exact"] = _wb_code(iso3)
    for pj in (json.loads(fetch.get(WB_PROJECTS, params=params).content).get("projects") or {}).values():
        cc = pj.get("countrycode")
        out.append({"kind": f"世行项目({pj.get('status') or ''})", "title": f"{pj.get('id')} {pj.get('project_name')}",
                    "country": geo.iso3(cc[0] if isinstance(cc, list) and cc else cc), "date": parse_date(pj.get("boardapprovaldate")),
                    "url": WB_PROJECT_URL.format(pid=pj.get("id")), "text": (pj.get("pdo") or "").strip() or pj.get("project_name") or "",
                    "deadline": None})
    return out
