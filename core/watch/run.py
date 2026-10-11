"""一次监测运行:逐个范围(各国、全球)逐信源 采集 → 去重与变更检测 → 规则初筛 → 跟进详情页与附件 → 大模型判定抽取
→ 定国别与领域 → 挂台账建议 → 入库。

范围:profile 里每个配了 watch 的国别一个(信源只出该国的条目);另有一个全球范围(global/watch.yaml 的多边开发银行等
多国信源,加上用户在界面里添加的网站),条目的国别从结构化字段、标题或模型判断里定。
首次运行某个信源时,早于回溯期、又没有未来截止日的条目只记为已见、不出线索(避免把几年的历史公告当成新提醒;
发布得早但还没截止的招标照样报出);页面变化监测的首次运行只建立基线。
水务词表或信源配置改动后,各信源里之前没过初筛、仍在回溯期或截止前、也没入库的条目重判一次(补回漏掉的公告)。
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote, urljoin

import httpx
import yaml

from core import geo, storage
from core.ledger import load_ledger, load_rules
from core.profile import Profile
from core.sources.http import SourceBlocked
from core.watch import fetch
from core.watch.collectors import KINDS, Ctx
from core.watch.link import build_index, firm, foreign, item_countries, suggest
from core.watch.llm import Classifier, LLMConfig
from core.watch.rules import evaluate, terms_version
from core.watch.store import Item, Store, signal_id
from core.watch.text import docx_text, html_text, pdf_text, scrub_contacts, soup

WATCH_SCHEMA = "osint-water/watch@1"
DEFAULTS = {"lookback_days": 60, "inbox_days": 14, "deadline_days": 30}
# 国别范围里,公告类信源(已按国别过滤)全部保存,新闻类只存初筛命中的;全球范围一律只存初筛命中的
STORE_ALL_KINDS = {"html_list", "wb_procnotices", "wb_projects", "page"}
GLOBAL = "global"
USER_SOURCES = "watch/sources_user.yaml"  # 数据目录里:用户在界面上添加的信源与停用的内置信源


class WatchConfigError(ValueError):
    pass


def load_watch(path: Path) -> dict:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return check_watch(raw, str(path))


def check_watch(raw: dict, where: str) -> dict:
    if raw.get("schema") != WATCH_SCHEMA:
        raise WatchConfigError(f"{where}: schema 应为 {WATCH_SCHEMA}")
    ids = [s.get("id") for s in raw.get("sources") or []]
    if len(ids) != len(set(ids)) or not all(ids):
        raise WatchConfigError(f"{where}: 信源 id 缺失或重复")
    for s in raw.get("sources") or []:
        if s.get("kind") not in KINDS:
            raise WatchConfigError(f"{where}: 信源 {s['id']} 的 kind={s.get('kind')!r} 不认识")
    raw.setdefault("sources", [])
    return raw


def load_user_sources(data_dir: Path) -> dict:
    """用户信源文件:{sources: [...], disabled: [内置信源 id…]};没有文件时为空。"""
    fp = Path(data_dir) / USER_SOURCES
    if not fp.exists():
        return {"schema": WATCH_SCHEMA, "sources": [], "disabled": []}
    raw = yaml.safe_load(fp.read_text(encoding="utf-8")) or {}
    raw.setdefault("schema", WATCH_SCHEMA)
    raw["disabled"] = list(raw.get("disabled") or [])
    return check_watch(raw, str(fp))


def save_user_sources(data_dir: Path, raw: dict) -> Path:
    check_watch(raw, USER_SOURCES)
    fp = Path(data_dir) / USER_SOURCES
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text("# 用户在界面上添加的信源与停用的内置信源;可手工编辑,格式同 countries/*/watch.yaml。\n"
                  + yaml.safe_dump(raw, allow_unicode=True, sort_keys=False, width=160), encoding="utf-8")
    return fp


@dataclass
class Scope:
    key: str  # 国别 ISO3,或 "global"
    iso3: str | None  # 国别范围的国别;全球范围为 None
    label: str
    wc: dict  # 监测配置(全球范围已并入用户信源)
    led: object | None  # 台账;全球范围为各国台账合并
    names: list[str] = field(default_factory=list)
    wb_code: str | None = None


def global_paths(profile: Profile) -> list[Path]:
    raw = (profile.watch or {}).get("global")
    if not raw:
        return []
    base = profile.path.parent if profile.path else Path.cwd()
    return [(base / p).resolve() for p in ([raw] if isinstance(raw, str) else raw)]


def scopes(profile: Profile, only: str | None = None, data_dir: Path | None = None) -> list[Scope]:
    """本次要跑的范围:各国(profile 里配了 watch 的)+ 全球(global 配置 + 用户信源)。only 可为国别 ISO3 或 global。"""
    rules = load_rules(profile.rules) if profile.rules else load_rules()
    user = load_user_sources(data_dir or storage.DATA_DIR)
    disabled = set(user["disabled"])
    out, ledgers = [], []
    for c in profile.countries:
        led = load_ledger(c.ledger, rules) if c.ledger else None
        if led:
            ledgers.append(led)
        if not c.watch:
            continue
        wc = load_watch(c.watch)
        wc["sources"] = [s for s in wc["sources"] if s["id"] not in disabled]
        out.append(Scope(c.iso3, c.iso3, c.name_zh or c.name_en, wc, led, wc.get("names") or [c.name_en], c.wb_country_code))
    gl = {"schema": WATCH_SCHEMA, "names": [], "entities": [], "sources": []}
    for fp in global_paths(profile):
        w = load_watch(fp)
        gl["entities"] += w.get("entities") or []
        gl["sources"] += [s for s in w["sources"] if s["id"] not in disabled]
    gl["sources"] += [{**s, "user": True} for s in user["sources"] if not s.get("disabled")]
    if gl["sources"]:
        merged = SimpleNamespace(items=[it for led in ledgers for it in led.items],
                                 countries={it["id"]: led.country for led in ledgers for it in led.items})  # 按国别挂接要用
        out.append(Scope(GLOBAL, None, "全球", gl, merged if merged.items else None))
    ids = [s["id"] for sc in out for s in sc.wc["sources"]]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        raise WatchConfigError(f"信源 id 在不同配置里重复:{', '.join(sorted(dup))}")
    if only:
        out = [sc for sc in out if sc.key.lower() == only.lower()]
    return out


@dataclass
class CountryRun:
    iso3: str  # 范围:国别 ISO3 或 "global"
    signals: list[dict] = field(default_factory=list)
    sources: dict = field(default_factory=dict)  # id → {status, items, new, error}
    reclassified: int = 0  # 本次补判的旧线索数


def _kind(page) -> str:
    ct, u = (page.content_type or "").lower(), page.url.lower().split("?")[0]
    if "pdf" in ct or u.endswith(".pdf") or page.content[:5] == b"%PDF-":
        return "pdf"
    if "wordprocessingml" in ct or u.endswith(".docx"):
        return "docx"
    return "html"


def doc_text(page, selector: str | None = None) -> tuple[str, int | None]:
    """网页、PDF、Word 转正文 → (文本, PDF 页数)。"""
    k = _kind(page)
    if k == "pdf":
        text, pages = pdf_text(page.content)
        return scrub_contacts(text), pages
    if k == "docx":
        return scrub_contacts(docx_text(page.content)), None
    return scrub_contacts(html_text(page.content, selector)), None


def _safe_url(u: str) -> str:
    return quote(u, safe=":/?&=%#+,;@~!$'()*")


def _cut(text: str, markers: list[str] | None) -> str:
    """在第一个出现的标记处截断:如详情页"Contact Administratif"之后是经办人姓名与联系方式,不入库。"""
    for m in markers or []:
        i = text.find(m)
        if i >= 0:
            text = text[:i].rstrip()
    return text


def _amount(text: str, cfg: dict) -> float | None:
    """正文里"标签 : 金额"的金额,如 "Estimation (en Dhs TTC) * : 118 200 000,00";认不出返回 None。"""
    m = re.search(re.escape(cfg["label"]) + r"[\s*]*:\s*(\d[\d \t\u202f\u00a0.,]*)", text)
    if not m:
        return None
    raw = re.sub(r"[ \t\u202f\u00a0]", "", m.group(1)).rstrip(".,")
    if "," in raw:  # 法语写法:逗号是小数点,点是千位分隔
        raw = raw.replace(".", "").replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def follow(it: Item, src: dict, store: Store) -> tuple[str, list[dict]]:
    """取详情页正文与附件(PDF/Word)文本,带缓存;失败只记在 docs 里,不影响线索入库。"""
    cfg = src.get("follow") if isinstance(src.get("follow"), dict) else {}
    texts, docs = [], []
    try:
        cache = store.doc_path(it.url)
        if cache.exists():
            body, raw = _cut(scrub_contacts(cache.read_text(encoding="utf-8")), cfg.get("cut")), None  # 旧缓存可能还带联系方式
        else:
            page = fetch.get(_safe_url(it.url))
            raw = page
            body, _ = doc_text(page, cfg.get("selector"))
            body = _cut(body, cfg.get("cut"))
            cache.write_text(body, encoding="utf-8")
            if _kind(page) != "html":
                raw = None  # 详情本身就是文件,没有再往下找附件的页面
        texts.append(body)
        docs.append({"url": it.url, "chars": len(body)})
        if cfg.get("attachments") and raw is not None:
            s = soup(raw.content)
            root = (s.select_one(cfg["selector"]) if cfg.get("selector") else None) or s
            links = [urljoin(raw.url, a["href"]) for a in root.select("a[href]")
                     if re.search(r"\.(pdf|docx?)(\?|$)", a["href"], re.IGNORECASE)]
            for u in list(dict.fromkeys(links))[: cfg.get("max_attachments", 3)]:
                c = store.doc_path(u)
                if c.exists():
                    t, pages = scrub_contacts(c.read_text(encoding="utf-8")), None
                else:
                    data = fetch.get(_safe_url(u), max_bytes=cfg.get("max_bytes", 15_000_000))
                    t, pages = doc_text(data)
                    c.write_text(t, encoding="utf-8")
                texts.append(f"[附件] {u}\n{t}")
                docs.append({"url": u, "chars": len(t), **({"pages": pages} if pages else {})})
    except (fetch.FetchError, SourceBlocked, httpx.HTTPError) as e:
        docs.append({"url": it.url, "error": f"{type(e).__name__}: {e}"[:200]})
    except Exception as e:  # noqa: BLE001  附件解析失败(坏 PDF 等)不影响其余
        docs.append({"url": it.url, "error": f"{type(e).__name__}: {e}"[:200]})
    return "\n\n".join(texts), docs


def _old(it: Item, cutoff: str, today: date) -> bool:
    """首次运行(与词表改动后重判)时哪些条目只记为已见:没有未来截止日、且发布早于回溯期或没有发布日期的。
    还没截止的公告不算旧:发布得早、仍能投的招标也要报出来。"""
    dl = it.fields.get("deadline")
    if dl and dl >= today.isoformat():
        return False
    return not it.published or it.published < cutoff


def _judge_version(tv: str, src: dict) -> str:
    """初筛依据的指纹:词表与实体词(tv)加上信源自己的配置(如 topic、关键词、金额门槛)。任一改了,
    这个信源之前没入库、仍在回溯期或截止前的条目重判一次。"""
    raw = tv + json.dumps(src, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _stored(store: Store, src: dict, key: str) -> bool:
    """这条以前入过库没有:按本信源、以及由它接手的旧信源(seed_from)的线索编号查,免得换了信源编号后重复出线索。"""
    ids = store.signal_ids()
    return any(signal_id(x, key) in ids for x in (src["id"], *(src.get("seed_from") or [])))


def _stamp(today: date) -> str:
    """线索首见、信源运行时间:正常运行用当前时间;回放或测试指定了别的运行日时,记在那一天。"""
    now = storage.now_iso()
    return now if now[:10] == today.isoformat() else f"{today.isoformat()}T00:00:00+00:00"


def _page_diff(old: str, new: str, limit: int = 40) -> str:
    lines = [ln for ln in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0)
             if ln[:1] in "+-" and not ln.startswith(("+++", "---"))]
    return "\n".join(lines[:limit]) + (f"\n……另有 {len(lines) - limit} 行变化" if len(lines) > limit else "")


def _item_country(it: Item, src: dict, scope: Scope) -> str | None:
    if scope.iso3:
        return scope.iso3
    if src.get("country"):
        return geo.iso3(src["country"])
    return geo.iso3(it.fields.get("country")) or next(iter(geo.detect(it.title)), None)


def run_scope(profile: Profile, scope: Scope, store: Store, clf: Classifier | None, today: date, cfg: dict) -> CountryRun:
    wc, led = scope.wc, scope.led
    index = build_index(led) if led else []
    system = clf.system(scope.label, (profile.bidder or {}).get("nationality"), led) if clf else ""  # 没有台账也判定,只是不挂台账
    ledger_ids = item_countries(led)  # 台账项目编号 → 国别
    ctx = Ctx(iso3=scope.iso3 or "", names=scope.names, wb_code=scope.wb_code or "",
              since=today - timedelta(days=cfg["lookback_days"]), today=today)
    now = _stamp(today)
    res = CountryRun(scope.key)
    jobs: list[tuple[dict, str]] = []
    tv = terms_version(wc.get("entities") or [])
    for src in wc["sources"]:
        sid = src["id"]
        ctx.warnings.clear()
        ctx.since = today - timedelta(days=src.get("lookback_days", cfg["lookback_days"]))
        cutoff = ctx.since.isoformat()
        try:
            items = KINDS[src["kind"]](src, ctx)
        except SourceBlocked as e:
            store.health(sid, status="blocked", error=str(e)[:300], last_run=now)
            res.sources[sid] = {"status": "blocked", "error": str(e)[:300]}
            continue
        except Exception as e:  # noqa: BLE001  单个信源失败不影响其余
            err = f"{type(e).__name__}: {e}"[:300]
            store.health(sid, status="error", error=err, last_run=now)
            res.sources[sid] = {"status": "error", "error": err}
            continue
        first = store.is_first_run(sid)
        jv = _judge_version(tv, src)
        rejudge = not first and store.src(sid).get("terms") != jv  # 词表或信源配置改过:之前没过初筛的条目重判一次
        if first:
            for other in src.get("seed_from") or []:  # 由国别信源改为全球信源时,沿用旧信源的已见记录,免得重复提醒
                for k, v in store.state["sources"].get(other, {}).get("seen", {}).items():
                    store.src(sid)["seen"].setdefault(k, v)
        fresh: list[Item] = []
        for it in items:
            prev = store.seen(sid, it.key)
            if src["kind"] == "page":  # 缓存存全文,线索里放差异
                cache = store.doc_path("page:" + it.url)
                old = cache.read_text(encoding="utf-8") if cache.exists() else None
                text = scrub_contacts(it.text)
                cache.write_text(text, encoding="utf-8")
                if prev and it.fingerprint != prev.get("fp") and old is not None:
                    it.change = "changed"
                    it.text = _page_diff(scrub_contacts(old), text)
                    fresh.append(it)
            elif prev is None:
                if not (first and _old(it, cutoff, today)):
                    fresh.append(it)
            elif it.fingerprint and prev.get("fp") and prev["fp"] != it.fingerprint:
                it.change = "changed"
                fresh.append(it)
            elif rejudge and not _old(it, cutoff, today) and not _stored(store, src, it.key):
                fresh.append(it)  # 仍在回溯期或截止前、以前没入库的:按新词表再判,过了初筛就作为新线索
            store.mark_seen(sid, it.key, it.fingerprint, today.isoformat())
        n_kept = 0
        default_store = "all" if scope.iso3 and src["kind"] in STORE_ALL_KINDS else "candidates"
        for it in fresh:
            it.text = scrub_contacts(it.text)  # 公告正文里的邮箱、电话不入库
            r = evaluate(it, src, wc.get("entities") or [])
            if not r["candidate"] and src.get("store", default_store) != "all":
                continue
            sig = {
                "id": signal_id(sid, it.key + (f"#{it.fingerprint}" if it.change == "changed" else "")),
                "country": _item_country(it, src, scope), "scope": scope.key,
                "source": sid, "source_name": src.get("name") or sid, "kind": src["kind"],
                "key": it.key, "url": it.url, "title": it.title, "published": it.published, "first_seen": now,
                "change": it.change, "lang": it.lang, "fields": {k: v for k, v in it.fields.items() if v},
                "rules": r, "docs": [], "llm": None, "links": [],
            }
            doc = ""
            if r["candidate"] and src.get("follow"):
                doc, sig["docs"] = follow(it, src, store)
            full = "\n".join(x for x in (it.text, doc) if x)
            if doc:  # 详情页与附件读到后补判阶段(标题里常没有"招标""意向书"字样);标题/字段已判出的不改
                r2 = evaluate(Item(key=it.key, url=it.url, title=it.title, text=full, fields=it.fields), src,
                              wc.get("entities") or [])
                keep = r["stage_from"] == "head"
                r.update({"stage": r["stage"] if keep else r2["stage"], "stage_from": r["stage_from"] if keep else r2["stage_from"],
                          "stage_terms": r2["stage_terms"], "water_terms": r2["water_terms"], "sectors": r2["sectors"]})
                if not sig["country"]:
                    sig["country"] = next(iter(geo.detect(full[:3000])), None)
            if src.get("min_amount") and doc:  # 规模门槛(如摩洛哥门户的预估金额):低于门槛的只记为已见,不入库
                amt = _amount(doc, src["min_amount"])
                if amt is not None:
                    sig["fields"]["estimate"] = f"{amt:,.0f} {src['min_amount'].get('currency', '')}".strip()
                    if amt < src["min_amount"]["value"]:
                        continue
            sig["excerpt"] = full[:1500] if doc else it.text[:6000]
            sig["links"] = suggest("\n".join([it.title, it.url, full[:50_000], str(it.fields)]), index, sig["country"])
            if it.fields.get("item") in ledger_ids and not any(x["item"] == it.fields["item"] for x in sig["links"]):
                sig["links"].insert(0, {"item": it.fields["item"], "confidence": "high", "by": "config", "reason": "监测该项目的页面"})
            if r["candidate"] and clf is not None and system:
                jobs.append((sig, full))
            res.signals.append(sig)
            n_kept += 1
        status = "ok" if items else "empty"
        warn = ";".join(ctx.warnings)[:300] or None
        store.health(sid, status=status, error=warn, last_run=now, last_ok=now, items=len(items), new=n_kept, terms=jv)
        res.sources[sid] = {"status": status, "items": len(items), "new": n_kept, "baseline": first, "error": warn}
    if clf is not None and system:
        for (sig, _), out in zip(jobs, clf.classify_many(system, jobs)):  # 并发判定
            if out is not None:
                _merge_llm(sig, out, ledger_ids)
        res.reclassified = reclassify(store, clf, system, scope.key, ledger_ids, today - timedelta(days=cfg["inbox_days"]))
    store.append_signals(res.signals)
    return res


def run_country(profile: Profile, c, store: Store, clf: Classifier | None, today: date, cfg: dict) -> CountryRun:
    """单个国别范围(兼容旧调用)。"""
    sc = next(s for s in scopes(profile, c.iso3, store.root.parent) if s.key == c.iso3)
    return run_scope(profile, sc, store, clf, today, cfg)


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _merge_llm(sig: dict, out: dict, ledger_ids: dict[str, str | None]) -> None:
    """模型结果入线索。全球范围里信源和标题都没定出国别的,用模型判断的国别;模型给出的台账对应(只收台账里有、
    与线索同一国的编号)追加到挂接建议,标 by: llm;国别定下后,按名称挂上的别国项目去掉。ledger_ids:项目编号 → 国别。"""
    sig["llm"] = {**out, "judged_at": _now_utc()}  # 判定时间进操作记录
    if not sig.get("country") and out.get("country"):
        sig["country"] = out["country"]
    sig["links"] = [x for x in sig["links"] if firm(x) or not foreign(ledger_ids.get(x["item"]), sig.get("country"))]
    for lk in out.get("ledger_links") or []:
        iid = lk.get("item")
        if (iid in ledger_ids and not foreign(ledger_ids[iid], sig.get("country"))
                and not any(x["item"] == iid for x in sig["links"])):
            sig["links"].append({**lk, "by": "llm"})


def _cached_docs(sig: dict, store: Store) -> str:
    """从正文缓存拼回线索的详情页与附件文本;页面变化线索没有 docs,判定时用摘录里的差异。"""
    parts = []
    for i, d in enumerate(sig.get("docs") or []):
        cache = store.doc_path(d["url"])
        if d.get("error") or not cache.exists():
            continue
        t = scrub_contacts(cache.read_text(encoding="utf-8"))
        parts.append(t if i == 0 else f"[附件] {d['url']}\n{t}")
    return "\n\n".join(parts)


def _needs_llm(s: dict) -> bool:
    """还没有模型结果;或结果是旧版字段(没有领域、国别)、需要按新版重判一次。"""
    x = s.get("llm")
    return x is None or (not x.get("_error") and "sectors" not in x)


def reclassify(store: Store, clf: Classifier, system: str, scope_key: str, ledger_ids: dict[str, str | None], since: date) -> int:
    """补判:近期入库、规则初筛通过、还没有(新版)模型结果的线索(当时没配凭据、限流、超出单次调用上限,或字段有更新)。"""
    keys = {scope_key, "manual"} if scope_key == GLOBAL else {scope_key}  # 人工提交的线索由全球范围补判
    pending = [s for s in store.load_signals(since=since)
               if (s.get("scope") or s["country"]) in keys and (s.get("rules") or {}).get("candidate") and _needs_llm(s)]
    if not pending or not clf.enabled:
        return 0
    jobs = [(s, _cached_docs(s, store)) for s in pending]
    done = []
    for s, out in zip(pending, clf.classify_many(system, jobs)):
        if out is not None:
            _merge_llm(s, out, ledger_ids)
            done.append(s)
    store.update_signals(done)
    return len(done)


def all_ledgers(profile: Profile):
    """profile 里各国台账合在一起(带 项目编号 → 国别),重挂线索用;都没有台账时 None。"""
    rules = load_rules(profile.rules) if profile.rules else load_rules()
    leds = [load_ledger(c.ledger, rules) for c in profile.countries if c.ledger]
    items = [it for led in leds for it in led.items]
    return SimpleNamespace(items=items, countries={it["id"]: led.country for led in leds for it in led.items}) if items else None


def ledger_key(led) -> str:
    """台账里参与挂接的内容(编号、名称、别名、国别)的指纹;变了就把库里的线索按新台账重挂一遍。"""
    if not led:
        return ""
    where = item_countries(led)
    rows = [[it["id"], where.get(it["id"]), it.get("name_en"), it.get("name_local"), it.get("aliases"), it.get("ids")] for it in led.items]
    return hashlib.sha1(json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()[:16]


def relink(store: Store, led) -> int:
    """按当前台账重算库里全部线索的规则挂接(编号、名称、别名):台账新增项目、补了编号或别名后,以前入库的线索也能对上。
    监测配置指定的、模型给的挂接保留(台账里已没有的项目、模型给的别国项目去掉)。顺序与采集时相同。→ 挂接有变的线索条数"""
    index, where = build_index(led), item_countries(led)
    changed = []
    for s in store.load_signals():
        old = s.get("links") or []
        cfg = [lk for lk in old if lk.get("by") == "config" and lk["item"] in where]
        text = "\n".join([s.get("title") or "", s.get("url") or "", ((s.get("excerpt") or "") + "\n" + _cached_docs(s, store))[:50_000],
                          str(s.get("fields") or {})])
        rule = [lk for lk in suggest(text, index, s.get("country")) if lk["item"] not in {x["item"] for x in cfg}]
        llm = [lk for lk in old if lk.get("by") == "llm" and lk["item"] in where and not foreign(where[lk["item"]], s.get("country"))
               and lk["item"] not in {x["item"] for x in cfg + rule}]
        if cfg + rule + llm != old:
            s["links"] = cfg + rule + llm
            changed.append(s)
    store.update_signals(changed)
    return len(changed)


def run(profile: Profile, only: str | None, today: date, use_llm: bool = True) -> tuple[list[CountryRun], Classifier | None]:
    cfg = {**DEFAULTS, **(getattr(profile, "watch", None) or {})}
    store = Store(storage.DATA_DIR)
    clf = Classifier(LLMConfig.from_profile(cfg.get("llm"))) if use_llm else None  # provider/model 见 profile watch.llm
    out = [run_scope(profile, sc, store, clf, today, cfg) for sc in scopes(profile, only, storage.DATA_DIR)]
    led = all_ledgers(profile)
    key = ledger_key(led)
    if key != store.state.get("ledger_key"):  # 台账改过(或第一次记指纹):以前入库的线索按新台账重挂
        n = relink(store, led) if led else 0
        store.state["ledger_key"] = key
        if n:
            print(f"  ▸ 台账有改动,按新台账重算挂接:{n} 条线索的挂接变了")
    store.save()
    return out, clf
