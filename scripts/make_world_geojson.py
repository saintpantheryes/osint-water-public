"""生成首页(ECharts)与分析页(Vega-Lite)共用的世界地图 portal/world.geo.json,按中国口径:

- 国界取 Natural Earth 10m「中国视角」数据(ne_10m_admin_0_countries_chn,公有领域):台湾、藏南、阿克赛钦在中国境内,
  科索沃在塞尔维亚境内;香港、澳门、黄岩岛并入中国。其余按国别码(ISO3,与线索的 country 字段相同)合并,不含南极洲。
- 南海断续线按自然资源部标准地图画十段(台湾以东一段在内),坐标取自 DataV.GeoAtlas 全国图的断续线(100000_JD)
  各段中线,单独一个线要素(name 为 SCS,kind 为 line),地图上画成深色短线,不参与悬停和点击。
- 用 mapshaper 保持拓扑地简化(相邻国界不出缝)并修自相交,坐标保留两位小数;面积很小的岛去掉(中国的不去)。
- 每个国别写上中文名(properties.zh,取自 core.geo),没有线索的国别悬停时也显示国名;写上大洲(properties.ct,core.geo.map_continent,「我的」页按大洲忽略用)。
- 写上经济水平(「我的」页一键忽略用):properties.inc 为世界银行收入组(HIC/UMC/LMC/LIC,下载时从世行接口取当年分类),
  properties.adv 为 1 表示 IMF 发达经济体(core.geo.ADVANCED)。

用法(要装 Node.js,mapshaper 由 npx 取;数据下载一次即可,不进仓库):
  curl -LO https://naciscdn.org/naturalearth/10m/cultural/ne_10m_admin_0_countries_chn.zip
  python scripts/make_world_geojson.py ne_10m_admin_0_countries_chn.zip
"""
from __future__ import annotations

import csv
import json
import math
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import geo  # noqa: E402

OUT = ROOT / "portal" / "world.geo.json"
MAPSHAPER = ["npx", "-y", "mapshaper@0.6"]
SIMPLIFY = "4%"            # 保留的顶点比例(visvalingam 加权),约为原 110m 世界地图的精度
MIN_ISLAND_KM2 = 300       # 小于此面积的岛去掉(中国的都保留)
SKIP = {"ATA"}             # 南极洲
MERGE = {"HKG": "CHN", "MAC": "CHN", "SCR": "CHN", "TWN": "CHN",  # 中国口径
         "ESB": "CYP", "WSB": "CYP",  # 塞浦路斯岛上的英属基地区
         "BRT": "SDN"}                # 比尔泰维勒(埃及与苏丹之间,北纬 22 度以南)
LINE = "SCS"
# 南海断续线十段,自台湾以东起顺时针;每段 5 个点(两端与中线上的三点),经度、纬度
SCS_DASHES = [
    [[122.79, 24.59], [122.72, 24.3], [122.65, 24.02], [122.58, 23.74], [122.51, 23.45]],
    [[121.91, 21.7], [121.72, 21.48], [121.53, 21.25], [121.35, 21.03], [121.16, 20.8]],
    [[120.02, 19.04], [119.88, 18.78], [119.74, 18.52], [119.6, 18.26], [119.46, 18.0]],
    [[119.06, 16.05], [119.06, 15.79], [119.06, 15.53], [119.06, 15.26], [119.06, 15.0]],
    [[118.53, 10.9], [118.68, 11.16], [118.79, 11.43], [118.9, 11.71], [118.98, 12.0]],
    [[116.25, 7.99], [116.07, 7.78], [115.89, 7.57], [115.71, 7.36], [115.53, 7.15]],
    [[111.78, 3.4], [112.06, 3.47], [112.33, 3.53], [112.6, 3.63], [112.86, 3.75]],
    [[108.3, 6.01], [108.24, 6.27], [108.21, 6.54], [108.21, 6.81], [108.23, 7.09]],
    [[110.06, 11.24], [110.19, 11.47], [110.28, 11.72], [110.32, 11.98], [110.33, 12.25]],
    [[109.86, 15.15], [109.78, 15.45], [109.65, 15.72], [109.48, 15.97], [109.3, 16.21]],
]


WB_COUNTRIES = "https://api.worldbank.org/v2/country?format=json&per_page=400"


def wb_income() -> dict[str, str]:
    """世行国别分类 → {ISO3: HIC/UMC/LMC/LIC};世行没分组的(如委内瑞拉暂停分类)不写。"""
    with urllib.request.urlopen(WB_COUNTRIES, timeout=60) as r:
        rows = json.load(r)[1]
    return {c["id"]: c["incomeLevel"]["id"] for c in rows if c["incomeLevel"]["id"] in geo.INCOME_CODES}


def economy(code: str, inc: dict[str, str]) -> dict:
    out = {"inc": inc[code]} if code in inc else {}
    return {**out, "adv": 1} if code in geo.ADVANCED else out


def code_of(rec: dict, known: set[str]) -> str | None:
    """Natural Earth 的一条记录 → 国别码;南极洲与认不出的返回 None。"""
    a3 = rec["ADM0_A3"]
    if a3 in SKIP:
        return None
    if a3 in MERGE:
        return MERGE[a3]
    if rec["SOVEREIGNT"] == "China":
        return "CHN"
    for k in ("ISO_A3", "ADM0_A3", "ISO_A3_EH"):
        if rec.get(k) in known:
            return rec[k]
    return None


def ring_km2(ring: list[list[float]]) -> float:
    """经纬度环的近似面积(平方公里,按纬度缩放经度)。"""
    lat = sum(p[1] for p in ring) / len(ring)
    kx, ky = 111.32 * math.cos(math.radians(lat)), 110.57
    s = sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(ring, ring[1:]))
    return abs(s) / 2 * kx * ky


def polygons(geom: dict) -> list[list[list[list[float]]]]:
    return [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]


def orient(poly: list[list[list[float]]]) -> list[list[list[float]]]:
    """外环顺时针、内环逆时针(d3-geo 的球面约定,Vega-Lite 按此判断哪边是里面;方向反了会把整个地球填满)。"""
    def cw(r: list[list[float]]) -> bool:
        return sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(r, r[1:])) < 0
    return [r if cw(r) == (i == 0) else r[::-1] for i, r in enumerate(poly)]


def tidy(code: str, geom: dict) -> dict | None:
    """去掉退化的环和小岛(中国的不去;全是小岛的国家,如马尔代夫,都留着);只剩一个面时写成 Polygon。"""
    polys = [orient(rings) for poly in polygons(geom) if (rings := [r for r in poly if len(r) >= 4])]
    big = [rings for rings in polys if ring_km2(rings[0]) >= MIN_ISLAND_KM2]
    if big and code != "CHN":
        polys = big
    if not polys:
        return None
    return {"type": "Polygon", "coordinates": polys[0]} if len(polys) == 1 else {"type": "MultiPolygon", "coordinates": polys}


def build(src: Path, work: Path) -> dict:
    if src.suffix == ".zip":
        with zipfile.ZipFile(src) as z:
            z.extractall(work)
        src = next(work.glob("*.shp"))
    known = set(geo.countries())
    attrs = work / "attrs.json"
    subprocess.run([*MAPSHAPER, "-i", str(src), "-o", "format=json", str(attrs)], check=True, capture_output=True)
    codes, unknown = {}, []
    for rec in json.loads(attrs.read_text(encoding="utf-8")):
        c = code_of(rec, known)
        if c:
            codes[rec["ADM0_A3"]] = c
        elif rec["ADM0_A3"] not in SKIP:
            unknown.append(f"{rec['ADM0_A3']} {rec['ADMIN']}")
    if unknown:
        raise SystemExit(f"认不出国别码,请在 MERGE 里指定:{', '.join(unknown)}")
    table = work / "codes.csv"
    with table.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["ADM0_A3", "code"])
        w.writerows(sorted(codes.items()))
    tmp = work / "world.json"
    subprocess.run([*MAPSHAPER, "-i", str(src), "-join", str(table), "keys=ADM0_A3,ADM0_A3", "string-fields=ADM0_A3,code",
                    "-filter", "!!code", "-dissolve", "code", "-simplify", "visvalingam", "weighted", SIMPLIFY, "keep-shapes",
                    "-o", "format=geojson", "precision=0.01", str(tmp)], check=True, capture_output=True)
    # 取两位小数后个别海岸线会自相交(格陵兰、缅甸等),再修一遍
    subprocess.run([*MAPSHAPER, "-i", str(tmp), "-clean", "-o", "format=geojson", "precision=0.01", "force", str(tmp)],
                   check=True, capture_output=True)
    feats, dropped, inc = [], [], wb_income()
    for f in json.loads(tmp.read_text(encoding="utf-8"))["features"]:
        code = f["properties"]["code"]
        geom = tidy(code, f["geometry"]) if f["geometry"] else None
        if geom:
            feats.append({"type": "Feature", "properties": {"name": code, "zh": geo.name_zh(code), "ct": geo.map_continent(code), **economy(code, inc)}, "geometry": geom})
        else:
            dropped.append(code)
    if dropped:
        print(f"太小、地图上不画:{' '.join(sorted(dropped))}")
    feats.sort(key=lambda f: f["properties"]["name"])
    feats.append({"type": "Feature", "properties": {"name": LINE, "zh": "南海断续线", "kind": "line"},
                  "geometry": {"type": "MultiLineString", "coordinates": SCS_DASHES}})
    return {"type": "FeatureCollection", "features": feats}


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    with tempfile.TemporaryDirectory() as d:
        data = build(Path(sys.argv[1]).resolve(), Path(d))
    OUT.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"{OUT.relative_to(ROOT)}:{len(data['features']) - 1} 个国别 + 南海断续线,{OUT.stat().st_size // 1024} KB")
