"""线索 → 台账项目的匹配建议(机器建议、人工确认)。

高:线索里出现台账登记的项目编号(EBRD 项目号、ECEPP 公告号、世行 P 号、IsDB AZE 号、欧佩克基金贷款号、亚投行编号……)
中:出现项目英文名、当地语名或别名(ledger 的 aliases)——只配同一国的台账项目:别名常是通用说法(如 non-revenue water),
   全球信源里别国的同类项目会误挂上来。线索国别未定时照配;编号吻合的不看国别(编号是唯一的)。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from core.ledger import Ledger


@dataclass(frozen=True)
class Key:
    item: str
    pattern: re.Pattern
    label: str
    confidence: str  # high | medium
    country: str | None = None  # 台账项目所在国(ISO3)


def item_country(item_id: str) -> str | None:
    """台账编号 → 国别:编号按 {ISO3}-NNN 编(core.ledger_edit.next_item_id);不合此式的 None。"""
    m = re.fullmatch(r"([A-Z]{3})-\d+", str(item_id or ""))
    return m[1] if m else None


def item_countries(led) -> dict[str, str | None]:
    """台账(或几国合并的台账)→ {项目编号: 国别}。合并的台账带 countries 映射,单国台账用它的 country,都没有时看编号。"""
    if not led:
        return {}
    known, own = getattr(led, "countries", None) or {}, getattr(led, "country", None)
    return {it["id"]: known.get(it["id"]) or own or item_country(it["id"]) for it in led.items}


def foreign(item_iso3: str | None, sig_iso3: str | None) -> bool:
    """台账项目与线索分属两国(两边国别都已知且不同)。"""
    return bool(item_iso3 and sig_iso3 and item_iso3 != sig_iso3)


def firm(lk: dict) -> bool:
    """编号吻合或监测配置指定的挂接:不看国别。"""
    return lk.get("by") == "config" or (lk.get("by") == "rule" and lk.get("confidence") == "high")


def _id_variants(v: str) -> list[str]:
    """'AZE 1028' → 同时认 AZE-1028、AZE1028;'17052PA' → 另认 17052;'PR 018783' → 另认 018783。"""
    v = str(v).strip()
    out = {v}
    m = re.fullmatch(r"([A-Z]{2,4})[ -]?(\d{3,})", v)
    if m:
        out |= {f"{m[1]} {m[2]}", f"{m[1]}-{m[2]}", f"{m[1]}{m[2]}"}
    m = re.fullmatch(r"(\d{4,})[A-Z]{1,3}", v)
    if m:
        out.add(m[1])
    m = re.fullmatch(r"[A-Z]{1,3} (\d{5,})", v)
    if m:
        out.add(m[1])
    return sorted(out)


def build_index(led: Ledger) -> list[Key]:
    keys: list[Key] = []
    countries = item_countries(led)
    for it in led.items:
        iid, iso = it["id"], countries.get(it["id"])
        for label, v in (it.get("ids") or {}).items():
            for token in _id_variants(v):
                if len(re.sub(r"\D", "", token)) < 4 and not re.search(r"[A-Z]{2}", token):
                    continue  # 太短的纯数字容易误配
                keys.append(Key(iid, re.compile(rf"(?<![\w]){re.escape(token)}(?![\w])"), f"{label} {token}", "high", iso))
        names = [it.get("name_en"), it.get("name_local"), *(it.get("aliases") or [])]
        for n in names:
            n = (n or "").strip()
            if len(n) >= 4:
                keys.append(Key(iid, re.compile(re.escape(n), re.IGNORECASE), n, "medium", iso))
    return keys


def suggest(text: str, index: list[Key], country: str | None = None) -> list[dict]:
    """→ [{item, confidence, by: rule, reason}],同一项目只留最高的一条。country:线索国别,按名称、别名只配同一国的项目。"""
    best: dict[str, dict] = {}
    for k in index:
        if k.confidence != "high" and foreign(k.country, country):
            continue
        if k.pattern.search(text):
            cur = best.get(k.item)
            if not cur or (cur["confidence"] == "medium" and k.confidence == "high"):
                best[k.item] = {"item": k.item, "confidence": k.confidence, "by": "rule", "reason": f"命中 {k.label}"}
    return sorted(best.values(), key=lambda x: (x["confidence"] != "high", x["item"]))
