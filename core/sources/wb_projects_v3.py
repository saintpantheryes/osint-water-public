"""世行项目库 v3 → project 表(在执行 + 储备)。v2 已冻结在 2024-12,勿用。

许可:World Bank Open Data,CC BY 4.0。
接口:https://search.worldbank.org/api/v3/projects?format=json&countrycode_exact=A^B&status_exact=Active^Pipeline&rows=…&os=…&fl=*
坑(实测,见 docs/sources/D1):
- countrycode_exact 用世行自有国别码(profile.wb_country_code),不是 ISO2;
- 不认识的参数被静默忽略 → 对带过滤的请求断言"过滤后 total < 不过滤 total";
- 部门信息在 major_sectors 嵌套里,新(WWA/WWC/WWF/WWW)旧(WA/WC/WF)两套代码并存;储备项目约半数无部门码,
  占比字段普遍为 0 → 储备项目不做机器判定,标 needs_review 由人读;
- 金额是字符串;多国/区域项目 countrycode 是列表。
"""
from __future__ import annotations

import json
import re

from core.profile import Profile
from core.sources import register
from core.sources.http import get_json
from core.storage import now_iso, write_raw

SOURCE = "wb_projects_v3"
URL = "https://search.worldbank.org/api/v3/projects"
DETAIL = "https://projects.worldbank.org/en/projects-operations/project-detail/{pid}"
PAGE = 500
KW = re.compile(r"water|sanitation|wastewater|sewer|desalin|irrigat|drainage|hydraul|eau|assainissement|agua|saneam|водоснабж|канализац", re.I)


def _f(x) -> float | None:
    try:
        return float(x) if x not in (None, "", "null") else None
    except (TypeError, ValueError):
        return None


def _sectors(p: dict) -> list[tuple[str, float]]:
    out = []
    for ms in p.get("major_sectors") or []:
        for s in (ms.get("major_sector") or {}).get("sectors") or []:
            code = (s.get("sector_code") or "").upper()
            if code:
                out.append((code, _f(s.get("sector_percent")) or 0.0))
    return out


def classify(p: dict) -> tuple[str, float, float]:
    """→ (water_class, core_water_pct, irrigation_pct)。
    core: 核心水务(供水/污水/卫生/水资源/水务公共管理)占比≥30
    irrigation: 核心<30 但 核心+灌排≥30
    keyword: 无部门码或占比不足,但名称/PDO 命中水务关键词(提示,非判定)
    needs_review: 储备项目且无部门码
    none: 其它
    """
    secs = _sectors(p)
    core = sum(pct for c, pct in secs if c.startswith("W"))
    irr = sum(pct for c, pct in secs if c in ("AI", "AAI"))
    text = f"{p.get('project_name','')} {p.get('pdo','')}"
    kw = bool(KW.search(text))
    if secs and core >= 30:
        return "core", core, irr
    if secs and core + irr >= 30:
        return "irrigation", core, irr
    if not secs and p.get("status") == "Pipeline":
        return ("keyword" if kw else "needs_review"), core, irr
    return ("keyword" if kw else "none"), core, irr


@register(SOURCE)
def run(profile: Profile) -> dict[str, list[dict]]:
    fetched_at = now_iso()
    wb2iso3 = {c.wb_country_code: c.iso3 for c in profile.countries}
    base = {"format": "json", "status_exact": "Active^Pipeline", "fl": "*", "rows": PAGE}
    # 断言过滤生效:不带国别过滤的总数必须大于带过滤的总数
    total_all = int(get_json(URL, {**base, "rows": 0})["total"])  # total 是字符串!
    params = {**base, "countrycode_exact": "^".join(profile.wb_codes)}
    projects: dict[str, dict] = {}
    os_ = 0
    while True:
        payload = get_json(URL, {**params, "os": os_})
        write_raw(SOURCE, f"page_{os_}", payload)
        total = int(payload["total"])
        if os_ == 0 and total >= total_all:
            raise RuntimeError(f"country filter ignored: filtered total {total} >= unfiltered {total_all}")
        projects.update(payload.get("projects") or {})
        os_ += PAGE
        if os_ >= total or not payload.get("projects"):
            break

    rows = []
    for pid, p in projects.items():
        codes = [c.upper() for c in (p.get("countrycode") or [])]
        iso3s = [wb2iso3[c] for c in codes if c in wb2iso3]
        wclass, core_pct, irr_pct = classify(p)
        ms = (p.get("milestones") or [{}])[0] if isinstance(p.get("milestones"), list) else {}
        rows.append(
            {
                "source": SOURCE,
                "source_id": pid,
                "funder": "WB",
                "window": "sovereign",  # 世行 IPF/PforR/DPF 均为主权业务;私营窗口见 IFC 适配器
                "name": p.get("project_name"),
                "iso3": iso3s[0] if len(iso3s) == 1 else None,  # 多国/区域项目置空,原始列表存 extras
                "country_display": p.get("countryshortname"),
                "region_wb": p.get("regionname"),
                "status": p.get("status"),
                "stage": p.get("last_stage_reached_name"),
                "board_date": p.get("boardapprovaldate_exact"),
                "closing_date": p.get("closingdate"),
                "amount_commit_usd": _f(p.get("totalamt")),  # IBRD+IDA 承诺
                "amount_grant_usd": _f(p.get("grantamt")),
                "amount_total_cost_usd": _f(p.get("lendprojectcost")),
                "currency": "USD",
                "implementing_agency": p.get("impagency"),
                "borrower": p.get("borrower"),
                "water_class": wclass,
                "core_water_pct": core_pct,
                "irrigation_pct": irr_pct,
                "sector_codes": ",".join(c for c, _ in _sectors(p)),
                "pdo": (p.get("pdo") or "")[:600],
                "financial_types": ",".join(p.get("projectfinancialtype") or []),
                "cofinancers": "; ".join((f.get("fincrname") or "") for f in (p.get("financers") or []))[:400],
                "last_updated": p.get("proj_last_upd_date"),
                "source_url": DETAIL.format(pid=pid),
                "fetched_at": fetched_at,
                "extras": json.dumps(
                    {"countrycodes": codes, "iso3s": iso3s, "milestones": ms, "themes_l2": p.get("themev2_level2_exact")},
                    ensure_ascii=False,
                ),
            }
        )
    # 每个样本国至少一条(已知零名单除外)
    seen = {r["iso3"] for r in rows if r["iso3"]}
    missing = [c for c in profile.iso3s if c not in seen and c not in profile.known_zero_countries]
    if missing:
        raise RuntimeError(f"no WB projects returned for {missing} — check wb_country_code in profile")
    return {"project": rows}
