"""按 HLUH-5km 城市用地栅格(Zenodo 10.5281/zenodo.18812020,urban_{年}_final_fraction.tif)算每座城市周边 12.5 公里内的建成区面积(平方公里),
写成 city_urban.json({城市码: [面积...]},年份列表在 years),给城市页「历史影像」页签画城镇化曲线。一次性离线跑,数据到 2023 年为止。

    python scripts/build_city_urban.py --tif-dir <解压后的 urban 目录> --from-base --out private/portal/media/city_urban.json
    python scripts/build_city_urban.py --tif-dir <解压后的 urban 目录> --cities cities.json --out private/portal/media/city_urban.json

--from-base 直接读在线库的城市表(TEABLE_URL、TEABLE_ADMIN_TOKEN);--cities 用导出的 [{key: GeoNames 编号, coords: "纬度,经度"}, ...]。
需要 numpy、tifffile、imagecodecs(LZW 解压)。新建的城市要重跑一次才有数;文件约 300 KB,主机上 compose 把 media/ 挂到页面目录。"""
from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path

import numpy as np
import tifffile

YEARS = [1950, 1960, 1970, 1980, 1990, 1995, 2000, 2005, 2010, 2015, 2020, 2023]
RADIUS_KM = 12.5
CELL = 0.05  # 度
KM_PER_DEG = 111.2


def grid(page) -> tuple[float, float]:
    tp = page.tags["ModelTiepointTag"].value  # (0,0,0, 左上经度, 左上纬度, 0)
    return float(tp[3]), float(tp[4])


def window(lat: float, lon: float, lon0: float, lat0: float, shape: tuple[int, int]):
    """→ (行切片, 列切片, 各格面积 km² 的掩膜数组):圆心 12.5 公里内的格子"""
    dlat = RADIUS_KM / KM_PER_DEG
    dlon = RADIUS_KM / (KM_PER_DEG * max(0.2, math.cos(math.radians(lat))))
    r0 = max(0, int((lat0 - (lat + dlat)) / CELL)); r1 = min(shape[0], int((lat0 - (lat - dlat)) / CELL) + 1)
    c0 = max(0, int(((lon - dlon) - lon0) / CELL)); c1 = min(shape[1], int(((lon + dlon) - lon0) / CELL) + 1)
    rows = np.arange(r0, r1); cols = np.arange(c0, c1)
    clat = lat0 - (rows + 0.5) * CELL; clon = lon0 + (cols + 0.5) * CELL
    dy = (clat[:, None] - lat) * KM_PER_DEG; dx = (clon[None, :] - lon) * KM_PER_DEG * np.cos(np.radians(clat[:, None]))
    inside = (dx * dx + dy * dy) <= RADIUS_KM * RADIUS_KM
    area = (CELL * KM_PER_DEG) * (CELL * KM_PER_DEG) * np.cos(np.radians(clat))[:, None] * np.ones_like(inside, dtype=float)
    return slice(r0, r1), slice(c0, c1), area * inside


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tif-dir", type=Path, required=True)
    ap.add_argument("--cities", type=Path, help="导出的城市 json(与 --from-base 二选一)")
    ap.add_argument("--from-base", action="store_true", help="直接读在线库的城市表")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    if a.from_base:
        import os
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from core import tstore
        b = tstore.Base(tstore.Client(*tstore.credentials(admin=not os.environ.get("TEABLE_TOKEN"))))
        src = [{"key": b.get("city", r, "key"), "coords": b.get("city", r, "coords")} for r in b.rows("city", props=["key", "coords"])]
    elif a.cities:
        src = json.loads(a.cities.read_text("utf-8"))
    else:
        ap.error("要 --cities 或 --from-base")
    cities = []
    for c in src:
        m = re.match(r"\s*(-?\d+(?:\.\d+)?)\s*[,,]\s*(-?\d+(?:\.\d+)?)", str(c.get("coords") or ""))
        if c.get("key") and m:
            cities.append((str(c["key"]), float(m.group(1)), float(m.group(2))))
    out: dict[str, list] = {k: [] for k, _, _ in cities}
    for y in YEARS:
        page = tifffile.TiffFile(a.tif_dir / f"urban_{y}_final_fraction.tif").pages[0]
        lon0, lat0 = grid(page)
        arr = page.asarray()
        for k, lat, lon in cities:
            rs, cs, area = window(lat, lon, lon0, lat0, arr.shape)
            frac = np.nan_to_num(arr[rs, cs].astype(float))
            out[k].append(round(float((frac * area).sum()), 1))
        print(y, "done")
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps({"years": YEARS, "radius_km": RADIUS_KM, "source": "HLUH-5km v1.0 (Peng, Zhang & Liu 2026, Zenodo 10.5281/zenodo.18812020, CC-BY 4.0)", "cities": out}, ensure_ascii=False, separators=(",", ":")), "utf-8")
    print(len(out), "cities →", a.out, a.out.stat().st_size, "bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
