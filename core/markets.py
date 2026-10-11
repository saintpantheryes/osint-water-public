"""使用方的市场分布:经营区域 → 国别,及在各国的存在情况(当地机构、在建项目、常驻人员、托管关系)。

区域介于"全部"与国别之间:界面的区域筛选、热榜区域榜单、国别概况与报告按它分组;表里的国别也是宏观指标与国别概况的
采集范围。文件由 profile 的 markets 字段指定(私有,如 private/markets.yaml);没配置时这些功能退回按大洲。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from core import geo

MARKETS_SCHEMA = "osint-water/markets@1"
ALL = "*"  # 筛选里的"全部市场国别"


class MarketsError(ValueError):
    pass


@dataclass(frozen=True)
class MarketCountry:
    iso3: str
    name: str  # 按使用方原表的叫法(如"刚果金""波黑")
    region: str
    local_entity: bool | None = None
    projects: bool | None = None
    staff: bool | None = None
    note: str = ""

    def presence(self) -> str:
        """→ "已设当地机构、有在建项目、有常驻人员" 这样的一句话(附原表备注)。"""
        parts = [txt for flag, txt in ((self.local_entity, "已设当地机构"), (self.projects, "有在建项目"), (self.staff, "有常驻人员"))
                 if flag]
        s = "、".join(parts) or "未设机构、无在建项目与常驻人员"
        return s + (f"({self.note})" if self.note else "")


@dataclass(frozen=True)
class Markets:
    name: str
    org: str  # 使用方简称(国别概况、报告里写"<简称>在该国")
    label: str  # 界面上这一级的叫法,如"经营区域"
    as_of: str
    source: str
    regions: tuple[str, ...]
    countries: tuple[MarketCountry, ...]

    def iso3s(self) -> tuple[str, ...]:
        return tuple(c.iso3 for c in self.countries)

    def get(self, iso3: str | None) -> MarketCountry | None:
        return next((c for c in self.countries if c.iso3 == iso3), None)

    def region_of(self, iso3: str | None) -> str:
        """国别所在区域;不在市场表里返回空串。"""
        c = self.get(iso3)
        return c.region if c else ""

    def members(self, region: str) -> list[str]:
        """区域里的国别 ISO3(按原表顺序);ALL 为全部市场国别。"""
        return [c.iso3 for c in self.countries if region == ALL or c.region == region]

    def region_map(self) -> dict[str, str]:
        return {c.iso3: c.region for c in self.countries}


def load_markets(path: str | Path | None) -> Markets | None:
    """读市场分布文件并校验(schema、国别码可识别、同一国别不重复)。path 为空或文件不存在时 None。"""
    if not path or not Path(path).exists():
        return None
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if raw.get("schema") != MARKETS_SCHEMA:
        raise MarketsError(f"{path}: schema 应为 {MARKETS_SCHEMA}")
    errs: list[str] = []
    countries: list[MarketCountry] = []
    regions: list[str] = []
    for g in raw.get("regions") or []:
        name = str(g.get("name") or "").strip()
        if not name or name in regions:
            errs.append(f"区域名为空或重复:{name!r}")
            continue
        regions.append(name)
        for c in g.get("countries") or []:
            iso3 = str(c.get("iso3") or "").upper()
            if iso3 not in geo.countries():
                errs.append(f"{name} / {c.get('name')}: 国别码 {iso3!r} 不认识")
                continue
            countries.append(MarketCountry(iso3=iso3, name=str(c.get("name") or geo.name_zh(iso3)), region=name,
                                           local_entity=c.get("local_entity"), projects=c.get("projects"), staff=c.get("staff"),
                                           note=str(c.get("note") or "").strip()))
    dup = sorted({c.iso3 for c in countries if sum(x.iso3 == c.iso3 for x in countries) > 1})
    if dup:
        errs.append(f"国别重复:{', '.join(dup)}")
    if errs:
        raise MarketsError(f"{path} 校验失败:\n  " + "\n  ".join(errs))
    return Markets(name=str(raw.get("name") or "市场分布"), org=str(raw.get("org") or "本单位"), label=str(raw.get("label") or "市场区域"),
                   as_of=str(raw.get("as_of") or ""),
                   source=str(raw.get("source") or ""), regions=tuple(regions), countries=tuple(countries))


def scope_iso3s(profile) -> tuple[str, ...]:
    """宏观指标与国别概况的采集范围:profile 的国别在前,再加市场分布表里的国别(去重)。"""
    out = list(profile.iso3s)
    m = load_markets(getattr(profile, "markets", None))
    out += [c for c in (m.iso3s() if m else ()) if c not in out]
    return tuple(out)
