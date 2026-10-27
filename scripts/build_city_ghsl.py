"""把 GHSL(欧盟联合研究中心「全球人居图层」,R2023A,100 米格网,CC BY 4.0)的建筑高度、建筑体量、人口三层裁到城市周边,转成城市页「建筑与人口」页签能直接读的小文件:
每座城市一个 media/ghsl/<城市码>.json(经纬度规则格网 + 三层整数数组),外加一张目录 media/ghsl/index.json({城市码: {name, pop, ...}}),页面按目录决定要不要显示页签。
一次性离线跑;原始分幅(每幅 1000 公里见方,几兆到十几兆)按需从 JRC 开放数据站下载,缓存在 --tiles 目录。

    python scripts/build_city_ghsl.py --cities cities.json --out private/portal/media/ghsl
    python scripts/build_city_ghsl.py --from-base --keys 587084 --out private/portal/media/ghsl

--from-base 直接读在线库的城市表(TEABLE_URL、TEABLE_ADMIN_TOKEN);--cities 用导出的 [{key, en, coords}, ...](coords 为「纬度, 经度」);--keys 只做这几座。
三层:GHS_BUILT_H ANBH 2018(格内建筑平均净高,米)、GHS_BUILT_V 2020(格内建筑总体量,立方米)、GHS_POP 2020(格内常住人口)。
原数据是等积的 Mollweide 投影,先按 --radius 公里切出正方形,再重采样到约 100 米的经纬度格网(人口、体量按「求和」重采样保总量,高度按「平均」),
存成整数:高度 0.1 米、体量 100 立方米、人口 1 人;连续的 0 压成一个负数(-n = n 个 0),页面解开。
范围缺省按人口定(--radius auto):100 万以上 20 公里、50 万以上 12 公里、其余 8 公里;也可给固定公里数。需要 rasterio、pyproj、numpy。"""
from __future__ import annotations

import argparse
import json
import math
import re
import urllib.request
import zipfile
from pathlib import Path

BASE = "https://jeodpp.jrc.ec.europa.eu/ftp/jrc-opendata/GHSL"
LAYERS = {  # 名 → (产品目录, 文件前缀, 重采样法, 存整数的倍率)
    "h": ("GHS_BUILT_H_GLOBE_R2023A/GHS_BUILT_H_ANBH_E2018_GLOBE_R2023A_54009_100/V1-0", "GHS_BUILT_H_ANBH_E2018_GLOBE_R2023A_54009_100_V1_0", "average", 10),
    "v": ("GHS_BUILT_V_GLOBE_R2023A/GHS_BUILT_V_E2020_GLOBE_R2023A_54009_100/V1-0", "GHS_BUILT_V_E2020_GLOBE_R2023A_54009_100_V1_0", "sum", 0.01),
    "p": ("GHS_POP_GLOBE_R2023A/GHS_POP_E2020_GLOBE_R2023A_54009_100/V1-0", "GHS_POP_E2020_GLOBE_R2023A_54009_100_V1_0", "sum", 1),
}
SOURCE = "GHSL R2023A(欧盟联合研究中心,CC BY 4.0):建筑高度 GHS-BUILT-H 2018、建筑体量 GHS-BUILT-V 2020、人口 GHS-POP 2020,100 米格网"
TILE_X0, TILE_Y0, TILE = -18041000.0, 9000000.0, 1000000.0  # 分幅:左上角原点,每幅 1000 公里


def latlon(s: str):
    m = re.match(r"\s*(-?\d+(?:\.\d+)?)\s*[,,]\s*(-?\d+(?:\.\d+)?)", str(s or ""))
    return (float(m.group(1)), float(m.group(2))) if m else None


def tile_of(x: float, y: float) -> str:
    return f"R{math.floor((TILE_Y0 - y) / TILE) + 1}_C{math.floor((x - TILE_X0) / TILE) + 1}"


def fetch_tile(layer: str, tile: str, cache: Path) -> Path:
    """分幅 tif 没有就下载并解压到缓存目录"""
    d, pre, _, _ = LAYERS[layer]
    tif = cache / f"{pre}_{tile}.tif"
    if tif.exists():
        return tif
    url = f"{BASE}/{d}/tiles/{pre}_{tile}.zip"
    zp = cache / f"{pre}_{tile}.zip"
    print("下载", url)
    urllib.request.urlretrieve(url, zp)
    with zipfile.ZipFile(zp) as z:
        for n in z.namelist():
            if n.endswith(".tif"):
                z.extract(n, cache)
    zp.unlink()
    return tif


def clip(layer: str, cx: float, cy: float, radius_m: float, cache: Path, lon0: float, lat0: float, dlon: float, dlat: float, ncol: int, nrow: int):
    """一层:从 Mollweide 分幅切出中心周边,重采样到给定的经纬度格网(跨分幅的城市取两幅拼)"""
    import numpy as np
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.transform import from_origin
    from rasterio.warp import reproject
    from rasterio.windows import from_bounds

    _, _, method, _ = LAYERS[layer]
    left, right, bottom, top = cx - radius_m, cx + radius_m, cy - radius_m, cy + radius_m
    tiles = sorted({tile_of(x, y) for x in (left, right) for y in (bottom, top)})
    srcs = []
    for t in tiles:
        ds = rasterio.open(fetch_tile(layer, t, cache))
        w = from_bounds(max(left, ds.bounds.left), max(bottom, ds.bounds.bottom), min(right, ds.bounds.right), min(top, ds.bounds.top), ds.transform)
        arr = ds.read(1, window=w).astype("float64")
        nod = ds.nodata
        if nod is not None:
            arr[arr == nod] = 0
        arr[arr < 0] = 0
        srcs.append((arr, ds.window_transform(w), ds.crs))
        ds.close()
    out = np.zeros((nrow, ncol), dtype="float64")
    dst_t = from_origin(lon0, lat0, dlon, dlat)
    for arr, tr, crs in srcs:
        part = np.zeros_like(out)
        reproject(arr, part, src_transform=tr, src_crs=crs, dst_transform=dst_t, dst_crs="EPSG:4326", resampling=getattr(Resampling, method), src_nodata=None, dst_nodata=0)
        out += part
    return out, float(sum(a.sum() for a, _, _ in srcs))


def build(city: dict, radius_km: float, cache: Path) -> dict | None:
    import numpy as np
    from pyproj import Transformer

    ll = latlon(city.get("coords"))
    if not ll:
        return None
    lat, lon = ll
    to_moll = Transformer.from_crs("EPSG:4326", "ESRI:54009", always_xy=True)
    cx, cy = to_moll.transform(lon, lat)
    r = radius_km * 1000
    # 经纬度格网:南北 100 米一格,东西也按当地 100 米
    dlat = 100 / 111320.0
    dlon = 100 / (111320.0 * math.cos(math.radians(lat)))
    nrow = ncol = int(round(2 * r / 100))
    lat0, lon0 = lat + nrow / 2 * dlat, lon - ncol / 2 * dlon
    layers, raw = {}, {}
    for k in LAYERS:
        layers[k], raw[k] = clip(k, cx, cy, r, cache, lon0, lat0, dlon, dlat, ncol, nrow)
    h, v, p = layers["h"], layers["v"], layers["p"]
    cell_km2 = (dlat * 111.32) * (dlon * 111.32 * math.cos(math.radians(lat)))  # 每格面积(km²)
    built = v >= 10000  # 每公顷建筑体量 1 万立方米以上算建成区(约 10% 占地 × 10 米)
    dens = p / cell_km2
    # 卡片用的汇总:都在 radius 公里见方的框内
    stats = {
        "pop": int(round(p.sum())), "pop_src": int(round(raw["p"])),
        "built_km2": round(float(built.sum() * cell_km2), 1),
        "volume_mm3": round(float(v.sum() / 1e6), 1),  # 百万立方米
        "mean_h": round(float((h * v).sum() / v.sum()), 1) if v.sum() else 0,  # 按体量加权的平均高度
        "dense_km2": round(float((dens >= 10000).sum() * cell_km2), 1), "dense_pop": int(round(p[dens >= 10000].sum())),  # 每平方公里 1 万人以上
        "tall_pop": int(round(p[h >= 15].sum())),  # 住在平均净高 15 米(约 5 层)以上格子里的人
        "max_dens": int(round(dens.max())), "max_h": round(float(h.max()), 1),
    }
    def enc(a, m):
        """整数数组,连续的 0 压成负数(-n 表示 n 个 0)"""
        out, run = [], 0
        for x in np.rint(a * m).astype("int64").ravel().tolist():
            if x == 0:
                run += 1
                continue
            if run:
                out.append(-run if run > 1 else 0)
                run = 0
            out.append(int(x))
        if run:
            out.append(-run if run > 1 else 0)
        return out
    return {"key": str(city["key"]), "name": city.get("en") or "", "source": SOURCE, "radius_km": radius_km, "rle": True,
            "grid": {"lat0": round(lat0, 6), "lon0": round(lon0, 6), "dlat": round(dlat, 8), "dlon": round(dlon, 8), "rows": nrow, "cols": ncol, "cell_km2": round(cell_km2, 5)},
            "scale": {"h": 0.1, "v": 100, "p": 1}, "stats": stats,
            "h": enc(h, LAYERS["h"][3]), "v": enc(v, LAYERS["v"][3]), "p": enc(p, LAYERS["p"][3])}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cities", type=Path, help="导出的城市 json(与 --from-base 二选一)")
    ap.add_argument("--from-base", action="store_true", help="直接读在线库的城市表")
    ap.add_argument("--keys", help="只做这几座(城市码,逗号分开)")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--tiles", type=Path, default=Path("ghsl-tiles"), help="分幅缓存目录")
    ap.add_argument("--radius", default="auto", help="中心周边多少公里(正方形半边长);缺省 auto = 按人口 20 / 12 / 8")
    ap.add_argument("--skip-done", action="store_true", help="目录里已有的城市不重做")
    a = ap.parse_args()
    if a.from_base:
        import os
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from core import tstore
        b = tstore.Base(tstore.Client(*tstore.credentials(admin=not os.environ.get("TEABLE_TOKEN"))))
        cities = [{"key": b.get("city", r, "key"), "en": b.get("city", r, "name_en"), "coords": b.get("city", r, "coords"), "pop": b.get("city", r, "population") or 0}
                  for r in b.rows("city", props=["key", "name_en", "coords", "population"])]
    elif a.cities:
        cities = json.loads(a.cities.read_text("utf-8"))
    else:
        ap.error("要 --cities 或 --from-base")
    if a.keys:
        want = {k.strip() for k in a.keys.split(",")}
        cities = [c for c in cities if str(c.get("key")) in want]
    a.out.mkdir(parents=True, exist_ok=True)
    a.tiles.mkdir(parents=True, exist_ok=True)
    idx_path = a.out / "index.json"
    index = json.loads(idx_path.read_text("utf-8")) if idx_path.exists() else {"cities": {}}
    index["source"] = SOURCE
    radius_of = (lambda c: 20 if (c.get("pop") or 0) >= 1e6 else 12 if (c.get("pop") or 0) >= 5e5 else 8) if a.radius == "auto" else (lambda c: float(a.radius))
    for c in cities:
        if a.skip_done and str(c.get("key")) in index["cities"] and (a.out / f"{c['key']}.json").exists():
            continue
        try:
            d = build(c, radius_of(c), a.tiles)
        except Exception as e:  # 一座城市出错不拦下一座(多半是分幅下载断了)
            print(c.get("key"), c.get("en"), "失败:", e)
            continue
        if not d:
            print(c.get("key"), c.get("en"), "没坐标,跳过")
            continue
        (a.out / f"{d['key']}.json").write_text(json.dumps(d, ensure_ascii=False, separators=(",", ":")), "utf-8")
        index["cities"][d["key"]] = {"name": d["name"], "pop": d["stats"]["pop"], "radius_km": d["radius_km"]}
        print(d["key"], d["name"], d["radius_km"], "km", d["stats"]["pop"], "人", flush=True)
        idx_path.write_text(json.dumps(index, ensure_ascii=False, separators=(",", ":")), "utf-8")  # 每座写一次,中途断了也能 --skip-done 接着跑
    idx_path.write_text(json.dumps(index, ensure_ascii=False, separators=(",", ":")), "utf-8")
    print(len(index["cities"]), "cities →", a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
