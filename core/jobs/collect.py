"""采集与判定(A01、A03;架构方案第 4 节第一行):每天 08:30。

读:信源表里启用的信源(怎么抓在 git 的 watch.yaml,按信源编号对应)、新库里已有线索的线索号与原文网址(去重)、商机(挂接)。
写:新线索(含判定字段与判定方式)、时间节点(按「所属 + 类型 + 日期原文」去重)、信源的最近采集与最近状态;
判定之后对这一轮刚判定的线索做建议与更新商机(core.jobs.suggest)。
只判「待处理且判定方式为空」的线索:新采的在这里判;以前没判成(没配模型、限流)的机器线索最后按原文摘录补判(E1)。

采集器、规则初筛、详情页与附件、模型判定沿用 core.watch 的现有逻辑(run_scope 的同一段),只把"写文件"换成"写新库"。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import timedelta

from core import geo, tstore
from core.sources.http import SourceBlocked
from core.watch import fetch
from core.watch.collectors import KINDS, Ctx as CCtx
from core.watch.link import build_index, item_countries, suggest as rule_links
from core.watch.outputs import MANUAL
from core.watch.rules import evaluate
from core.watch.run import (
    DEFAULTS, GLOBAL, STORE_ALL_KINDS, _amount, _item_country, _merge_llm, _old, _page_diff, follow, global_paths, load_watch,
)
from core.watch.store import Item, signal_id
from core.watch.text import scrub_contacts

from . import suggest as sg
from .common import Ctx, deadlines_of, judged_by, lead_no, lead_values, now_iso


@dataclass
class Scope:
    key: str  # 国别 ISO3 或 global
    iso3: str | None
    label: str
    wc: dict
    names: list[str] = field(default_factory=list)
    wb_code: str | None = None


@dataclass
class Summary:
    sources: dict[str, dict] = field(default_factory=dict)  # 信源编号 → {status, items, new, error}
    leads: int = 0
    judged: int = 0
    milestones: int = 0
    rejudged: int = 0
    ignored: list[str] = field(default_factory=list)  # 忽略、没采的国别(ISO3)
    dropped: int = 0  # 全球信源里判到忽略国别、没入库的条目
    suggest: dict = field(default_factory=dict)
    note: str = ""

    def text(self) -> str:
        lines = [f"新线索 {self.leads} 条(判定 {self.judged},补判 {self.rejudged}),时间节点 {self.milestones} 个"]
        for sid, st in self.sources.items():
            flag = {"ok": "·", "empty": "·", "error": "✗", "blocked": "⛔", "skip": "-"}.get(st["status"], "?")
            extra = (f"条目 {st.get('items')} 新增 {st.get('new')}" if st["status"] in ("ok", "empty") else st.get("error") or st["status"])
            lines.append(f"  {flag} {sid:<22} {extra}")
        if self.ignored or self.dropped:
            lines.append(f"  忽略国别不采:{','.join(self.ignored) or '—'};全球信源里判到忽略国别没入库 {self.dropped} 条")
        if self.suggest:
            lines.append("  建议与更新商机:" + ",".join(f"{k} {v}" for k, v in self.suggest.items()))
        if self.note:
            lines.append("  " + self.note)
        return "\n".join(lines)


def scopes(ctx: Ctx, only: str | None = None) -> list[Scope]:
    """git 里的监测配置:各国(profile 里配了 watch 的)+ 全球。抓不抓看信源表(启用),这里只列配置。"""
    out = []
    for c in ctx.profile.countries:
        if c.watch:
            wc = load_watch(c.watch)
            out.append(Scope(c.iso3, c.iso3, c.name_zh or c.name_en, wc, wc.get("names") or [c.name_en], c.wb_country_code))
    gl = {"schema": "osint-water/watch@1", "names": [], "entities": [], "sources": []}
    for fp in global_paths(ctx.profile):
        w = load_watch(fp)
        gl["entities"] += w.get("entities") or []
        gl["sources"] += w["sources"]
    if gl["sources"]:
        out.append(Scope(GLOBAL, None, "全球", gl))
    if only:
        out = [s for s in out if s.key.lower() == only.lower()]
    return out


def run(ctx: Ctx, only: str | None = None, source: str | None = None, with_suggest: bool = True) -> Summary:
    cfg = {**DEFAULTS, **(ctx.profile.watch or {})}
    res = Summary()
    fetch.reset_blocked()  # 常驻的后台任务:上一轮被限流的主机这一轮重新试
    nos, urls = ctx.lead_keys()
    opps = ctx.opps()
    judged_round: list[tuple[dict, dict]] = []  # (线索 dict, 新库记录)
    skip = ctx.ignored()
    for sc in scopes(ctx, only):
        if sc.iso3 and sc.iso3.upper() in skip:
            res.ignored.append(sc.iso3)
            continue
        judged_round += _run_scope(ctx, sc, cfg, nos, urls, opps, res, source)
    res.rejudged = _rejudge_pending(ctx, cfg, opps, judged_round)
    if with_suggest and judged_round:
        res.suggest = sg.run_round(ctx, judged_round)
    if ctx.clf is not None:
        res.note = f"模型:调用 {ctx.clf.calls} 次;{ctx.clf.note or ''}{';出错 ' + str(len(ctx.clf.errors)) if ctx.clf.errors else ''}"
    ctx.cache.save()
    return res


def _run_scope(ctx: Ctx, sc: Scope, cfg: dict, nos: dict, urls: dict, opps, res: Summary, only_source: str | None) -> list[tuple[dict, dict]]:
    led = opps.ledger_like(sc.iso3)
    index = build_index(led) if led else []
    ledger_ids = item_countries(led)
    clf = ctx.clf
    system = clf.system(sc.label, (ctx.profile.bidder or {}).get("nationality"), led) if clf else ""
    cctx = CCtx(iso3=sc.iso3 or "", names=sc.names, wb_code=sc.wb_code or "", since=ctx.today - timedelta(days=cfg["lookback_days"]), today=ctx.today)
    now = now_iso()
    store = ctx.cache
    sigs: list[dict] = []
    jobs: list[tuple[dict, str]] = []
    for src in sc.wc["sources"]:
        sid = src["id"]
        if only_source and sid != only_source:
            continue
        rid = ctx.source_rid(sid, src.get("name"), _kind_zh(src), src.get("country") or (sc.iso3 or "全球"), src.get("url"))
        if not ctx.source_enabled(sid):
            res.sources[sid] = {"status": "skip", "error": "表里停用"}
            continue
        cctx.warnings.clear()
        cctx.since = ctx.today - timedelta(days=src.get("lookback_days", cfg["lookback_days"]))
        cutoff = cctx.since.isoformat()
        try:
            items = KINDS[src["kind"]](src, cctx)
        except SourceBlocked as e:
            res.sources[sid] = {"status": "blocked", "error": str(e)[:300]}
            ctx.source_status(sid, "blocked", now)
            continue
        except Exception as e:  # noqa: BLE001  单个信源失败不影响其余
            res.sources[sid] = {"status": "error", "error": f"{type(e).__name__}: {e}"[:300]}
            ctx.source_status(sid, "error", now)
            continue
        first = store.is_first_run(sid) and not ctx.base.get("source", ctx.sources()[sid], "last_run")
        fresh: list[Item] = []
        for it in items:
            prev = store.seen(sid, it.key)
            if src["kind"] == "page":
                cache = store.doc_path("page:" + it.url)
                old = cache.read_text(encoding="utf-8") if cache.exists() else None
                text = scrub_contacts(it.text)
                cache.write_text(text, encoding="utf-8")
                if old is not None and it.fingerprint != (prev or {}).get("fp"):
                    it.change = "changed"
                    it.text = _page_diff(scrub_contacts(old), text)
                    fresh.append(it)
            elif prev is None:
                if not (first and _old(it, cutoff, ctx.today)):
                    fresh.append(it)
            elif it.fingerprint and prev.get("fp") and prev["fp"] != it.fingerprint:
                it.change = "changed"
                fresh.append(it)
            store.mark_seen(sid, it.key, it.fingerprint, ctx.today.isoformat())
        n_kept = 0
        default_store = "all" if sc.iso3 and src["kind"] in STORE_ALL_KINDS else "candidates"
        for it in fresh:
            sig_id = signal_id(sid, it.key + (f"#{it.fingerprint}" if it.change == "changed" else ""))
            if lead_no(sig_id) in nos or (it.url or "").strip().rstrip("/") in urls:
                continue  # 新库里已有(以前入库过、或同一网址由别的信源入过)
            it.text = scrub_contacts(it.text)
            r = evaluate(it, src, sc.wc.get("entities") or [])
            if not r["candidate"] and src.get("store", default_store) != "all":
                continue
            sig = {"id": sig_id, "country": _item_country(it, src, _scope_ns(sc)), "scope": sc.key, "source": sid,
                   "source_name": src.get("name") or sid, "kind": src["kind"], "key": it.key, "url": it.url, "title": it.title,
                   "published": it.published, "first_seen": now, "change": it.change, "lang": it.lang,
                   "fields": {k: v for k, v in it.fields.items() if v}, "rules": r, "docs": [], "llm": None, "links": [], "_rid": rid}
            if sig["country"] and str(sig["country"]).upper() in ctx.ignored():  # 忽略的国别:详情页也不去抓
                res.dropped += 1
                continue
            doc = ""
            if r["candidate"] and src.get("follow"):
                doc, sig["docs"] = follow(it, src, store)
            full = "\n".join(x for x in (it.text, doc) if x)
            if doc:
                r2 = evaluate(Item(key=it.key, url=it.url, title=it.title, text=full, fields=it.fields), src, sc.wc.get("entities") or [])
                keep = r["stage_from"] == "head"
                r.update({"stage": r["stage"] if keep else r2["stage"], "stage_from": r["stage_from"] if keep else r2["stage_from"],
                          "stage_terms": r2["stage_terms"], "water_terms": r2["water_terms"], "sectors": r2["sectors"]})
                if not sig["country"]:
                    sig["country"] = next(iter(geo.detect(full[:3000])), None)
            if sig["country"] and str(sig["country"]).upper() in ctx.ignored():
                res.dropped += 1
                continue
            if src.get("min_amount") and doc:
                amt = _amount(doc, src["min_amount"])
                if amt is not None:
                    sig["fields"]["estimate"] = f"{amt:,.0f} {src['min_amount'].get('currency', '')}".strip()
                    if amt < src["min_amount"]["value"]:
                        continue
            sig["excerpt"] = full[:1500] if doc else it.text[:6000]
            sig["links"] = rule_links("\n".join([it.title, it.url, full[:50_000], str(it.fields)]), index, sig["country"])
            if r["candidate"] and clf is not None and system:
                jobs.append((sig, full))
            sigs.append(sig)
            nos[lead_no(sig_id)] = "(本轮)"
            if it.url:
                urls[it.url.strip().rstrip("/")] = "(本轮)"
            n_kept += 1
        status = "ok" if items else "empty"
        warn = ";".join(cctx.warnings)[:300] or None
        store.health(sid, status=status, error=warn, last_run=now, last_ok=now, items=len(items), new=n_kept)
        ctx.source_status(sid, status, now)
        res.sources[sid] = {"status": status, "items": len(items), "new": n_kept, "baseline": first, "error": warn}
    if clf is not None and system and jobs:
        for (sig, _), out in zip(jobs, clf.classify_many(system, jobs)):
            if out is not None:
                _merge_llm(sig, out, ledger_ids)
    return _write_leads(ctx, sigs, res)


def _scope_ns(sc: Scope):
    from types import SimpleNamespace
    return SimpleNamespace(iso3=sc.iso3)


def _kind_zh(src: dict) -> str | None:
    from .common import SOURCE_KIND
    return SOURCE_KIND.get(src.get("kind"))


def _write_leads(ctx: Ctx, sigs: list[dict], res: Summary) -> list[tuple[dict, dict]]:
    """新线索写进线索表(含判定字段),截止日写成时间节点。→ 本轮判定了的 (线索 dict, 记录)。"""
    if not sigs:
        return []
    rows = []
    for s in sigs:
        rows.append(lead_values(ctx, s, s.get("_rid"), ctx.country_rid(s.get("country"))))
    recs = ctx.base.create("lead", rows)
    res.leads += len(recs)
    out = []
    for s, rec in zip(sigs, recs):
        if judged_by(s):
            res.judged += 1
        if judged_by(s) == "模型":
            out.append((s, rec))
    res.milestones += write_milestones(ctx, [(s, rec, None) for s, rec in zip(sigs, recs)])
    return out


def milestone_keys(ctx: Ctx, lead_no_of: dict[str, str] | None = None, opp_code_of: dict[str, str] | None = None) -> dict[str, dict]:
    """新库里已有时间节点:键(所属 lead:线索号 / opp:商机编号 / cty:ISO3 + 类型 + 日期原文)→ 记录。"""
    b = ctx.base
    if lead_no_of is None:
        lead_no_of = {rid: no for no, rid in ctx.lead_keys()[0].items()}
    if opp_code_of is None:
        opp_code_of = {r["rid"]: r["code"] for r in ctx.opps().rows}
    keys: dict[str, dict] = {}
    for r in b.rows("milestone", props=["lead", "opportunity", "country", "kind", "日期原文"]):
        kind, raw = b.get("milestone", r, "kind"), b.get("milestone", r, "日期原文")
        ld, op, cy = (b.get("milestone", r, p) for p in ("lead", "opportunity", "country"))
        from core.tstore import link_id
        owner = None
        if link_id(ld) in lead_no_of:
            owner = "lead:" + lead_no_of[link_id(ld)]
        elif link_id(op) in opp_code_of:
            owner = "opp:" + opp_code_of[link_id(op)]
        elif ctx.iso_of(link_id(cy)):
            owner = "cty:" + str(ctx.iso_of(link_id(cy)))
        if owner and kind and raw:
            keys[f"{owner}|{kind}|{raw}"] = r
    return keys


def write_milestones(ctx: Ctx, triples: list[tuple[dict, dict, str | None]], existing: dict[str, dict] | None = None) -> int:
    """线索给出的截止日 → 时间节点(线索;所属商机已定的也关联商机),按「所属 + 类型 + 日期原文」去重;
    已有的节点还没挂商机、这次定了商机的,补上商机。triples = [(线索 dict, 线索记录, 商机记录编号或 None)]。→ 新建几条。"""
    rows: list[dict] = []
    keys: dict[str, dict] = {} if existing is None else existing
    if existing is None and any(deadlines_of(s) for s, _, _ in triples):
        keys = milestone_keys(ctx)
    b = ctx.base
    for s, rec, opp_rid in triples:
        no = lead_no(s["id"])
        for day, kind, label in deadlines_of(s):
            k = f"lead:{no}|{kind}|{day}"
            if k in keys:
                old = keys[k]
                if opp_rid and old.get("id") and tstore.is_empty(b.get("milestone", old, "opportunity")):
                    b.update("milestone", old["id"], {"opportunity": opp_rid})
                    old.setdefault("fields", {})[b.fid("milestone", "opportunity")] = {"id": opp_rid}
                continue
            row = {"lead": rec["id"], "opportunity": opp_rid, "kind": kind, "day": day, "日期原文": day, "status": "官方计划",
                   "label": label or None, "source": rec["id"]}
            keys[k] = row
            rows.append(row)
    if rows:
        made = b.create("milestone", rows)
        for row, r in zip(rows, made):
            for k, v in keys.items():
                if v is row:
                    keys[k] = r
    return len(rows)


def _rejudge_pending(ctx: Ctx, cfg: dict, opps, judged_round: list) -> int:
    """以前没判成的机器线索(待处理、判定方式为空、不是上传的):按原文摘录补判(模型可用时)。判成的加进本轮,也做建议与更新。"""
    clf = ctx.clf
    if clf is None or not clf.enabled:
        return 0
    b = ctx.base
    flt = b.filter(b.cond("lead", "status", "is", "待处理"), b.cond("lead", "judged_by", "isEmpty"), b.cond("lead", "origin", "is", "机器采集"))
    pending = [r for r in b.rows("lead", filter=flt) if b.get("lead", r, "excerpt")]
    if not pending:
        return 0
    led = opps.ledger_like()
    ledger_ids = item_countries(led)
    system = clf.system("各国", (ctx.profile.bidder or {}).get("nationality"), led)
    jobs = []
    for r in pending:
        src = ctx.sources()
        src_title = (b.get("lead", r, "source") or {})
        sid = next((k for k, v in src.items() if v["id"] == (src_title.get("id") if isinstance(src_title, dict) else None)), "")
        sig = {"id": str(b.get("lead", r, "key") or r["id"]), "source": sid, "source_name": src_title.get("title") if isinstance(src_title, dict) else sid,
               "url": b.get("lead", r, "url"), "title": b.get("lead", r, "title"), "published": b.get("lead", r, "published"),
               "fields": {}, "rules": {"candidate": True}, "excerpt": b.get("lead", r, "excerpt"), "links": [],
               "country": ctx.iso_of(_lid(b.get("lead", r, "country")))}
        jobs.append((sig, sig["excerpt"]))
    done = 0
    for (sig, _), out in zip(jobs, clf.classify_many(system, jobs)):
        if out is None:
            continue
        sig["links"] = rule_links("\n".join([sig["title"] or "", sig["url"] or "", sig["excerpt"] or ""]), build_index(led) if led else [], sig["country"])
        _merge_llm(sig, out, ledger_ids)
        rec = next(r for r in pending if str(b.get("lead", r, "key") or r["id"]) == sig["id"])
        vals = lead_values(ctx, sig, None, ctx.country_rid(sig.get("country")))
        want = {k: vals[k] for k in ("relevant", "category", "sectors", "country", "summary_zh", "funder", "project_name", "amount", "judged_by")}
        b.fill("lead", rec, want)
        write_milestones(ctx, [(sig, rec, None)])
        judged_round.append((sig, rec))
        done += 1
    return done


def _lid(v):
    from core.tstore import link_id
    return link_id(v)


__all__ = ["run", "Summary", "scopes", "write_milestones", "milestone_keys", "json", "MANUAL"]
