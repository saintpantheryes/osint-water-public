"""线索的中文名称(日报与「线索分析」清单用;页面里 portal.js 的 zhName 是同一规则的 JavaScript 版,词表共用 portal/zh_terms.json)。

线索原题是信源的原文(多为英文),只有模型判过的线索才有中文摘要与项目名;服务器没配模型键时,规则判定的线索两者都空。
取名顺序:模型给的项目名 → 中文摘要首行 → 用词表从原题挑出认得的词拼成「中文要点」(如「污水处理厂、招标公告、世界银行」)→ 都没有时留空,页面只显示原题。
要点不是翻译:只说明这条线索讲的是什么,原题照样给出。
"""
from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

from core.ledger import ROOT

TERMS_PATH = ROOT / "portal" / "zh_terms.json"
LEDGER_PREFIX = re.compile(r"^台账出处\s*[(（][^)）]*[)）]\s*[::]\s*")  # core.ledger_leads 补成的线索的题头
_ACRONYM = re.compile(r"^[A-Z&0-9][A-Z&0-9-]{1,5}$")
_LETTER_BEFORE, _LETTER_AFTER = r"(?<![^\W\d_])", r"(?![^\W\d_])"  # 前/后不是字母(各语种通用)


def fold(s: str) -> str:
    """不分大小写、不分重音、弯引号统一(与 core.watch.rules._fold 同一思路;词与原题同样折叠)。"""
    s = unicodedata.normalize("NFD", s).replace("’", "'").replace("‘", "'").replace("ʼ", "'")
    return "".join(c for c in s if not unicodedata.combining(c))


@lru_cache(maxsize=2)
def load_terms(path: str | Path = TERMS_PATH) -> tuple[tuple[tuple[re.Pattern, str, bool], ...], int]:
    """→ ((正则, 中文, 区分大小写), …) 长词在前;缩写(全大写 ≤6 位)按整词、区分大小写;≤4 个字母的词按整词;其余从词首起配。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    out = []
    for t, zh in sorted(raw["terms"].items(), key=lambda kv: -len(kv[0])):
        if _ACRONYM.match(t):
            out.append((re.compile(_LETTER_BEFORE + re.escape(t) + _LETTER_AFTER), zh, True))
        else:
            tail = _LETTER_AFTER if len(t) <= 4 else ""
            out.append((re.compile(_LETTER_BEFORE + re.escape(fold(t)) + tail, re.IGNORECASE), zh, False))
    return tuple(out), int(raw.get("max") or 6)


def key_points(title: str, path: str | Path = TERMS_PATH) -> str:
    """原题 → 「中文要点」:按出现顺序列出认得的词(去重,最多 max 个),一个都不认得时为空串。"""
    s = LEDGER_PREFIX.sub("", str(title or "").strip().splitlines()[0] if str(title or "").strip() else "")
    folded = fold(s)
    terms, mx = load_terms(path)
    covered: list[tuple[int, int]] = []
    hits: list[tuple[int, str]] = []
    for pat, zh, cased in terms:
        for m in pat.finditer(s if cased else folded):
            if any(a < m.end() and m.start() < b for a, b in covered):
                continue
            covered.append((m.start(), m.end()))
            hits.append((m.start(), zh))
    out: list[str] = []
    for _, zh in sorted(hits):
        if zh not in out:
            out.append(zh)
    return "、".join(out[:mx])


def zh_name(title: str | None, summary_zh: str | None = None, project_name: str | None = None, n: int = 60, title_zh: str | None = None) -> tuple[str, str]:
    """→ (中文名称, 来源):来源是「译名」(原题的中文直译,2026-10-28 起清单一律先用它)「项目名」「摘要」「要点」之一,都没有时 ("", "")。"""
    first = lambda s: (str(s or "").strip().splitlines() or [""])[0][:n]
    if first(title_zh):
        return first(title_zh), "译名"
    if first(project_name):
        return first(project_name), "项目名"
    if first(summary_zh):
        return first(summary_zh), "摘要"
    kp = key_points(title or "")
    return (kp[:n], "要点") if kp else ("", "")


# 报告里第一次出现时补中文全称的银行与出资机构缩写(全称取词表 portal/zh_terms.json,新增机构在词表里加一行、这里加缩写)
FUNDERS = ("IBRD", "IDA", "IFC", "MIGA", "ADB", "AIIB", "AfDB", "EBRD", "EIB", "IsDB", "IDB", "EDB", "NDB", "CEB", "BOAD", "KfW", "AFD", "JICA",
           "JBIC", "OFID", "GCF", "MCC", "USAID", "UNDP", "UNICEF")
_URLISH = re.compile(r"\]\([^)]*\)|<https?://[^>]*>|https?://\S+")  # 链接地址与网址里的字母不动


def expand_acronyms(text: str, path: str | Path = TERMS_PATH) -> str:
    """每个缩写在全文第一次出现时补上中文全称:IDA 与 IBRD 混合 → IDA(国际开发协会)与 IBRD(国际复兴开发银行)混合。
    已经带了括注或紧挨着中文全称的不再补;网址、链接地址里的不动。"""
    names = json.loads(Path(path).read_text(encoding="utf-8"))["terms"]
    todo = {a: names[a] for a in FUNDERS if a in names}
    if not todo:
        return text
    pat = re.compile(_LETTER_BEFORE + "(" + "|".join(sorted(map(re.escape, todo), key=len, reverse=True)) + ")" + _LETTER_AFTER)
    seen: set[str] = set()

    def sub(m: re.Match) -> str:
        a, s, e = m.group(1), m.string, m.end()
        if a in seen:
            return a
        seen.add(a)
        zh = todo[a]
        if s[e:e + 1] in ("(", "\uff08") or s[max(0, m.start() - len(zh) - 1):m.start()].rstrip(" (\uff08").endswith(zh):
            return a
        return f"{a}({zh})"

    out, last = [], 0
    for u in _URLISH.finditer(text):
        out.append(pat.sub(sub, text[last:u.start()]))
        out.append(u.group(0))
        last = u.end()
    out.append(pat.sub(sub, text[last:]))
    return "".join(out)
