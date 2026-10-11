"""世行国别元数据 → country 表(收入组、贷款类别、区域、首都坐标)。

范围:profile 的国别 + 市场分布表里的国别(后者 tier 记 market)。
许可:World Bank Open Data,CC BY 4.0。
接口:https://api.worldbank.org/v2/country/{iso3;iso3;…}?format=json
"""
from __future__ import annotations

from core import geo
from core.markets import scope_iso3s
from core.profile import Country, Profile
from core.sources import register
from core.sources.http import get_json
from core.storage import now_iso, write_raw

SOURCE = "wb_country_meta"
URL = "https://api.worldbank.org/v2/country/{codes}"


@register(SOURCE)
def run(profile: Profile) -> dict[str, list[dict]]:
    fetched_at = now_iso()
    scope = scope_iso3s(profile)
    payload = get_json(URL.format(codes=";".join(scope)), {"format": "json", "per_page": 300})
    write_raw(SOURCE, "country", payload)
    meta = {r["id"].upper(): r for r in payload[1]}
    known = {c.iso3: c for c in profile.countries}
    rows = []
    for iso3 in scope:
        g = geo.countries().get(iso3, {})
        c = known.get(iso3) or Country(iso2=g.get("iso2", ""), iso3=iso3, wb_country_code=g.get("iso2", ""),
                                       name_en=(meta.get(iso3) or {}).get("name") or g.get("name_en", iso3),
                                       name_zh=g.get("name_zh", ""), tier="market")
        m = meta.get(c.iso3, {})
        rows.append(
            {
                "iso2": c.iso2,
                "iso3": c.iso3,
                "wb_country_code": c.wb_country_code,
                "name_en": c.name_en,
                "name_zh": c.name_zh,
                "tier": c.tier,
                "region_wb": ((m.get("region") or {}).get("value") or "").strip() or None,  # 接口取值偶带尾随空格
                "income_group": ((m.get("incomeLevel") or {}).get("value") or "").strip() or None,
                "lending_type": ((m.get("lendingType") or {}).get("value") or "").strip() or None,
                "capital": m.get("capitalCity") or None,
                "lon": float(m["longitude"]) if m.get("longitude") else None,
                "lat": float(m["latitude"]) if m.get("latitude") else None,
                "source_url": URL.format(codes=c.iso3),
                "fetched_at": fetched_at,
            }
        )
    missing = [r["iso3"] for r in rows if r["income_group"] is None and r["iso3"] in known]
    if missing:
        raise RuntimeError(f"world bank country meta missing for {missing} — check iso3 in profile")
    gaps = [r["iso3"] for r in rows if r["income_group"] is None and r["iso3"] not in known]
    if gaps:
        print(f"    ! 世行没有这些市场国别的元数据:{', '.join(gaps)}")
    return {"country": rows}
