"""A25 导入存量水厂:按私有配置仓的 data/plants.csv.gz(使用方 6 月做的 water-tracker 的水厂数据导出,只留国别表里有的国家)
把自来水厂(含海水淡化厂)、污水厂(含中水回用厂)建进两张表;只新建、只填空(R2),去重按编号。
城市:导入数据的 GeoNames 编号在城市表里就挂它,不在的挂同一国家 25 公里内最近的规模以上城市,再没有就只挂国别。
命令行:python -m core.jobs.plants [--file 路径] [--dry-run];后台每月 1 日城市维护之后也跑一遍(补新城市的关联)。库里还没建这两张表时跳过。"""
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
DATA = ROOT / "private" / "data" / "plants.csv.gz"
NEAR_KM = 25.0
OIDS = {"supply": "supply_plant", "sewage": "sewage_plant"}
FIELDS = ("name", "kind", "status", "capacity", "pop_served", "built", "process", "level", "operator", "coords", "source")


def read_rows(path: Path) -> list[dict]:
    raw = path.read_bytes()
    text = gzip.decompress(raw).decode("utf-8") if raw[:2] == b"\x1f\x8b" else raw.decode("utf-8")
    return list(csv.DictReader(io.StringIO(text)))


def _num(v: str | None) -> float | None:
    try:
        x = float(v) if v not in (None, "") else None
    except ValueError:
        return None
    return x if x is None or math.isfinite(x) else None


def _km(a: tuple[float, float], b: tuple[float, float]) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(h)))


def _coords(v: object) -> tuple[float, float] | None:
    try:
        la, lo = (float(x) for x in str(v or "").replace(",", ",").split(","))
        return la, lo
    except ValueError:
        return None


class CityIndex:
    """城市表:GeoNames 编号 → 记录;同一国家里最近的城市(25 公里内)。"""

    def __init__(self, b: tstore.Base):
        self.by_key: dict[str, str] = {}
        self.by_country: dict[str, list[tuple[tuple[float, float], str]]] = {}
        if "city" not in b.t:
            return
        for r in b.rows("city", props=["key", "country", "coords"]):
            key, crid, xy = b.get("city", r, "key"), tstore.link_id(b.get("city", r, "country")), _coords(b.get("city", r, "coords"))
            if key:
                self.by_key[str(key)] = r["id"]
            if crid and xy:
                self.by_country.setdefault(crid, []).append((xy, r["id"]))

    def find(self, geonames: str | None, country_rid: str | None, xy: tuple[float, float] | None) -> str | None:
        if geonames and geonames in self.by_key:
            return self.by_key[geonames]
        if not (country_rid and xy):
            return None
        best = min(((_km(xy, c), rid) for c, rid in self.by_country.get(country_rid, [])), default=None)
        return best[1] if best and best[0] <= NEAR_KM else None


def values(row: dict, country_rid: str, city_rid: str | None) -> dict:
    """数据文件的一行 → 本体属性的值(空的不写)。"""
    xy = (_num(row.get("lat")), _num(row.get("lon")))
    v = {"name": row.get("name") or None, "kind": row.get("kind") or None, "status": row.get("status") or None, "country": country_rid, "city": city_rid,
         "capacity": _num(row.get("capacity")), "pop_served": _num(row.get("pop_served")), "built": row.get("built") or None,
         "process": row.get("process") or None, "level": row.get("level") or None, "operator": row.get("operator") or None,
         "coords": f"{xy[0]:.5f},{xy[1]:.5f}" if None not in xy else None, "source": row.get("source") or None}
    if v["pop_served"] is not None:
        v["pop_served"] = round(v["pop_served"])
    return {k: x for k, x in v.items() if x is not None}


def run(b: tstore.Base, path: Path | None = None, dry_run: bool = False, log=print) -> dict:
    """→ {对象: {新建, 补空, 挂上城市}};两张表都没建或没有数据文件时跳过。"""
    path = path or DATA
    todo = {g: oid for g, oid in OIDS.items() if oid in b.t}
    if not todo:
        return {"跳过": "库里还没有自来水厂、污水厂表(先跑建表命令)"}
    if not path.exists():
        return {"跳过": f"没有数据文件 {path}"}
    rows = read_rows(path)
    countries = {str(b.get("country", r, "key")): r["id"] for r in b.rows("country", props=["key"]) if b.get("country", r, "key")}
    cities = CityIndex(b)
    out: dict = {}
    for group, oid in todo.items():
        props = ["key", "country", "city", *[p for p in FIELDS if b.has(oid, p)]]
        have = {str(b.get(oid, r, "key")): r for r in b.rows(oid, props=props) if b.get(oid, r, "key")}
        new, ups, n_city = [], [], 0
        for row in rows:
            if row.get("group") != group:
                continue
            crid = countries.get(str(row.get("iso3") or ""))
            if not crid:
                continue
            xy = (_num(row.get("lat")), _num(row.get("lon")))
            city = cities.find(row.get("geonames") or None, crid, xy if None not in xy else None)
            want = {k: x for k, x in values(row, crid, city).items() if k in ("country", "city") or b.has(oid, k)}
            rec = have.get(row["key"])
            if rec is None:
                new.append({"key": row["key"], **want})
                n_city += bool(city)
                continue
            cur = {p: b.get(oid, rec, p) for p in want}
            changes, _ = tstore.fill_empty(cur, want)
            if changes:
                ups.append((rec["id"], changes))
                n_city += "city" in changes
        if not dry_run:
            b.create(oid, new)
            b.update_many(oid, ups)
        out[oid] = {"新建": len(new), "补空": len(ups), "挂上城市": n_city}
        log(f"  {oid}:新建 {len(new)},补空 {len(ups)},挂上城市 {n_city}" + ("(只算,没写)" if dry_run else ""))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m core.jobs.plants", description="A25 导入存量水厂(只新建、只填空)")
    ap.add_argument("--file", type=Path, default=DATA, help=f"数据文件(缺省 {DATA})")
    ap.add_argument("--dry-run", action="store_true", help="只算要建、要补多少,不写")
    a = ap.parse_args(argv)
    b = tstore.Base(tstore.Client(*tstore.credentials(admin=not os.environ.get("TEABLE_TOKEN"))))  # 机器账号的令牌;只有管理员令牌时用它
    print(run(b, a.file, a.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
