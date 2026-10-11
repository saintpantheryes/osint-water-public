"""把监测结果写成系统内提醒页、运行摘要与周报文件。"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from core import opportunity, storage
from core.ledger import MILESTONES, derive, load_ledger, load_rules
from core.profile import Profile
from core.report import to_docx
from core.watch.llm import Classifier
from core.watch.outputs import (
    PROCUREMENT, category, deadline_rows, relevant, render_digest, render_global, render_inbox, render_overview, signal_tables,
)
from core.watch.run import DEFAULTS, GLOBAL, CountryRun, global_paths, load_user_sources, load_watch
from core.watch.store import Store


SUMMARY_JSON = "summary.json"  # 数据目录 watch/ 下:本次运行的数字摘要(表格「每日摘要」用;自动入账补上条数)
DUE_DAYS = 14  # 每日摘要里"14 天内截止"的天数


def _cfg(profile: Profile) -> dict:
    return {**DEFAULTS, **(profile.watch or {})}


def read_summary(root: Path) -> dict:
    """watch/summary.json → {date, at, new, new_relevant, due_14d, autoledger?, source_errors, notes};没有或读不出时空。"""
    fp = Path(root) / "watch" / SUMMARY_JSON
    try:
        d = json.loads(fp.read_text(encoding="utf-8")) if fp.exists() else {}
    except (OSError, json.JSONDecodeError):
        return {}
    return d if isinstance(d, dict) else {}


def update_summary(root: Path, **kv) -> None:
    """往 watch/summary.json 补几项(自动入账条数等);没有摘要时不写。"""
    d = read_summary(root)
    if d:
        (Path(root) / "watch" / SUMMARY_JSON).write_text(json.dumps({**d, **kv}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def _untriaged(sigs: list[dict], triage: dict) -> list[dict]:
    done = {str(k) for k, v in triage.items() if (v or {}).get("status") in ("done", "ignore")}
    return [s for s in sigs if s["id"] not in done and s["id"][:8] not in done]


def llm_notes(clf: Classifier | None) -> list[str]:
    if clf is None:
        return ["本次未启用模型(--no-llm),只用规则初筛"]
    notes = []
    if clf.fatal:  # 余额不足、密钥无效:放在最前面
        notes.append(f"⚠ {clf.fatal}")
    if clf.calls:
        u = clf.usage
        notes.append(f"模型 {clf.label}:调用 {clf.calls} 次,输入 {u['input']:,} tokens(另缓存命中 {u['cache_read']:,}),"
                     f"输出 {u['output']:,} tokens")
    if clf.note:
        notes.append(clf.note)
    other = [e for e in dict.fromkeys(clf.errors) if e != clf.fatal]
    if other:
        notes.append("模型调用出错:" + ";".join(other[:3]))
    return notes


def _country_ctx(profile: Profile, c, today: date):
    rules = load_rules(profile.rules) if profile.rules else load_rules()
    led = load_ledger(c.ledger, rules) if c.ledger else None
    ds = derive(led, rules, profile.bidder, profile.lead_times, today) if led else []
    srcs = load_watch(c.watch)["sources"] if c.watch else []
    return led, ds, [s["id"] for s in srcs], {s["id"]: s.get("name") or s["id"] for s in srcs}


def _global_sources(profile: Profile) -> list[dict]:
    out = [s for fp in global_paths(profile) for s in load_watch(fp)["sources"]]
    return out + load_user_sources(storage.DATA_DIR)["sources"]


def _latest(root: Path, kind: str, pattern: str) -> str:
    """reports/{kind}/ 下按文件名(含日期或周次)排最后的一份,链到提醒总览(月报的公开版不算,总览链内部版)。"""
    d = root / "reports" / kind
    fps = sorted(fp for fp in d.glob(pattern) if not fp.stem.endswith("_公开版")) if d.exists() else []
    return f"[{fps[-1].stem}](reports/{kind}/{fps[-1].name})" if fps else "—"


def write_outputs(profile: Profile, runs: list[CountryRun], clf: Classifier | None, today: date) -> Path:
    cfg = _cfg(profile)
    store = Store(storage.DATA_DIR)
    root = storage.DATA_DIR
    triage = store.triage()
    reviewed = set(store.ledger_reviewed())
    notes = llm_notes(clf)
    recent = store.load_signals(since=today - timedelta(days=cfg["inbox_days"]))
    overview, summary = [], [f"# 监测运行摘要 {storage.now_iso()}\n", *[f"> {n}" for n in notes], ""]
    stats = {"date": today.isoformat(), "at": storage.now_iso(), "new": 0, "new_relevant": 0, "due_14d": 0, "source_errors": [],
             "notes": notes}
    (root / "inbox").mkdir(parents=True, exist_ok=True)
    for c in profile.countries:
        if not (c.watch or c.ledger):
            continue
        led, ds, ids, names = _country_ctx(profile, c, today)
        label = c.name_zh or c.name_en
        sigs = _untriaged([s for s in recent if s["country"] == c.iso3], triage)
        md = render_inbox(label, sigs, led, ds, store.state, ids, names, today, cfg, notes, reviewed)
        (root / "inbox" / f"{c.iso3}.md").write_text(md, encoding="utf-8")
        rel = [s for s in sigs if relevant(s)]
        bad = sum(1 for sid in ids if store.state["sources"].get(sid, {}).get("status") in ("error", "blocked"))
        stats["due_14d"] += len(deadline_rows(led, ds, rel, today, DUE_DAYS))
        overview.append([label, len(rel), sum(1 for s in rel if category(s) in PROCUREMENT),
                         len(deadline_rows(led, ds, rel, today, cfg["deadline_days"])), bad, f"[inbox/{c.iso3}.md](inbox/{c.iso3}.md)",
                         _latest(root, "weekly", f"{c.iso3}_周报_*.md"), _latest(root, "monthly", f"{c.iso3}_项目机会分析_*.md")])
    focus = {c.iso3 for c in profile.countries}
    gsrc = _global_sources(profile)
    gids, gnames = [x["id"] for x in gsrc], {x["id"]: x.get("name") or x["id"] for x in gsrc}
    if gsrc:
        gsigs = _untriaged([s for s in recent if s.get("country") not in focus], triage)
        (root / "inbox" / "GLOBAL.md").write_text(render_global(gsigs, store.state, gids, gnames, today, cfg, notes), encoding="utf-8")
        rel = [s for s in gsigs if relevant(s)]
        bad = sum(1 for sid in gids if store.state["sources"].get(sid, {}).get("status") in ("error", "blocked"))
        stats["due_14d"] += len(deadline_rows(None, [], rel, today, DUE_DAYS))
        overview.append(["全球(其他国别)", len(rel), sum(1 for s in rel if category(s) in PROCUREMENT),
                         len(deadline_rows(None, [], rel, today, cfg["deadline_days"])), bad, "[inbox/GLOBAL.md](inbox/GLOBAL.md)", "—", "—"])
    all_names = {**gnames}
    for c in profile.countries:
        if c.watch:
            all_names.update(_country_ctx(profile, c, today)[3])
    for r in runs:
        label = "全球信源" if r.iso3 == GLOBAL else next((c.name_zh or c.name_en for c in profile.countries if c.iso3 == r.iso3), r.iso3)
        new_rel = [s for s in r.signals if relevant(s)]
        summary += [f"## {label}:本次新增线索 {len(r.signals)} 条,判定相关 {len(new_rel)} 条"
                    + (f";补判旧线索 {r.reclassified} 条" if r.reclassified else "") + "\n",
                    *signal_tables(new_rel[:40], today, with_country=r.iso3 == GLOBAL)]
        errs = [f"{all_names.get(k, k)}:{v.get('status')} {v.get('error') or ''}".strip() for k, v in r.sources.items()
                if v.get("status") in ("error", "blocked")]
        if errs:
            summary += ["**信源异常**\n", *[f"- {e}" for e in errs], ""]
        stats["new"] += len(r.signals)
        stats["new_relevant"] += len(new_rel)
        stats["source_errors"] += errs
    (root / "README.md").write_text(render_overview(overview, storage.now_iso(), notes), encoding="utf-8")
    fp = root / "watch" / "summary.md"
    fp.write_text("\n".join(summary) + "\n", encoding="utf-8")
    (root / "watch" / SUMMARY_JSON).write_text(json.dumps(stats, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return fp


def write_digest(profile: Profile, iso3: str, end: date, docx: bool = True) -> list[Path]:
    """周报:end 往前 7 天(含 end)首次发现的线索。"""
    cfg = _cfg(profile)
    c = profile.country(iso3)
    store = Store(storage.DATA_DIR)
    start = end - timedelta(days=6)
    ignored = {str(k) for k, v in store.triage().items() if (v or {}).get("status") == "ignore"}  # 忽略的(无关或重复)不进周报
    sigs = [s for s in store.load_signals(since=start) if s["country"] == c.iso3 and s["first_seen"][:10] <= end.isoformat()
            and s["id"] not in ignored and s["id"][:8] not in ignored]
    led, ds, ids, names = _country_ctx(profile, c, end)
    nodes = {d.item["id"]: f"{MILESTONES.get(d.timing.node.type, d.timing.node.type)} {d.timing.node.when.raw}" for d in ds if d.timing.node}
    progress = opportunity.progress_md(opportunity.book_for(profile, store), store.events(), c.iso3, start.isoformat(), end.isoformat(), nodes)
    md = render_digest(c.name_zh or c.name_en, start, end, sigs, led, ds, store.state, ids, names, cfg, [], set(store.ledger_reviewed()),
                       progress)
    y, w, _ = end.isocalendar()
    out_dir = storage.DATA_DIR / "reports" / "weekly"
    out_dir.mkdir(parents=True, exist_ok=True)
    fp = out_dir / f"{c.iso3}_周报_{y}-W{w:02d}.md"
    fp.write_text(md, encoding="utf-8")
    out = [fp]
    if docx:
        out.append(to_docx(fp, fp.with_suffix(".docx")))
    return out
