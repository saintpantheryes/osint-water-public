"""A26 导入水风险与经济数据:按私有配置仓 data/city_risk.csv.gz(按 GeoNames 编号)与 data/country_risk.csv(按 ISO3)
给城市、国别填 Aqueduct 4.0 水风险、Kummu 等(2025)人均 GDP、世行涉水项目;只填空(R2)。
命令行:python -m core.jobs.risk [--dry-run];后台每天城市维护之后也跑一遍(新城市、新国别跟着补)。库里没有这些字段时跳过。"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import math
import os
import sys
from pathlib import Path

from core import tstore

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "private" / "data"
FILES = {"city": "city_risk.csv.gz", "country": "country_risk.csv"}
KEYS = {"city": "geonames", "country": "iso3"}
SOURCE = {"city": "WRI Aqueduct 4.0(2023);Kummu 等 2025 人均 GDP(2024 年值)",
          "country": "WRI Aqueduct 4.0 国别排名(2023);Kummu 等 2025 人均 GDP;世行项目库 2020-10 至 2024"}
INTS = {"gdp_pc", "wb_water_n", "wb_water_musd"}


def read_rows(path: Path) -> list[dict]:
    raw = path.read_bytes()
    text = gzip.decompress(raw).decode("utf-8") if raw[:2] == b"\x1f\x8b" else raw.decode("utf-8")
    return list(csv.DictReader(io.StringIO(text)))


def _num(v: str | None, prop: str) -> float | int | None:
    try:
        x = float(v) if v not in (None, "") else None
    except ValueError:
        return None
    if x is None or not math.isfinite(x):
        return None
    return round(x) if prop in INTS else x


def run(b: tstore.Base, data_dir: Path | None = None, dry_run: bool = False, log=print) -> dict:
    """→ {对象: 补了几行};字段没建或没有数据文件时跳过。"""
    data_dir = data_dir or DATA
    out: dict = {}
    for oid, fname in FILES.items():
        path = data_dir / fname
        if oid not in b.t or not path.exists():
            continue
        rows = read_rows(path)
        props = [p for p in (rows[0].keys() if rows else []) if p != KEYS[oid] and b.has(oid, p)]
        if not props:
            continue
        src = b.has(oid, "risk_source")
        data = {r[KEYS[oid]]: r for r in rows}
        ups = []
        for rec in b.rows(oid, props=["key", *props, *(["risk_source"] if src else [])]):
            row = data.get(str(b.get(oid, rec, "key") or ""))
            if not row:
                continue
            want = {p: _num(row.get(p), p) for p in props}
            want = {p: v for p, v in want.items() if v is not None}
            if want and src:
                want["risk_source"] = SOURCE[oid]
            changes, _ = tstore.fill_empty({p: b.get(oid, rec, p) for p in want}, want)
            if changes:
                ups.append((rec["id"], changes))
        if not dry_run:
            b.update_many(oid, ups)
        out[oid] = len(ups)
        log(f"  {oid}:补 {len(ups)} 行" + ("(只算,没写)" if dry_run else ""))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m core.jobs.risk", description="A26 导入水风险与经济数据(只填空)")
    ap.add_argument("--dir", type=Path, default=DATA, help=f"数据目录(缺省 {DATA})")
    ap.add_argument("--dry-run", action="store_true", help="只算要补多少,不写")
    a = ap.parse_args(argv)
    b = tstore.Base(tstore.Client(*tstore.credentials(admin=not os.environ.get("TEABLE_TOKEN"))))
    print(run(b, a.dir, a.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
