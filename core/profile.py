"""profile.yaml 的加载与校验。core 与使用方之间唯一的接缝。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

# 部门共用的多维表格(自托管 Teable,core/teable.py):base 库名(按名找,没有就建);pull 是否把同事在表里改的人管字段拉回系统;
# server_sync:表格只由服务器上的系统同步、每 30 秒一次(core/live_sync.py),定时任务与本机界面不再同步
TEABLE_DEFAULTS = {"base": "水务项目机会", "pull": False, "internal_reports": False, "notify_owner": False, "server_sync": False}


@dataclass(frozen=True)
class Country:
    iso2: str
    iso3: str
    wb_country_code: str  # 世行系接口自有国别码(多数=iso2;例外如 CD→ZR、TL→TP)
    name_en: str
    name_zh: str
    tier: str  # focus | cultivate | watch;按台账文件自动加进来的新国家为 market
    ledger: Path | None = None  # 项目机会台账(相对 profile 所在目录)
    overlay: Path | None = None  # 使用方私有叠加(同上)
    watch: Path | None = None  # 监测信源配置 countries/{ISO3}/watch.yaml(同上)


@dataclass(frozen=True)
class Profile:
    name: str
    countries: tuple[Country, ...]
    sources_enabled: tuple[str, ...]
    sector_scope: dict
    thresholds: dict
    languages: tuple[str, ...]
    known_zero_countries: tuple[str, ...]
    report: dict = field(default_factory=dict)
    bidder: dict = field(default_factory=dict)  # nationality(ISO3)、local_entity{ISO3: 形式}
    lead_times: dict = field(default_factory=dict)  # 内部流程周期(天),按节点类型分项
    city_min_population: int = 100000  # 「规模以上城市」的人口门槛(A22)
    rules: Path | None = None  # 投标资格规则表;不设则用 reference/procurement_rules.yaml
    data_dir: Path | None = None  # 该 profile 的快照目录;不设则用 OSINT_WATER_DATA 或仓库 data/
    watch: dict = field(default_factory=dict)  # 监测参数:lookback_days、inbox_days、deadline_days、llm{model, effort, max_calls…}
    markets: Path | None = None  # 使用方的市场分布(经营区域 → 国别),见 core/markets.py;不设则没有区域一级
    teable: dict = field(default_factory=lambda: dict(TEABLE_DEFAULTS))  # 多维表格:base、pull、internal_reports、notify_owner,缺的项取缺省
    assistant: dict = field(default_factory=dict)  # 门户「助手」页:llm{provider, model, effort…} 可与采集判定用不同的模型;缺省沿用 watch.llm
    path: Path | None = None
    ledger_dir: Path | None = None  # 各国台账所在目录(countries/),取自 profile 里写的台账路径;新国家的台账建在这里

    def ledger_file(self, iso3: str) -> Path | None:
        return ledger_file(self, iso3)

    @property
    def iso3s(self) -> tuple[str, ...]:
        return tuple(c.iso3 for c in self.countries)

    @property
    def wb_codes(self) -> tuple[str, ...]:
        return tuple(c.wb_country_code for c in self.countries)

    def country(self, iso3: str) -> Country:
        for c in self.countries:
            if c.iso3 == iso3.upper():
                return c
        raise KeyError(f"profile 中没有国别 {iso3}")


def ledger_file(profile, iso3: str) -> Path | None:
    """这国台账文件的路径:profile 里有的用它;没有的按台账目录 ledger_dir/{ISO3}/ledger.yaml(文件可能还不存在,
    建第一个商机时写出);都没有为 None。profile 也可以是只有 countries(、ledger_dir)的替身。"""
    iso3 = str(iso3 or "").upper()
    c = next((c for c in profile.countries if c.iso3 == iso3 and c.ledger), None)
    if c:
        return Path(c.ledger)
    d = getattr(profile, "ledger_dir", None)
    return Path(d) / iso3 / "ledger.yaml" if d and len(iso3) == 3 and iso3.isalpha() else None


REQUIRED_COUNTRY_KEYS = ("iso2", "iso3", "name_en", "tier")


def load_profile(path: str | Path) -> Profile:
    path = Path(path).resolve()
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    base = path.parent
    countries = []
    for c in raw["countries"]:
        missing = [k for k in REQUIRED_COUNTRY_KEYS if k not in c]
        if missing:
            raise ValueError(f"profile country entry missing {missing}: {c}")
        countries.append(
            Country(
                iso2=c["iso2"].upper(),
                iso3=c["iso3"].upper(),
                wb_country_code=str(c.get("wb_country_code") or c["iso2"]).upper(),
                name_en=c["name_en"],
                name_zh=c.get("name_zh", ""),
                tier=c["tier"],
                ledger=(base / c["ledger"]).resolve() if c.get("ledger") else None,
                overlay=(base / c["overlay"]).resolve() if c.get("overlay") else None,
                watch=(base / c["watch"]).resolve() if c.get("watch") else None,
            )
        )
    if len({c.iso3 for c in countries}) != len(countries):
        raise ValueError("duplicate iso3 in profile")
    # 新国家的台账:profile 里没写、但台账目录下已有 {ISO3}/ledger.yaml 的(在这个国家建第一个商机时系统建的),也算进来,
    # 内部叠加放在已有叠加文件的同一目录(文件等表格里改了优先级、负责人时再建)
    ledger_dir = next((c.ledger.parent.parent for c in countries if c.ledger), None)
    overlay_dir = next((c.overlay.parent for c in countries if c.overlay), base / "overlay")
    if ledger_dir and ledger_dir.is_dir():
        from core import geo  # 用到时再导入:geo 读参考表,profile 本身不依赖它

        declared = {c.iso3 for c in countries}
        for fp in sorted(ledger_dir.glob("*/ledger.yaml")):
            iso3, g = fp.parent.name.upper(), geo.countries().get(fp.parent.name.upper())
            if iso3 in declared or not g:
                continue
            countries.append(Country(iso2=g.get("iso2", ""), iso3=iso3, wb_country_code=g.get("iso2", ""), name_en=g.get("name_en", iso3),
                                     name_zh=g.get("name_zh", ""), tier="market", ledger=fp.resolve(),
                                     overlay=(overlay_dir / f"{iso3}.yaml").resolve()))
    bidder = raw.get("bidder") or {}
    if bidder.get("nationality"):
        bidder = {**bidder, "nationality": str(bidder["nationality"]).upper()}
    return Profile(
        name=raw["name"],
        countries=tuple(countries),
        sources_enabled=tuple(raw.get("sources_enabled", [])),
        sector_scope=raw.get("sector_scope", {}),
        thresholds=raw.get("thresholds", {}),
        languages=tuple(raw.get("languages", ["en"])),
        known_zero_countries=tuple(x.upper() for x in raw.get("known_zero_countries", [])),
        report=raw.get("report", {}),
        bidder=bidder,
        lead_times=raw.get("lead_times") or {},
        city_min_population=int(raw.get("city_min_population") or 100000),
        rules=(base / raw["procurement_rules"]).resolve() if raw.get("procurement_rules") else None,
        data_dir=(base / raw["data_dir"]).resolve() if raw.get("data_dir") else None,
        watch=raw.get("watch") or {},
        markets=(base / raw["markets"]).resolve() if raw.get("markets") else None,
        teable=_teable(raw.get("teable")),
        assistant=raw.get("assistant") or {},
        path=path,
        ledger_dir=ledger_dir,
    )


def _teable(raw: dict | None) -> dict:
    t = {**TEABLE_DEFAULTS, **(raw or {})}
    return {**t, "base": str(t.get("base") or TEABLE_DEFAULTS["base"]).strip(), "pull": bool(t.get("pull")),
            "internal_reports": bool(t.get("internal_reports")), "notify_owner": bool(t.get("notify_owner")),
            "server_sync": bool(t.get("server_sync"))}
