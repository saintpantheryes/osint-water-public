"""线索查询:把线索库摊平成一行一条(国别中文名、大洲、区域、领域、类别、下一个截止日、处理状态……),按条件筛选;
保存常用筛选(视图)。网页界面、报告与导出共用。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path

import yaml

from core import geo
from core.watch.events import STAGES, current_next, current_owners, current_stages
from core.watch.outputs import (
    CAT_ZH, LEDGER, PROCUREMENT, SECTOR_ZH, category, deadlines, links_of, relevant, sectors_of, summary,
)
from core.watch.store import TRIAGE_TEMPLATE, Store

VIEWS = "watch/views.yaml"  # 数据目录里:保存的筛选
STATUS_ZH = {"new": "待处理", "done": "已处理", "ignore": "已忽略"}
# 处理状态的含义:待处理 = 还没看;已处理 = 看过并跟进了(移出待处理清单和提醒页,仍是有效线索);
# 已忽略 = 无关或重复(当作噪声)。分析、报告、周报、截止预警统计"待处理 + 已处理",不含已忽略——与筛选方案里存的状态无关。
COUNTED = ["new", "done"]


def row(sig: dict, today: date, triage: dict, markets: dict[str, str] | None = None, stages: dict[str, dict] | None = None,
        owners: dict[str, dict] | None = None, nexts: dict[str, dict] | None = None) -> dict:
    """一条线索 → 展示与筛选用的一行。markets:国别 → 使用方的经营区域(core.markets 的 region_map);
    stages、owners、nexts:线索号 → 当前跟踪进展、负责人、下一步(core.watch.events 的 current_stages / current_owners / current_next)。"""
    t = triage.get(sig["id"]) or triage.get(sig["id"][:8]) or {}
    stage = ((stages or {}).get(sig["id"][:8]) or {}).get("value", "")
    nxt = (nexts or {}).get(sig["id"][:8]) or {}
    iso = sig.get("country")
    future = sorted((d, k) for d, k in deadlines(sig) if d >= today.isoformat())
    secs = sectors_of(sig)
    cat = category(sig)
    x = sig.get("llm") if sig.get("llm") and not sig["llm"].get("_error") else {}
    return {
        "id": sig["id"], "no": sig["id"][:8], "first_seen": sig["first_seen"][:10], "published": sig.get("published") or "",
        "country": iso or "", "country_zh": geo.name_zh(iso) if iso else "未识别",
        "continent": geo.continent(iso), "region": geo.region(iso), "market": (markets or {}).get(iso or "", ""),
        "sectors": secs, "sectors_zh": "、".join(SECTOR_ZH.get(k, k) for k in secs),
        "category": cat, "category_zh": CAT_ZH.get(cat, cat), "procurement": cat in PROCUREMENT,
        "relevant": relevant(sig), "title": sig["title"], "url": sig["url"],
        "source": sig["source"], "source_name": sig.get("source_name") or sig["source"],
        "deadline": future[0][0] if future else "", "deadline_type": future[0][1] if future else "",
        "links": "、".join(("●" if lk["confidence"] == "high" else "○") + lk["item"] for lk in links_of(sig)),
        "summary": summary(sig), "summary_zh": x.get("summary_zh") or sig.get("summary_zh") or "", "status": t.get("status") or "new",
        "note": t.get("note") or "",
        "funder": x.get("funder") or "", "project": x.get("project") or "",
        "amount": f"{x['amount']['value']:,.0f} {x['amount']['currency']}" if x.get("amount")
                  else (sig.get("fields") or {}).get("estimate", ""),  # 模型没判时用信源上的预估金额
        "judged_by": "模型" if x else "台账" if sig.get("source") == LEDGER else "规则", "scope": sig.get("scope") or sig.get("country") or "",
        "stage": stage, "stage_zh": STAGES.get(stage, ""),
        "owner": ((owners or {}).get(sig["id"][:8]) or {}).get("value", ""), "next_step": nxt.get("value", ""),
        "next_due": nxt.get("due", ""),
    }


def geo_options(rows: list[dict], continents: list[str], in_markets: set[str] | None, regions: list[str],
                countries: list[str]) -> tuple[list[str], list[str], list[str], list[str]]:
    """筛选栏的地理条件逐级收窄:大洲、经营区域(in_markets 为所选区域里的国别,None 为不限)→ 地理分区 → 国别。
    选项只列范围内的;已选的分区、国别里地理上不在范围内的去掉,地理上符合、只是暂时没有线索的保留
    (方案里存的关注国别不能因为没数据被悄悄放宽)。→ (分区选项, 国别选项, 保留的已选分区, 保留的已选国别)"""
    conts = set(continents)
    cont_of = {r: k for k, rs in geo.regions_by_continent().items() for r in rs}

    def region_ok(x: str) -> bool:  # 分区在所选大洲里;选了经营区域时,还要有区域里的国别在这个分区
        return (not conts or cont_of.get(x) in conts) and (in_markets is None or any(geo.region(c) == x for c in in_markets))

    keep_r = [x for x in regions if region_ok(x)]

    def country_ok(c: str) -> bool:
        return ((not conts or geo.continent(c) in conts) and (in_markets is None or c in in_markets)
                and (not keep_r or geo.region(c) in keep_r))

    keep_c = [c for c in countries if country_ok(c)]
    region_opts = sorted({r["region"] for r in rows if region_ok(r["region"])} | set(keep_r))
    country_opts = sorted({r["country"] for r in rows if r["country"] and country_ok(r["country"])}
                          | {c for c in (in_markets or ()) if country_ok(c)} | set(keep_c), key=geo.name_zh)
    return region_opts, country_opts, keep_r, keep_c


@dataclass
class Filter:
    """筛选条件;空列表表示不限。日期按"发现"(入库)日期,也可改按发布日期。"""

    days: int | None = 30  # 最近多少天(None 为不限)
    date_field: str = "first_seen"  # first_seen | published
    continents: list[str] = field(default_factory=list)
    regions: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)  # ISO3
    markets: list[str] = field(default_factory=list)  # 使用方的经营区域(core.markets);"*" 为全部市场国别
    sectors: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    status: list[str] = field(default_factory=lambda: ["new"])
    keyword: str = ""  # 空格分隔,全部命中(标题、摘要、项目、出资方、来源、国别、线索号、负责人、下一步)
    relevant_only: bool = True
    procurement_only: bool = False
    deadline_within: int | None = None  # 只看未来 N 天内截止的
    linked_only: bool = False  # 只看挂上台账的

    @classmethod
    def from_dict(cls, d: dict) -> Filter:
        return cls(**{k: v for k, v in (d or {}).items() if k in cls.__dataclass_fields__})


def apply(rows: list[dict], f: Filter, today: date) -> list[dict]:
    since = (today - timedelta(days=f.days)).isoformat() if f.days is not None else ""  # 0 为"今天"
    kws = [k.casefold() for k in f.keyword.split() if k.strip()]
    until = (today + timedelta(days=f.deadline_within)).isoformat() if f.deadline_within is not None else None
    out = []
    for r in rows:
        if since and (r[f.date_field] or r["first_seen"]) < since:
            continue
        if f.relevant_only and not r["relevant"]:
            continue
        if f.procurement_only and not r["procurement"]:
            continue
        if f.continents and r["continent"] not in f.continents:
            continue
        if f.regions and r["region"] not in f.regions:
            continue
        if f.markets and not (r.get("market") and ("*" in f.markets or r["market"] in f.markets)):
            continue
        if f.countries and r["country"] not in f.countries:
            continue
        if f.sectors and not set(r["sectors"]) & set(f.sectors):
            continue
        if f.categories and r["category"] not in f.categories:
            continue
        if f.sources and r["source"] not in f.sources:
            continue
        if f.status and r["status"] not in f.status:
            continue
        if until is not None and not (r["deadline"] and r["deadline"] <= until):
            continue
        if f.linked_only and not r["links"]:
            continue
        if kws:
            hay = " ".join([r["title"], r["summary"], r["project"], r["funder"], r["source_name"], r["country_zh"], r["no"],
                            r.get("owner", ""), r.get("next_step", "")]).casefold()
            if not all(k in hay for k in kws):
                continue
        out.append(r)
    return sorted(out, key=lambda r: (r["published"] or r["first_seen"], r["first_seen"]), reverse=True)


def load_rows(store: Store, today: date, days: int | None = None, markets: dict[str, str] | None = None) -> list[dict]:
    """线索库 → 行;days 给定时只读最近 days 天首见的(按月份文件跳读,快)。markets:国别 → 经营区域。"""
    since = today - timedelta(days=days) if days else None
    events = store.events()
    triage, stages, owners, nexts = store.triage(), current_stages(events), current_owners(events), current_next(events)
    return [row(s, today, triage, markets, stages, owners, nexts) for s in store.load_signals(since=since)]


CAL_BASIS = {"deadline": "截止日", "published": "发布日", "first_seen": "发现日"}


def calendar_items(rows: list[dict], signals: dict[str, dict], basis: str = "deadline") -> list[dict]:
    """日历视图:行 → [{date, kind, basis, row}],按日期排。按截止日时取线索的全部截止日(含已过的),同一天的几种截止
    (如投标截止、开标)并成一格;按发布日、发现日时一条一格。没有该日期或日期写法不对的不在内。
    signals:线索 id → 原始线索(截止日从这里取)。"""
    out: dict[tuple[str, str], dict] = {}
    for r in rows:
        if basis == "deadline":
            days = deadlines(signals.get(r["id"]) or {})
        else:
            days = [((r.get(basis) or "")[:10], CAL_BASIS[basis])]
        for d, kind in days:
            try:
                date.fromisoformat(d)
            except (TypeError, ValueError):
                continue
            it = out.setdefault((d, r["id"]), {"date": d, "kind": kind, "basis": basis, "row": r})
            if kind not in it["kind"].split("、"):
                it["kind"] += "、" + kind
    return sorted(out.values(), key=lambda it: (it["date"], it["row"]["country_zh"], it["row"]["title"]))


# ---------- 保存的筛选(视图) ----------

def load_views(data_dir: Path) -> dict[str, dict]:
    fp = Path(data_dir) / VIEWS
    return (yaml.safe_load(fp.read_text(encoding="utf-8")) or {}) if fp.exists() else {}


def _write_views(data_dir: Path, views: dict[str, dict]) -> None:
    fp = Path(data_dir) / VIEWS
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(yaml.safe_dump(views, allow_unicode=True, sort_keys=False), encoding="utf-8")


def save_view(data_dir: Path, name: str, f: Filter) -> None:
    """新建方案,或用新条件覆盖同名方案(位置不变)。"""
    views = load_views(data_dir)
    views[name] = asdict(f)
    _write_views(data_dir, views)


def rename_view(data_dir: Path, old: str, new: str) -> None:
    """方案改名,在列表里的位置不变;新名字为空或已被别的方案用了 → ValueError。"""
    views = load_views(data_dir)
    new = new.strip()
    if old not in views:
        raise ValueError(f"没有名为「{old}」的方案")
    if not new:
        raise ValueError("新名字不能为空")
    if new != old and new in views:
        raise ValueError(f"已经有名为「{new}」的方案")
    _write_views(data_dir, {(new if k == old else k): v for k, v in views.items()})


def delete_view(data_dir: Path, name: str) -> None:
    views = load_views(data_dir)
    if views.pop(name, None) is not None:
        _write_views(data_dir, views)


def same_filter(a: Filter, b: Filter) -> bool:
    """两组条件是否相同(多选项不计先后)。"""
    norm = lambda f: {k: sorted(v) if isinstance(v, list) else v for k, v in asdict(f).items()}  # noqa: E731
    return norm(a) == norm(b)


# ---------- 处理状态 ----------

def _write_triage(store: Store, tri: dict) -> None:
    body = yaml.safe_dump(tri, allow_unicode=True, sort_keys=True) if tri else ""
    (store.root / "triage.yaml").write_text(TRIAGE_TEMPLATE + ("\n" + body if body else ""), encoding="utf-8")


def set_status(store: Store, ids: list[str], status: str, note: str = "", by: str | None = None, at: str | None = None,
               conflict: str = "") -> None:
    """把线索标为 done / ignore / new(new 即撤销标记),写 watch/triage.yaml,记进操作记录(by 为操作人,缺省本机的;
    conflict:表格拉回时覆盖掉的本机值,记在操作记录里)。"""
    tri = store.triage()
    for sid in ids:
        key = sid[:8]
        tri.pop(sid, None)
        if status == "new":
            tri.pop(key, None)
        else:
            tri[key] = {"status": status, **({"note": note} if note else {})}
    _write_triage(store, tri)
    for sid in ids:
        store.log_event(sid, "status", status, note, by=by, at=at, conflict=conflict)


def set_triage_note(store: Store, sid: str, note: str) -> bool:
    """只改已处理 / 已忽略线索的处理备注(不改状态、不记操作记录;表格里只改了备注时用)→ 是否改了。"""
    tri = store.triage()
    key = next((k for k in (sid, sid[:8]) if isinstance(tri.get(k), dict)), None)
    if key is None:
        return False
    entry = {k: v for k, v in tri.pop(key).items() if k != "note"}
    tri[sid[:8]] = {**entry, **({"note": note} if note else {})}
    _write_triage(store, tri)
    return True
