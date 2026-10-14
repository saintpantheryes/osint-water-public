"""法规问答(本体 v2.24:法规、法规条文、国别定义、排放限值)。

助手的提问解析(core/assistant.plan)先问这里:提问像法规问题的(排放标准、某国法律里怎么定义、法规条文怎么规定),
计划的对象是「法规」(law),带 law = {kind, params, concepts, keywords}:
  limit   排放标准:页面按国别列排放限值(同一国几种口径分开,写明出处条号,待核实的标出)
  define  本国定义:页面列国别定义(通用概念 → 本国说法、定义、和通用说法的差别)
  text    别的法规问题:页面请后台 /law 按国别与关键词查条文;配了模型的请它只按查到的条文作答、每句带出处角标
  list    只问「某国有哪些法规」:页面列这国的法规
词表全在本体 semantic.words.law / effluent_param 与行业术语(通用概念);改说法改本体或术语表,不改程序。
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from functools import lru_cache

import yaml

from core import geo
from core.tstore import link_id

KINDS = ("limit", "define", "text", "list")
KIND_ZH = {"limit": "排放限值(按口径分开列,注明出处)", "define": "本国定义(和通用说法的差别)", "text": "法规条文(按国别与关键词查)", "list": "这国收了哪些法规"}
# 明说查别的对象的:不当法规问题(「排放标准」这类说法除外,问的就是标准)
OTHER_RE = re.compile(r"线索|商机|中标|招标|投标|授标|项目机会|节点|截止|报告|日报|周报|月报|新闻|公告")
FILLER_RE = re.compile(r"水务|相关|主要|现行|有效|是多少|有多少|多少|是什么|有哪些|哪些|什么|怎么样|怎样|如何|怎么|吗|呢|吧|请问|一下|告诉我|关于|对于|里面|里|中|的|了|和|与|及|对|在|是|有|要|会|能|可以|最|国家|该国|当地|现行|目前|现在|请|帮我|查|看看|看")
SPLIT_RE = re.compile(r"[\s,，。.;；:：、?？!！()（）「」“”\"'/]+")


@dataclass
class Words:
    limit: list[str] = field(default_factory=list)
    define: list[str] = field(default_factory=list)
    text: list[str] = field(default_factory=list)
    params: dict[str, list[str]] = field(default_factory=dict)  # 指标 → 说法(小写、长的在前)


@lru_cache(maxsize=1)
def words() -> Words:
    from core.assistant import ONTO_PATH
    o = yaml.safe_load(ONTO_PATH.read_text(encoding="utf-8"))
    w = ((o.get("semantic") or {}).get("words") or {})
    srt = lambda xs: sorted({str(x).lower() for x in xs or []}, key=len, reverse=True)  # noqa: E731
    law = w.get("law") or {}
    return Words(limit=srt(law.get("limit")), define=srt(law.get("define")), text=srt(law.get("text")),
                 params={k: srt([k, *v]) for k, v in (w.get("effluent_param") or {}).items()})


_live_concepts: list[dict] | None = None  # 行业术语表里的全部术语(含只作法规概念、没写相关词的);None = 本体 terms 节
_lock = threading.Lock()


def set_concepts(rows: list[dict] | None) -> None:
    """行业术语表的行(属性 term、aliases、disabled)→ 通用概念;None 回到本体 terms 节。assistant.set_terms 一并调。"""
    global _live_concepts
    with _lock:
        _live_concepts = None if rows is None else [c for c in (_concept(r) for r in rows) if c]


def _concept(t: dict) -> dict | None:
    from core.assistant import _words
    term = str(t.get("term") or "").strip()
    if not term or t.get("disabled"):
        return None
    return {"term": term, "triggers": sorted({x.lower() for x in [term, *_words(t.get("aliases"))]}, key=len, reverse=True)}


@lru_cache(maxsize=1)
def _seed_concepts() -> list[dict]:
    from core.assistant import ONTO_PATH
    o = yaml.safe_load(ONTO_PATH.read_text(encoding="utf-8"))
    return [c for c in (_concept(t) for t in o.get("terms") or []) if c]


def concepts() -> list[dict]:
    with _lock:
        return list(_live_concepts) if _live_concepts is not None else _seed_concepts()


def _hit(text: str, w: str) -> bool:
    if w.isascii():
        return re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", text) is not None
    return w in text


def params_in(text: str) -> list[str]:
    return [k for k, ws in words().params.items() if any(_hit(text, w) for w in ws)]


def concepts_in(text: str) -> list[str]:
    out, rest = [], text
    for c in sorted(concepts(), key=lambda c: -max(len(t) for t in c["triggers"])):
        if any(_hit(rest, t) for t in c["triggers"]):
            out.append(c["term"])
            for t in c["triggers"]:
                rest = rest.replace(t, " ")
    return out


def keywords(text: str, countries: list[str]) -> list[str]:
    """提问里去掉国名、法规说法与虚词后剩下的词(两个字以上),给条文检索用;后台再请模型补原文的说法。"""
    t = text
    for iso in countries:
        c = geo.countries().get(iso) or {}
        for n in [c.get("name_zh"), c.get("name_en"), *(c.get("aliases") or [])]:
            if n:
                t = t.replace(str(n).lower(), " ")
    for w in words().text + words().define:
        t = t.replace(w, " ")
    t = FILLER_RE.sub(" ", t)
    return list(dict.fromkeys(x for x in SPLIT_RE.split(t) if len(x) >= 2 and not x.isdigit()))[:8]


def kind_of(text: str) -> str | None:
    """提问 → 法规问题的种类;不像法规问题 → None。"""
    w = words()
    if any(_hit(text, x) for x in w.limit):
        return "limit"
    other = OTHER_RE.search(text)
    if params_in(text) and re.search(r"限值|标准|不超过|上限|多少|要求", text) and not other:
        return "limit"
    if other:
        return None
    if any(x in text for x in w.define) and (geo.detect(text) or "法" in text) and concepts_in(text):
        return "define"
    if any(_hit(text, x) for x in w.text):
        return "text"
    return None


FOLLOW_WORDS_RE = re.compile(r"^\s*(?:那么|那就|那|其中|这些|同样|换成|如果是|再看|还是)|呢\s*[?？]?\s*$")


def _only_follow(text: str, isos: list[str]) -> bool:
    """「那吉尔吉斯斯坦呢」「那 COD 呢」:去掉国名、接着问的字、指标与虚词后不剩别的(剩「营商环境」之类就是换了话题)。"""
    rest = FOLLOW_WORDS_RE.sub(" ", text)
    for iso in isos:
        c = geo.countries().get(iso) or {}
        for n in [c.get("name_zh"), c.get("name_en"), *(c.get("aliases") or [])]:
            if n:
                rest = rest.replace(str(n).lower(), " ")
    for ws in words().params.values():
        for w in ws:
            rest = re.sub(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", " ", rest) if w.isascii() else rest.replace(w, " ")
    rest = FILLER_RE.sub(" ", rest)
    return not [x for x in SPLIT_RE.split(rest) if x.strip()]


def detect(q: str, prev: dict | None = None) -> dict | None:
    """一句提问(与上一问的计划)→ 法规问题的 {kind, params, concepts, keywords, countries};不是法规问题 → None。
    接着问(上一问是法规、这句没说别的,如「那吉尔吉斯斯坦呢」)沿用上一问的种类、指标与概念,换成这句的国别。"""
    from core.assistant import _follow_up, _norm
    text = _norm(q)
    isos = list(dict.fromkeys(geo.detect(q)))
    kind = kind_of(text)
    inherited = []
    if kind is None and prev and prev.get("object") == "law" and isinstance(prev.get("law"), dict) and _follow_up(q) and _only_follow(text, isos):
        pl = prev["law"]
        kind = pl.get("kind") if pl.get("kind") in KINDS else "limit"
        inherited.append("问的是什么")
        out = {"kind": kind, "params": params_in(text) or [x for x in pl.get("params") or [] if x in words().params],
               "concepts": concepts_in(text) or [str(x) for x in pl.get("concepts") or []][:5],
               "keywords": (keywords(text, isos) or [str(x)[:40] for x in pl.get("keywords") or []][:8]) if kind == "text" else [], "countries": isos, "inherited": inherited}
        if not isos:
            out["countries"] = [c["iso3"] for c in ((prev.get("filters") or {}).get("countries") or []) if isinstance(c, dict) and geo.iso3(c.get("iso3")) == c.get("iso3")]
            if out["countries"]:
                inherited.append("国别")
        return out
    if kind is None:
        return None
    kw = keywords(text, isos) if kind == "text" else []
    if kind == "text" and not kw:
        kind = "list"
    cs = concepts_in(text)
    if kind == "limit" and "排放标准" not in cs and any(c["term"] == "排放标准" for c in concepts()):
        cs = ["排放标准", *cs]
    return {"kind": kind, "params": params_in(text) if kind == "limit" else [], "concepts": cs, "keywords": kw, "countries": isos, "inherited": inherited}


def understood(p: dict) -> list[list[str]]:
    lw, f = p["law"], p["filters"]
    u = [["对象", "法规"]]
    if f["countries"]:
        u.append(["国别", "、".join(c["name"] for c in f["countries"])])
    else:
        u.append(["国别", "收了法规原文的各国"])
    u.append(["查", KIND_ZH[lw["kind"]]])
    if lw.get("params"):
        u.append(["指标", "、".join(lw["params"])])
    if lw.get("concepts") and lw["kind"] in ("define", "limit"):
        u.append(["通用概念", "、".join(lw["concepts"]) + "(按各国自己的定义)"])
    if lw.get("keywords") and lw["kind"] == "text":
        u.append(["关键词", "、".join(lw["keywords"])])
    if lw.get("inherited"):
        u.append(["沿用上一问", "、".join(lw["inherited"])])
    return u


# ---------------------------------------------------------------- 后台:按条文作答(/law)

SEARCH_SYSTEM = ("你帮人在各国水务相关法规的条文里检索。给你一个中文问题和要查的国家(带法规原文的语言),"
                 "列出检索用的词:中文 3 到 8 个(法律用语、同义说法),每种原文语言 3 到 8 个(法文、俄文、阿塞拜疆文、英文等法规原文里会用的词干,词干要短,如 concession、концесс、güzəşt)。")
SEARCH_GUIDE = '\n输出格式:只输出一个 JSON 对象 {"zh": ["…"], "orig": ["…"]},不要任何其他文字。\n'
SEARCH_SCHEMA = {"type": "object", "properties": {"zh": {"type": "array", "items": {"type": "string"}}, "orig": {"type": "array", "items": {"type": "string"}}},
                 "required": ["zh", "orig"], "additionalProperties": False}
ANSWER_SYSTEM = ("你是熟悉各国水务法规的分析员。只根据下面给的条文回答中文问题:每句话末尾用 [序号] 标出依据的条文;条文没有直接说的,明说「查到的条文没有直接规定」,"
                 "不要用常识或别国的规定补;各国分开说;数字、期限、金额照条文写,本币金额后面括注约合美元;回答 80 到 250 字。")
ANSWER_GUIDE = '\n输出格式:只输出一个 JSON 对象 {"answer": "回答(带 [序号])", "used": [用到的序号]},不要任何其他文字。\n'
ANSWER_SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}, "used": {"type": "array", "items": {"type": "integer"}}},
                 "required": ["answer", "used"], "additionalProperties": False}
TOP = 8
CACHE_SECONDS = 600


class Library:
    """后台进程里的条文缓存:按国别读法规与条文(十分钟内不重读),给 /law 检索。"""

    def __init__(self, base_factory):
        self.base_factory = base_factory  # → tstore.Base(助手服务的 Assistant.db:自己每 10 分钟重连)
        self._cache: dict[str, tuple[float, list[dict], dict[str, dict]]] = {}
        self._lock = threading.Lock()

    def db(self):
        return self.base_factory()

    def country_rows(self, iso: str) -> tuple[list[dict], dict[str, dict]]:
        """一国 → (条文 [{key, label, text, text_zh, page, seq, law}], 法规 {记录编号: {key, name, name_orig, url, language}})"""
        with self._lock:
            hit = self._cache.get(iso)
            if hit and time.time() - hit[0] < CACHE_SECONDS:
                return hit[1], hit[2]
        b = self.db()
        if "law" not in b.t or "law_clause" not in b.t:
            return [], {}
        crid = next((r["id"] for r in b.rows("country", filter=b.filter(b.cond("country", "key", "is", iso)), props=["key"])), None)
        if not crid:
            return [], {}
        laws = {}
        for r in b.rows("law", props=["key", "name", "name_orig", "url", "language", "country"]):
            if link_id(b.get("law", r, "country")) == crid:
                laws[r["id"]] = {"key": b.get("law", r, "key"), "name": b.get("law", r, "name"), "name_orig": b.get("law", r, "name_orig"),
                                 "url": b.get("law", r, "url"), "language": b.get("law", r, "language")}
        clauses = []
        if laws:
            for r in b.rows("law_clause", props=["key", "label", "text", "text_zh", "page", "seq", "law", "country"]):
                lid = link_id(b.get("law_clause", r, "law"))
                if lid in laws:
                    clauses.append({"id": r["id"], "key": b.get("law_clause", r, "key"), "label": b.get("law_clause", r, "label"),
                                    "text": b.get("law_clause", r, "text") or "", "text_zh": b.get("law_clause", r, "text_zh") or "",
                                    "page": b.get("law_clause", r, "page"), "seq": b.get("law_clause", r, "seq"), "law": lid})
        with self._lock:
            self._cache[iso] = (time.time(), clauses, laws)
        return clauses, laws


def _score(c: dict, zh: list[str], orig: list[str]) -> float:
    t, z = c["text"].lower(), c["text_zh"]
    s = sum(2.0 for w in zh if w and w in z) + sum(1.5 for w in orig if len(w) >= 3 and w.lower() in t)
    return s / (1 + len(t) / 6000)  # 很长的附表别总排前面


def answer(lib: Library, q: str, law_plan: dict, countries: list[str], clf=None, log=print) -> dict:
    """按条文作答 → {ok, answer, cites:[{n, key, law, law_key, label, page, text, text_zh, url}], searched:{国名: 条数}, missing:[{name, iso3}], note}"""
    kw = [str(x) for x in law_plan.get("keywords") or []][:8]
    zh, orig, note = list(kw), [], ""
    if clf is not None and getattr(clf, "enabled", False):
        langs = {}
        for iso in countries:
            _, laws = lib.country_rows(iso)
            langs[geo.name_zh(iso)] = sorted({str(x["language"] or "") for x in laws.values() if x.get("language")})
        out = clf.ask(SEARCH_SYSTEM, json.dumps({"问题": q, "国家与原文语言": langs}, ensure_ascii=False), SEARCH_SCHEMA, SEARCH_GUIDE)
        if out and not out.get("_error"):
            zh = list(dict.fromkeys(zh + [str(x).strip() for x in out.get("zh") or [] if str(x).strip()]))[:12]
            orig = [str(x).strip() for x in out.get("orig") or [] if str(x).strip()][:16]
    pool, searched, missing = [], {}, []
    for iso in countries:
        clauses, laws = lib.country_rows(iso)
        if not clauses:
            missing.append({"name": geo.name_zh(iso), "iso3": iso})
            continue
        searched[geo.name_zh(iso)] = len(clauses)
        for c in clauses:
            s = _score(c, zh, orig)
            if s > 0:
                pool.append((s, iso, c, laws[c["law"]]))
    pool.sort(key=lambda x: -x[0])
    top, per = [], {}
    for s, iso, c, lw in pool:  # 每国最多 TOP 条,合计不超过 TOP + 4
        if per.get(iso, 0) >= TOP or len(top) >= TOP + 4:
            continue
        per[iso] = per.get(iso, 0) + 1
        top.append((iso, c, lw))
    cites = [{"n": i + 1, "country": geo.name_zh(iso), "key": c["key"], "seq": c["seq"], "law": lw["name"], "law_key": lw["key"], "label": c["label"], "page": c["page"],
              "text": c["text"][:4000], "text_zh": c["text_zh"][:4000], "url": lw.get("url")} for i, (iso, c, lw) in enumerate(top)]
    res = {"ok": True, "answer": "", "cites": cites, "searched": searched, "missing": missing, "keywords": zh + orig, "note": note}
    if not cites:
        return res
    if clf is None or not getattr(clf, "enabled", False):
        res["note"] = "没配模型:列出按关键词查到的条文,没有概括"
        return res
    ctx = [{"序号": x["n"], "国家": x["country"], "法规": x["law"], "条号": x["label"], "条文": (x["text_zh"] or x["text"])[:2500]} for x in cites]
    out = clf.ask(ANSWER_SYSTEM, json.dumps({"问题": q, "条文": ctx}, ensure_ascii=False), ANSWER_SCHEMA, ANSWER_GUIDE)
    if not out or out.get("_error"):
        res["note"] = "模型没回答成,列出查到的条文" + (f"({out['_error']})" if out and out.get("_error") else "")
        return res
    res["answer"] = str(out.get("answer") or "").strip()[:2000]
    used = {int(x) for x in out.get("used") or [] if isinstance(x, int) or str(x).isdigit()}
    if used:
        res["cites"] = [x for x in cites if x["n"] in used] + [x for x in cites if x["n"] not in used]
        res["used"] = sorted(used)
    res["model"] = out.get("_model")
    return res
