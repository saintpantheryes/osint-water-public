"""A37 导入法规与条文、A39 摘录国别定义、A40 摘录排放限值:按私有配置仓的数据文件建进「法规」「法规条文」「国别定义」「排放限值」四张表。
只新建、只填空(R2),去重按编号;国别定义、排放限值新建时核实状态记「待核实」(人对过原文改「已核实」,A41)。

数据文件(私有配置仓 data/):
  laws.csv               一部法规一行:key, iso3, name, name_orig, kind, number, issued, status, version, language, url, source, summary
  law_clauses.csv.gz     一条一行:key, law_key, seq, label, heading, text, text_zh, page(scripts/fetch_laws.py 取原文、识别、拆条写出)
  local_terms.csv        key, iso3, concept, term_orig, term_zh, definition, definition_orig, relation, difference, law_key, clause_key
  discharge_limits.csv   key, iso3, law_key, clause_key, param, param_orig, value, value_text, unit, basis, receiving, scope, sampling
后两份是读条文摘出来的(人或助手读原文整理),数字照原文。

后台每个周期另有 translate:给没有中文的条文请模型译一批(配了模型时,按字数分批)。
命令行:python -m core.jobs.laws [--dir 数据目录] [--dry-run]。库里还没建表时跳过。"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from core import tstore
from core.jobs.awards import read_rows  # 同样的 csv(.gz) 读写

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "private" / "data"
LAW_COLS = ["key", "iso3", "name", "name_orig", "kind", "number", "issued", "status", "version", "language", "url", "source", "summary"]
CLAUSE_COLS = ["key", "law_key", "seq", "label", "heading", "text", "text_zh", "page"]
TERM_COLS = ["key", "iso3", "concept", "term_orig", "term_zh", "definition", "definition_orig", "relation", "difference", "law_key", "clause_key"]
LIMIT_COLS = ["key", "iso3", "law_key", "clause_key", "param", "param_orig", "value", "value_text", "unit", "basis", "receiving", "scope", "sampling"]
MAX_TEXT = 60_000  # 一条条文的长度上限(附表偶尔很长;超出的截断并注明)
ENUMS = {"law": ("kind", "status"), "local_term": ("relation",), "discharge_limit": ("param", "basis", "receiving")}

__all__ = ["LAW_COLS", "CLAUSE_COLS", "TERM_COLS", "LIMIT_COLS", "run", "translate"]


def _s(x) -> str:
    return str(x if x is not None else "").strip()


def _num(x) -> float | None:
    try:
        return float(str(x).replace(",", ".")) if _s(x) else None
    except ValueError:
        return None


def _keys(b: tstore.Base, oid: str) -> dict[str, dict]:
    return {_s(b.get(oid, r, "key")): r for r in b.rows(oid, props=["key"]) if b.get(oid, r, "key")}


def _upsert(b: tstore.Base, oid: str, rows: list[tuple[str, dict]], dry_run: bool, on_create: dict | None = None) -> tuple[int, int, dict[str, str]]:
    """[(编号, 值)] → 只新建、只填空;→ (新建, 补空, 编号 → 记录编号)。选项里没有的值不写(选项来自本体,先跑建表命令)。"""
    props = sorted({p for _, v in rows for p in v if b.has(oid, p)} | {"key"})
    have = {_s(b.get(oid, r, "key")): r for r in b.rows(oid, props=props) if b.get(oid, r, "key")}
    allowed = {p: {c["name"] for c in (b.field(oid, p).get("options") or {}).get("choices") or []} for p in ENUMS.get(oid, ()) if b.has(oid, p)}
    new_keys, new, ups = [], [], []
    for key, want in rows:
        want = {p: x for p, x in want.items() if x not in (None, "") and b.has(oid, p)}
        for p, ok in allowed.items():
            if p in want and want[p] not in ok:
                want.pop(p)
        rec = have.get(key)
        if rec is None:
            new_keys.append(key)
            new.append({"key": key, **{p: x for p, x in (on_create or {}).items() if b.has(oid, p)}, **want})
            continue
        changes, _ = tstore.fill_empty({p: b.get(oid, rec, p) for p in want}, want)
        if changes:
            ups.append((rec["id"], changes))
    ids = {k: r["id"] for k, r in have.items()}
    if not dry_run:
        for i in range(0, len(new), 200):
            made = b.create(oid, new[i:i + 200])
            ids.update({k: m["id"] for k, m in zip(new_keys[i:i + 200], made)})
        for i in range(0, len(ups), 200):
            b.update_many(oid, ups[i:i + 200])
    return len(new), len(ups), ids


def run(b: tstore.Base, data: Path | None = None, dry_run: bool = False, log=print) -> dict:
    """A37 + A39 + A40。→ {表: {新建, 补空}, 跳过: …}"""
    data = data or DATA
    if "law" in b.missing or "law" not in b.t:
        return {"跳过": "库里还没有「法规」表(先跑建表命令)"}
    if not (data / "laws.csv").exists():
        return {"跳过": f"没有数据文件 {data / 'laws.csv'}"}
    countries = {_s(b.get("country", r, "key")): r["id"] for r in b.rows("country", props=["key"]) if b.get("country", r, "key")}
    out: dict = {}
    # 法规
    laws, law_iso, skipped = [], {}, 0
    for r in read_rows(data / "laws.csv"):
        key, crid = _s(r.get("key")), countries.get(_s(r.get("iso3")))
        if not key or not crid:
            skipped += bool(key)
            continue
        law_iso[key] = crid
        laws.append((key, {"name": _s(r.get("name")), "name_orig": _s(r.get("name_orig")), "kind": _s(r.get("kind")), "number": _s(r.get("number")),
                           "issued": _s(r.get("issued")) or None, "status": _s(r.get("status")), "version": _s(r.get("version")), "language": _s(r.get("language")),
                           "country": crid, "url": _s(r.get("url")), "source": _s(r.get("source")), "summary": _s(r.get("summary"))}))
    n, u, law_ids = _upsert(b, "law", laws, dry_run)
    out["法规"] = {"新建": n, "补空": u, "国别表里没有": skipped}
    # 条文
    clause_ids: dict[str, str] = {}
    cpath = next((p for p in (data / "law_clauses.csv.gz", data / "law_clauses.csv") if p.exists()), None)
    if cpath and "law_clause" in b.t:
        rows, orphan = [], 0
        for r in read_rows(cpath):
            key, lk = _s(r.get("key")), _s(r.get("law_key"))
            if not key or lk not in law_ids:
                orphan += bool(key)
                continue
            text = _s(r.get("text"))
            if len(text) > MAX_TEXT:
                text = text[:MAX_TEXT] + "\n……(太长,截断;全文见原文链接)"
            rows.append((key, {"label": _s(r.get("label")), "seq": _num(r.get("seq")), "heading": _s(r.get("heading")), "text": text,
                               "text_zh": _s(r.get("text_zh")), "page": _s(r.get("page")), "law": law_ids[lk], "country": law_iso[lk]}))
        n, u, clause_ids = _upsert(b, "law_clause", rows, dry_run)
        out["条文"] = {"新建": n, "补空": u, "法规对不上": orphan}
    elif "law_clause" in b.t:  # 这次没带条文文件:摘录照样挂到库里已有的条文上
        clause_ids = {k: r["id"] for k, r in _keys(b, "law_clause").items()}
    # 国别定义、排放限值(新建时核实状态「待核实」)
    for oid, fname, label, build in (("local_term", "local_terms.csv", "国别定义", _term_row), ("discharge_limit", "discharge_limits.csv", "排放限值", _limit_row)):
        path = data / fname
        if oid not in b.t or not path.exists():
            continue
        rows, bad = [], 0
        for r in read_rows(path):
            key, crid = _s(r.get("key")), countries.get(_s(r.get("iso3")))
            if not key or not crid:
                bad += bool(key)
                continue
            v = build(r)
            v.update({"country": crid, "law": law_ids.get(_s(r.get("law_key"))), "clause": clause_ids.get(_s(r.get("clause_key")))})
            rows.append((key, v))
        n, u, _ = _upsert(b, oid, rows, dry_run, on_create={"verified": "待核实"})
        out[label] = {"新建": n, "补空": u, "国别表里没有": bad}
    if "local_term" in b.t and (data / "local_terms.csv").exists() and "term" in b.t:
        known = {_s(b.get("term", r, "term")) for r in b.rows("term", props=["term"])}
        miss = sorted({_s(r.get("concept")) for r in read_rows(data / "local_terms.csv")} - known - {""})
        if miss:
            out["行业术语表里还没有的通用概念"] = miss
    log(f"  laws:{out}" + ("(只算,没写)" if dry_run else ""))
    return out


def _term_row(r: dict) -> dict:
    return {p: _s(r.get(p)) for p in ("concept", "term_orig", "term_zh", "definition", "definition_orig", "relation", "difference")}


def _limit_row(r: dict) -> dict:
    v = {p: _s(r.get(p)) for p in ("param", "param_orig", "value_text", "unit", "basis", "receiving", "scope", "sampling")}
    v["value"] = _num(r.get("value"))
    return v


ZH_SYSTEM = ("你是法律翻译,熟悉水务、环境、公私合作与政府采购法规。把每条法规条文完整译成准确、规范的中文法律文字:不删减、不概括、不解释,"
             "条号、数字、单位、文号照原文;机构用通用中文译名;原文是表格的按行译,一行一条。")
ZH_GUIDE = '\n输出格式:只输出一个 JSON 对象 {"items": [{"i": 序号, "zh": "中文译文"}, …]},不要任何其他文字。\n'
ZH_SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": {
    "type": "object", "properties": {"i": {"type": "integer"}, "zh": {"type": "string"}}, "required": ["i", "zh"], "additionalProperties": False}}},
    "required": ["items"], "additionalProperties": False}
TRANSLATE_CHARS = 5_000  # 一批原文的字数上限:译文约三四千 token,在不思考模式 8000 的输出上限之内
TRANSLATE_TAKE = 40


def translate(b: tstore.Base, clf, max_chars: int = TRANSLATE_CHARS, log=print) -> int:
    """没有中文的条文请模型译一批(按顺序,先排在前面的法规);只填空。没配模型、表没建时什么也不做。→ 写了几条"""
    oid = "law_clause"
    if clf is None or not getattr(clf, "enabled", False) or not hasattr(clf, "ask") or oid not in b.t or not b.has(oid, "text_zh"):
        return 0
    it = b.api.iter_records(b.t[oid], filter=b.filter(b.cond(oid, "text_zh", "isEmpty"), b.cond(oid, "text", "isNotEmpty")),
                            projection=[b.fid(oid, "text"), b.fid(oid, "label")], take=TRANSLATE_TAKE)
    rows, total = [], 0
    for _, r in zip(range(TRANSLATE_TAKE), it):
        t = _s(b.get(oid, r, "text"))
        if rows and total + len(t) > max_chars:
            break
        rows.append((r, t[:max_chars]))
        total += len(t)
    if not rows:
        return 0
    out = clf.ask(ZH_SYSTEM, json.dumps([{"i": i, "t": t} for i, (_, t) in enumerate(rows)], ensure_ascii=False), ZH_SCHEMA, ZH_GUIDE)
    if not out or out.get("_error"):
        log(f"  ! 法规条文没译成:{(out or {}).get('_error') or '模型未返回'}")
        return 0
    ups = []
    for x in out.get("items") or []:
        try:
            i, zh = int(x.get("i")), _s(x.get("zh"))
        except (TypeError, ValueError, AttributeError):
            continue
        if 0 <= i < len(rows) and zh:
            ups.append((rows[i][0]["id"], {"text_zh": zh + ("\n(原文较长,只译了前面部分)" if len(_s(b.get(oid, rows[i][0], "text"))) > max_chars else "")}))
    b.update_many(oid, ups)
    return len(ups)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m core.jobs.laws", description="A37 导入法规与条文、A39/A40 国别定义与排放限值(只新建、只填空)")
    ap.add_argument("--dir", type=Path, default=DATA, help=f"数据目录(缺省 {DATA})")
    ap.add_argument("--dry-run", action="store_true", help="只算要建、要补多少,不写")
    a = ap.parse_args(argv)
    b = tstore.Base(tstore.Client(*tstore.credentials(admin=not os.environ.get("TEABLE_TOKEN"))))
    print(run(b, a.dir, a.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
