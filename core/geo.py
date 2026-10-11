"""国别参考:代码归一、中文名、大洲与区域、从文本里识别国名。数据见 reference/countries.yaml(联合国 M49 标准)。"""
from __future__ import annotations

import re
from functools import lru_cache

import yaml

from core.ledger import ROOT

PATH = ROOT / "reference" / "countries.yaml"
CONTINENTS = ["亚洲", "非洲", "欧洲", "美洲", "大洋洲", "其他"]
_CJK = re.compile("[\u3400-\u9fff]")
_CYR = re.compile("[\u0400-\u04ff]")


@lru_cache(maxsize=1)
def _raw() -> dict:
    return yaml.safe_load(PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def countries() -> dict[str, dict]:
    """ISO3 → {iso2, name_zh, name_en, continent_zh, region_zh, aliases…}。"""
    return {c["iso3"]: c for c in _raw()["countries"]}


@lru_cache(maxsize=1)
def _codes() -> dict[str, str]:
    m = {c["iso2"]: c["iso3"] for c in _raw()["countries"] if c.get("iso2")}
    m.update(_raw().get("wb_codes") or {})
    return m


@lru_cache(maxsize=1)
def _patterns() -> tuple[list[re.Pattern], dict[str, str]]:
    """国名与别名的正则:拉丁字母按整词匹配;西里尔字母允许带格变化词尾(в Азербайджане);中文不看词边界。
    长的在前,避免"Guinea"先配上"Papua New Guinea"。"""
    names: dict[str, str] = {}
    for c in countries().values():
        for n in [c["name_zh"], c["name_en"], *c.get("aliases", [])]:
            if n and (len(n) >= 4 or _CJK.search(n)):
                names.setdefault(n.casefold(), c["iso3"])

    def alt(group):
        return "|".join(map(re.escape, sorted(group, key=len, reverse=True)))

    cjk = [n for n in names if _CJK.search(n)]
    cyr = [n for n in names if _CYR.search(n) and not _CJK.search(n)]
    words = [n for n in names if not _CJK.search(n) and not _CYR.search(n)]
    pats = [re.compile(r"(?<!\w)(" + alt(words) + r")(?!\w)", re.IGNORECASE),
            re.compile(r"(?<!\w)(" + alt(cyr) + r")[а-яё]{0,3}(?!\w)", re.IGNORECASE)]
    if cjk:
        pats.append(re.compile("(" + alt(cjk) + ")"))
    return pats, names


def detect(text: str | None) -> list[str]:
    """文本里提到的国别 ISO3,按首次出现的位置排序、去重。"""
    if not text:
        return []
    pats, names = _patterns()
    hits = [(m.start(), names.get(m.group(1).casefold())) for rx in pats for m in rx.finditer(text)]
    out: list[str] = []
    for _, iso in sorted(hits):
        if iso and iso not in out:
            out.append(iso)
    return out


def iso3(value: str | None) -> str | None:
    """ISO3、ISO2、世界银行国别码或国名 → ISO3;认不出返回 None。"""
    if not value:
        return None
    s = str(value).strip()
    if s.upper() in countries():
        return s.upper()
    if len(s) == 2 and s.upper() in _codes():
        return _codes()[s.upper()]
    found = detect(s)
    return found[0] if found else None


def name_zh(code: str | None) -> str:
    c = countries().get(code or "")
    return c["name_zh"] if c else (code or "未识别")


def continent(code: str | None) -> str:
    c = countries().get(code or "")
    return c["continent_zh"] if c else "其他"


def map_continent(code: str | None) -> str:
    """地图上按大洲一键忽略用的分组(「我的」页):美洲拆成北美洲(含中美洲、加勒比)与南美洲,其余同 continent。"""
    ct = continent(code)
    if ct == "美洲":
        return "南美洲" if region(code) == "南美洲" else "北美洲"
    return ct


# IMF《世界经济展望》的「发达经济体」(2025 年 4 月版,41 个;香港、澳门、台湾在中国口径里并入中国,不单列)。「我的」页一键忽略发达国家用
ADVANCED = frozenset("AND AUS AUT BEL CAN HRV CYP CZE DNK EST FIN FRA DEU GRC ISL IRL ISR ITA JPN KOR LVA LTU LUX MLT NLD NZL NOR PRT PRI SMR "
                     "SGP SVK SVN ESP SWE CHE GBR USA".split())
# 世界银行按人均国民收入(GNI,阿特拉斯法)分的四组;地图属性 inc 存这四个码
INCOME_CODES = {"HIC": "高收入", "UMC": "中等偏上收入", "LMC": "中等偏下收入", "LIC": "低收入"}


def region(code: str | None) -> str:
    c = countries().get(code or "")
    return c["region_zh"] if c else "其他"


def regions_by_continent() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {k: [] for k in CONTINENTS}
    for c in countries().values():
        lst = out.setdefault(c["continent_zh"], [])
        if c["region_zh"] not in lst:
            lst.append(c["region_zh"])
    return {k: sorted(v) for k, v in out.items() if v}
