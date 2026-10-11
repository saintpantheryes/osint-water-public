"""A21 折算人民币:每个后台周期,把「投资规模与货币都有、折合人民币为空」的线索与商机按当日汇率折成万元(core/fx.py);
商机顺带补「规模(美元)」。旧线索「金额」文字可解析的(「5,000,000 USD」)先拆成投资规模与货币。只填空(R2);库里还没建这些字段时跳过。"""
from __future__ import annotations

from core import fx as fx_mod

from .common import Ctx

BATCH = 200


def _ready(ctx: Ctx, oid: str, props: tuple[str, ...]) -> bool:
    return all(ctx.base.has(oid, p) for p in props)


def split_lead_amounts(ctx: Ctx, limit: int = BATCH) -> int:
    """线索:「金额」文字 → 投资规模 + 货币(只在投资规模为空时;币种不在选项里的留空、只填数字)。"""
    b = ctx.base
    if not _ready(ctx, "lead", ("amount", "amount_value", "currency")):
        return 0
    allowed = {c["name"] for c in (b.field("lead", "currency").get("options") or {}).get("choices") or []}
    rows = b.rows("lead", filter=b.filter(b.cond("lead", "amount", "isNotEmpty"), b.cond("lead", "amount_value", "isEmpty")), props=["amount"])
    out = []
    for r in rows[:limit]:
        parsed = fx_mod.parse_amount(b.get("lead", r, "amount"))
        if parsed:
            out.append((r["id"], {"amount_value": parsed[0], "currency": parsed[1] if parsed[1] in allowed else None}))
    b.update_many("lead", out)
    return len(out)


def convert(ctx: Ctx, oid: str, value_prop: str, cny_prop: str, usd_prop: str | None = None, limit: int = BATCH) -> int:
    b = ctx.base
    if not _ready(ctx, oid, (value_prop, "currency", cny_prop)):
        return 0
    rows = b.rows(oid, filter=b.filter(b.cond(oid, value_prop, "isNotEmpty"), b.cond(oid, "currency", "isNotEmpty"), b.cond(oid, cny_prop, "isEmpty")),
                  props=[value_prop, "currency"] + ([usd_prop] if usd_prop and b.has(oid, usd_prop) else []))
    out = []
    for r in rows[:limit]:
        v, cur = b.get(oid, r, value_prop), b.get(oid, r, "currency")
        cny = ctx.fx.to_cny(v, cur)
        if cny is None:
            continue
        vals = {cny_prop: fx_mod.wan(cny)}
        if usd_prop and b.has(oid, usd_prop) and b.get(oid, r, usd_prop) in (None, ""):
            usd = ctx.fx.to_usd(v, cur)
            if usd is not None:
                vals[usd_prop] = round(usd)
        out.append((r["id"], vals))
    b.update_many(oid, out)
    return len(out)


def run(ctx: Ctx) -> dict:
    out: dict = {}
    n = split_lead_amounts(ctx)
    if n:
        out["线索拆金额"] = n
    n = convert(ctx, "lead", "amount_value", "amount_cny")
    if n:
        out["线索折算"] = n
    n = convert(ctx, "opportunity", "size_value", "size_cny", "size_usd")
    if n:
        out["商机折算"] = n
    if out and ctx.fx.source:
        out["汇率"] = ctx.fx.source
    return out
