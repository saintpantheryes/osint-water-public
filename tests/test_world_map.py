"""首页与分析页共用的世界地图(portal/world.geo.json)按中国口径:台湾、藏南、阿克赛钦在中国境内,港澳并入中国,
画南海断续线十段;要素名是认得的国别码,带中文名;外环顺时针(方向反了 Vega-Lite 会把整个地球填满)。"""
import json
from pathlib import Path

from core import geo

WORLD = Path(__file__).resolve().parent.parent / "portal" / "world.geo.json"


def _in_ring(x: float, y: float, ring: list) -> bool:
    c = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            c = not c
    return c


def _polys(geom: dict) -> list:
    return [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]


def _inside(pt: tuple[float, float], geom: dict) -> bool:
    return any(_in_ring(*pt, p[0]) and not any(_in_ring(*pt, h) for h in p[1:]) for p in _polys(geom))


def _signed(ring: list) -> float:
    return sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(ring, ring[1:]))


def test_world_map_is_china_standard():
    data = json.loads(WORLD.read_text(encoding="utf-8"))
    feats = {f["properties"]["name"]: f for f in data["features"]}
    assert len(feats) == len(data["features"])  # 每个国别一个要素
    land = {k: f for k, f in feats.items() if f["properties"].get("kind") != "line"}
    cs = geo.countries()
    assert all(k in cs and f["properties"]["zh"] == geo.name_zh(k) for k, f in land.items())
    assert not {"TWN", "HKG", "MAC"} & set(feats)
    chn, ind = land["CHN"]["geometry"], land["IND"]["geometry"]
    for pt in [(121.56, 25.03), (120.3, 22.62), (114.17, 22.35), (91.86, 27.58), (93.61, 27.08), (79.0, 35.0)]:
        assert _inside(pt, chn), pt  # 台北、高雄、香港、达旺、伊塔那加(藏南)、阿克赛钦
    assert not _inside((91.86, 27.58), ind) and _inside((78.0, 22.0), ind)
    assert _inside((21.17, 42.66), land["SRB"]["geometry"])  # 普里什蒂纳
    line = feats["SCS"]
    assert line["properties"] == {"name": "SCS", "zh": "南海断续线", "kind": "line"}
    dashes = line["geometry"]["coordinates"]
    assert line["geometry"]["type"] == "MultiLineString" and len(dashes) == 10
    assert all(108 <= x <= 123 and 3 <= y <= 25 for d in dashes for x, y in d)
    assert any(min(x for x, _ in d) > 122 for d in dashes)  # 台湾以东一段
    for f in land.values():
        for poly in _polys(f["geometry"]):
            assert _signed(poly[0]) < 0 and all(_signed(h) > 0 for h in poly[1:]), f["properties"]["name"]
