"""台账更新:人核对原文后,把一条线索里的事实写进台账——登记来源,新增或修改时间节点与采购标段,改阶段,补编号与备注;
或按线索新建台账项目。界面「商机」「待确认」页的"更新台账""建成商机"按钮用它。

- 用 ruamel 往返读写,保留原文件的注释、顺序、缩进与写法(台账是人工维护的文件);
- 写之前把改后的台账整体按 core.ledger 的规则校验,不通过就不写,错误原样返回;
- 来源编号从 S501 起顺延(S0xx–S4xx 为此前各批人工核实);本次新增与修改的每条事实都引用这条来源。
"""
from __future__ import annotations

import difflib
import io
import re
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import SingleQuotedScalarString
from ruamel.yaml.util import load_yaml_guess_indent

from core import geo
from core.ledger import MILESTONES, LedgerError, load_ledger, parse_pdate

SRC_PREFIX, SRC_START = "S", 501
SRC_GROUP = "---------- 界面登记(线索核对原文后写入) ----------"
ITEM_GROUP = "======================= 界面新建(按线索核对原文) ======================="
# 各层字段的书写顺序(新加的字段插在对应位置,src 总在最后)
ITEM_ORDER = ["id", "name_zh", "name_en", "name_local", "aliases", "brief", "location", "segments", "in_scope", "stage", "stage_note",
              "owner", "ids", "financing", "total_cost", "scope", "procurement", "milestones", "parties", "notes", "verification", "src"]
MS_FIELDS = ["type", "date", "status", "note"]
PKG_FIELDS = ["name", "type", "status", "published", "deadline", "deadline_type", "awardee", "value", "note"]
DATE_FIELDS = {"date", "published", "deadline"}
# 线索里截止日的类型 → 台账节点类型;线索类别 → 标段类型、节点类型
DEADLINE_MS = {"eoi_deadline": "eoi_deadline", "pq_deadline": "pq_deadline", "bid_deadline": "bid_deadline", "rfp_deadline": "rfp_deadline"}
CATEGORY_DEADLINE = {"eoi": "eoi_deadline", "prequalification": "pq_deadline"}  # 信源结构化字段里的截止日按类别定类型,其余为投标截止
CATEGORY_PKG = {"eoi": "consultancy", "tender": "works", "prequalification": "works", "spn": "works"}
CATEGORY_MS = {"gpn": "gpn", "tender": "tender_launch", "spn": "tender_launch", "award": "award"}
PROCUREMENT_CATS = {"eoi", "tender", "prequalification", "spn", "gpn"}
EARLY_STAGES = {"plan", "preparation", "financing_signed"}
OFFICIAL = re.compile(r"(\.gov\.[a-z]{2}|\.gov|\.int|worldbank\.org|ebrd\.com|isdb\.org|aiib\.org|iadb\.org|undp\.org|un\.org|"
                      r"opecfund\.org|adb\.org|afdb\.org|eib\.org|ungm\.org)(/|:|$)", re.IGNORECASE)


@dataclass
class Update:
    """一次台账更新(界面上逐项确认后生成)。日期可写 2026-10-12,也可写 2026-10 / 2026-Q4 / 2027-H2 这类部分日期。"""

    item_id: str = ""  # 已有项目的编号;新建项目时留空并填 new_item
    source: dict | None = None  # {title, publisher, date, grade, url, note, via};via 默认 primary_read(界面上保存前须确认读过原文),
    # 系统自动写入的为 auto_extract
    milestones: list[dict] = field(default_factory=list)  # 新增节点 [{type, date, status, note}]
    packages: list[dict] = field(default_factory=list)  # 新增标段 [{name, type, status, published, deadline, deadline_type, awardee, value, note}]
    # 改已有节点/标段:[{kind: milestone|package, index, match: {字段: 原值}, set: {字段: 新值}, cite: 是否加引用}]
    # match 用来确认打开弹窗后台账没被别处改过(改过就报错,不按位置误改)
    edits: list[dict] = field(default_factory=list)
    stage: str | None = None
    stage_note: str | None = None
    ids: dict = field(default_factory=dict)
    note: str | None = None
    new_item: dict | None = None  # 新建项目:{name_zh, name_en, name_local, segments, stage, brief, funder, owner, location}


@dataclass
class Result:
    errors: list[str]
    source_id: str | None = None
    item_id: str | None = None
    diff: str = ""
    changes: list[str] = field(default_factory=list)  # 中文摘要,写提交说明与界面提示用


# ---------- 读写 ----------

def _yaml(text: str = "") -> YAML:
    """往返读写的 YAML:保留引号与注释、不折行、null 写成 null、不用锚点;缩进按原文件猜(AZE 台账的列表不缩进,测试样例缩进 2)。"""
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096
    seq, off = 2, 0
    if text:
        _, ind, bsi = load_yaml_guess_indent(text)
        if ind and bsi is not None:
            seq, off = ind, bsi
    y.indent(mapping=2, sequence=seq, offset=off)
    y.representer.add_representer(type(None), lambda r, d: r.represent_scalar("tag:yaml.org,2002:null", "null"))
    y.representer.ignore_aliases = lambda *_: True  # 同一个值写两处(如当天日期)时照写,不用 &id001 / *id001 锚点
    return y


def _q(v: object) -> object:
    """流式写法 {…}/[…] 里带 "?" 的文字(如网址的查询串)加单引号:写库用的 ruamel 不加,读库用的 PyYAML 却认不出来。"""
    return SingleQuotedScalarString(v) if isinstance(v, str) and "?" in v else v


def _flow_seq(values: list) -> CommentedSeq:
    s = CommentedSeq(_q(v) for v in values)
    s.fa.set_flow_style()
    return s


def _flow_map(pairs: list[tuple]) -> CommentedMap:
    m = CommentedMap((k, _q(v)) for k, v in pairs)
    m.fa.set_flow_style()
    return m


def _like(seq: CommentedSeq, m: CommentedMap) -> CommentedMap:
    """新条目沿用同一列表里已有条目的写法(流式 {…} 或分行)。"""
    if seq and isinstance(seq[0], CommentedMap) and seq[0].fa.flow_style():
        m.fa.set_flow_style()
        for k in m:
            m[k] = _q(m[k])
    return m


def _put(obj: CommentedMap, key: str, value: object, order: list[str]) -> None:
    """设字段;原来没有的字段按 order 插到该在的位置(而不是追加在 src 后面)。"""
    if obj.fa.flow_style():
        value = _q(value)
    if key in obj or key not in order:
        obj[key] = value
        return
    rank = order.index(key)
    pos = next((i for i, k in enumerate(obj) if k in order and order.index(k) > rank), len(obj))
    obj.insert(pos, key, value)


def _date_value(v: object) -> object:
    """完整日期写成 YAML 日期(与文件里的写法一致),部分日期(2026-10、2026-Q4)保留字符串。"""
    s = str(v).strip()
    try:
        return date.fromisoformat(s)
    except ValueError:
        return s


def _clean(v: object) -> str:
    return "" if v is None else str(v).strip()


def _cite(obj: CommentedMap, sid: str | None, order: list[str]) -> bool:
    """在条目的 src 里加上来源编号 → 是否真的加了(已经引用的不重复)。"""
    if not sid:
        return False
    s = obj.get("src")
    if s is None:
        _put(obj, "src", _flow_seq([sid]), order + ["src"])
    elif isinstance(s, list):
        if sid in s:
            return False
        s.append(sid)
    elif s != sid:
        obj["src"] = _flow_seq([s, sid])
    else:
        return False
    return True


def brief_changes(changes: list[str], limit: int = 160) -> str:
    """改动摘要连成一句(写提交说明、运行摘要):超长时按整条截断,末尾注"等 N 处",不从词中间截断。"""
    out: list[str] = []
    n = 0
    for c in changes:
        if out and n + len(c) + 1 > limit:
            return ";".join(out) + f" 等 {len(changes)} 处"
        out.append(c)
        n += len(c) + 1
    return ";".join(out)


def _unified(a: str, b: str, name: str) -> str:
    return "\n".join(difflib.unified_diff(a.splitlines(), b.splitlines(), f"{name}(原)", f"{name}(改后)", lineterm="", n=1))


# ---------- 编号与默认值 ----------

def _src_num(k: object) -> int | None:
    m = re.fullmatch(rf"{SRC_PREFIX}(\d+)", str(k))
    return int(m[1]) if m else None


def next_source_id(sources: dict) -> str:
    nums = [n for n in map(_src_num, sources) if n is not None and n >= SRC_START]
    return f"{SRC_PREFIX}{max(nums or [SRC_START - 1]) + 1}"


def next_item_id(items: list[dict], country: str) -> str:
    nums = [int(str(it.get("id"))[-3:]) for it in items if re.fullmatch(rf"{country}-\d{{3}}", str(it.get("id", "")))]
    return f"{country}-{max(nums or [0]) + 1:03d}"


def grade_of(url: str | None, kind: str | None) -> str:
    """来源等级的默认值:政府与国际组织、多边开发银行网站 A;新闻 RSS/GDELT C;其余 B(界面上可改)。"""
    host = re.sub(r"^[a-z]+://", "", str(url or ""), flags=re.IGNORECASE).split("/")[0]
    if host and OFFICIAL.search(host + "/"):
        return "A"
    return "C" if kind in ("rss", "gdelt") else "B"


def check_date(v: object) -> str | None:
    """界面输入的日期检查:→ 错误说明;可用 2026 / 2026-H2 / 2026-Q4 / 2026-12 / 2026-12-15。"""
    try:
        parse_pdate(str(v).strip())
    except ValueError as e:
        return str(e)
    return None


def packages_of(item: dict | None) -> list:
    return ((item or {}).get("procurement") or {}).get("packages") or []


# ---------- 按线索给出建议 ----------

def _sig_deadlines(sig: dict, cat: str) -> list[tuple[str, str]]:
    """→ [(节点类型, 日期)]:模型抽取的在前;信源结构化字段里的截止日模型没抽到时补上。"""
    x = sig.get("llm") if sig.get("llm") and not sig["llm"].get("_error") else {}
    out = [(DEADLINE_MS[d["type"]], d["date"]) for d in x.get("deadlines") or [] if d.get("type") in DEADLINE_MS and d.get("date")]
    fd = (sig.get("fields") or {}).get("deadline")
    if fd and all(day != fd for _, day in out):
        out.append((CATEGORY_DEADLINE.get(cat, "bid_deadline"), fd))
    return out


def propose(sig: dict, item: dict | None, today: date) -> Update:
    """按线索给出建议的更新内容(界面上预填,人对照原文逐项确认或修改)。item 为 None 时是新建项目。
    台账里已有同一天同类型的节点、同一截止日的标段时,不再新增,改为在原条目上加引用。"""
    x = sig.get("llm") if sig.get("llm") and not sig["llm"].get("_error") else {}
    cat = x.get("category") or (sig.get("rules") or {}).get("stage") or "other"
    pub = sig.get("published")
    t = today.isoformat()
    src = {"title": sig["title"][:200], "publisher": sig.get("source_name") or "",
           "date": pub or f"{sig['first_seen'][:10]} 查询", "grade": grade_of(sig.get("url"), sig.get("kind")),
           "url": sig.get("url") or "", "note": f"系统线索 {sig['id'][:8]} 核对原文后登记({t})"}
    old_ms = (item or {}).get("milestones") or []
    old_pk = packages_of(item)
    upd = Update(item_id=(item or {}).get("id", ""), source=src)

    def cite(kind: str, idx: int, match: dict, **set_: str) -> None:
        if not any(e["kind"] == kind and e["index"] == idx for e in upd.edits):
            upd.edits.append({"kind": kind, "index": idx, "match": match, "set": set_, "cite": True})

    def add_ms(typ: str, day: str, status: str) -> None:
        i = next((i for i, m in enumerate(old_ms) if m.get("type") == typ and str(m.get("date")) == day), None)
        if i is not None:
            cite("milestone", i, {"type": typ, "date": day})
        elif all((m["type"], m["date"]) != (typ, day) for m in upd.milestones):
            upd.milestones.append({"type": typ, "date": day, "status": status, "note": ""})

    if cat in CATEGORY_MS and pub:  # 公告发布日即节点实际发生日;发布日不详时不猜
        add_ms(CATEGORY_MS[cat], pub, "actual")
    dls = _sig_deadlines(sig, cat)
    for typ, day in dls:
        add_ms(typ, day, "planned" if day >= t else "actual")
    if cat in CATEGORY_PKG:
        typ, day = dls[0] if dls else (CATEGORY_DEADLINE.get(cat, "bid_deadline"), "")
        i = next((i for i, p in enumerate(old_pk) if day and str(p.get("deadline")) == day), None)
        if i is None:
            upd.packages.append({"name": sig["title"][:100], "type": CATEGORY_PKG[cat],
                                 "status": "closed" if day and day < t else "open", "published": pub or "", "deadline": day,
                                 "deadline_type": typ, "awardee": "", "note": ""})
        else:
            p = old_pk[i]
            cite("package", i, {"name": str(p.get("name"))}, **({"status": "open"} if p.get("status") == "planned" and day >= t else {}))
    if item is not None and item.get("stage") in EARLY_STAGES and cat in PROCUREMENT_CATS:
        upd.stage = "procurement"
    if item is None:
        upd.new_item = {"name_zh": x.get("project") or "", "name_en": (x.get("project") or sig["title"])[:120], "segments": [],
                        "stage": "procurement" if cat in PROCUREMENT_CATS else "preparation", "brief": x.get("summary_zh") or "",
                        "funder": x.get("funder") or ""}
    return upd


# ---------- 写入 ----------

def _register(srcs: CommentedMap, s: dict, changes: list[str]) -> tuple[str | None, str | None]:
    """登记来源 → (编号, 错误)。同一网址已登记过的直接引用原编号。"""
    url, title = _clean(s.get("url")), _clean(s.get("title"))
    if not (url or title):
        return None, "来源至少要有网址或标题"
    if url:
        known = next((k for k, v in srcs.items() if isinstance(v, dict) and _clean(v.get("url")).rstrip("/") == url.rstrip("/")), None)
        if known:
            changes.append(f"引用已登记的来源 {known}")
            return str(known), None
    sid = next_source_id(srcs)
    pairs = [("title", title or url), ("publisher", _clean(s.get("publisher"))), ("date", _clean(s.get("date"))),
             ("grade", _clean(s.get("grade")) or "B"), ("via", _clean(s.get("via")) or "primary_read"), ("url", url),
             ("note", _clean(s.get("note")))]
    first = not any((n := _src_num(k)) is not None and n >= SRC_START for k in srcs)
    srcs[sid] = _flow_map([(k, v) for k, v in pairs if v])
    if first:
        srcs.yaml_set_comment_before_after_key(sid, before="\n" + SRC_GROUP, indent=2)
    changes.append(f"登记来源 {sid}")
    return sid, None


def _new_entry(fields: dict, order: list[str], sid: str | None, defaults: dict | None = None) -> CommentedMap:
    m = CommentedMap()
    for k in order:
        v = _clean(fields.get(k))
        if not v or (defaults and defaults.get(k) == v):
            continue
        m[k] = _date_value(v) if k in DATE_FIELDS else v
    if sid:
        m["src"] = _flow_seq([sid])
    return m


def _edit(obj: CommentedMap, set_: dict, order: list[str], defaults: dict) -> list[str]:
    """改已有条目的字段 → 改了哪些(中文)。空值即删掉该字段;与默认值相同且原来没写的不加。"""
    out = []
    for k, v in set_.items():
        v = _clean(v)
        old = _clean(obj.get(k)) or defaults.get(k, "")
        if v == old:
            continue
        if not v or (k not in obj and defaults.get(k) == v):
            if k in obj:
                del obj[k]
                out.append(f"{k} 清空")
            continue
        _put(obj, k, _date_value(v) if k in DATE_FIELDS else v, order)
        out.append(f"{k} {old or '—'} → {v}")
    return out


def skeleton(iso3: str, today: date) -> tuple[str, str]:
    """新国家的空台账与来源登记(在这个国家建第一个商机时用,文件随第一个项目一起写出)→ (ledger.yaml, sources.yaml)。"""
    name = geo.name_zh(iso3)
    return (f"# {name}项目机会台账(在这个国家建第一个商机时系统建的)。只写公开来源事实,每条事实带来源编号(见同目录 sources.yaml)。\n"
            "# 判断、优先级、非公开信息不写这里,写在内部叠加(private/overlay)与团队表格的项目表。\n"
            f"schema: osint-water/ledger@1\ncountry: {iso3}\ntitle: {name}水务项目机会台账\nas_of: {today.isoformat()}\nopportunities:\n",
            f"# {name}台账来源登记。S501 起为界面「更新台账」、跟踪即入账按线索登记。\n"
            "# grade:A 政府/国际组织/出资方一手;B 权威二手(行业媒体、招标库、专业机构);C 媒体与企业新闻;D 不可用。\nsources:\n")


def apply(ledger_path: str | Path, upd: Update, today: date, rules: dict | None = None, write: bool = True) -> Result:
    """把更新写进台账与来源登记(write=False 只校验并给出改动预览)。返回错误、来源编号、项目编号、差异与中文摘要。
    台账文件还不存在的(新国家的第一个商机),从空台账起步,保存时连同目录一起建出来。"""
    ledger_path = Path(ledger_path)
    fresh = not ledger_path.exists()
    new_led_text, new_src_text = skeleton(ledger_path.parent.name.upper(), today) if fresh else ("", "")
    old_led = new_led_text if fresh else ledger_path.read_text(encoding="utf-8")
    yl = _yaml(old_led)
    led_doc = yl.load(old_led)
    # 来源登记一般在同目录 sources.yaml;也允许直接写在台账文件的 sources 里
    inline = led_doc.get("sources") is not None
    src_path = ledger_path if inline else ledger_path.with_name("sources.yaml")
    old_src = old_led if inline else (new_src_text if fresh else src_path.read_text(encoding="utf-8") if src_path.exists()
                                      else "sources: {}\n")
    ys = yl if inline else _yaml(old_src)
    src_doc = led_doc if inline else ys.load(old_src)
    if not src_doc.get("sources"):  # 空的({} 或没写)换成分行写法,新登记的来源一条一行
        src_doc["sources"] = CommentedMap()
    if not led_doc.get("opportunities"):
        led_doc["opportunities"] = CommentedSeq()
    items = led_doc["opportunities"]
    changes: list[str] = []
    errs: list[str] = []

    if upd.new_item is not None:
        ni = upd.new_item
        if not _clean(ni.get("name_zh")):
            return Result(["新项目缺少中文名称"])
        if not ni.get("segments"):
            return Result(["新项目至少选一个细分领域"])
        iid = next_item_id(items, str(led_doc["country"]).upper())
        item = CommentedMap([("id", iid), ("name_zh", _clean(ni["name_zh"]))])
        for k in ("name_en", "name_local", "brief"):
            if _clean(ni.get(k)):
                item[k] = _clean(ni[k])
        if loc := [(k, _clean(v)) for k, v in (ni.get("location") or {}).items() if k in ("city_zh", "city_en", "detail") and _clean(v)]:
            item["location"] = _flow_map(loc)
        item["segments"] = _flow_seq(list(ni["segments"]))
        item["stage"] = ni.get("stage") or "preparation"
        if owner := {_clean(k): _clean(v) for k, v in (ni.get("owner") or {}).items() if _clean(k) and _clean(v)}:
            item["owner"] = _flow_map(list(owner.items()))
        items.append(_like(items, item))
        if ITEM_GROUP not in old_led:  # 界面新建的项目排在最后,第一次时加一行分组注释
            items.yaml_set_comment_before_after_key(len(items) - 1, before="\n" + ITEM_GROUP, indent=0)
        changes.append(f"新建项目 {iid} {item['name_zh']}")
    else:
        item = next((it for it in items if it.get("id") == upd.item_id), None)
        if item is None:
            return Result([f"台账里没有 {upd.item_id}"])
        iid = upd.item_id

    sid = None
    if upd.source:
        sid, err = _register(src_doc["sources"], upd.source, changes)
        if err:
            return Result([err])
    refs = [sid] if sid else []

    if upd.new_item is not None and _clean(upd.new_item.get("funder")):
        fin = CommentedMap([("funder", _clean(upd.new_item["funder"])), ("instrument", "unknown"), ("status", "reported")])
        if refs:
            fin["src"] = _flow_seq(refs)
        _put(item, "financing", CommentedSeq([fin]), ITEM_ORDER)
    if upd.stage and upd.stage != item.get("stage"):
        changes.append(f"阶段 {item.get('stage')} → {upd.stage}")
        item["stage"] = upd.stage
    if _clean(upd.stage_note) and _clean(upd.stage_note) != _clean(item.get("stage_note")):
        _put(item, "stage_note", _clean(upd.stage_note), ITEM_ORDER)
        changes.append("改阶段说明")
    for k, v in (upd.ids or {}).items():
        k, v = _clean(k), _clean(v)
        if k and v and _clean((item.get("ids") or {}).get(k)) != v:
            if item.get("ids") is None:
                _put(item, "ids", _flow_map([]), ITEM_ORDER)
            item["ids"][k] = _q(v)
            changes.append(f"编号 {k}:{v}")

    # 改已有节点与标段(先改,再追加新增的,位置不受影响)
    for e in upd.edits:
        kind = e.get("kind")
        seq = item.get("milestones") if kind == "milestone" else packages_of(item)
        idx = e.get("index", -1)
        if kind not in ("milestone", "package") or not seq or not 0 <= idx < len(seq):
            errs.append(f"要改的{'节点' if kind == 'milestone' else '标段'}不存在(第 {idx + 1} 条)")
            continue
        obj = seq[idx]
        if any(_clean(obj.get(k)) != _clean(v) for k, v in (e.get("match") or {}).items()):
            errs.append("台账在打开弹窗之后被改过(可能刚同步了新版本),请关掉弹窗重新打开再改")
            continue
        order, defaults = (MS_FIELDS, {}) if kind == "milestone" else (PKG_FIELDS, {"deadline_type": "bid_deadline"})
        what = _edit(obj, e.get("set") or {}, order + ["src"], defaults)
        label = (f"节点 {MILESTONES.get(obj.get('type'), obj.get('type'))} {obj.get('date')}" if kind == "milestone"
                 else f"标段 {obj.get('name')}")
        if what:
            changes.append(f"改{label}:{';'.join(what)}")
        if (what or e.get("cite")) and _cite(obj, sid, order) and not what:
            changes.append(f"{label} 加引用 {sid}")

    for p in upd.packages:
        if not _clean(p.get("name")):
            errs.append("新增标段缺少名称")
            continue
        if item.get("procurement") is None:
            _put(item, "procurement", CommentedMap([("regime", "unknown")]), ITEM_ORDER)
        if item["procurement"].get("packages") is None:
            item["procurement"]["packages"] = CommentedSeq()
        seq = item["procurement"]["packages"]
        seq.append(_like(seq, _new_entry(p, PKG_FIELDS, sid, {"deadline_type": "bid_deadline"})))
        if not _clean(p.get("deadline")) and "deadline_type" in seq[-1]:
            del seq[-1]["deadline_type"]
        changes.append(f"新增标段:{_clean(p['name'])}" + (f"(截止 {_clean(p['deadline'])})" if _clean(p.get("deadline")) else ""))
    for m in upd.milestones:
        if not (_clean(m.get("type")) and _clean(m.get("date")) and _clean(m.get("status"))):
            errs.append("新增节点要有类型、日期和状态")
            continue
        if item.get("milestones") is None:
            _put(item, "milestones", CommentedSeq(), ITEM_ORDER)
        seq = item["milestones"]
        seq.append(_like(seq, _new_entry(m, MS_FIELDS, sid)))
        changes.append(f"新增节点:{MILESTONES.get(m['type'], m['type'])} {_clean(m['date'])}")
    if _clean(upd.note):
        if item.get("notes") is None:
            _put(item, "notes", CommentedSeq(), ITEM_ORDER)
        n = CommentedMap([("text", _clean(upd.note))])
        if refs:
            n["src"] = _flow_seq(refs)
        item["notes"].append(_like(item["notes"], n))
        changes.append("新增备注")
    if upd.new_item is not None:
        auto = (upd.source or {}).get("via") == "auto_extract"
        _put(item, "verification", CommentedMap([
            ("status", "unverified"), ("checked_at", today), ("method", "auto_extract" if auto else "primary_read"),
            ("pending", CommentedSeq(["系统按线索自动新建,名称、领域未经人工核对;待补充资金、规模与节点" if auto
                                      else "按线索新建,待补充资金、规模与节点"]))]), ITEM_ORDER)
    if errs:
        return Result(errs, sid, iid, "", changes)
    if not [c for c in changes if not c.startswith(("登记来源", "引用已登记"))]:
        if changes and changes[0].startswith("引用已登记"):
            return Result([f"台账里已有这些内容,并且已经引用这条来源({sid}),不用更新"], sid, iid, "", changes)
        return Result(["没有选择要写入台账的内容(至少新增或修改一处,或给已有条目加引用)"], sid, iid, "", changes)

    led_doc["as_of"] = today
    buf = io.StringIO()
    yl.dump(led_doc, buf)
    new_led = buf.getvalue()
    if inline:
        new_src = new_led
    else:
        buf = io.StringIO()
        ys.dump(src_doc, buf)
        new_src = buf.getvalue()
    with tempfile.TemporaryDirectory() as td:  # 整体校验改后的台账,不通过不写
        tl = Path(td) / "ledger.yaml"
        tl.write_text(new_led, encoding="utf-8")
        if not inline:
            (Path(td) / "sources.yaml").write_text(new_src, encoding="utf-8")
        try:
            load_ledger(tl, rules)
        except LedgerError as e:
            errs = [ln.strip() for ln in str(e).splitlines()[1:]] or [str(e)]
        except Exception as e:  # noqa: BLE001  YAML 写坏等,原样报出
            errs = [f"{type(e).__name__}: {e}"]
    diff = _unified(old_led, new_led, ledger_path.name)
    if not inline:
        diff = "\n\n".join(d for d in (diff, _unified(old_src, new_src, src_path.name)) if d)
    if errs:
        return Result(errs, sid, iid, diff, changes)
    if write:
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        ledger_path.write_text(new_led, encoding="utf-8")
        if not inline:
            src_path.write_text(new_src, encoding="utf-8")
    return Result([], sid, iid, diff, changes)


def remove_source(ledger_path: str | Path, sid: str, item_id: str, added: dict | None, today: date, rules: dict | None = None,
                  write: bool = True) -> Result:
    """撤销一次按来源写入的更新(系统自动写入的"撤销"):删掉来源登记,台账各处引用里去掉它;这次新增的节点、标段
    (added:{milestones: [[类型, 日期]], packages: [[名称, 截止]], stage: [原阶段, 新阶段]})只剩这条来源的删掉,
    之后别的来源也引用了的留着;阶段仍是这次改成的就改回去。按内容撤销,不按提交还原:之后的写入挨着同一处也不冲突。"""
    ledger_path = Path(ledger_path)
    old_led = ledger_path.read_text(encoding="utf-8")
    yl = _yaml(old_led)
    led_doc = yl.load(old_led)
    inline = led_doc.get("sources") is not None
    src_path = ledger_path if inline else ledger_path.with_name("sources.yaml")
    old_src = old_led if inline else src_path.read_text(encoding="utf-8")
    ys = yl if inline else _yaml(old_src)
    src_doc = led_doc if inline else ys.load(old_src)
    changes: list[str] = []
    srcs = src_doc.get("sources") or {}
    if sid in srcs:
        del srcs[sid]
        changes.append(f"删去来源 {sid}")

    def strip(obj: CommentedMap) -> bool:
        """条目的 src 里去掉 sid → 是否去掉后一条也不剩(src 整个删掉)。"""
        s_ = obj.get("src")
        if isinstance(s_, list) and sid in s_:
            s_.remove(sid)
            if not s_:
                del obj["src"]
                return True
        elif s_ == sid:
            del obj["src"]
            return True
        return False

    def walk(node: object) -> None:
        if isinstance(node, dict):
            strip(node)
            for v in list(node.values()):
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    added = added or {}
    item = next((it for it in led_doc["opportunities"] if it.get("id") == item_id), None)
    if item is not None:
        for kind, key, want in (("milestone", "milestones", {(str(t), str(d)) for t, d in added.get("milestones") or []}),
                                ("package", "packages", {(str(n), str(d or "")) for n, d in added.get("packages") or []})):
            seq = item.get(key) if kind == "milestone" else (item.get("procurement") or {}).get(key)
            if not seq:
                continue
            for i in reversed(range(len(seq))):
                e = seq[i]
                ident = (_clean(e.get("type")), _clean(e.get("date"))) if kind == "milestone" else (_clean(e.get("name")), _clean(e.get("deadline")))
                if strip(e) and ident in want:
                    del seq[i]
                    changes.append(f"删去{'节点' if kind == 'milestone' else '标段'} {' '.join(x for x in ident if x)}")
            if not seq:
                del (item if kind == "milestone" else item["procurement"])[key]
        old_stage, new_stage = (added.get("stage") or [None, None])[:2]
        if old_stage and new_stage and item.get("stage") == new_stage:
            item["stage"] = old_stage
            changes.append(f"阶段 {new_stage} → {old_stage}")
    before = len(changes)
    walk(led_doc["opportunities"])  # 其余引用(给已有节点、标段加的引用)
    buf = io.StringIO()
    yl.dump(led_doc, buf)
    if buf.getvalue() != old_led and len(changes) == before:
        changes.append(f"去掉引用 {sid}")
    if not changes:
        return Result([f"台账里已经没有来源 {sid} 写入的内容(可能已手工改过),不用撤销"], sid, item_id)
    led_doc["as_of"] = today
    buf = io.StringIO()
    yl.dump(led_doc, buf)
    new_led = buf.getvalue()
    if inline:
        new_src = new_led
    else:
        buf = io.StringIO()
        ys.dump(src_doc, buf)
        new_src = buf.getvalue()
    errs: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        tl = Path(td) / "ledger.yaml"
        tl.write_text(new_led, encoding="utf-8")
        if not inline:
            (Path(td) / "sources.yaml").write_text(new_src, encoding="utf-8")
        try:
            load_ledger(tl, rules)
        except LedgerError as e:
            errs = [ln.strip() for ln in str(e).splitlines()[1:]] or [str(e)]
    diff = _unified(old_led, new_led, ledger_path.name)
    if not inline:
        diff = "\n\n".join(d for d in (diff, _unified(old_src, new_src, src_path.name)) if d)
    if errs:
        return Result(errs, sid, item_id, diff, changes)
    if write:
        ledger_path.write_text(new_led, encoding="utf-8")
        if not inline:
            src_path.write_text(new_src, encoding="utf-8")
    return Result([], sid, item_id, diff, changes)
