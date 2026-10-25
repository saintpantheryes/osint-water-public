"""把 MSDCW 街区数据(figshare 10.6084/m9.figshare.26021314,Tang 等 2025《Scientific Data》,CC BY 4.0)转成城市页「街区」页签能直接读的小文件:
每座城市每个层级一个 GeoJSON(media/blocks/<城市码>_L<层级>.geojson),一个城市轮廓(<城市码>_outline.geojson,1 级街区合并后的外边界,页面上用另一种颜色画),外加一张目录 media/blocks/index.json({城市码: {name, levels: {层级: 街区数}}}),
页面按目录决定要不要显示「街区」页签。一次性离线跑;原始数据是 2022 年 5 月 OpenStreetMap 道路切出来的,只收人口 50 万以上的城市。

    python scripts/build_city_blocks.py --shp-dir <解压后的 shp 目录> --from-base --out private/portal/media/blocks
    python scripts/build_city_blocks.py --shp-dir <解压后的 shp 目录> --cities cities.json --out private/portal/media/blocks

--from-base 直接读在线库的城市表(TEABLE_URL、TEABLE_ADMIN_TOKEN);--cities 用导出的 [{key, iso, en, coords}, ...]。
MSDCW 文件名是「国名_城市名_L层级.shp」,对上库里的城市:先按国别 + 英文名(去重音、不分大小写,带几个别名),对不上再取同国 30 公里内最近的一座。
层级 1–5 从粗到细(1 = 只按高速和主干道切,5 = 到居住区小路);默认只转 1–3,第 5 级一座大城市就有几十兆,网页吃不消。
几何按 5 米容差简化、坐标留 5 位小数;需要 shapely、pyshp。"""
from __future__ import annotations

import argparse
import json
import math
import re
import unicodedata
from pathlib import Path

ISO_BY_NAME = {"Azerbaijan": "AZE", "Brazil": "BRA", "Colombia": "COL", "Cote d'Ivoire": "CIV", "Gabon": "GAB", "Guinea": "GIN", "Kazakhstan": "KAZ", "Kenya": "KEN",
               "Kyrgyzstan": "KGZ", "Mali": "MLI", "Morocco": "MAR", "Nepal": "NPL", "Sierra Leone": "SLE", "Turkey": "TUR", "Uzbekistan": "UZB", "Papua New Guinea": "PNG"}
ALIAS = {"shimkent": "shymkent", "zmit": "izmit", "tanger": "tangier", "vittoria": "vitoria", "fez": "fes"}  # MSDCW 的写法 → GeoNames 的写法
SOURCE = "MSDCW(Tang 等 2025,Scientific Data 12:456;figshare 10.6084/m9.figshare.26021314,CC BY 4.0;道路为 2022 年 5 月 OpenStreetMap)"


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z]", "", s)


def latlon(s: str):
    m = re.match(r"\s*(-?\d+(?:\.\d+)?)\s*[,,]\s*(-?\d+(?:\.\d+)?)", str(s or ""))
    return (float(m.group(1)), float(m.group(2))) if m else None


def km(a, b) -> float:
    return math.hypot((a[0] - b[0]) * 111.2, (a[1] - b[1]) * 111.2 * math.cos(math.radians((a[0] + b[0]) / 2)))


def match(country: str, city: str, centre, cities: list[dict]) -> dict | None:
    """MSDCW 的国名 + 城市名 → 库里的城市行;名字对不上就取同国 30 公里内最近的"""
    iso = ISO_BY_NAME.get(country)
    same = [c for c in cities if c.get("iso") == iso and c.get("key")]
    n = norm(city)
    n = ALIAS.get(n, n)
    for c in same:
        if norm(c.get("en") or "") == n:
            return c
    if centre:
        near = sorted(((km(centre, latlon(c.get("coords"))), c) for c in same if latlon(c.get("coords"))), key=lambda t: t[0])
        if near and near[0][0] <= 30:
            return near[0][1]
    return None


def convert(shp: Path, tol: float) -> tuple[dict, tuple[float, float]]:
    """一个 shapefile → (GeoJSON FeatureCollection, 中心点纬经度);多边形简化并去掉太小的碎片"""
    import shapefile
    from shapely.geometry import shape, mapping

    rd = shapefile.Reader(str(shp))
    feats = []
    xs = ys = 0.0
    for sr in rd.iterShapes():
        g = shape(sr.__geo_interface__)
        if g.is_empty:
            continue
        g = g.simplify(tol, preserve_topology=True)
        if g.is_empty:
            continue
        feats.append({"type": "Feature", "properties": {}, "geometry": json.loads(json.dumps(mapping(g)), parse_float=lambda x: round(float(x), 5))})
        c = g.representative_point()
        xs += c.x; ys += c.y
    n = max(1, len(feats))
    return {"type": "FeatureCollection", "features": feats}, (ys / n, xs / n)


def outline(shp: Path) -> dict:
    """1 级街区合并成城市轮廓(外边界,只留外环;小缝先用正负缓冲填平),简化到约 20 米"""
    import shapefile
    from shapely.geometry import shape, mapping, Polygon, MultiPolygon
    from shapely.ops import unary_union

    rd = shapefile.Reader(str(shp))
    u = unary_union([shape(sr.__geo_interface__).buffer(0) for sr in rd.iterShapes()]).buffer(0.0004).buffer(-0.0004)
    polys = [u] if isinstance(u, Polygon) else [p for p in u.geoms if isinstance(p, Polygon)] if isinstance(u, MultiPolygon) else []
    g = MultiPolygon([Polygon(p.exterior) for p in polys]).simplify(0.0002, preserve_topology=True)
    return {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": json.loads(json.dumps(mapping(g)), parse_float=lambda x: round(float(x), 5))}]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shp-dir", type=Path, required=True)
    ap.add_argument("--cities", type=Path, help="导出的城市 json(与 --from-base 二选一)")
    ap.add_argument("--from-base", action="store_true", help="直接读在线库的城市表")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--levels", default="1,2,3", help="转哪几个层级,默认 1,2,3")
    ap.add_argument("--tolerance", type=float, default=0.00005, help="简化容差(度),默认约 5 米")
    a = ap.parse_args()
    if a.from_base:
        import os
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from core import tstore
        b = tstore.Base(tstore.Client(*tstore.credentials(admin=not os.environ.get("TEABLE_TOKEN"))))
        iso = {r["id"]: b.get("country", r, "key") for r in b.rows("country", props=["key"])}
        cities = []
        for r in b.rows("city", props=["key", "name_en", "country", "coords"]):
            co = b.get("city", r, "country")
            if isinstance(co, list):
                co = co[0] if co else None
            cities.append({"key": b.get("city", r, "key"), "en": b.get("city", r, "name_en"), "coords": b.get("city", r, "coords"), "iso": iso.get(co.get("id") if isinstance(co, dict) else co)})
    elif a.cities:
        cities = json.loads(a.cities.read_text("utf-8"))
    else:
        ap.error("要 --cities 或 --from-base")
    levels = [int(x) for x in a.levels.split(",") if x.strip()]
    a.out.mkdir(parents=True, exist_ok=True)
    idx_path = a.out / "index.json"
    index = json.loads(idx_path.read_text("utf-8")) if idx_path.exists() else {"source": SOURCE, "cities": {}}
    index["source"] = SOURCE
    unmatched = []
    for shp in sorted(a.shp_dir.glob("*_L1.shp")):
        country, city = shp.stem[:-3].split("_", 1)
        _, centre = convert(shp, 0.01)
        row = match(country, city, centre, cities)
        if not row:
            unmatched.append(f"{country}_{city}")
            continue
        key = str(row["key"])
        ent = {"name": city, "country": country, "levels": {}, "outline": True}
        (a.out / f"{key}_outline.geojson").write_text(json.dumps(outline(shp), separators=(",", ":")), "utf-8")
        for lv in levels:
            src = shp.with_name(f"{shp.stem[:-1]}{lv}.shp")
            if not src.exists():
                continue
            fc, _ = convert(src, a.tolerance)
            (a.out / f"{key}_L{lv}.geojson").write_text(json.dumps(fc, separators=(",", ":")), "utf-8")
            ent["levels"][str(lv)] = len(fc["features"])
        index["cities"][key] = ent
        print(key, country, city, ent["levels"])
    idx_path.write_text(json.dumps(index, ensure_ascii=False, separators=(",", ":")), "utf-8")
    if unmatched:
        print("没对上库里城市的:", ", ".join(unmatched))
    print(len(index["cities"]), "cities →", a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
