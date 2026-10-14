"""语义层说法(本体 phrase,v2.25):助手理解提问的口语,一个指向一行,放在 Teable「语义层说法」表里由懂行的人改。

初始说法在本体 semantic 节(指标、算法、主体工程、法规问法、排放指标);后台每个周期给表里没有的指向补一行(A43,只新建)。
助手服务每分钟读一次表(assistant.set_phrases):表里有这一行的按表里的说法认,表里没有的用本体的;指向写错(本体里没有)的那一行不用。
"""
from __future__ import annotations

import re
import threading
from functools import lru_cache

import yaml

KINDS = ("指标", "算法", "主体工程", "法规问法", "排放指标")
LAW_ASK = {"limit": "问排放标准", "define": "问定义", "text": "问法规条文"}  # 法规问法的键 → 表里的指向名

_lock = threading.Lock()
_live: dict[tuple[str, str], list[str]] | None = None  # (类别, 指向) → 说法;None = 只用本体


def _split(x) -> list[str]:
    parts = x if isinstance(x, list) else re.split(r"[\n、,，;；]+", str(x or ""))
    return list(dict.fromkeys(str(w).strip() for w in parts if str(w).strip()))


@lru_cache(maxsize=1)
def seed() -> list[dict]:
    """本体 semantic 节 → 表的行 [{kind, target, words, note, key}]:key 是本体里的英文键(指标 id、算法 id、法规问法键),主体工程与排放指标就是指向本身。
    没有说法的(「数量」)不出行:它不靠说法认。"""
    from core.assistant import ONTO_PATH
    sem = (yaml.safe_load(ONTO_PATH.read_text(encoding="utf-8")).get("semantic") or {})
    w = sem.get("words") or {}
    aggs = {a["id"]: a["name"] for a in sem.get("aggs") or []}
    out = []
    for m in sem.get("measures") or []:
        if m.get("words"):
            unit = m["unit"] if isinstance(m.get("unit"), str) else ""
            note = f"单位 {unit};" if unit else ""
            note += "算法:" + "、".join(aggs.get(a, a) for a in m.get("aggs") or []) + "(第一个是没说时的缺省)"
            if m.get("missing"):
                note += ";缺数时:" + m["missing"]
            out.append({"kind": "指标", "target": m["name"], "key": m["id"], "words": list(m["words"]), "note": note})
    for a in sem.get("aggs") or []:
        if a.get("words"):
            out.append({"kind": "算法", "target": a["name"], "key": a["id"], "words": list(a["words"]), "note": ""})
    for k, ws in (w.get("award_asset") or {}).items():
        out.append({"kind": "主体工程", "target": k, "key": k, "words": list(ws), "note": "已中标项目的主体工程"})
    for k, ws in (w.get("law") or {}).items():
        out.append({"kind": "法规问法", "target": LAW_ASK.get(k, k), "key": k, "words": list(ws),
                    "note": {"limit": "问的是排放标准:列排放限值表", "define": "问的是定义:列本国定义", "text": "别的法规问题:按条文作答"}.get(k, "")})
    for k, ws in (w.get("effluent_param") or {}).items():
        out.append({"kind": "排放指标", "target": k, "key": k, "words": list(ws), "note": "排放限值的指标"})
    return out


def set_rows(rows: list[dict] | None) -> bool:
    """换上表里读到的行(字段按本体属性 id:target、kind、words);None 回到只用本体。说法有变返回 True(调用方据此清掉词表缓存)。
    同一指向写了几行的,说法合在一起;指向在本体里没有的不用(unknown 列出它们)。"""
    global _live
    known = {(r["kind"], r["target"]) for r in seed()}
    new = None
    if rows is not None:
        new = {}
        for r in rows:
            k = (str(r.get("kind") or "").strip(), str(r.get("target") or "").strip())
            if k in known:
                new[k] = list(dict.fromkeys(new.get(k, []) + _split(r.get("words"))))
    with _lock:
        changed, _live = new != _live, new
    return changed


def unknown(rows: list[dict]) -> list[dict]:
    """表里指向写错(本体里没有这个类别 + 指向)的行。"""
    known = {(r["kind"], r["target"]) for r in seed()}
    return [r for r in rows if (str(r.get("kind") or "").strip(), str(r.get("target") or "").strip()) not in known]


def words(kind: str, key: str) -> list[str]:
    """一个指向现在认的说法:表里有这一行的用表里的,没有的用本体的。key 是本体里的英文键(见 seed)。"""
    row = next((r for r in seed() if r["kind"] == kind and r["key"] == key), None)
    if row is None:
        return []
    with _lock:
        live = _live
    if live is not None and (kind, row["target"]) in live:
        return list(live[(kind, row["target"])])
    return list(row["words"])


def fill(base, log=print) -> dict:
    """A43:本体里有、表里还没有的指向补一行(带初始说法与说明);只新建,不改已有的行。表没建时跳过。"""
    if "phrase" not in base.t:
        return {}
    have = {(str(base.get("phrase", r, "kind") or ""), str(base.get("phrase", r, "target") or "")) for r in base.rows("phrase", props=["kind", "target"])}
    todo = [r for r in seed() if (r["kind"], r["target"]) not in have]
    if not todo:
        return {}
    base.create("phrase", [{"target": r["target"], "kind": r["kind"], "words": "、".join(r["words"]), "note": r["note"]} for r in todo])
    log(f"语义层说法补了 {len(todo)} 行")
    return {"phrases_added": len(todo)}
