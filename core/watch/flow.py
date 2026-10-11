"""首页的全过程视图:每条线索从 数源 → 评估入库 → 商机 → 形成报告 走过的路(使用方的流程图:线索 → 判定 → 商机 → 报告),
汇总成桑基图的节点与连线;已生成的报告各引用了哪些线索(按报告 Markdown 里的线索号)。选中一份报告、一个国别或图上的一个节点时,
相关线索那一段单独算一条连线,页面上高亮。

一条线索走过的节点:
- 数源:线索最多的前 8 个信源各一个节点,其余并成"其他信源";
- 评估入库:模型(或规则)判定相关的入库待办,判定无关的到此为止(人工提交的不判无关);
- 商机:属于一个在跟踪的商机的线索算"属于商机"(core.opportunity:设了进展的,含中标、未中标);商机放弃了的、线索标为忽略的
  算"忽略或放弃";其余相关线索按处理状态为"待处理"或"已处理·未跟踪"(看过了、没成商机)。桑基图里每个节点数的都是线索:
  "属于商机"是这些商机的所属线索条数,不是商机个数——从台账列入、还没有线索的商机不在图上(首页点这一格,右侧按商机列出);
- 形成报告:写进上报领导的报告的——国别报告、项目报告(formal;每月 1 日自动生成的国别报告与自建的一样算);周报、
  月度台账分析、线索简报等定期自动生成的是周期性报告,用来跟踪进展,不算。"写进"(covered)有两种:报告里引用了线索号;
  或线索所属的正式商机写进了报告(报告里有商机的台账编号,如国别报告的项目机会清单),且报告日期不早于线索入库——
  台账出处补成的线索、并进商机的线索都这样算。
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path

from core import geo
from core.markets import ALL

COLUMNS = ["数源", "评估入库", "商机", "形成报告"]
RELEVANT, IRRELEVANT = "判定相关·入库", "判定无关"
OPP, PENDING, DONE, DROPPED = "属于商机", "待处理", "已处理·未跟踪", "忽略或放弃"  # 第三列(商机)里线索的去向
IN_REPORT = "写进报告"
OTHER = "其他信源"
TOP_SOURCES = 8
FORMAL = ("国别报告", "项目报告")  # 上报领导的报告(自建的、每月自动生成的都算);周报、月度台账分析、线索简报等是周期性报告
KINDS = (("项目报告", "项目报告"), ("国别报告", "国别报告"), ("领域专题", "领域专题"), ("线索简报", "线索简报"), ("_周报_", "周报"),
         ("项目机会分析", "月度台账分析"))
WHERE = {"weekly": "每周自动", "monthly": "每月自动", "custom": "自建"}
_LEAD = re.compile(r"(?<![0-9a-f])[0-9a-f]{8}(?![0-9a-f])")
_ITEM = re.compile(r"\b[A-Z]{3}-\d{3}\b")
_WHEN = re.compile(r"(\d{4}-\d{2}-\d{2}|\d{4}-W\d{2})")


def kind_of(name: str) -> str:
    """报告文件名 → 类型。"""
    return next((v for k, v in KINDS if k in name), "报告")


def when(name: str) -> str:
    """报告文件名里的日期(或周)。"""
    m = _WHEN.search(name)
    return m.group(1) if m else ""


def when_date(label: str) -> date | None:
    """日期或周(2026-W40,取这一周的周日)→ 日期;认不出时 None。报告按它排先后。"""
    try:
        if "-W" in label:
            y, w = label.split("-W")
            return date.fromisocalendar(int(y), int(w), 7)
        return date.fromisoformat(label)
    except ValueError:
        return None


@dataclass
class Report:
    key: str  # reports/ 下的相对路径(不带后缀),选报告时用
    name: str
    kind: str
    when: str
    where: str  # 每周自动 / 每月自动 / 自建
    docx: Path | None = None
    md: Path | None = None
    leads: list[str] = field(default_factory=list)  # 引用的线索号(本库里有的,按出现先后)
    items: list[str] = field(default_factory=list)  # 引用的台账编号
    countries: list[str] = field(default_factory=list)  # 文件名里的国别(ISO3,如"AZE_周报""国别报告_阿塞拜疆")


def is_formal(name: str) -> bool:
    """报告文件名 → 是不是上报领导的报告(国别报告、项目报告;不论自建还是每月自动生成)。"""
    return kind_of(name) in FORMAL


def formal(r: Report) -> bool:
    """上报领导的报告:国别报告、项目报告——自建的与每月 1 日自动生成的国别报告都算。周报、月度台账分析、线索简报等
    定期生成的是周期性报告(跟踪进展),不算。"""
    return r.kind in FORMAL


def covered(r: Report, items_of: dict[str, str], seen: dict[str, str] | None = None) -> list[str]:
    """一份报告写进的线索号:报告里引用了的(按出现先后),加上所属的正式商机写进了这份报告的——报告里有商机的台账编号,
    且报告日期不早于线索入库(入库以前写的报告里不会有它;报告日期认不出的不比)。
    items_of:线索号 → 所属正式商机的编号(在跟踪的,含已结束的);seen:线索号 → 入库日期(YYYY-MM-DD 开头)。"""
    d = when_date(r.when)
    day, its, direct = (d.isoformat() if d else ""), set(r.items), set(r.leads)
    via = sorted(no for no, iid in items_of.items()
                 if iid in its and no not in direct and not (day and str((seen or {}).get(no) or "")[:10] > day))
    return [*r.leads, *via]


def _name_countries(stem: str) -> list[str]:
    head = stem.split("_", 1)[0]
    found = [head] if head in geo.countries() else []
    return list(dict.fromkeys(found + geo.detect(stem.replace("_", " "))))


def load_reports(root: Path, known: set[str]) -> list[Report]:
    """数据目录 reports/ 下的报告(Word 与同名 Markdown 算一份;引用从 Markdown 里读),新的在前。known:本库的线索号。"""
    root = Path(root)
    files: dict[Path, dict[str, Path]] = {}
    if root.exists():
        for fp in root.glob("**/*"):
            if fp.suffix in (".docx", ".md") and fp.is_file():
                files.setdefault(fp.with_suffix(""), {})[fp.suffix] = fp
    out = []
    for stem, fs in files.items():
        md = fs.get(".md")
        text = md.read_text(encoding="utf-8", errors="replace") if md else ""
        out.append(Report(key=stem.relative_to(root).as_posix(), name=stem.name, kind=kind_of(stem.name), when=when(stem.name),
                          where=WHERE.get(stem.parent.name, stem.parent.name), docx=fs.get(".docx"), md=md,
                          leads=[x for x in dict.fromkeys(_LEAD.findall(text)) if x in known],
                          items=list(dict.fromkeys(_ITEM.findall(text))), countries=_name_countries(stem.name)))
    out.sort(key=lambda r: (when_date(r.when) or date.min, r.name), reverse=True)
    return out


def source_nodes(rows: list[dict]) -> dict[str, str]:
    """信源名 → 桑基图上的节点:线索最多的前 8 个信源各一个节点,其余并成"其他信源"(只多一个时不并)。"""
    cnt = Counter(r["source_name"] for r in rows)
    top = {s for s, _ in cnt.most_common(TOP_SOURCES)}
    return {s: s if s in top or len(cnt) <= TOP_SOURCES + 1 else OTHER for s in cnt}


def path(r: dict, source_node: str, cited: set[str], opp: str | None = None) -> list[str]:
    """一条线索走过的节点(见本模块说明)。opp:线索所属的在跟踪商机的进展(不属于在跟踪的商机为 None)。"""
    nodes = [source_node, RELEVANT if r["relevant"] else IRRELEVANT]
    if opp == "dropped" or (not opp and (r["status"] == "ignore" or r.get("stage") == "dropped")):
        nodes.append(DROPPED)
    elif opp:
        nodes.append(OPP)
    elif r["relevant"]:
        nodes.append(DONE if r["status"] == "done" else PENDING)
    if r["no"] in cited:
        nodes.append(IN_REPORT)
    return nodes


def paths(rows: list[dict], cited: set[str], opps: dict[str, str] | None = None) -> dict[str, list[str]]:
    """线索号 → 走过的节点。opps:线索号 → 所属在跟踪商机的进展(core.opportunity.Book.tracked 与 members 算出)。"""
    srcs = source_nodes(rows)
    opps = opps or {}
    return {r["no"]: path(r, srcs[r["source_name"]], cited, opps.get(r["no"])) for r in rows}


def sankey(rows: list[dict], cited: set[str], selected: set[str] | None = None, opps: dict[str, str] | None = None) -> dict:
    """→ {nodes: [{name, depth, value, selected}], links: [{source, target, value, selected}]}。
    选中的线索(selected,线索号)在每一段单独一条连线(selected=True),其余一条;没有选中时 selected 全为 False。"""
    ps = paths(rows, cited, opps)
    total, on, seg, seg_on = Counter(), Counter(), Counter(), Counter()
    for no, p in ps.items():
        hit = bool(selected) and no in selected
        for n in p:
            total[n] += 1
            on[n] += hit
        for a, b in zip(p, p[1:]):
            seg[(a, b)] += 1
            seg_on[(a, b)] += hit
    srcs = [s for s, _ in Counter(p[0] for p in ps.values()).most_common() if s != OTHER] + ([OTHER] if OTHER in total else [])
    order = [(srcs, 0), ([RELEVANT, IRRELEVANT], 1), ([OPP, PENDING, DONE, DROPPED], 2), ([IN_REPORT], 3)]
    nodes = [{"name": n, "depth": d, "value": total[n], "selected": on[n]} for names, d in order for n in names if total[n]]
    links = []
    for (a, b), v in seg.items():
        s = seg_on[(a, b)]
        if s:
            links.append({"source": a, "target": b, "value": s, "selected": True})
        if v - s:
            links.append({"source": a, "target": b, "value": v - s, "selected": False})
    return {"nodes": nodes, "links": links}


def through(rows: list[dict], cited: set[str], node: str, opps: dict[str, str] | None = None) -> set[str]:
    """走过某个节点的线索号。"""
    return {no for no, p in paths(rows, cited, opps).items() if node in p}


def countries(rows: list[dict], selected: set[str] | None = None) -> list[dict]:
    """国别 → 线索数(与其中选中的);未识别国别的不在内。按线索数由多到少。"""
    cnt = Counter(r["country"] for r in rows if r["country"])
    on = Counter(r["country"] for r in rows if r["country"] and selected and r["no"] in selected)
    return [{"iso3": c, "name": geo.name_zh(c), "value": n, "selected": on[c]} for c, n in cnt.most_common()]


def stats(rows: list[dict], cited: set[str], opps: dict[str, str] | None = None) -> dict:
    """首页的几个数:信源、线索、判定相关入库、属于在跟踪商机的线索、写进(上报领导的)报告的线索。"""
    ps = paths(rows, cited, opps)
    return {"sources": len({r["source_name"] for r in rows}), "leads": len(rows),
            "relevant": sum(RELEVANT in p for p in ps.values()), "opps": sum(OPP in p for p in ps.values()),
            "reported": sum(IN_REPORT in p for p in ps.values())}


def opp_stages(members: dict, tracked: dict[str, dict]) -> dict[str, str]:
    """线索号 → 所属在跟踪商机的进展(members:Book.members;tracked:Book.tracked 的结果)。"""
    return {no: tracked[m.item]["value"] for no, m in members.items() if m.item in tracked}


# ---------- 筛选:入库时间、业务领域、区域、国别、资金来源 ----------

OUTSIDE = "市场外国别"  # 不在使用方市场分布表里的国别(区域条件用);在表里的任一区域都算"全部市场国别"(ALL)
NO_FUNDER, OTHER_FUNDER = "未注明", "其他出资方"
# 资金来源归一:线索里的出资方是模型从公告里抽的原话(World Bank (IDA)、Banque Mondiale、世界银行/IDA……),归到几类。
# (名称, 不分大小写的词, 区分大小写的缩写);一条线索可以有几个出资方。
FUNDERS = (
    ("世界银行", r"world ?bank|banque mondiale|banco mundial|世界银行|世行|国际开发协会|international development association"
                r"|international bank for reconstruction", r"IDA|IBRD|BIRD"),
    ("欧洲复兴开发银行", r"european bank for reconstruction|欧洲复兴开发银行", r"EBRD|BERD"),
    ("伊斯兰开发银行", r"islamic development bank|banque islamique|伊斯兰开发银行", r"IsDB|BID ?Islamique"),
    ("美洲开发银行", r"inter-american development|美洲开发银行", r"IDB|IADB"),
    ("亚洲基础设施投资银行", r"asian infrastructure investment|亚投行|亚洲基础设施投资银行", r"AIIB"),
    ("亚洲开发银行", r"asian development bank|亚洲开发银行", r"ADB"),
    ("非洲开发银行", r"african development bank|banque africaine de d[ée]veloppement|非洲开发银行", r"AfDB|BAD"),
    ("欧佩克基金", r"opec ?fund|欧佩克", r"OFID"),
    ("欧洲投资银行", r"european investment bank|欧洲投资银行", r"EIB|BEI"),
    ("欧盟", r"european union|union europ[ée]enne|欧盟", r"EU|UE|CINEA|NIP"),
    ("法国开发署", r"agence fran[cç]aise de d[ée]veloppement|法国开发署", r"AFD"),
    ("联合国开发署", r"联合国开发计划署|联合国开发署", r"UNDP|PNUD"),
    ("本国政府与业主", r"government|gouvernement|ministry|minist[eè]re|commune|municipal|政府|预算|budget", r"ONEE|SOCAR"),
)
_FUNDER_RE = [(name, re.compile(words, re.I), re.compile(rf"(?<![A-Za-z])(?:{acr})(?![A-Za-z])")) for name, words, acr in FUNDERS]
# 没写出资方时:多边开发银行、联合国机构自己的采购公告栏,出资方就是它(按信源编号的开头认)
SOURCE_FUNDER = (("wb_", "世界银行"), ("ebrd", "欧洲复兴开发银行"), ("isdb", "伊斯兰开发银行"), ("idb_", "美洲开发银行"),
                 ("aiib", "亚洲基础设施投资银行"), ("adb_", "亚洲开发银行"), ("opecfund", "欧佩克基金"), ("undp", "联合国开发署"))


def funders_of(r: dict) -> list[str]:
    """一条线索的资金来源(归一后的名称,可能几个)。没写出资方的,按信源认得出的写信源的出资方,认不出的"未注明"。"""
    return list(_funders(str(r.get("funder") or ""), str(r.get("source") or "")))


@lru_cache(maxsize=4096)  # 出资方的写法不多:每种只认一次(筛选时每条线索要数好几遍)
def _funders(text: str, source: str) -> tuple[str, ...]:
    hits = tuple(name for name, words, acr in _FUNDER_RE if words.search(text) or acr.search(text))
    if hits:
        return hits
    if text.strip():
        return (OTHER_FUNDER,)
    return next(((v,) for k, v in SOURCE_FUNDER if source.startswith(k)), (NO_FUNDER,))


def _values(r: dict, dim: str) -> list[str]:
    """一条线索在某一类条件上的取值(多值的,命中一个就算)。"""
    if dim == "sectors":
        return list(r.get("sectors") or [])
    if dim == "markets":
        return [r["market"], ALL] if r.get("market") else [OUTSIDE]
    if dim == "continents":
        return [r.get("continent") or ""]
    if dim == "countries":
        return [r["country"]] if r.get("country") else []
    if dim == "funders":
        return funders_of(r)
    raise KeyError(dim)


DIMS = ("sectors", "markets", "continents", "countries", "funders")


def narrow(rows: list[dict], start: str = "", end: str = "", field: str = "first_seen", **picked) -> list[dict]:
    """按日期(默认入库日期 first_seen;field="published" 按发布日期,没有发布日期的按入库日期)在 start~end 之内(含首尾,空为不限)
    与各类条件筛:picked = {sectors|markets|continents|countries|funders: [选中的值]};
    同一类里选几个是"或",不同类之间是"且";没选的类不限。"""
    want = {d: set(v) for d, v in picked.items() if v}
    unknown = set(want) - set(DIMS)
    if unknown:
        raise KeyError(f"不认识的条件:{'、'.join(sorted(unknown))}")
    out = []
    for r in rows:
        d = str(r.get(field) or r["first_seen"])[:10]
        if (start and d < start) or (end and d > end):
            continue
        if all(set(_values(r, dim)) & vals for dim, vals in want.items()):
            out.append(r)
    return out


def facets(rows: list[dict], start: str = "", end: str = "", field: str = "first_seen", **picked) -> dict[str, Counter]:
    """各类条件每个选项的线索数,按其余条件筛过以后数(选了"污水"以后,区域旁的数是污水线索在各区域的条数)。"""
    out = {}
    for dim in DIMS:
        sub = narrow(rows, start, end, field, **{k: v for k, v in picked.items() if k != dim})
        out[dim] = Counter(x for r in sub for x in _values(r, dim))
    return out
