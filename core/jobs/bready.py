"""A36 导入营商环境数据(世界银行 B-READY):按私有配置仓 data/bready.csv(按 ISO3,由 scripts/build_bready.py 从世行数据包整理)
给国别填营商环境总分、供水得分、公共采购得分、商业水价、外企参标限制等。
同一期数据只填空(人改过的不动);文件换了新一期(「营商环境数据版本」变了)整组替换,和世行宏观指标一样要新的。
命令行:python -m core.jobs.bready [--dry-run];后台每天城市维护之后也跑一遍(新国别跟着补)。库里没有这些字段时跳过。"""
from __future__ import annotations

import argparse
import csv
import io
import math
import os
import sys
from pathlib import Path

from core import tstore

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "private" / "data"
FILE = "bready.csv"
COLS = ["iso3", "bready_total", "bready_water", "bready_procure", "bready_dispute", "water_tariff", "water_tariff_cur", "water_tariff_usd",
        "water_reliable", "water_conn_days", "foreign_bid", "award_days", "gov_pay_days", "bready_items", "bready_edition"]
NUMS = {"bready_total", "bready_water", "bready_procure", "bready_dispute", "water_tariff", "water_tariff_usd", "water_reliable", "water_conn_days",
        "award_days", "gov_pay_days"}
EDITION = "bready_edition"


def read_rows(path: Path) -> list[dict]:
    return list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8"))))


def _val(prop: str, v: str | None):
    if v in (None, ""):
        return None
    if prop not in NUMS:
        return v
    try:
        x = float(v)
    except ValueError:
        return None
    return x if math.isfinite(x) else None


def run(b: tstore.Base, data_dir: Path | None = None, dry_run: bool = False, log=print) -> dict:
    """→ {"country": 改了几行};字段没建或没有数据文件时跳过。"""
    path = (data_dir or DATA) / FILE
    if "country" not in b.t or not path.exists() or not b.has("country", EDITION):
        return {}
    rows = read_rows(path)
    props = [p for p in COLS[1:] if b.has("country", p)]
    data = {r["iso3"]: r for r in rows}
    ups, clears = [], []
    for rec in b.rows("country", props=["key", *props]):
        row = data.get(str(b.get("country", rec, "key") or ""))
        if not row:
            continue
        want = {p: _val(p, row.get(p)) for p in props}
        have = {p: b.get("country", rec, p) for p in props}
        if str(have.get(EDITION) or "") != str(want.get(EDITION) or ""):  # 新一期:整组替换,新一期没有的数清掉(不留上一期的)
            changes = {p: v for p, v in want.items() if not tstore.is_empty(v)}
            gone = [p for p, v in want.items() if tstore.is_empty(v) and not tstore.is_empty(have.get(p))]
            if gone:
                clears.append((rec["id"], {b.field("country", p)["id"]: None for p in gone}))
        else:
            changes, _ = tstore.fill_empty(have, want)
        if changes:
            ups.append((rec["id"], changes))
    n = len({rid for rid, _ in ups} | {rid for rid, _ in clears})
    if not dry_run:
        b.update_many("country", ups)
        if clears:  # update_many 不写空值(机器平时不清人的字段),换期清旧数单独发
            b.api.update_records(b.t["country"], clears)
    log(f"  营商环境(B-READY):改 {n} 国" + ("(只算,没写)" if dry_run else ""))
    return {"country": n}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m core.jobs.bready", description="A36 导入营商环境数据(世行 B-READY)")
    ap.add_argument("--dir", type=Path, default=DATA, help=f"数据目录(缺省 {DATA})")
    ap.add_argument("--dry-run", action="store_true", help="只算要改多少,不写")
    a = ap.parse_args(argv)
    b = tstore.Base(tstore.Client(*tstore.credentials(admin=not os.environ.get("TEABLE_TOKEN"))))
    print(run(b, a.dir, a.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
