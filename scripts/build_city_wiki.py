"""生成 reference/city_wiki.tsv:规模以上城市的维基数据条目、面积与中英文维基百科条目(GeoNames 编号 → 维基数据号、面积、中文条目、英文条目),
供 core/jobs/cities.py 给城市表补面积(只填空)与维基百科链接。

面积:维基数据的「面积」(P2046,CC0),按单位换算成平方公里;一个条目几个值时取标为"首选"的,没有就取最新引用的那个。
维基百科条目:条目的中文、英文站内链接(zhwiki、enwiki),没有中文条目的只记英文。
条目怎么找:按 GeoNames 编号(P1566)查,条目坐标须在 GeoNames 坐标 30 公里内;一个编号对上几个条目时取各语种维基百科条目最多的那个(城市本身通常最多)。
按编号查不到、或查到的条目没有中英文维基百科(巴库记的是另一条)的,再按英文名查、取 30 公里内条目最多的。
用法:python scripts/build_city_wiki.py [cities15000.txt] [--min-population 100000]   不给文件则联网下载一次。
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
import urllib.parse
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.jobs.cities import CITIES_URL, WIKI_TABLE  # noqa: E402
from scripts.build_city_zh import SPARQL, _get, _km  # noqa: E402

BATCH = 200
# 面积单位 → 平方公里
UNIT_KM2 = {"Q712226": 1.0, "Q25343": 1e-6, "Q35852": 0.01, "Q232291": 2.589988, "Q81292": 0.00404686}
ZH = "https://zh.wikipedia.org/"
EN = "https://en.wikipedia.org/"


def _title(url: str) -> str:
    return urllib.parse.unquote(url.rsplit("/wiki/", 1)[-1]).replace("_", " ") if url else ""


def _parse(res: dict, key: str) -> dict[str, dict]:
    """SPARQL 结果 → {(条目, 查询键): {lat, lon, links, area, zh, en}}。面积取首选值,否则取第一个。"""
    out: dict[tuple[str, str], dict] = {}
    for b in res["results"]["bindings"]:
        i, k = b["i"]["value"].rsplit("/", 1)[-1], b[key]["value"]
        d = out.setdefault((i, k), {"links": int(b["n"]["value"]), "lat": None, "lon": None, "area": None, "area_rank": "", "zh": "", "en": ""})
        m = re.match(r"Point\(([-\d.eE]+) ([-\d.eE]+)\)", (b.get("c") or {}).get("value", ""))
        if m:
            d["lat"], d["lon"] = float(m[2]), float(m[1])
        if b.get("a") and b.get("u"):
            unit = b["u"]["value"].rsplit("/", 1)[-1]
            rank = b.get("r", {}).get("value", "").rsplit("#", 1)[-1]
            if unit in UNIT_KM2 and (d["area"] is None or (rank == "PreferredRank" and d["area_rank"] != "PreferredRank")):
                d["area"], d["area_rank"] = float(b["a"]["value"]) * UNIT_KM2[unit], rank
        if b.get("zh"):
            d["zh"] = b["zh"]["value"]
        if b.get("en"):
            d["en"] = b["en"]["value"]
    return out


_FIELDS = ("?i ?n ?c ?a ?u ?r ?zh ?en WHERE {{ {head} ?i wikibase:sitelinks ?n. OPTIONAL {{ ?i wdt:P625 ?c }} "
           "OPTIONAL {{ ?i p:P2046 ?s. ?s wikibase:rank ?r. ?s psv:P2046 ?v. ?v wikibase:quantityAmount ?a; wikibase:quantityUnit ?u. FILTER(?r != wikibase:DeprecatedRank) }} "
           f"OPTIONAL {{{{ ?zh schema:about ?i; schema:isPartOf <{ZH}> }}}} OPTIONAL {{{{ ?en schema:about ?i; schema:isPartOf <{EN}> }}}} }}}}")


def by_id(cities: dict[str, tuple[list[str], float, float]]) -> dict[str, dict]:
    """按 GeoNames 编号查 → 编号 → 选中的条目。条目要有坐标、离 GeoNames 坐标 30 公里内、有中文或英文维基百科;几个候选取维基百科条目最多的。"""
    ids = sorted(cities, key=int)
    best: dict[str, dict] = {}
    for i in range(0, len(ids), BATCH):
        vals = " ".join(f'"{g}"' for g in ids[i:i + BATCH])
        q = "SELECT ?g " + _FIELDS.format(head=f"VALUES ?g {{{vals}}} ?i wdt:P1566 ?g.")
        res = json.loads(_get(SPARQL, urllib.parse.urlencode({"query": q}).encode()))
        for (item, g), d in _parse(res, "g").items():
            if d["lat"] is None or not (d["zh"] or d["en"]) or _km(d["lat"], d["lon"], cities[g][1], cities[g][2]) > 30:
                continue
            if g not in best or d["links"] > best[g]["links"]:
                best[g] = {**d, "qid": item}
        print(f"  按编号 {min(i + BATCH, len(ids))}/{len(ids)}", file=sys.stderr)
    return best


def by_name(cities: dict[str, tuple[list[str], float, float]]) -> dict[str, dict]:
    """按英文名(含 ASCII 写法)查 → 编号 → 30 公里内、维基百科条目最多的条目。"""
    want: dict[str, list[str]] = {}
    for g, (names, _, _) in cities.items():
        for n in names:
            want.setdefault(n, []).append(g)
    best: dict[str, dict] = {}
    names = sorted(want)
    for i in range(0, len(names), BATCH):
        vals = " ".join(json.dumps(n, ensure_ascii=False) + "@en" for n in names[i:i + BATCH])
        q = "SELECT ?l " + _FIELDS.format(head=f"VALUES ?l {{{vals}}} ?i rdfs:label ?l; wdt:P625 ?c0.")
        res = json.loads(_get(SPARQL, urllib.parse.urlencode({"query": q}).encode()))
        for (item, n), d in _parse(res, "l").items():
            if d["lat"] is None or not (d["zh"] or d["en"]):
                continue
            for g in want.get(n, []):
                if _km(d["lat"], d["lon"], cities[g][1], cities[g][2]) <= 30 and (g not in best or d["links"] > best[g]["links"]):
                    best[g] = {**d, "qid": item}
        print(f"  按名 {min(i + BATCH, len(names))}/{len(names)}", file=sys.stderr)
    return best


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cities", nargs="?", help="已下载的 cities15000.txt")
    ap.add_argument("--min-population", type=int, default=100000)
    a = ap.parse_args()
    if a.cities:
        text = Path(a.cities).read_text(encoding="utf-8")
    else:
        with zipfile.ZipFile(io.BytesIO(_get(CITIES_URL))) as z:
            text = z.read("cities15000.txt").decode("utf-8")
    en: dict[str, str] = {}
    geo: dict[str, tuple[list[str], float, float]] = {}
    for line in text.splitlines():
        c = line.split("\t")
        if len(c) >= 19 and c[14].isdigit() and int(c[14]) >= a.min_population:
            en[c[0]] = c[1]
            geo[c[0]] = (sorted({c[1], c[2]}), float(c[4]), float(c[5]))
    got = by_id(geo)
    got.update(by_name({g: v for g, v in geo.items() if g not in got}))
    n_area = 0
    with open(WIKI_TABLE, "w", encoding="utf-8") as f:
        f.write("# GeoNames 编号\t维基数据号\t面积(平方公里,维基数据 P2046)\t中文维基百科条目\t英文维基百科条目\t英文名(对照用)。"
                "由 scripts/build_city_wiki.py 从维基数据生成;人工改正直接改这里。\n")
        for g in sorted(en, key=int):
            d = got.get(g)
            if not d:
                continue
            v = d["area"] or 0
            area = (str(round(v, 1)) if v < 100 else str(round(v))) if v > 0 else ""
            n_area += bool(area)
            f.write(f"{g}\t{d['qid']}\t{area}\t{_title(d['zh'])}\t{_title(d['en'])}\t{en[g]}\n")
    print(f"写入 {len(got)} / {len(en)} 个城市(有面积 {n_area})→ {WIKI_TABLE.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
