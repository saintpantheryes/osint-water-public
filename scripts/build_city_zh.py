"""生成 reference/city_zh.tsv:规模以上城市的简体中文名(GeoNames 编号 → 中文名),供 core/jobs/cities.py 取城市中文名。

GeoNames 的中文别名不分简繁、也不分大陆与港台译名(达累斯萨拉姆在里面是「三蘭港」,内罗毕是「內羅比」),
所以中文名改从维基数据(Wikidata,CC0)取:按 GeoNames 编号(P1566)查条目,依次用 zh-cn、zh-hans、zh-sg、zh-my 标签,
都没有时用 zh 标签;一律再转一次简体(有的 zh-cn 标签也是繁体),去掉消歧义的括注(「马里诺区 (莫斯科)」→「马里诺区」);夹外文字母或生僻喃字的不要。
维基数据记的 GeoNames 编号有时是另一条(巴库、埃里温),按编号查不到的再按英文名查、取 30 公里内最近的条目。
两样都查不到的城市不写进表,任务里退回 GeoNames 中文别名转简体。
用法:python scripts/build_city_zh.py [cities15000.txt] [--min-population 100000]   不给文件则联网下载一次。
"""
from __future__ import annotations

import argparse
import io
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.jobs.cities import CITIES_URL, ZH_TABLE, to_simplified  # noqa: E402

SPARQL = "https://query.wikidata.org/sparql"
UA = os.environ.get("OW_USER_AGENT") or "osint-water/0.1 (+https://github.com/saintpantheryes/osint-water-public)"
LANGS = ("zh-cn", "zh-hans", "zh-sg", "zh-my")  # 简体标签,按此先后;都没有再用 zh 转简体
BATCH = 300
# 维基数据标签本身错了的,人工改正(GeoNames 编号 → 中文名)
FIXES = {
    "1710544": "伊里加",  # Iriga City,维基数据简体标签误作「阿里卡」
    "3462672": "加马",  # Gama(巴西利亚),简体标签误作旧名「圣玛丽亚」
}


def _get(url: str, data: bytes | None = None) -> bytes:
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, "Accept": "application/sparql-results+json"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return r.read()
        except Exception:  # noqa: BLE001
            if attempt == 3:
                raise
            time.sleep(5 * (attempt + 1))
    raise RuntimeError("unreachable")


def labels(cities: dict[str, tuple[list[str], float, float]]) -> dict[str, dict[str, str]]:
    """按 GeoNames 编号查 → GeoNames 编号 → {语言: 标签}。条目坐标离 GeoNames 坐标超过 30 公里的不要(维基数据偶有编号记错,
    如智利阿里卡记成了菲律宾伊里加的编号);一个编号对上几个条目取最近的。"""
    ids = sorted(cities, key=int)
    best: dict[str, tuple[float, dict[str, str]]] = {}
    for i in range(0, len(ids), BATCH):
        vals = " ".join(f'"{g}"' for g in ids[i:i + BATCH])
        langs = ",".join(f'"{x}"' for x in (*LANGS, "zh"))
        q = (f"SELECT ?i ?g ?c ?l WHERE {{ VALUES ?g {{{vals}}} ?i wdt:P1566 ?g; rdfs:label ?l. OPTIONAL {{ ?i wdt:P625 ?c }} "
             f"FILTER(LANG(?l) IN ({langs})) }}")
        res = json.loads(_get(SPARQL, urllib.parse.urlencode({"query": q}).encode()))
        found: dict[tuple[str, str], tuple[float, dict[str, str]]] = {}
        for b in res["results"]["bindings"]:
            g = b["g"]["value"]
            d = 0.0
            m = re.match(r"Point\(([-\d.eE]+) ([-\d.eE]+)\)", (b.get("c") or {}).get("value", ""))
            if m:
                d = _km(float(m[2]), float(m[1]), cities[g][1], cities[g][2])
            found.setdefault((b["i"]["value"], g), (d, {}))[1][b["l"]["xml:lang"]] = b["l"]["value"].strip()
        for (_, g), (d, labs) in found.items():
            if d <= 30 and (g not in best or d < best[g][0]):
                best[g] = (d, labs)
        print(f"  按编号 {min(i + BATCH, len(ids))}/{len(ids)}", file=sys.stderr)
    return {g: labs for g, (_, labs) in best.items()}


def by_name(cities: dict[str, tuple[list[str], float, float]]) -> dict[str, dict[str, str]]:
    """按英文名(含 ASCII 写法)查有坐标的条目,取离 GeoNames 坐标 30 公里内最近的 → GeoNames 编号 → {语言: 标签}。"""
    want: dict[str, list[str]] = {}
    for g, (names, _, _) in cities.items():
        for n in names:
            want.setdefault(n, []).append(g)
    best: dict[str, tuple[float, dict[str, str]]] = {}
    names = sorted(want)
    for i in range(0, len(names), BATCH):
        vals = " ".join(json.dumps(n, ensure_ascii=False) + "@en" for n in names[i:i + BATCH])
        langs = ",".join(f'"{x}"' for x in (*LANGS, "zh"))
        q = (f"SELECT ?i ?n ?c ?l WHERE {{ VALUES ?n {{{vals}}} ?i rdfs:label ?n; wdt:P625 ?c; rdfs:label ?l. "
             f"FILTER(LANG(?l) IN ({langs})) }}")
        res = json.loads(_get(SPARQL, urllib.parse.urlencode({"query": q}).encode()))
        found: dict[tuple[str, str], tuple[float, float, dict[str, str]]] = {}
        for b in res["results"]["bindings"]:
            m = re.match(r"Point\(([-\d.eE]+) ([-\d.eE]+)\)", b["c"]["value"])
            if not m:
                continue
            k = (b["i"]["value"], b["n"]["value"])
            found.setdefault(k, (float(m[2]), float(m[1]), {}))[2][b["l"]["xml:lang"]] = b["l"]["value"].strip()
        for (_, n), (lat, lon, labs) in found.items():
            for g in want.get(n, []):
                d = _km(lat, lon, cities[g][1], cities[g][2])
                if d <= 30 and (g not in best or d < best[g][0]):
                    best[g] = (d, labs)
        print(f"  按名 {min(i + BATCH, len(names))}/{len(names)}", file=sys.stderr)
    return {g: labs for g, (_, labs) in best.items()}


def _km(a1: float, o1: float, a2: float, o2: float) -> float:
    p1, p2 = math.radians(a1), math.radians(a2)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(o2 - o1) / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


_QUALIFIER = re.compile(r"\s*([(（][^)）]*[)）]|[,，].*)$")


def pick(langs: dict[str, str]) -> str | None:
    name = next((langs[x] for x in (*LANGS, "zh") if langs.get(x)), None)
    if not name:
        return None
    name = to_simplified(_QUALIFIER.sub("", name)).strip()
    # 只收常用汉字(加数字、间隔号);夹拉丁字母的(「Cileunyi区」)、越南喃字(「我𠤩市」)不要
    return name if re.fullmatch(r"[一-鿿0-9·・-]+", name) else None


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
    got = {g: v for g, v in labels(geo).items() if pick(v)}
    got.update(by_name({g: v for g, v in geo.items() if g not in got}))
    rows = [(g, name, en[g]) for g in sorted(en, key=int) if (name := FIXES.get(g) or pick(got.get(g, {})))]
    with open(ZH_TABLE, "w", encoding="utf-8") as f:
        f.write("# GeoNames 编号\t简体中文名\t英文名(对照用)。由 scripts/build_city_zh.py 从维基数据生成;人工改正直接改这里。\n")
        for g, name, e in rows:
            f.write(f"{g}\t{name}\t{e}\n")
    print(f"写入 {len(rows)} / {len(en)} 个城市 → {ZH_TABLE.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
