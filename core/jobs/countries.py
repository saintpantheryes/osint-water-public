"""国别信息(A11):每月 1 日按期刷新;线索、商机里出现了新国别时补建、补填。

国别表一国一行(ISO3 作标识)。机器字段:国名、大洲与区域(只填空);宏观与水务指标、收入组与贷款类别(按期替换——指标就是要新的);
基础信息、水务管理机构、水务国企、相关法律法规(列表只追加)。人管的市场分级(「重点」由人标)、经营区域、我方在当地、国别备注不动。
数据:世行国别元数据与 IMF 数据映射接口(core.sources 的两个适配器),快照写在本机缓存目录(不写进程序检出);抓不到时用 profile
数据目录里最近一期快照;两处都没有就只写 reference 里的基础与水务资料。
"""
from __future__ import annotations

from pathlib import Path

from core import geo, storage, tstore
from core.country_facts import METRICS, SRC_ZH, load_facts, water_desc
from core.markets import MarketsError, load_markets

from .common import Ctx

SOURCES = ("wb_country_meta", "imf_datamapper")
LISTS = (("institutions", "water_agencies"), ("enterprises", "water_soes"), ("laws", "laws"))


def _lines(items: list[str]) -> str | None:
    items = [x.strip() for x in items if x and x.strip()]
    return "\n".join(items) if items else None


def fetch_snapshots(ctx: Ctx, data_dir: Path) -> bool:
    """把世行、IMF 的国别数据抓成当日快照写到 data_dir;两个都失败才算失败。"""
    from core.sources import REGISTRY, load_all
    load_all()
    old = storage.DATA_DIR
    storage.DATA_DIR = data_dir
    ok = 0
    try:
        tables: dict[str, list[dict]] = {}
        for name in SOURCES:
            fn = REGISTRY.get(name)
            if fn is None:
                continue
            try:
                out = fn(ctx.profile)
            except Exception as e:  # noqa: BLE001  一个接口失败不拖累另一个
                ctx.log(f"  ! 国别数据 {name} 没抓到:{type(e).__name__}: {e}")
                continue
            for t, rows in out.items():
                tables.setdefault(t, []).extend(rows)
            ok += 1
        for t, rows in tables.items():
            if rows:
                storage.write_snapshot(t, rows)
    finally:
        storage.DATA_DIR = old
    return ok > 0


_fetch = fetch_snapshots  # 测试里换掉
_facts = load_facts


def _has_snapshot(d: Path) -> bool:
    return any((d / "snapshots").glob("*/country_indicator.parquet")) if d and (d / "snapshots").exists() else False


def facts_for(ctx: Ctx, isos: list[str], refresh: bool) -> dict:
    cache_dir = ctx.cache.root.parent / "country_data"
    cache_dir.mkdir(parents=True, exist_ok=True)
    if refresh or not _has_snapshot(cache_dir):
        if not _fetch(ctx, cache_dir):
            ctx.log("  ! 世行、IMF 都没抓到:用已有快照")
    data_dir = cache_dir if _has_snapshot(cache_dir) else (ctx.profile.data_dir if _has_snapshot(ctx.profile.data_dir or Path("/nonexistent")) else cache_dir)
    markets = None
    try:
        markets = load_markets(ctx.profile.markets) if ctx.profile.markets else None
    except (MarketsError, OSError) as e:
        ctx.log(f"  ! 市场分布文件读不了:{e}")
    return _facts(data_dir, markets, isos)


def scope(ctx: Ctx) -> list[str]:
    """要维护的国别:表里已有的 ∪ profile 配置的(没有行的建)。线索、商机写入时已按国别建行,所以表里的就是出现过的。"""
    isos = {c.iso3 for c in ctx.profile.countries}
    for iso in list(isos):
        ctx.country_rid(iso)
    b = ctx.base
    for r in b.rows("country", props=["key"]):
        k = b.get("country", r, "key")
        if k:
            isos.add(str(k))
    return sorted(i for i in isos if i in geo.countries() or any(c.iso3 == i for c in ctx.profile.countries))


def run(ctx: Ctx, refresh: bool = False, only_missing: bool = False) -> dict:
    """refresh:按期刷新全部(每月 1 日);only_missing:只补「宏观与水务指标」还空着的(新国别出现时,每天跑一次很便宜)。→ 计数。"""
    b = ctx.base
    isos = scope(ctx)
    rows = {str(b.get("country", r, "key")): r for r in b.rows("country") if b.get("country", r, "key")}
    if only_missing and not refresh:
        isos = [i for i in isos if i in rows and tstore.is_empty(b.get("country", rows[i], "indicators"))]
    if not isos:
        return {}
    facts = facts_for(ctx, isos, refresh)
    n_fill = n_rep = n_app = 0
    for iso in isos:
        rec = rows.get(iso) or b.record("country", ctx.country_rid(iso))
        f = facts.get(iso)
        fill = {"name_zh": geo.name_zh(iso) if iso in geo.countries() else None,
                "region": " · ".join(x for x in (geo.continent(iso), geo.region(iso)) if x) or None}
        if f and f.market:
            fill["market"] = f.market.region
        changes, _ = b.fill("country", rec, fill)
        n_fill += len(changes)
        if not f:
            continue
        upd: dict = {}
        vals = [f.values[m.key] for m in METRICS if m.key in f.values]
        ind = _lines([f"{v.metric.label}:{v.text(f.currency_code)}({SRC_ZH.get(v.source, v.source)} {v.url})" for v in vals])
        if ind and ind != b.get("country", rec, "indicators"):
            upd["indicators"] = ind  # 按期替换(R2 的例外:指标要新的)
        if f.meta:
            inc = f"{f.income()};{f.lending()}"
            if inc != b.get("country", rec, "income_group"):
                upd["income_group"] = inc
        n_rep += len(upd)
        appends = {"basics": [f"{k}:{v}" + (f"({s} {u})" if u else f"({s})") for k, v, s, u in f.basics_rows(internal=False)]}
        for part, prop in LISTS:
            appends[prop] = [" · ".join(x for x in (
                str(e.get("name_zh") or e.get("name") or e.get("name_en") or ""), str(e.get("name_local") or e.get("name_en") or ""),
                water_desc(e), str(e.get("url") or "")) if x) for e in (f.water.get(part) or [])]
        for prop, items in appends.items():
            new = tstore.append_lines(b.get("country", rec, prop), [x for x in items if x.strip()])
            if new is not None:
                upd[prop] = new
                n_app += 1
        if upd:
            b.update("country", rec["id"], upd)
    ctx.log(f"  ▸ 国别信息:{len(isos)} 国,填空 {n_fill} 处、替换 {n_rep} 处、追加 {n_app} 处")
    return {"国别": len(isos), "填空": n_fill, "替换": n_rep, "追加": n_app}
