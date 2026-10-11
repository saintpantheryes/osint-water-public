"""A22 维护城市:按 GeoNames 公开数据把人口 ≥ 门槛(profile.city_min_population,缺省 10 万)的城市建进「城市」表,一国一批;
已有的只填空(R2),去重按城市码(GeoNames 编号)。人口与面积都有、密度为空的算密度。每月 1 日全量(refresh),其余日子只补还没有城市的国别。
数据文件(cities15000.zip 约 2 MB、admin1CodesASCII.txt)一月取一次存本机缓存;取不到就用上次的,再没有就跳过。库里还没建「城市」表时跳过。"""
from __future__ import annotations

import io
import re
import time
import zipfile
from pathlib import Path

import httpx

from core import geo, tstore

from .common import Ctx

CITIES_URL = "https://download.geonames.org/export/dump/cities15000.zip"
ADMIN1_URL = "https://download.geonames.org/export/dump/admin1CodesASCII.txt"
MAX_AGE = 30 * 86400
_CJK = re.compile("[㐀-鿿]")
SOURCE = "GeoNames cities15000"


def _fresh(fp: Path) -> bool:
    return fp.exists() and fp.stat().st_size > 0 and time.time() - fp.stat().st_mtime < MAX_AGE


def fetch_data(cache_dir: Path, refresh: bool = False, client: httpx.Client | None = None, log=print) -> tuple[str, str] | None:
    """→ (cities15000.txt 内容, admin1CodesASCII.txt 内容);取不到且没缓存返回 None。"""
    cache_dir.mkdir(parents=True, exist_ok=True)
    cf, af = cache_dir / "cities15000.txt", cache_dir / "admin1CodesASCII.txt"
    if refresh or not _fresh(cf) or not _fresh(af):
        own = client is None
        client = client or httpx.Client(timeout=120.0, follow_redirects=True, headers={"User-Agent": "osint-water/1.0"})
        try:
            r = client.get(CITIES_URL)
            r.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                cf.write_bytes(z.read("cities15000.txt"))
            r = client.get(ADMIN1_URL)
            r.raise_for_status()
            af.write_bytes(r.content)
        except Exception as e:  # noqa: BLE001
            log(f"  ! GeoNames 没取到:{type(e).__name__}: {e}" + (";用上次的缓存" if cf.exists() and af.exists() else ""))
        finally:
            if own:
                client.close()
    if not (cf.exists() and af.exists()):
        return None
    return cf.read_text(encoding="utf-8", errors="replace"), af.read_text(encoding="utf-8", errors="replace")


def parse_admin1(text: str) -> dict[str, str]:
    """「AZ.01\\tAbsheron Region\\t…」→ {"AZ.01": "Abşeron"(第 2 列是 ASCII 名,第 1 列是本地名)}。"""
    out = {}
    for line in text.splitlines():
        cols = line.split("\t")
        if len(cols) >= 2:
            out[cols[0]] = cols[1]
    return out


def zh_of(alternatenames: str) -> str | None:
    for alt in (alternatenames or "").split(","):
        alt = alt.strip()
        if alt and _CJK.search(alt):
            return alt
    return None


def parse_cities(text: str, iso2: str, min_population: int, admin: dict[str, str]) -> list[dict]:
    """GeoNames 城市表(制表符分列)→ 某国人口 ≥ 门槛的城市,按人口降序;值按本体属性。"""
    rows = []
    for line in text.splitlines():
        c = line.split("\t")
        if len(c) < 19 or c[8] != iso2:
            continue
        try:
            pop = int(c[14] or 0)
        except ValueError:
            continue
        if pop < min_population:
            continue
        rows.append({"key": c[0], "name_zh": zh_of(c[3]) or c[1], "name_en": c[1], "admin": admin.get(f"{iso2}.{c[10]}") or None,
                     "coords": f"{float(c[4]):.4f},{float(c[5]):.4f}", "population": pop,
                     "pop_year": int(c[18][:4]) if re.match(r"\d{4}-", c[18] or "") else None, "source": f"{SOURCE}({(c[18] or '')[:7]})"})
    rows.sort(key=lambda r: -r["population"])
    return rows


def _isos(ctx: Ctx, only_missing: bool) -> list[str]:
    b = ctx.base
    isos = [str(b.get("country", r, "key")) for r in b.rows("country", props=["key"]) if b.get("country", r, "key")]
    if not only_missing:
        return isos
    have = {tstore.link_id(b.get("city", r, "country")) for r in b.rows("city", props=["country"])}
    return [i for i in isos if ctx.country_rid(i, create=False) not in have]


def run(ctx: Ctx, refresh: bool = False, only_missing: bool = False, client: httpx.Client | None = None) -> dict:
    b = ctx.base
    if "city" not in b.t:
        return {}
    isos = _isos(ctx, only_missing)
    out: dict = {}
    if isos:
        data = fetch_data(ctx.cache.root.parent / "geonames", refresh=refresh, client=client, log=ctx.log)
        if data is None:
            return {"跳过": "没有 GeoNames 数据"}
        cities_txt, admin = data[0], parse_admin1(data[1])
        have = {str(b.get("city", r, "key")): r for r in b.rows("city") if b.get("city", r, "key")}
        n_new = n_fill = 0
        for iso in isos:
            g = geo.countries().get(iso) or {}
            iso2 = str(g.get("iso2") or "").upper()
            if not iso2:
                continue
            crid = ctx.country_rid(iso, create=False)
            new = []
            for row in parse_cities(cities_txt, iso2, ctx.profile.city_min_population, admin):
                row["country"] = crid
                rec = have.get(row["key"])
                if rec is None:
                    new.append(row)
                else:
                    changes, _ = b.fill("city", rec, {k: v for k, v in row.items() if k != "key"})
                    n_fill += len(changes)
            if new:
                b.create("city", new)
                n_new += len(new)
        if n_new:
            out["新建"] = n_new
        if n_fill:
            out["补空"] = n_fill
    # 密度:人口与面积都有、密度为空
    if b.has("city", "density") and b.has("city", "area_km2"):
        rows = b.rows("city", filter=b.filter(b.cond("city", "population", "isNotEmpty"), b.cond("city", "area_km2", "isNotEmpty"), b.cond("city", "density", "isEmpty")),
                      props=["population", "area_km2"])
        ups = []
        for r in rows:
            pop, area = b.get("city", r, "population"), b.get("city", r, "area_km2")
            if pop and area and float(area) > 0:
                ups.append((r["id"], {"density": round(float(pop) / float(area))}))
        b.update_many("city", ups)
        if ups:
            out["密度"] = len(ups)
    return out
