"""规则初筛:水务词 × 机会阶段词 × 国别。召回优先——宁可多送给模型判定,不漏公告。"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

import yaml

from core.ledger import ROOT
from core.watch.store import Item

TERMS_PATH = ROOT / "reference" / "watch_terms.yaml"
_ACRONYM = re.compile(r"^[A-Z]{2,5}$")


SECTORS = ["water_supply", "wastewater", "hydraulic", "desalination", "stormwater", "environment", "other"]
LANGS_SKIP = {"zh"}


@lru_cache(maxsize=4)
def load_terms(path: str | Path = TERMS_PATH) -> dict:
    """→ {water: 全部水务词, sector_terms: {领域: 词}, sector_zh: {领域: 中文名}, stage: {阶段: 词}, stage_order}。"""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    sector_terms, sector_zh = {}, {}
    for k, v in (raw.get("sectors") or {}).items():
        sector_terms[k] = [t for lang, ts in v.items() if lang not in LANGS_SKIP for t in ts]
        sector_zh[k] = v.get("zh", k)
    water = list(dict.fromkeys(t for ts in sector_terms.values() for t in ts))
    stages = {k: list(v) for k, v in (raw.get("stage") or {}).items()}
    return {"water": water, "sector_terms": sector_terms, "sector_zh": sector_zh, "stage": stages,
            "stage_order": raw.get("stage_order") or list(stages)}


def terms_version(entities: list[str] | tuple = (), path: str | Path = TERMS_PATH) -> str:
    """词表与实体词的指纹。词表改了之后,各信源里之前没过初筛、仍在回溯期或截止前的条目会按新词表重判一次。"""
    raw = Path(path).read_bytes() + "\n".join(entities).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:12]


@lru_cache(maxsize=4)
def term_sectors(path: str | Path = TERMS_PATH) -> dict[str, list[str]]:
    """词 → 所属领域(给没有记下领域的旧线索按命中词补领域)。"""
    out: dict[str, list[str]] = {}
    for k, ts in load_terms(path)["sector_terms"].items():
        for t in ts:
            if k != "general" and k not in out.setdefault(t, []):
                out[t].append(k)
    return out


_APOSTROPHES = str.maketrans({"\u2019": "'", "\u2018": "'", "\u02bc": "'"})


def _fold(s: str) -> str:
    """不分大小写、不分重音比较用(词与正文同样折叠)。
    - 法语、西语公告标题常全大写且不带重音(STATION D'EPURATION、EAUX USEES),去掉重音后才配得上 épuration、usées;
    - 弯引号统一成直引号(adduction d’eau 与 d'eau);
    - 阿塞拜疆语/土耳其语的 I/ı、İ/i 与默认大小写映射不同:"İ".casefold() 带组合点(随重音一起去掉),
      大写 I 折成 i 而原词写 ı(统一成 i)。"""
    s = s.casefold().replace("ı", "i").translate(_APOSTROPHES)
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _find(text: str, folded: str, t: str) -> int:
    """词在文中首次出现的位置(-1 为没有):从词首开始匹配(词干可带后缀);缩写按整词、区分大小写;/…/ 为正则。"""
    if _ACRONYM.match(t):
        m = re.search(rf"(?<![A-Za-z]){t}(?![A-Za-z])", text)
    elif len(t) > 2 and t.startswith("/") and t.endswith("/"):
        m = _regex(t[1:-1]).search(folded)
    else:
        m = _plain(t).search(folded)
    return m.start() if m else -1


@lru_cache(maxsize=4096)
def _plain(t: str) -> re.Pattern:
    """普通词从词首开始匹配;词组里前面的词可带复数词尾 s/x(station de pompage 也配 stations de pompage、
    réseau d'eau 也配 réseaux d'eau;最后一个词本来就按词干匹配)。"""
    return re.compile(r"(?<!\w)" + r"(?:s|x)? ".join(re.escape(w) for w in _fold(t).split(" ")))


@lru_cache(maxsize=256)
def _regex(body: str) -> re.Pattern:
    return re.compile(r"(?<!\w)" + _fold(body))


def _hits(text: str, terms: list[str]) -> list[str]:
    folded = _fold(text)
    return [t for t in terms if _find(text, folded, t) >= 0]


def _stage(text: str, terms: dict) -> tuple[str | None, list[str]]:
    """阶段取最早出现的阶段词(公告类型一般写在最前面,正文后面常顺带提到别的阶段);同位置按 stage_order。"""
    folded = _fold(text)
    best, hits = None, []
    for k in terms["stage_order"]:
        for t in terms["stage"].get(k, []):
            pos = _find(text, folded, t)
            if pos >= 0:
                hits.append(t)
                if best is None or pos < best[0]:
                    best = (pos, k)
    return (best[1] if best else None), hits


def evaluate(it: Item, src: dict, entities: list[str], terms: dict | None = None) -> dict:
    """→ {water, water_terms, sectors, stage, stage_from, stage_terms, entities, candidate}。
    阶段先看标题与结构化字段(公告类型栏),没有再看正文。"""
    terms = terms or load_terms()
    head = " \n ".join([it.title, *(str(v) for v in it.fields.values() if v)])
    text = " ".join([head, it.text[:20_000]])
    water_terms = _hits(text, terms["water"])
    hit = set(water_terms)
    sectors = [k for k in SECTORS if k != "other" and hit & set(terms["sector_terms"].get(k, []))]
    ent = _hits(text, entities)
    stage, head_terms = _stage(head, terms)
    stage_from = "head" if stage else None
    body_stage, body_terms = _stage(it.text[:20_000], terms)
    if not stage and body_stage:
        stage, stage_from = body_stage, "body"
    # 信源标了 topic: water,或是人工挑的项目页(page 类),都视为水务,不要求命中水务词
    water = bool(water_terms) or src.get("topic") == "water" or src.get("kind") == "page"
    return {
        "water": water, "water_terms": water_terms[:8], "sectors": sectors, "stage": stage, "stage_from": stage_from,
        "stage_terms": list(dict.fromkeys(head_terms + body_terms))[:8], "entities": ent[:5],
        "candidate": water or bool(ent),
    }
