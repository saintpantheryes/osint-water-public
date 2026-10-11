"""世行 WDI 指标 → country_indicator 长表(存年份,不用 mrnev=1,前端必须显示数据年)。

范围:profile 的国别 + 市场分布表里的国别(core.markets.scope_iso3s)。
许可:World Bank Open Data,CC BY 4.0。
接口:https://api.worldbank.org/v2/country/{iso3;…}/indicator/{code}?format=json&date=YYYY:YYYY&per_page=…
注意:WGI 指标另有 source=3,此处不含。
"""
from __future__ import annotations

from datetime import UTC, datetime

import httpx

from core.markets import scope_iso3s
from core.profile import Profile
from core.sources import register
from core.sources.http import get_json
from core.storage import now_iso, write_raw

SOURCE = "wdi_indicators"
URL = "https://api.worldbank.org/v2/country/{codes}/indicator/{code}"

# 最小指标集(见 docs/sources/E):键=WDI 代码,值=中文名/单位
INDICATORS: dict[str, tuple[str, str]] = {
    "NY.GDP.MKTP.CD": ("GDP(现价美元)", "USD"),
    "NY.GDP.PCAP.CD": ("人均 GDP(现价美元)", "USD"),
    "NY.GDP.MKTP.KD.ZG": ("GDP 实际增速", "%"),
    "NY.GNP.PCAP.CD": ("人均 GNI(Atlas)", "USD"),
    "SP.POP.TOTL": ("总人口", "人"),
    "SP.POP.GROW": ("人口增速", "%"),
    "SP.URB.TOTL.IN.ZS": ("城镇化率", "%"),
    "SP.URB.GROW": ("城镇人口增速", "%"),
    "BX.KLT.DINV.CD.WD": ("FDI 净流入", "USD"),
    "FP.CPI.TOTL.ZG": ("CPI 通胀", "%"),
    "PA.NUS.FCRF": ("官方汇率(本币/USD,年均)", "LCU/USD"),
    "DT.DOD.DECT.GN.ZS": ("外债/GNI", "%"),
    "SH.H2O.BASW.ZS": ("基本饮水覆盖", "%"),
    "SH.H2O.SMDW.ZS": ("安全管理饮水覆盖", "%"),
    "SH.STA.BASS.ZS": ("基本卫生覆盖", "%"),
    "SH.STA.SMSS.ZS": ("安全管理卫生覆盖", "%"),
    "ER.H2O.FWST.ZS": ("淡水压力(取水/可再生资源)", "%"),
    # 国别概况与报告第 2–4 章用到的:国土、产业结构、外部支付能力、水资源
    "AG.SRF.TOTL.K2": ("国土面积", "km²"),
    "EN.POP.DNST": ("人口密度", "人/km²"),
    "NV.AGR.TOTL.ZS": ("农林渔业增加值/GDP", "%"),
    "NV.IND.TOTL.ZS": ("工业增加值/GDP", "%"),
    "NV.SRV.TOTL.ZS": ("服务业增加值/GDP", "%"),
    "FI.RES.TOTL.CD": ("外汇储备(含黄金)", "USD"),
    "DT.DOD.DECT.CD": ("外债总额", "USD"),
    "ER.H2O.INTR.PC": ("人均境内可再生淡水", "m³"),
    "ER.H2O.FWTL.K3": ("年淡水取用量", "亿 m³"),
    "ER.H2O.FWAG.ZS": ("取水中农业占比", "%"),
    "ER.H2O.FWIN.ZS": ("取水中工业占比", "%"),
    "ER.H2O.FWDM.ZS": ("取水中生活占比", "%"),
}
SCALE = {"ER.H2O.FWTL.K3": 10.0}  # WDI 单位 10 亿 m³ → 亿 m³
YEARS_BACK = 10


@register(SOURCE)
def run(profile: Profile) -> dict[str, list[dict]]:
    fetched_at = now_iso()
    y1 = datetime.now(UTC).year
    date = f"{y1 - YEARS_BACK}:{y1}"
    scope = scope_iso3s(profile)
    codes = ";".join(scope)
    rows: list[dict] = []
    skipped: list[str] = []
    for code, (label, unit) in INDICATORS.items():
        try:
            # 世行 API 偶有单个指标响应超过一分钟;超时重试一次仍失败就跳过该指标(报告里标缺),不拖垮整个源
            payload = get_json(URL.format(codes=codes, code=code), {"format": "json", "date": date, "per_page": 2000}, timeout=90.0)
        except httpx.TimeoutException:
            skipped.append(code)
            continue
        write_raw(SOURCE, code, payload)
        if len(payload) < 2 or not payload[1]:
            continue  # 该指标对样本国无数据,不算失败(报告里标缺)
        for r in payload[1]:
            if r.get("value") is None:
                continue
            rows.append(
                {
                    "iso3": r["countryiso3code"].upper(),
                    "indicator": code,
                    "indicator_label": label,
                    "unit": unit,
                    "year": int(r["date"]),
                    "value": float(r["value"]) * SCALE.get(code, 1.0),
                    "source": "WB-WDI",
                    "is_forecast": False,
                    "source_url": URL.format(codes=r["countryiso3code"], code=code),
                    "fetched_at": fetched_at,
                }
            )
    # 断言:每个样本国至少有一条(防国别码写错导致静默丢国);市场表里的国别缺数只提示,不拖垮整个源
    seen = {r["iso3"] for r in rows}
    missing = [c for c in profile.iso3s if c not in seen and c not in profile.known_zero_countries]
    if missing:
        raise RuntimeError(f"WDI returned nothing for {missing} — check iso3 / wb codes")
    gaps = [c for c in scope if c not in seen and c not in profile.iso3s]
    if gaps:
        print(f"    ! WDI 没有这些市场国别的数据:{', '.join(gaps)}")
    if skipped:
        print(f"    ! WDI 超时跳过 {len(skipped)} 项指标:{', '.join(skipped)}")
    return {"country_indicator": rows}
