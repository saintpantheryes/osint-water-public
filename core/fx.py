"""汇率(A21):把线索、商机的「投资规模 + 货币」折成人民币万元(商机顺带算美元)。

来源按顺序试:open.er-api.com(人民币基准,免密钥)→ 欧央行每日牌价(欧元基准,再按欧元兑人民币换)→ 程序内置的近似表(STATIC,
2026 年 10 月前后的大致牌价,只在两个接口都取不到时用,后台日志会写明)。一天取一次,存本机缓存文件;不用表格公式(R12)。
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

import httpx

ERAPI_URL = "https://open.er-api.com/v6/latest/CNY"
ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
TIMEOUT = 20.0

# 1 单位外币 = 多少人民币(近似;只做兜底)
STATIC: dict[str, float] = {
    "CNY": 1.0, "USD": 7.15, "EUR": 7.85, "GBP": 9.2, "JPY": 0.047, "CHF": 8.3, "AUD": 4.7, "CAD": 5.2, "SGD": 5.4, "HKD": 0.92,
    "AED": 1.95, "SAR": 1.91, "QAR": 1.96, "KWD": 23.3, "OMR": 18.6, "EGP": 0.15, "MAD": 0.74, "DZD": 0.054, "TND": 2.35,
    "NGN": 0.0047, "GHS": 0.52, "KES": 0.055, "TZS": 0.0027, "UGX": 0.0019, "ETB": 0.051, "ZAR": 0.39, "XOF": 0.012, "XAF": 0.012,
    "INR": 0.082, "PKR": 0.026, "BDT": 0.059, "LKR": 0.024, "NPR": 0.051, "IDR": 0.00043, "MYR": 1.65, "THB": 0.21, "VND": 0.00028,
    "PHP": 0.123, "MMK": 0.0034, "KHR": 0.00177, "KZT": 0.0135, "UZS": 0.00057, "AZN": 4.2, "GEL": 2.6, "AMD": 0.0185, "KGS": 0.082,
    "TJS": 0.68, "TMT": 2.04, "MNT": 0.002, "TRY": 0.18, "IQD": 0.0055, "JOD": 10.1, "LBP": 0.00008, "PLN": 1.85, "RON": 1.55,
    "HUF": 0.0198, "CZK": 0.32, "RSD": 0.067, "UAH": 0.17, "RUB": 0.085, "BRL": 1.3, "MXN": 0.38, "COP": 0.0017, "PEN": 1.95,
    "CLP": 0.0075, "ARS": 0.005, "BOB": 1.03, "PYG": 0.00092, "UYU": 0.175, "DOP": 0.115, "JMD": 0.045, "FJD": 3.1, "PGK": 1.75,
    "WST": 2.55, "TOP": 2.95, "SBD": 0.85, "VUV": 0.06,
}

AMOUNT_RE = re.compile(r"^\s*(?:([A-Z]{3})\s*)?([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:([A-Z]{3}))?\s*$")


def parse_amount(text: object) -> tuple[float, str] | None:
    """「5,000,000 USD」「USD 5000000」「12.5 EUR」→ (数值, 币种);别的写法(百万、million、括号说明)不解析,留给人。"""
    m = AMOUNT_RE.match(str(text or ""))
    if not m:
        return None
    cur = m.group(1) or m.group(3)
    if not cur or (m.group(1) and m.group(3)):
        return None
    try:
        return float(m.group(2).replace(",", "")), cur.upper()
    except ValueError:
        return None


def _from_erapi(client: httpx.Client) -> dict[str, float] | None:
    r = client.get(ERAPI_URL)
    r.raise_for_status()
    data = r.json()
    if data.get("result") != "success" or not isinstance(data.get("rates"), dict):
        return None
    out = {}
    for code, per_cny in data["rates"].items():  # 1 CNY = per_cny 外币 → 1 外币 = 1/per_cny CNY
        try:
            if float(per_cny) > 0:
                out[str(code).upper()] = 1.0 / float(per_cny)
        except (TypeError, ValueError):
            continue
    out["CNY"] = 1.0
    return out if len(out) > 10 else None


def _from_ecb(client: httpx.Client) -> dict[str, float] | None:
    r = client.get(ECB_URL)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    per_eur = {c.get("currency"): float(c.get("rate")) for c in root.iter() if c.get("currency") and c.get("rate")}
    cny_per_eur = per_eur.get("CNY")
    if not cny_per_eur:
        return None
    out = {code: cny_per_eur / rate for code, rate in per_eur.items() if rate > 0}
    out["EUR"], out["CNY"] = cny_per_eur, 1.0
    return out


def fetch(client: httpx.Client | None = None, log=print) -> tuple[dict[str, float], str]:
    """→ (1 外币 = 多少人民币, 来源名)。两个接口都失败用内置表。"""
    own = client is None
    client = client or httpx.Client(timeout=TIMEOUT, follow_redirects=True, headers={"User-Agent": "osint-water/1.0"})
    try:
        for name, fn in (("open.er-api.com", _from_erapi), ("欧央行", _from_ecb)):
            try:
                rates = fn(client)
            except Exception as e:  # noqa: BLE001
                log(f"  汇率 {name} 没取到:{type(e).__name__}: {e}")
                continue
            if rates:
                return rates, name
    finally:
        if own:
            client.close()
    log("  汇率两个接口都没取到,用程序内置的近似表")
    return dict(STATIC), "内置近似表"


class Rates:
    """当天的汇率:缓存文件(cache_dir/fx.json,按日期)→ 取接口 → 内置表。to_cny / to_usd 给出折算值。"""

    def __init__(self, cache_dir: Path | None = None, today: date | None = None, client: httpx.Client | None = None, log=print):
        self.cache_dir, self.today, self.client, self.log = cache_dir, today or date.today(), client, log
        self._rates: dict[str, float] | None = None
        self.source = ""

    def rates(self) -> dict[str, float]:
        if self._rates is not None:
            return self._rates
        fp = (self.cache_dir / "fx.json") if self.cache_dir else None
        if fp and fp.exists():
            try:
                cached = json.loads(fp.read_text(encoding="utf-8"))
                if cached.get("date") == self.today.isoformat() and cached.get("rates"):
                    self._rates, self.source = {k: float(v) for k, v in cached["rates"].items()}, str(cached.get("source") or "缓存")
                    return self._rates
            except (ValueError, OSError):
                pass
        self._rates, self.source = fetch(self.client, self.log)
        if fp and self.source != "内置近似表":
            try:
                fp.parent.mkdir(parents=True, exist_ok=True)
                fp.write_text(json.dumps({"date": self.today.isoformat(), "source": self.source, "rates": self._rates}, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass
        return self._rates

    def rate(self, currency: str | None) -> float | None:
        if not currency:
            return None
        return self.rates().get(str(currency).upper()) or STATIC.get(str(currency).upper())

    def to_cny(self, value: float | None, currency: str | None) -> float | None:
        r = self.rate(currency)
        return float(value) * r if value is not None and r else None

    def to_usd(self, value: float | None, currency: str | None) -> float | None:
        cny, usd = self.to_cny(value, currency), self.rate("USD")
        return cny / usd if cny is not None and usd else None


def wan(x: float | None) -> int | None:
    """元 → 万元,四舍五入到整数(表里的「折合人民币(万元)」)。"""
    return None if x is None else int(round(x / 1e4))
