"""IMF WEO(DataMapper)→ country_indicator 长表,含至 2030 年前后的预测值(is_forecast=True)。

许可:IMF 数据使用条款(公开、须署名)。
接口:https://www.imf.org/external/datamapper/api/v1/{INDICATOR}   —— 返回全部经济体,客户端按 iso3 过滤。
坑:该站对"伪装浏览器"UA 返回 403,对如实的非浏览器 UA 正常。路径里的国别过滤无效。
"""
from __future__ import annotations

from datetime import UTC, datetime

from core.markets import scope_iso3s
from core.profile import Profile
from core.sources import register
from core.sources.http import get_json
from core.storage import now_iso, write_raw

SOURCE = "imf_datamapper"
URL = "https://www.imf.org/external/datamapper/api/v1/{code}"
INDICATORS: dict[str, tuple[str, str]] = {
    "NGDP_RPCH": ("GDP 实际增速(IMF,含预测)", "%"),
    "PCPIPCH": ("CPI 通胀(IMF,含预测)", "%"),
    "GGXWDG_NGDP": ("政府总债务/GDP(IMF)", "%"),
    "BCA_NGDPD": ("经常账户/GDP(IMF)", "%"),
    "LP": ("人口(IMF,含预测)", "百万人"),
    "NGDPD": ("GDP(IMF,含预测)", "十亿美元"),
    "NGDPDPC": ("人均 GDP(IMF,含预测)", "美元"),
    "GGXCNL_NGDP": ("政府净借贷/GDP(IMF,负为赤字)", "%"),
}
YEARS_BACK = 10


@register(SOURCE)
def run(profile: Profile) -> dict[str, list[dict]]:
    fetched_at = now_iso()
    this_year = datetime.now(UTC).year
    rows: list[dict] = []
    scope = scope_iso3s(profile)
    for code, (label, unit) in INDICATORS.items():
        payload = get_json(URL.format(code=code))
        write_raw(SOURCE, code, payload)
        series = (payload.get("values") or {}).get(code) or {}
        for iso3 in scope:
            for year, val in (series.get(iso3) or {}).items():
                y = int(year)
                if val is None or y < this_year - YEARS_BACK:
                    continue
                rows.append(
                    {
                        "iso3": iso3,
                        "indicator": code,
                        "indicator_label": label,
                        "unit": unit,
                        "year": y,
                        "value": float(val),
                        "source": "IMF-WEO",
                        "is_forecast": y >= this_year,  # WEO 当年及以后为估计/预测
                        "source_url": f"https://www.imf.org/external/datamapper/{code}@WEO/{iso3}",
                        "fetched_at": fetched_at,
                    }
                )
    seen = {r["iso3"] for r in rows}
    missing = [c for c in profile.iso3s if c not in seen and c not in profile.known_zero_countries]
    if missing:
        raise RuntimeError(f"IMF returned nothing for {missing}")
    gaps = [c for c in scope if c not in seen and c not in profile.iso3s]
    if gaps:
        print(f"    ! IMF 没有这些市场国别的数据:{', '.join(gaps)}")
    return {"country_indicator": rows}
