"""补全上传的线索(A03 的 R8 分支):每 2 分钟。

同事在线索表新建一行、填原文网址(来源方式缺省「用户上传」,A02)。这里找「来源方式 = 用户上传且判定方式为空」的行:抓网页(或 PDF、Word),
补标题、原文摘录、国别、发布日期、中文摘要、类别、领域、出资方、项目名称、金额;信源记「人工上传」;是否相关恒为「是」,
判定方式写「不判定」(不判相关性)。挂接建议与"可能是新项目"也给(core.jobs.suggest),人确认归属。
只填空:同事自己填了的标题、国别不动。抓不到网页的也写判定方式(标题留原样、摘录写抓取失败),不反复抓。
"""
from __future__ import annotations

import httpx

from core import geo, tstore
from core.sources.http import SourceBlocked
from core.watch import fetch
from core.watch.link import build_index, item_countries, suggest as rule_links
from core.watch.manual import SOURCE_NAME, _from_url
from core.watch.outputs import MANUAL
from core.watch.run import _merge_llm
from core.watch.store import signal_id

from . import suggest as sg
from .common import Ctx, lead_values, now_iso
from .collect import write_milestones


def pending(ctx: Ctx) -> list[dict]:
    b = ctx.base
    flt = b.filter(b.cond("lead", "origin", "is", "用户上传"), b.cond("lead", "judged_by", "isEmpty"))
    return [r for r in b.rows("lead", filter=flt) if b.get("lead", r, "url")]


def run(ctx: Ctx, limit: int = 20) -> dict:
    rows = pending(ctx)[:limit]
    if not rows:
        return {}
    b = ctx.base
    src_rid = ctx.source_rid(MANUAL, SOURCE_NAME, "人工上传", "全球")
    opps = ctx.opps()
    led = opps.ledger_like()
    index = build_index(led) if led else []
    judged = []
    n_ok = n_fail = 0
    for rec in rows:
        url = str(b.get("lead", rec, "url")).strip()
        sig = {"id": signal_id(MANUAL, url), "source": MANUAL,
               "source_name": SOURCE_NAME, "url": url, "title": b.get("lead", rec, "title") or "", "published": None, "fields": {},
               "rules": {"candidate": True}, "docs": [], "llm": None, "links": [], "first_seen": now_iso(),
               "country": ctx.iso_of(tstore.link_id(b.get("lead", rec, "country")))}
        try:
            title, body, sig["docs"] = _from_url(url, ctx.cache)
        except (fetch.FetchError, SourceBlocked, httpx.HTTPError, OSError, ValueError) as e:
            n_fail += 1
            b.fill("lead", rec, {"key": sig["id"][:8], "source": src_rid, "relevant": True, "judged_by": "不判定",
                                 "excerpt": f"抓取失败:{type(e).__name__}: {e}"[:500]})
            continue
        sig["title"] = sig["title"] or title
        sig["excerpt"] = body[:1500]
        if not sig["country"]:
            sig["country"] = next(iter(geo.detect(f"{sig['title']}\n{body[:3000]}")), None)
        sig["links"] = rule_links("\n".join([sig["title"], url, body[:50_000]]), index, sig["country"])
        if ctx.clf is not None and ctx.clf.enabled:
            system = ctx.clf.system(geo.name_zh(sig["country"]) if sig["country"] else "各国", (ctx.profile.bidder or {}).get("nationality"),
                                    opps.ledger_like(sig["country"]))
            out = ctx.clf.classify(system, sig, body)
            if out is not None and "_error" not in out:
                _merge_llm(sig, out, item_countries(led))
                sig["country"] = sig.get("country") or out.get("country")
        vals = lead_values(ctx, sig, src_rid, ctx.country_rid(sig.get("country")))
        vals.update({"relevant": True, "judged_by": "不判定"})  # R8:上传的不判相关性
        vals.pop("status", None)
        vals.pop("origin", None)
        b.fill("lead", rec, vals)
        write_milestones(ctx, [(sig, rec, None)])
        if sig.get("llm"):
            sig["llm"]["relevant"] = True
            judged.append((sig, rec))
        n_ok += 1
    stats = {"补全": n_ok, "抓取失败": n_fail}
    if judged:
        stats.update(sg.run_round(ctx, judged))
    return stats
