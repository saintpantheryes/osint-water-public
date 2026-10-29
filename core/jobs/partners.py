"""A51 导入潜在合作伙伴:按私有配置仓的 data/partners.csv(每国 3 家本地承包商,公开资料待核实)建进「供应商」表,勾上「潜在合作伙伴」;
只新建、只填空(R2):标识照 A28 的名称规范化,中过标、已在表里的同一家只补空并勾上;人改过的中文名、简介、理由不动,人去掉的勾不打回。
国别表里没有的国家跳过;选项里没有的性质、领域不写。
命令行:python -m core.jobs.partners [--path 路径] [--dry-run]。库里还没建表、没建字段时跳过。"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from core import tstore
from core.jobs.awards import contractor_key, read_rows, write_rows

ROOT = Path(__file__).resolve().parents[2]
PARTNERS = ROOT / "private" / "data" / "partners.csv"
COLS = ["key", "iso3", "rank", "name", "name_zh", "kind", "website", "sectors", "profile", "reason", "url", "note"]
OID = "contractor"

__all__ = ["COLS", "PARTNERS", "read_rows", "write_rows", "run"]


def _countries(b: tstore.Base) -> dict[str, tuple[str, str]]:
    """国别码 → (记录编号, 中文名)"""
    return {str(b.get("country", r, "key")): (r["id"], str(b.get("country", r, "name_zh") or ""))
            for r in b.rows("country", props=["key", "name_zh"]) if b.get("country", r, "key")}


def _sectors(v: str | None, allowed: set[str]) -> list[str]:
    return [x for x in dict.fromkeys(s.strip() for s in str(v or "").replace("、", ";").replace(",", ";").split(";")) if x and x in allowed]


def run(b: tstore.Base, path: Path | None = None, dry_run: bool = False, log=print) -> dict:
    """→ {新建, 补空, 国别表里没有, 没有名称}"""
    path = path or PARTNERS
    if OID not in b.t:
        return {"跳过": "库里还没有「供应商」表(先跑建表命令)"}
    if not b.has(OID, "partner"):
        return {"跳过": "供应商表还没有「潜在合作伙伴」字段(先跑建表命令)"}
    if not path.exists():
        return {"跳过": f"没有数据文件 {path}"}
    countries = _countries(b)
    choices = lambda p: {c["name"] for c in (b.field(OID, p).get("options") or {}).get("choices") or []} if b.has(OID, p) else set()  # noqa: E731
    kinds, sectors = choices("kind"), choices("sectors")
    want: dict[str, dict] = {}
    skipped = bad = 0
    for r in read_rows(path):
        name = " ".join(str(r.get("name") or "").split())
        k = contractor_key(name)
        if not k:
            bad += 1
            continue
        iso = str(r.get("iso3") or "").strip().upper()
        if iso not in countries:
            skipped += 1
            continue
        crid, zh = countries[iso]
        try:
            rank = int(float(str(r.get("rank") or "").strip()))
        except ValueError:
            rank = None
        v = {"name": name, "name_zh": r.get("name_zh"), "nationality": zh or None, "nationality_code": iso, "country": crid,
             "kind": r.get("kind") if str(r.get("kind") or "").strip() in kinds else None,
             "website": r.get("website"), "sectors": _sectors(r.get("sectors"), sectors) or None,
             "profile": r.get("profile"), "partner": True, "partner_rank": rank, "partner_reason": r.get("reason"), "source_url": r.get("url")}  # 备注列归人写(R2),不导
        v = {p: (x.strip() if isinstance(x, str) else x) for p, x in v.items() if x not in (None, "", []) and b.has(OID, p)}
        want.setdefault(k, v)  # 同一家写了两行的,先写的算
    props = sorted({p for v in want.values() for p in v} | {"key"})
    have = {str(b.get(OID, r, "key")): r for r in b.rows(OID, props=props) if b.get(OID, r, "key")}
    new, ups = [], []
    for k, v in want.items():
        rec = have.get(k)
        if rec is None:
            new.append({"key": k, **v})
            continue
        cur = {p: b.get(OID, rec, p) for p in v}
        if "country" in cur:
            cur["country"] = tstore.link_id(cur["country"])
        if "partner" in v and not tstore.is_empty(cur.get("partner_rank")) and tstore.is_empty(cur.get("partner")):
            v = {p: x for p, x in v.items() if p != "partner"}  # 导过(有推荐排序)、人去掉了勾的:不打回
        changes, _ = tstore.fill_empty(cur, v)
        if changes:
            ups.append((rec["id"], changes))
    if not dry_run:
        b.create(OID, new)
        b.update_many(OID, ups)
    log(f"  contractor(合作伙伴):新建 {len(new)},补空 {len(ups)},国别表里没有的跳过 {skipped},没有名称 {bad}" + ("(只算,没写)" if dry_run else ""))
    return {"新建": len(new), "补空": len(ups), "国别表里没有": skipped, "没有名称": bad}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m core.jobs.partners", description="A51 导入潜在合作伙伴(只新建、只填空)")
    ap.add_argument("--path", type=Path, default=PARTNERS, help=f"数据文件(缺省 {PARTNERS})")
    ap.add_argument("--dry-run", action="store_true", help="只算要建、要补多少,不写")
    a = ap.parse_args(argv)
    b = tstore.Base(tstore.Client(*tstore.credentials(admin=not os.environ.get("TEABLE_TOKEN"))))
    print(run(b, a.path, a.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
