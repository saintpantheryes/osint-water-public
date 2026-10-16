"""招标监测报告(A14,报告类型「招标监测」;本体 v2.29):一国在招的水务标段一张总表,每条信息带回原公告的角标链接。

章节:在招标段(截止未到,带剩余天数)→ 本期新发布 → 本期已截止(等结果)→ 本期授标(授标线索 + 已中标项目)→ 各标段资格要求 → 覆盖说明 → 原公告链接。
使用方(2026-10-16)的三条要求:只看水务(库里本来只收水务线索);出 Word;每条都能点角标回到原文核对。
资格要求:配了模型时请模型从原文摘录里抽(中文),没配时按关键句规则抽英文原句,表里写明抽法;抽过的存本机缓存,不重复花钱。
期间「YYYY-MM-DD~YYYY-MM-DD」,留空 = 到当天的近 30 天;每周一给「重点」国别自建一份(期间 = 上周),见 reports.ensure_periodic。
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta

from core import geo, tstore
from core import ontology as onto_mod

from . import brief
from .common import Ctx

DEFAULT_DAYS = 30
PERIOD_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\s*[~～至/—–-]+\s*(\d{4}-\d{2}-\d{2})")
DEADLINE_KINDS = onto_mod.group_values("milestone_kind", "采购截止")  # 意向书截止、资格预审截止、投标截止、PPP标书截止
AWARD_CAT = "授标或短名单"
OPEN_CATS = tuple(c for c in brief.PROCUREMENT if c != AWARD_CAT)  # 招标、意向书、资格预审、总采购公告、特定采购公告
EXCERPT_CHARS = 6000
CACHE_NAME = "tender_elig.json"
ELIG_FIELDS = (("qualification", "资格与业绩要求"), ("jv", "联合体"), ("bid_security", "投标保证金"), ("docs", "文件获取与开标"))
ELIG_SYSTEM = ("你是国际工程招标文件分析员。下面是一份水务招标或采购公告的原文摘录,请只根据原文抽出投标人最关心的四项,用简洁中文写,"
               "原文没写的填「原文未见」,不要推测:资格与业绩要求(类似业绩、营业额、财务要求等);联合体是否允许;投标保证金金额或形式;"
               "招标文件怎么获取(费用、方式)与开标时间。")
ELIG_GUIDE = '\n输出格式:只输出一个 JSON 对象 {"qualification": "…", "jv": "…", "bid_security": "…", "docs": "…"},不要任何其他文字。\n'
ELIG_SCHEMA = {"type": "object", "properties": {k: {"type": "string"} for k, _ in ELIG_FIELDS}, "required": [k for k, _ in ELIG_FIELDS]}
# 没配模型时按关键句抽:每项的关键词(小写比对),取含关键词的前两句原文
ELIG_RULES = {
    "qualification": ("turnover", "similar", "experience", "qualification", "completed", "financial", "competitive procurement", "open to all"),
    "jv": ("joint venture", "consortium", " jv"),
    "bid_security": ("bid security", "bid-securing", "bid securing", "guarantee", "security declaration"),
    "docs": ("may be obtained", "may be purchased", "non-refundable", "nonrefundable", "fee of", "will be opened", "bid opening", "opened in the presence",
             "must be delivered", "deadline for"),
}
_SENT = re.compile(r"(?<=[.;!?。;])\s+")


# ---------- 期间 ----------

def window(period: str, today: date) -> tuple[date, date]:
    """期间文字 → (起, 止)。「2026-03-11~2026-04-10」照填;其余(含空)= 到今天的近 30 天。"""
    m = PERIOD_RE.search(period or "")
    if m:
        try:
            a, z = date.fromisoformat(m.group(1)), date.fromisoformat(m.group(2))
            if a <= z:
                return a, z
        except ValueError:
            pass
    return today - timedelta(days=DEFAULT_DAYS - 1), today


def period_text(iso: str, start: date, end: date) -> str:
    return f"{iso} {start.isoformat()}~{end.isoformat()}"


# ---------- 取数 ----------

def _day(ctx: Ctx, v: object) -> str:
    return tstore.day(v, ctx.base.tz) or ""


def _country_leads(ctx: Ctx, iso: str) -> list[dict]:
    b = ctx.base
    return [r for r in b.rows("lead", filter=b.filter(b.cond("lead", "relevant", "is", True)))
            if ctx.iso_of(tstore.link_id(b.get("lead", r, "country"))) == iso]


def _deadlines(ctx: Ctx, lead_rids: set[str]) -> dict[str, list[tuple[str, str, str]]]:
    """线索 → [(日期, 类型, 说明)],只取采购截止类节点,按日期升序。"""
    b = ctx.base
    out: dict[str, list[tuple[str, str, str]]] = {}
    for r in b.rows("milestone"):
        lid = tstore.link_id(b.get("milestone", r, "lead"))
        kind = str(b.get("milestone", r, "kind") or "")
        d = _day(ctx, b.get("milestone", r, "day"))
        if lid in lead_rids and kind in DEADLINE_KINDS and d:
            out.setdefault(lid, []).append((d, kind, str(b.get("milestone", r, "label") or "")))
    for v in out.values():
        v.sort()
    return out


def _published(ctx: Ctx, r: dict) -> str:
    b = ctx.base
    return _day(ctx, b.get("lead", r, "published")) or _day(ctx, b.get("lead", r, "found_at"))


def _awards_between(ctx: Ctx, iso: str, start: date, end: date) -> list[dict]:
    b = ctx.base
    if "award" not in b.t:
        return []
    out = []
    for r in b.rows("award"):
        if ctx.iso_of(tstore.link_id(b.get("award", r, "country"))) != iso:
            continue
        d = _day(ctx, b.get("award", r, "awarded"))
        if start.isoformat() <= d <= end.isoformat():
            out.append(r)
    out.sort(key=lambda r: _day(ctx, b.get("award", r, "awarded")), reverse=True)
    return out


# ---------- 资格要求 ----------

def _rules(text: str) -> dict[str, str]:
    sents = [s.strip() for s in _SENT.split(re.sub(r"\s+", " ", text or "")) if s.strip() and len(s.strip()) <= 400]  # 没标点的大段(表格、抬头)不算句子
    out = {}
    for key, words in ELIG_RULES.items():
        pats = [re.compile(r"\b" + re.escape(w.strip()) + r"\b", re.I) for w in words]
        hits = [s for s in sents if any(p.search(s) for p in pats)]
        out[key] = " ".join(h[:220] + ("…" if len(h) > 220 else "") for h in hits[:2]) or "原文未见"
    return out


def eligibility(ctx: Ctx, leads: list[dict]) -> dict[str, dict]:
    """→ {线索号: {qualification, jv, bid_security, docs, how}}。how = 模型 / 规则;缓存在本机,线索号相同不再抽。"""
    b = ctx.base
    fp = ctx.cache.root.parent / CACHE_NAME
    try:
        done: dict[str, dict] = json.loads(fp.read_text(encoding="utf-8")) if fp.exists() else {}
    except (OSError, ValueError):
        done = {}
    clf = ctx.clf
    use_model = clf is not None and getattr(clf, "enabled", False) and hasattr(clf, "ask")
    changed = False
    for r in leads:
        key = str(b.get("lead", r, "key") or "")
        if not key or key in done and (done[key].get("how") == "模型" or not use_model):
            continue
        text = str(b.get("lead", r, "excerpt") or "")[:EXCERPT_CHARS]
        got = None
        if use_model and text.strip():
            title = str(b.get("lead", r, "title") or "")
            out = clf.ask(ELIG_SYSTEM, f"标题:{title}\n\n原文摘录:\n{text}", ELIG_SCHEMA, ELIG_GUIDE)
            if out and not out.get("_error"):
                got = {k: str(out.get(k) or "原文未见").strip()[:300] for k, _ in ELIG_FIELDS}
                got["how"] = "模型"
            else:
                ctx.log(f"  ! 资格要求抽取没成功({key}),改按规则:{(out or {}).get('_error') or '模型未返回'}")
        if got is None:
            got = {**_rules(text), "how": "规则"}
        done[key] = got
        changed = True
    if changed:
        try:
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(json.dumps(done, ensure_ascii=False, indent=0), encoding="utf-8")
        except OSError:
            pass
    return done


# ---------- 排版 ----------

class _Refs:
    """角标:同一网址一个编号;正文里 ^[n](<url>)^ 是上标超链接(pandoc → Word 真超链接),文末列全。"""

    def __init__(self) -> None:
        self.urls: list[tuple[str, str]] = []
        self._n: dict[str, int] = {}

    def mark(self, url: str, title: str = "", orig: str = "") -> str:
        url = (url or "").strip()
        if not url.startswith(("http://", "https://")):
            return ""
        if url not in self._n:
            self.urls.append((url, title, orig))
            self._n[url] = len(self.urls)
        return f"^[{self._n[url]}](<{url}>)^"

    def chapter(self) -> str:
        """文末一行一条:角标号、网站、中文名(可点)、原文名,打印出来也能核。"""
        from .reports import _host, _md_text
        if not self.urls:
            return "(无)\n"
        return "\n".join(f"{i}. {_host(u)}:[{_md_text((t or u)[:90])}](<{u}>)" + (f"(原文:{_md_text(o[:110])}{'…' if len(o) > 110 else ''})" if o and o != t else "")
                         for i, (u, t, o) in enumerate(self.urls, 1)) + "\n"


def _amount(ctx: Ctx, r: dict) -> str:
    b = ctx.base
    raw = str(b.get("lead", r, "amount") or "").strip()
    if not raw:
        v, cur = b.get("lead", r, "amount_value"), b.get("lead", r, "currency")
        if v and cur:
            raw = f"{float(v):,.0f} {cur}"
    return brief._amount(raw, b.get("lead", r, "amount_cny")) if raw else ""


def _price(ctx: Ctx, r: dict) -> str:
    b = ctx.base
    v, cur = b.get("award", r, "price"), b.get("award", r, "currency")
    if not v:
        return ""
    return brief._amount(f"{float(v):,.0f} {cur or ''}".strip(), b.get("award", r, "price_cny"))


def _days_left(d: str, asof: date) -> str:
    try:
        n = (date.fromisoformat(d[:10]) - asof).days
    except ValueError:
        return ""
    return "今天截止" if n == 0 else f"剩 {n} 天"


def _title_cell(ctx: Ctx, r: dict, zh_of: dict[str, str], refs: _Refs) -> str:
    """表格里只放中文名 + 角标(表格窄,原题放到文末「原公告链接」里,点角标也能看到)。"""
    from .reports import _lead_title, _orig_title
    cell, _ = _lead_title(ctx, r, zh_of)
    url = str(ctx.base.get("lead", r, "url") or "")
    m = re.match(r"\[(.*?)\]\(<[^>]*>\)(.*)$", cell, re.S)  # 去掉标题本身的链接,改成角标(标题文字留着)
    if m:
        cell = m.group(1) + m.group(2)
    name = cell.split("(原文:", 1)[0]
    return name + refs.mark(url, name, _orig_title(ctx, r))


def _source_name(ctx: Ctx, r: dict) -> str:
    from .reports import _cell, _host
    s = _cell(ctx.base.get("lead", r, "source"))
    return s or _host(str(ctx.base.get("lead", r, "url") or ""))


# ---------- 报告 ----------

def build(ctx: Ctx, iso: str, start: date, end: date) -> tuple[str, set[str], set[str], str]:
    """→ (Markdown, 引用的线索 rid, 写进的商机 rid, 文件名用的范围)。「截至」= 期间止日(自建的上周报告也按上周日算剩余天数)。"""
    from .reports import _cell, _compose, _known, _table, translate_titles
    b = ctx.base
    name = geo.name_zh(iso) or iso
    leads = _country_leads(ctx, iso)
    dl = _deadlines(ctx, {r["id"] for r in leads})
    g = lambda r, p: b.get("lead", r, p)  # noqa: E731
    s, e = start.isoformat(), end.isoformat()

    def deadline_of(r: dict) -> str:
        ds = dl.get(r["id"]) or []
        future = [x for x in ds if x[0] >= e]
        if future:
            return future[0][0]
        nd = _day(ctx, g(r, "nearest_deadline"))
        return nd if nd >= e else (ds[-1][0] if ds else nd)

    proc = [r for r in leads if str(g(r, "category") or "") in OPEN_CATS]
    open_ = sorted((r for r in proc if deadline_of(r) >= e), key=deadline_of)
    new = sorted((r for r in proc if s <= _published(ctx, r) <= e), key=lambda r: _published(ctx, r), reverse=True)
    closed = sorted((r for r in proc if (dl.get(r["id"]) and s <= dl[r["id"]][-1][0] <= e and deadline_of(r) < e)), key=lambda r: dl[r["id"]][-1][0])
    awarded_leads = sorted((r for r in leads if str(g(r, "category") or "") == AWARD_CAT and s <= _published(ctx, r) <= e),
                           key=lambda r: _published(ctx, r), reverse=True)
    awards = _awards_between(ctx, iso, start, end)
    elig = eligibility(ctx, open_)
    refs = _Refs()
    zh_of = translate_titles(ctx, open_ + new + closed + awarded_leads)
    funders = sorted({str(g(r, "funder") or "").strip() for r in open_ if g(r, "funder")})
    soonest = deadline_of(open_[0]) if open_ else ""
    overview = (f"截至 {e},{name}在招的水务标段 {len(open_)} 个" + (f",最近的 {soonest} 截止({_days_left(soonest, end)})" if soonest else "")
                + (f",出资方:{'、'.join(funders)}" if funders else "")
                + f";本期({s} 至 {e})新发布 {len(new)} 个、已截止等结果 {len(closed)} 个、授标 {len(awarded_leads) + len(awards)} 项。\n\n"
                + "点每条后面的角标数字可打开原公告核对;「剩几天」按 " + e + " 算。\n")

    def lead_row(r: dict, with_deadline: bool = True) -> list[object]:
        d = deadline_of(r) if with_deadline else (dl.get(r["id"]) or [("", "", "")])[-1][0]
        kinds = "、".join(dict.fromkeys(k for _, k, _ in dl.get(r["id"]) or []))
        when = (f"{d}({_days_left(d, end)})" if with_deadline else f"{d}({kinds})") if d else "未抽到截止日"
        return [when, _title_cell(ctx, r, zh_of, refs), g(r, "category"), g(r, "funder") or "", _amount(ctx, r), _published(ctx, r), g(r, "key")]

    def award_row(r: dict) -> list[object]:
        ga = lambda p: b.get("award", r, p)  # noqa: E731
        zh = (str(ga("name_zh") or "").strip() or str(ga("name") or "").strip())[:80]
        title = zh + refs.mark(str(ga("url") or ""), zh, str(ga("name") or "").strip())
        winner = str(ga("winner") or "") + (f"({ga('winner_country')})" if ga("winner_country") else "")
        return [_day(ctx, ga("awarded")), title, ga("sector"), winner, _price(ctx, r), (ga("funder") or "") + (f"({ga('project')})" if ga("project") else "")]

    def award_lead_row(r: dict) -> list[object]:
        return [_published(ctx, r), _title_cell(ctx, r, zh_of, refs), "、".join(g(r, "sectors") or []), "", _amount(ctx, r), g(r, "funder") or "",
                str(g(r, "summary_zh") or "")[:80]]

    head = ["截止(剩余)", "标段(点角标看原公告)", "类别", "出资方", "金额", "发布", "线索号"]
    chapters = [
        ("在招标段(截止未到)", overview + "\n" + _table(head, [lead_row(r) for r in open_])),
        ("本期新发布", _table(head, [lead_row(r) for r in new])),
        ("本期已截止、等结果", "截止日在本期内、还没见到授标结果的标段:\n\n" + _table(["截止(类型)", *head[1:]], [lead_row(r, with_deadline=False) for r in closed])),
        ("本期授标", "授标、短名单公告(线索):\n\n" + _table(["日期", "标段(点角标看原公告)", "领域", "中标单位", "金额", "出资方", "摘要"], [award_lead_row(r) for r in awarded_leads])
         + "\n已中标项目(开发银行、采购网的合同授予记录):\n\n" + _table(["签约", "合同(点角标看来源)", "类别", "中标单位(国别)", "中标价", "资金来源(所属项目)"], [award_row(r) for r in awards])),
        ("各标段资格要求", _elig_chapter(ctx, open_, elig, zh_of, refs)),
        ("覆盖说明", _coverage(ctx, iso, leads, open_, elig, start, end)),
        ("原公告链接", refs.chapter()),
    ]
    title = f"{name}水务招标监测({s} 至 {e})"
    opps = _opps_of(ctx, open_ + new + closed)
    md = _compose(title, "招标监测", ctx, chapters, _known(ctx, leads, opps))
    cited = {r["id"] for r in open_ + new + closed + awarded_leads}
    tail = e[5:].replace("-", "") if e[:4] == s[:4] else e.replace("-", "")
    return md, cited, {r["id"] for r in opps}, f"{iso}_{s.replace('-', '')}-{tail}"  # 文件名:UZB_20260401-0501


def _opps_of(ctx: Ctx, leads: list[dict]) -> list[dict]:
    b = ctx.base
    rids = {tstore.link_id(b.get("lead", r, "opportunity")) for r in leads} - {None, ""}
    return [r for r in b.rows("opportunity") if r["id"] in rids] if rids else []


def _elig_chapter(ctx: Ctx, open_: list[dict], elig: dict[str, dict], zh_of: dict[str, str], refs: _Refs) -> str:
    from .reports import _table
    b = ctx.base
    if not open_:
        return "(无在招标段)\n"
    hows = {elig.get(str(b.get("lead", r, "key") or ""), {}).get("how", "") for r in open_}
    how = "模型从原文摘录里抽出、人工请以原文为准" if hows == {"模型"} else ("按关键句规则从原文摘录里抽的英文原句(没配模型),请点角标看原文" if hows == {"规则"}
                                                         else "部分由模型抽、部分按关键句规则抽(英文原句)")
    opp_basis = {}
    for r in open_:
        oid = tstore.link_id(b.get("lead", r, "opportunity"))
        if oid:
            o = b.record("opportunity", oid)
            basis = str(b.get("opportunity", o, "eligibility_basis") or "").strip()
            if basis:
                opp_basis[r["id"]] = f"{b.get('opportunity', o, 'eligibility') or ''} {basis}".strip()
    from .reports import _md_text
    parts = [f"抽法:{how}。\n"]
    for r in open_:  # 一个标段一段,一行一项(六列的表在 Word 里挤不下)
        it = elig.get(str(b.get("lead", r, "key") or ""), {})
        parts.append(f"**{_title_cell(ctx, r, zh_of, refs)}**\n")
        lines = [f"- {label}:{_md_text(str(it.get(key) or '原文未见'))}" for key, label in ELIG_FIELDS]
        if opp_basis.get(r["id"]):
            lines.append(f"- 商机表里的资格判断:{_md_text(opp_basis[r['id']])}")
        parts.append("\n".join(lines) + "\n")
    return "\n".join(parts)


def _coverage(ctx: Ctx, iso: str, leads: list[dict], open_: list[dict], elig: dict[str, dict], start: date, end: date) -> str:
    from collections import Counter
    b = ctx.base
    srcs = Counter(_source_name(ctx, r) for r in leads)
    name = geo.name_zh(iso) or iso
    lines = [
        f"- 范围:只看水务(供水、污水、水利、海水淡化、雨水排涝、水环境);{name}库里相关线索 {len(leads)} 条,来自 {len(srcs)} 个信源:"
        + ("、".join(f"{k} {v}" for k, v in srcs.most_common(8)) or "无") + "。",
        f"- 「在招」= 有采购截止节点且截止日不早于 {end.isoformat()};没有抽到截止日的公告不在第一章,在「本期新发布」里按发布日期列。",
        "- 金额按原文记,括号里是折算人民币(万元,当日汇率,A21 任务算的);没折算的只有原文。",
        "- 资格要求:" + ("模型抽取" if all(v.get("how") == "模型" for v in (elig.get(str(b.get("lead", r, "key") or ""), {}) for r in open_)) and open_ else "关键句规则抽取(英文原句)")
        + ",只作初筛,投标前以招标文件为准。",
        "- 没覆盖:库里没收的信源(各国采购网未接入的、要登录的门户)、只在招标文件里写的条款;AFD、亚开行等门户的公告以库里采到的为限。",
    ]
    return "\n".join(lines) + "\n"


__all__ = ["build", "window", "period_text", "eligibility", "DEFAULT_DAYS"]
