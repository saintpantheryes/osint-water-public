"""A31 导入研究资料、A33 导入水务事件:按私有配置仓的 data/studies.csv.gz、data/events.csv.gz 建进「研究资料」「水务事件」两张表;
只新建、只填空(R2),去重按编号。数据由 scripts/fetch_wb_studies.py 从世界银行文件库与项目库取(项目评估、完成报告、环境社会评估、行业研究,
项目的批准与关闭);综述与摘自原文的事件(机构改革、规划、水价、工程投运)由人或助手读原文整理后放进同样的列。
事件的城市:city_en 列写了英文城市名的,对上该国规模以上城市(英文名相同,不分大小写)才挂。
后台每个周期另有 translate:给没有中文标题的研究资料请模型译一批(配了模型时)。
命令行:python -m core.jobs.studies [--studies 路径] [--events 路径] [--dry-run]。库里还没建表时跳过。"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from core import tstore
from core.jobs.awards import read_rows, write_rows  # 同样的 csv(.gz) 读写

ROOT = Path(__file__).resolve().parents[2]
STUDIES = ROOT / "private" / "data" / "studies.csv.gz"
EVENTS = ROOT / "private" / "data" / "events.csv.gz"
STUDY_COLS = ["key", "iso3", "name", "name_zh", "kind", "publisher", "published", "language", "project", "url", "summary"]
EVENT_COLS = ["key", "iso3", "day", "kind", "title", "city_en", "source", "url"]
PDATE_RAW = "日期原文"  # pdate 的第二个字段(core/ontology.PDATE_SUFFIX)

__all__ = ["STUDY_COLS", "EVENT_COLS", "read_rows", "write_rows", "run", "run_events", "pdate_start", "translate"]


def pdate_start(raw: str | None) -> str | None:
    """「2023」「2023-07」「2023-07-18」→ 区间起点 2023-01-01 / 2023-07-01 / 2023-07-18;认不出返回 None。"""
    m = re.fullmatch(r"\s*(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?\s*", str(raw or ""))
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2) or 1), int(m.group(3) or 1)
    return f"{y:04d}-{mo:02d}-{d:02d}" if 1 <= mo <= 12 and 1 <= d <= 31 else None


def _countries(b: tstore.Base) -> dict[str, str]:
    return {str(b.get("country", r, "key")): r["id"] for r in b.rows("country", props=["key"]) if b.get("country", r, "key")}


def _upsert(b: tstore.Base, oid: str, rows: list[tuple[str, dict]], dry_run: bool) -> tuple[int, int]:
    """[(编号, 值)] → 只新建、只填空;→ (新建, 补空)"""
    props = sorted({p for _, v in rows for p in v} | {"key"})
    have = {str(b.get(oid, r, "key")): r for r in b.rows(oid, props=[p for p in props if b.has(oid, p)]) if b.get(oid, r, "key")}
    allowed = {p: {c["name"] for c in (b.field(oid, p).get("options") or {}).get("choices") or []} for p in ("kind",) if b.has(oid, p)}
    new, ups = [], []
    for key, want in rows:
        want = {p: x for p, x in want.items() if x not in (None, "") and b.has(oid, p)}
        for p, ok in allowed.items():  # 选项里没有的值不写(选项来自本体)
            if p in want and want[p] not in ok:
                want.pop(p)
        rec = have.get(key)
        if rec is None:
            new.append({"key": key, **want})
            continue
        changes, _ = tstore.fill_empty({p: b.get(oid, rec, p) for p in want}, want)
        if changes:
            ups.append((rec["id"], changes))
    if not dry_run:
        b.create(oid, new)
        b.update_many(oid, ups)
    return len(new), len(ups)


def run(b: tstore.Base, path: Path | None = None, dry_run: bool = False, log=print) -> dict:
    """A31:研究资料。→ {新建, 补空, 国别表里没有}"""
    oid, path = "study", path or STUDIES
    if oid not in b.t:
        return {"跳过": "库里还没有「研究资料」表(先跑建表命令)"}
    if not path.exists():
        return {"跳过": f"没有数据文件 {path}"}
    countries, rows, skipped, seen = _countries(b), [], 0, set()
    for r in read_rows(path):
        key, crid = str(r.get("key") or "").strip(), countries.get(str(r.get("iso3") or "").strip())
        if not key or key in seen:
            continue
        seen.add(key)
        if not crid:
            skipped += 1
            continue
        v = {p: (str(r.get(p) or "").strip() or None) for p in STUDY_COLS if p not in ("key", "iso3")}
        rows.append((key, {**v, "country": crid}))
    n_new, n_up = _upsert(b, oid, rows, dry_run)
    log(f"  study:新建 {n_new},补空 {n_up},国别表里没有的跳过 {skipped}" + ("(只算,没写)" if dry_run else ""))
    return {"新建": n_new, "补空": n_up, "国别表里没有": skipped}


def run_events(b: tstore.Base, path: Path | None = None, dry_run: bool = False, log=print) -> dict:
    """A33:水务事件。→ {新建, 补空, 挂上城市, 国别表里没有, 日期认不出}"""
    oid, path = "water_event", path or EVENTS
    if oid not in b.t:
        return {"跳过": "库里还没有「水务事件」表(先跑建表命令)"}
    if not path.exists():
        return {"跳过": f"没有数据文件 {path}"}
    countries = _countries(b)
    cities: dict[tuple[str, str], str] = {}
    if "city" in b.t and b.has(oid, "city"):
        for r in sorted(b.rows("city", props=["name_en", "country", "population"]), key=lambda r: float(b.get("city", r, "population") or 0)):
            crid, name = tstore.link_id(b.get("city", r, "country")), str(b.get("city", r, "name_en") or "").strip().lower()
            if crid and name:
                cities[(crid, name)] = r["id"]  # 同名的取人口最多的(排在后面的覆盖前面的)
    rows, skipped, bad, n_city, seen = [], 0, 0, 0, set()
    for r in read_rows(path):
        key, crid = str(r.get("key") or "").strip(), countries.get(str(r.get("iso3") or "").strip())
        if not key or key in seen:
            continue
        seen.add(key)
        if not crid:
            skipped += 1
            continue
        raw = str(r.get("day") or "").strip()
        start = pdate_start(raw)
        if not start:
            bad += 1
            continue
        city = cities.get((crid, str(r.get("city_en") or "").strip().lower()))
        n_city += bool(city)
        v = {"day": start, PDATE_RAW: raw, "title": r.get("title"), "kind": r.get("kind"), "country": crid, "city": city,
             "source": r.get("source"), "url": r.get("url")}
        rows.append((key, {p: (x.strip() if isinstance(x, str) else x) for p, x in v.items()}))
    n_new, n_up = _upsert(b, oid, rows, dry_run)
    log(f"  water_event:新建 {n_new},补空 {n_up},挂上城市 {n_city},国别表里没有的跳过 {skipped},日期认不出 {bad}" + ("(只算,没写)" if dry_run else ""))
    return {"新建": n_new, "补空": n_up, "挂上城市": n_city, "国别表里没有": skipped, "日期认不出": bad}


ZH_SYSTEM = "你是水务与国际发展领域的翻译。把每份文件标题译成简洁准确的中文(一般 40 字以内):国名、项目名照译,机构用通用中文译名,项目编号保留原文,不要解释。"
ZH_GUIDE = '\n输出格式:只输出一个 JSON 对象 {"items": [{"i": 序号, "zh": "中文标题"}, …]},不要任何其他文字。\n'
ZH_SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": {
    "type": "object", "properties": {"i": {"type": "integer"}, "zh": {"type": "string"}}, "required": ["i", "zh"], "additionalProperties": False}}},
    "required": ["items"], "additionalProperties": False}
TRANSLATE_BATCH = 40


def translate(b: tstore.Base, clf, limit: int = TRANSLATE_BATCH, log=print) -> int:
    """没有中文标题的研究资料请模型译一批;只填空。没配模型、表没建时什么也不做。→ 写了几条"""
    oid = "study"
    if clf is None or not getattr(clf, "enabled", False) or not hasattr(clf, "ask") or oid not in b.t or not b.has(oid, "name_zh"):
        return 0
    it = b.api.iter_records(b.t[oid], filter=b.filter(b.cond(oid, "name_zh", "isEmpty"), b.cond(oid, "name", "isNotEmpty")),
                            projection=[b.fid(oid, "name")], take=limit)
    rows = [r for _, r in zip(range(limit), it)]
    if not rows:
        return 0
    names = [str(b.get(oid, r, "name") or "")[:300] for r in rows]
    out = clf.ask(ZH_SYSTEM, json.dumps([{"i": i, "t": t} for i, t in enumerate(names)], ensure_ascii=False), ZH_SCHEMA, ZH_GUIDE)
    if not out or out.get("_error"):
        log(f"  ! 研究资料中文标题没译成:{(out or {}).get('_error') or '模型未返回'}")
        return 0
    ups = []
    for x in out.get("items") or []:
        try:
            i, zh = int(x.get("i")), str(x.get("zh") or "").strip()
        except (TypeError, ValueError, AttributeError):
            continue
        if 0 <= i < len(rows) and zh:
            ups.append((rows[i]["id"], {"name_zh": zh[:200]}))
    b.update_many(oid, ups)
    return len(ups)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m core.jobs.studies", description="A31 导入研究资料、A33 导入水务事件(只新建、只填空)")
    ap.add_argument("--studies", type=Path, default=STUDIES, help=f"研究资料数据文件(缺省 {STUDIES})")
    ap.add_argument("--events", type=Path, default=EVENTS, help=f"水务事件数据文件(缺省 {EVENTS})")
    ap.add_argument("--dry-run", action="store_true", help="只算要建、要补多少,不写")
    a = ap.parse_args(argv)
    b = tstore.Base(tstore.Client(*tstore.credentials(admin=not os.environ.get("TEABLE_TOKEN"))))
    print(run(b, a.studies, a.dry_run))
    print(run_events(b, a.events, a.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
