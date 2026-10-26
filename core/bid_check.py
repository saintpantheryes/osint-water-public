"""投标评估(2026-10-18 使用方:「发一个项目链接,助手能否直接给出是否可以投标的结论,并附一份项目报告」):

看一条链接、一份文件,或清单里的一条,按八项检查给三档结论,每条理由带依据链接;另出 Word 评估报告。
使用方定的四步:信源是否真实、是否在国别范围内、招标要求是否满足、时间是否满足;这里再补四项:是不是水务、阶段能不能投、
库里有没有同一项目、我方在该国的基础。

    assess(facts, today, records, scope, lib) → {
        "verdict": 建议投标 | 有条件可投 | 不建议投标,
        "summary": 一句话(结论 + 主要原因),
        "checks": [{"key", "name", "status": 通过 | 有条件 | 不通过 | 不明, "why", "evidence": [{"name", "url"}]}],
        "blockers": [硬伤], "conditions": [缺什么], "qualify": 资格对照(link_check.qualify 的结果)或 None,
    }
    report_markdown(result, today) → 单条的评估报告(Markdown,角标链接);list_report_markdown(result, today) → 清单的总体报告。

结论规则:任一项「不通过」→ 不建议投标;否则有「有条件」或关键项「不明」(信源、国别、阶段、时间、资格)→ 有条件可投;全过 → 建议投标。
只按正文、库里和公开渠道查到的东西判,判不了的写「不明」并说明要查什么,不把「先核实」推给人。
"""
from __future__ import annotations

import re
from datetime import date

from core import geo
from core.markets import Markets

VERDICTS = ("建议投标", "有条件可投", "不建议投标")
STATUS = ("通过", "有条件", "不通过", "不明")
CHECKS = (("sector", "是不是水务项目"), ("source", "信源是否真实"), ("scope", "是否在国别范围内"), ("stage", "阶段能不能投"),
          ("time", "时间是否来得及"), ("quals", "招标要求是否满足"), ("library", "库里有没有同一项目"), ("presence", "我方在该国的基础"))
KEY_CHECKS = {"sector", "source", "scope", "stage", "time", "quals"}  # 这几项「不明」也算有条件;库里、基础只是说明
WATER = {"供水", "污水", "水利", "海水淡化", "雨水排涝", "水环境"}
NO_BID_STAGES = {"授标", "在建", "建成"}
EARLY_STAGES = {"立项或规划", "融资"}
BID_CATS = {"招标", "意向书", "资格预审", "总采购公告", "特定采购公告"}
TIGHT_DAYS = 21  # 截止日不到三周算紧(施工标备标一般四到六周;意向书、资格预审要的材料少,写明)
SHORT_CATS = {"意向书", "资格预审", "总采购公告"}

# 金额:「12.3 million USD」「USD 200 million」「1.5 亿美元」「EUR 4.2m」→ (数值, 币种);折算给 money_line
_CUR = [("USD", r"美元|美金|usd|us\$|\$"), ("EUR", r"欧元|eur|€"), ("GBP", r"英镑|gbp|£"), ("JPY", r"日元|jpy|¥"), ("MAD", r"迪拉姆|dirhams?|mad"),
        ("KGS", r"索姆|kgs"), ("AZN", r"马纳特|manats?|azn"), ("KZT", r"坚戈|tenge|kzt"), ("UZS", r"苏姆|soums?|uzs"), ("EGP", r"埃及镑|egp"),
        ("SAR", r"沙特里亚尔|sar"), ("AED", r"迪拉姆|aed"), ("XOF", r"西非法郎|fcfa|xof"), ("TRY", r"里拉|try"), ("INR", r"卢比|inr"), ("CNY", r"人民币|rmb|cny|元")]
_MULT = {"千万": 1e7, "百万": 1e6, "万": 1e4, "亿": 1e8, "千": 1e3, "million": 1e6, "mn": 1e6, "mln": 1e6, "m": 1e6, "billion": 1e9, "bn": 1e9, "b": 1e9, "k": 1e3, "thousand": 1e3}
_NUM = r"(\d+(?:[.,]\d+)*)\s*(千万|百万|万|亿|千|million|mln|mn|billion|bn|thousand|m(?![a-z³3²])|bn|b(?![a-z])|k(?![a-z]))?"
_CURS = "|".join(p for _, p in _CUR)
_MONEY_AFTER = re.compile(_NUM + r"\s*(" + _CURS + r")(?![a-z])", re.IGNORECASE)
_MONEY_BEFORE = re.compile(r"(" + _CURS + r")\s*" + _NUM, re.IGNORECASE)


def parse_money(text: object) -> tuple[float, str] | None:
    """金额文字 → (数值, 币种);认不出(只有规模、没币种)→ None。"""
    t = str(text or "").strip()
    if not t:
        return None
    m = _MONEY_AFTER.search(t)
    if m:
        num, mult, cur = m.group(1), m.group(2), m.group(3)
    else:
        m = _MONEY_BEFORE.search(t)
        if not m:
            return None
        cur, num, mult = m.group(1), m.group(2), m.group(3)
    try:
        v = float(num.replace(",", "")) * _MULT.get((mult or "").lower(), 1)
    except ValueError:
        return None
    code = next((c for c, p in _CUR if re.fullmatch(p, cur, re.IGNORECASE)), None)
    return (v, code) if code and v > 0 else None


def _rate(cur: str) -> float:
    from core import assistant
    return assistant._rate(cur)


def _wan(v: float) -> str:
    return f"{v / 1e4:,.0f} 万"


def money_line(text: object) -> str:
    """原文金额 + 折算:「12.3 million USD(≈ 1,230 万美元,≈ 8,795 万元人民币)」;认不出币种的原样给。"""
    raw = str(text or "").strip()
    p = parse_money(raw)
    if not p:
        return raw
    v, cur = p
    cny = v * _rate(cur)
    usd = cny / _rate("USD")
    parts = []
    if cur != "USD":
        parts.append(f"≈ {_wan(usd)}美元")
    if cur != "CNY":
        parts.append(f"≈ {_wan(cny)}元人民币")
    return raw + ("(" + ",".join(parts) + ")" if parts else "")


# ---------- 国别范围 ----------

class Scope:
    """39 个市场国别:来自市场分布表(私有配置,带主责、当地机构)或库里国别表的「市场分级」。rows: iso3 → {name, region, lead, presence}。"""

    def __init__(self, rows: dict[str, dict] | None, org: str = "本单位", source: str = ""):
        self.rows = rows or {}
        self.org = org
        self.source = source

    @classmethod
    def from_markets(cls, m: Markets | None) -> "Scope":
        if m is None:
            return cls(None)
        return cls({c.iso3: {"name": c.name, "region": c.region, "lead": c.lead, "presence": c.presence(),
                             "any_presence": bool(c.local_entity or c.projects or c.staff)} for c in m.countries}, m.org, m.name)

    @classmethod
    def from_db(cls, db) -> "Scope":
        """库里国别表:市场分级 ∈ 重点、市场 的算在范围内;「我方在当地」照抄。"""
        rows: dict[str, dict] = {}
        props = [p for p in ("key", "tier", "market", "presence", "name_zh") if db.has("country", p)]
        for r in db.rows("country", props=props):
            g = lambda p: db.get("country", r, p) if p in props else None  # noqa: E731
            if str(g("tier") or "") not in ("重点", "市场"):
                continue
            iso = str(g("key") or "").upper()
            pres = str(g("presence") or "").strip()
            rows[iso] = {"name": str(g("name_zh") or geo.name_zh(iso)), "region": str(g("market") or ""), "lead": None, "presence": pres,
                         "any_presence": bool(pres) and not pres.startswith("未设")}
        return cls(rows, source="国别表的市场分级")

    def known(self) -> bool:
        return bool(self.rows)

    def get(self, iso: str | None) -> dict | None:
        return self.rows.get(iso or "")


# ---------- 八项检查 ----------

def _ev(name: str | None, url: str | None) -> list[dict]:
    return [{"name": (name or url or "")[:120], "url": url}] if url else []


def _days(deadline: str | None, today: date) -> int | None:
    try:
        return (date.fromisoformat(str(deadline)[:10]) - today).days if deadline else None
    except ValueError:
        return None


def assess(facts: dict, today: date, records: dict[str, list[dict]] | None, scope: Scope | None, lib: dict | None) -> dict:
    """facts(由 link_check 归一):title, url, country(ISO3), sectors, stage, category, deadline, requirements, credibility{level, reason},
    source{level, label, domain}, original{title, url, kind}, sure, outside[{title, url, kind}], verify_links[{name, url}], pros, cons。
    lib:same_url / in_library / related_leads / related_opps。records:link_check.our_records(db)。"""
    from core.link_check import qualify

    checks: list[dict] = []
    iso = facts.get("country")
    url, title = facts.get("url"), facts.get("title") or ""
    src_ev = _ev(title, url)

    # 1 水务
    sectors = [s for s in (facts.get("sectors") or []) if s]
    water = [s for s in sectors if s in WATER]
    if water:
        checks.append({"key": "sector", "status": "通过", "why": f"领域:{'、'.join(water)}。", "evidence": src_ev})
    elif sectors:
        checks.append({"key": "sector", "status": "不通过", "why": f"不是水务项目(领域:{'、'.join(sectors)})。", "evidence": src_ev})
    else:
        checks.append({"key": "sector", "status": "不明", "why": "正文里没看出领域;按水务项目往下看。", "evidence": src_ev})

    # 2 信源
    src, cred = facts.get("source") or {}, facts.get("credibility") or {}
    original, sure = facts.get("original"), bool(facts.get("sure"))
    lvl = src.get("level")
    if sure and original and original.get("url"):
        checks.append({"key": "source", "status": "通过", "why": f"找到了原公告({original.get('kind') or '官方渠道'}),信源可信。",
                       "evidence": _ev(original.get("title"), original.get("url"))})
    elif lvl == "高" and cred.get("level") != "低":
        checks.append({"key": "source", "status": "通过", "why": f"{src.get('label') or '官方或开发银行网站'}(一手公告)。" + (cred.get("reason") or ""), "evidence": src_ev})
    else:
        corroborated = [x for x in (facts.get("outside") or []) if not str(x.get("kind") or "").startswith("新闻")]
        news = [x for x in (facts.get("outside") or []) if str(x.get("kind") or "").startswith("新闻")]
        if lvl == "中" and corroborated:
            checks.append({"key": "source", "status": "通过", "why": f"{src.get('label') or '媒体'}转述,世行渠道里有对得上的项目。", "evidence": src_ev + _ev(corroborated[0].get("title"), corroborated[0].get("url"))})
        else:
            who = src.get("label") or ("上传的文件" if not url else "其他网站")
            why = f"{who},可信度{cred.get('level') or lvl or '待核实'};助手在世行采购网" + ("和新闻里找了,只有新闻旁证" if news else "和近三个月新闻里都没找到原公告") + "。投标前要拿到原公告或招标文件。"
            ev = src_ev + (_ev(news[0].get("title"), news[0].get("url")) if news else []) + [{"name": x["name"], "url": x["url"]} for x in (facts.get("verify_links") or [])[:3]]
            checks.append({"key": "source", "status": "有条件", "why": why, "evidence": ev})

    # 3 国别范围
    if not iso:
        checks.append({"key": "scope", "status": "不明", "why": "正文里没认出项目所在国,请补充国别。", "evidence": []})
    elif scope is None or not scope.known():
        checks.append({"key": "scope", "status": "不明", "why": f"{geo.name_zh(iso)}:没有配置市场分布表,查不了国别范围。", "evidence": []})
    elif (row := scope.get(iso)):
        lead = row.get("lead")
        why = f"{row['name']}在市场国别内({row['region']})" + ("," + f"{scope.org}是主责单位" if lead else (";集团清单里该国不归本单位主责,投标前先确认归口" if lead is False else "")) + "。"
        checks.append({"key": "scope", "status": "有条件" if lead is False else "通过", "why": why, "evidence": []})
    else:
        checks.append({"key": "scope", "status": "不通过", "why": f"{geo.name_zh(iso)}不在市场国别内(忽略国别),不投。", "evidence": []})

    # 4 阶段
    stage, cat = facts.get("stage"), facts.get("category")
    if stage in NO_BID_STAGES or cat == "授标或短名单":
        checks.append({"key": "stage", "status": "不通过", "why": f"项目已到「{stage or cat}」,不是投标机会;可作分包或供货线索看。", "evidence": src_ev})
    elif stage in EARLY_STAGES and cat not in BID_CATS:
        checks.append({"key": "stage", "status": "有条件", "why": f"项目在「{stage}」阶段,还没招标;先入库跟踪,等采购公告再投。", "evidence": src_ev})
    elif stage == "招标" or cat in BID_CATS:
        checks.append({"key": "stage", "status": "通过", "why": f"正在采购({cat or stage})。", "evidence": src_ev})
    else:
        checks.append({"key": "stage", "status": "不明", "why": "正文没写到哪一步;按招标中看,投标前核对。", "evidence": src_ev})

    # 5 时间
    dl, days = facts.get("deadline"), _days(facts.get("deadline"), today)
    if days is None:
        checks.append({"key": "time", "status": "不明", "why": "正文没写截止日(或没抽到);截止日以招标文件为准。", "evidence": src_ev})
    elif days < 0:
        checks.append({"key": "time", "status": "不通过", "why": f"已于 {dl} 截止(过了 {-days} 天)。", "evidence": src_ev})
    elif days < TIGHT_DAYS:
        soft = cat in SHORT_CATS
        checks.append({"key": "time", "status": "有条件", "why": f"{dl} 截止,只剩 {days} 天" + (f";{cat}要的材料少,抓紧能赶上。" if soft else ";施工标备标一般要四到六周,要马上决定、集中人手。"), "evidence": src_ev})
    else:
        checks.append({"key": "time", "status": "通过", "why": f"{dl} 截止,还有 {days} 天。", "evidence": src_ev})

    # 6 招标要求
    req = facts.get("requirements")
    qa = None
    n_ours = len((records or {}).get("本单位") or [])
    if records is None or not n_ours:
        checks.append({"key": "quals", "status": "不明", "why": "公司资料里没有本单位业绩,对照不了;请在「我方资料」里补业绩。", "evidence": src_ev})
    else:
        qa = qualify(req, water or sectors, records, today)
        status = {"满足": "通过", "不满足": "不通过", "不确定": "不明"}[qa["verdict"]]
        why = qa["why"] + ("".join(" " + g + "。" for g in qa["gaps"]) if qa["gaps"] else "")
        checks.append({"key": "quals", "status": status, "why": why, "evidence": src_ev})

    # 7 库里
    lib = lib or {}
    have = (lib.get("same_url") or []) + (lib.get("in_library") or [])
    opps = lib.get("related_opps") or []
    if have:
        r = have[0]
        why = "库里已有这一条" + (f",已归入商机「{r.get('opportunity')}」" if r.get("opportunity") else "") + "。"
        checks.append({"key": "library", "status": "通过", "why": why, "evidence": _ev(r.get("title"), r.get("url"))})
    elif opps:
        gone = [o for o in opps if str(o.get("stage") or "") in ("放弃", "未中标")]
        why = "库里有相关商机:" + "、".join(f"「{o.get('name_zh') or o.get('code')}」({o.get('stage') or '阶段不详'})" for o in opps[:3]) + "。" + ("之前已放弃或未中标,再投前看跟进记录。" if gone else "")
        checks.append({"key": "library", "status": "有条件" if gone else "通过", "why": why, "evidence": []})
    elif lib.get("related_leads") or lib.get("related"):
        n = len(lib.get("related_leads") or lib.get("related") or [])
        checks.append({"key": "library", "status": "不明", "why": f"库里有 {n} 条可能相关的线索(同项目别的标段或同名地名),没有这一条。", "evidence": []})
    else:
        checks.append({"key": "library", "status": "不明", "why": "库里没有同一项目,是新线索。", "evidence": []})

    # 8 我方基础
    row = scope.get(iso) if scope is not None and iso else None
    if row and row.get("presence"):
        checks.append({"key": "presence", "status": "通过" if row.get("any_presence") else "有条件", "why": f"{scope.org}在{row['name']}:{row['presence']}。" + ("" if row.get("any_presence") else "没有当地机构和在建项目,投标资源要从总部派。"), "evidence": []})
    else:
        checks.append({"key": "presence", "status": "不明", "why": "没有我方在该国的资料。", "evidence": []})

    names = dict(CHECKS)
    for c in checks:
        c["name"] = names[c["key"]]
    blockers = [c for c in checks if c["status"] == "不通过"]
    conds = [c for c in checks if c["status"] == "有条件" or (c["status"] == "不明" and c["key"] in KEY_CHECKS)]
    if blockers:
        verdict = "不建议投标"
        summary = f"不建议投标:{';'.join(_short(c) for c in blockers)}。"
    elif conds:
        verdict = "有条件可投"
        summary = f"有条件可投,还缺:{';'.join(_short(c) for c in conds)}。"
    else:
        verdict = "建议投标"
        summary = "建议投标:信源可信、在国别范围内、正在招标、时间来得及、业绩够门槛。"
    return {"verdict": verdict, "summary": summary, "checks": checks, "blockers": [_short(c) for c in blockers], "conditions": [_short(c) for c in conds],
            "qualify": qa, "days_left": days}


def _short(c: dict) -> str:
    """检查项的一句话(结论里用):名称 + 原因第一句。"""
    why = re.split(r"[;;。]", c["why"], maxsplit=1)[0]
    return f"{c['name']}:{why}"


# ---------- 报告(Markdown,角标链接;Word 由 core.watch.reporting.to_docx_styled 转) ----------

class Refs:
    """角标:同一网址一个编号;^[n](<url>)^ 经 pandoc 成为上标超链接;文末列全。"""

    def __init__(self) -> None:
        self.urls: list[tuple[str, str]] = []
        self._n: dict[str, int] = {}

    def mark(self, url: str | None, name: str = "") -> str:
        url = (url or "").strip()
        if not url.startswith(("http://", "https://")):
            return ""
        if url not in self._n:
            self.urls.append((url, name))
            self._n[url] = len(self.urls)
        return f"^[{self._n[url]}](<{url}>)^"

    def chapter(self) -> str:
        if not self.urls:
            return "(无)\n"
        return "\n".join(f"{i}. [{_md(n or u)[:100]}](<{u}>)  {_host(u)}" for i, (u, n) in enumerate(self.urls, 1)) + "\n"


def _md(s: str) -> str:
    return re.sub(r"([\\`*_\[\]<>#|])", r"\\\1", str(s or ""))


def _host(url: str) -> str:
    m = re.match(r"https?://(?:www\.)?([^/?#]+)", url)
    return m.group(1) if m else ""


def _table(head: list[str], body: list[list[object]]) -> str:
    if not body:
        return "(无)\n"
    cell = lambda x: ("" if x is None else str(x)).replace("|", "/").replace("\n", " ").strip() or "—"  # noqa: E731
    return "\n".join(["| " + " | ".join(head) + " |", "|" + "---|" * len(head)] + ["| " + " | ".join(cell(c) for c in r) + " |" for r in body]) + "\n"


def _check_rows(bid: dict, refs: Refs) -> list[list[str]]:
    rows = []
    for c in bid["checks"]:
        marks = "".join(refs.mark(e["url"], e["name"]) for e in c.get("evidence") or [] if e.get("url"))
        rows.append([c["name"], c["status"], _md(c["why"]) + marks])
    return rows


def _facts_of(result: dict) -> dict:
    """单条结果(link_check.check 的返回)→ 报告用的字段。"""
    a = result.get("analysis") or {}
    return {"title_zh": a.get("title_zh") or result.get("title") or "", "title": result.get("title") or "", "url": result.get("final_url") or result.get("url") or "",
            "summary": a.get("summary_zh") or "", "project": a.get("project_name"), "country": (result.get("country") or {}).get("name"),
            "city": a.get("city"), "funder": a.get("funder"), "amount": a.get("amount"), "stage": a.get("stage"), "category": a.get("category"),
            "sectors": "、".join(a.get("sectors") or []), "date": result.get("published"), "deadline": a.get("deadline"), "freshness": a.get("freshness"),
            "pros": a.get("pros") or [], "cons": a.get("cons") or [], "jv": a.get("jv"), "model": a.get("model"), "source": result.get("source") or {}}


def _basics(f: dict, bid: dict, refs: Refs) -> str:
    mark = refs.mark(f["url"], f["title_zh"] or f["title"])
    days = bid.get("days_left")
    rows = [["项目名称", _md(f["title_zh"]) + (f"(原文:{_md(f['title'][:120])})" if f["title"] and f["title"] != f["title_zh"] else "") + mark],
            ["项目简介", _md(f["summary"]) + mark], ["国别 / 城市", "、".join(x for x in (f["country"], f["city"]) if x) or "待核实"],
            ["出资方", _md(f["funder"] or "原文未见")], ["金额或规模", _md(money_line(f["amount"])) if f["amount"] else "原文未见"],
            ["阶段 / 类别", "、".join(x for x in (f["stage"], f["category"]) if x) or "待核实"], ["领域", f["sectors"] or "待核实"],
            ["信息日期", f["date"] or "原文未见"], ["投标截止", (f["deadline"] + (f"(剩 {days} 天)" if days is not None and days >= 0 else f"(已过 {-days} 天)" if days is not None else "")) if f["deadline"] else "原文未见,以招标文件为准"],
            ["联合体", f["jv"] or "原文未见"], ["时效", _md(f["freshness"] or "")]]
    return _table(["项目", "内容(角标回原文)"], rows)


def _quals_chapter(bid: dict, facts: dict | None = None) -> str:
    qa = bid.get("qualify")
    req = (facts or {}).get("requirements") if facts else None
    parts = []
    if req:
        rows = [["单个类似合同金额", f"{req['similar_usd'] / 1e4:,.0f} 万美元" if req.get("similar_usd") else "原文未见"],
                ["年均营业额", f"{req['turnover_usd'] / 1e4:,.0f} 万美元" if req.get("turnover_usd") else "原文未见"],
                ["财务资源", f"{req['financial_usd'] / 1e4:,.0f} 万美元" if req.get("financial_usd") else "原文未见"],
                ["业绩年限", f"近 {int(req['years'])} 年" if req.get("years") else "原文未见"], ["特定资格", _md(req.get("specific") or "原文未见")]]
        parts.append("公告写的门槛:\n\n" + _table(["门槛", "要求"], rows))
    else:
        parts.append("公告正文里没有写资格门槛(或没抽到),以招标文件为准。\n")
    if qa:
        parts.append(f"**对照结果:{qa['verdict']}。**{_md(qa['why'])}\n")
        if qa.get("records"):
            parts.append("对照用的本单位业绩(只比单个类似合同金额):\n\n" + _table(["业绩", "年份", "金额"], [[_md(r["name"]), r["year"] or "年份不详", f"{r['usd'] / 1e4:,.0f} 万美元"] for r in qa["records"]]))
        if qa.get("finance"):
            parts.append("营业额、财务资源(按公司资料里的近三年审计数据):\n\n" + _table(["门槛", "对照"], [[f["item"], ("够。" if f["ok"] else "不够。") + _md(f["text"])] for f in qa["finance"]]))
        if qa.get("siblings"):
            parts.append("兄弟单位够门槛的(仅供参考,不能当本单位业绩):" + "、".join(_md(r["name"]) for r in qa["siblings"]) + "\n")
        if qa.get("gaps"):
            parts.append("系统答不了的:\n\n" + "\n".join(f"- {_md(g)}" for g in qa["gaps"]) + "\n")
    else:
        parts.append("公司资料里没有本单位业绩,没法对照;请先在「我方资料」里补业绩。\n")
    return "\n".join(parts)


def _actions(bid: dict, f: dict) -> str:
    v = bid["verdict"]
    steps = []
    if v == "不建议投标":
        steps.append("不投;" + ("已授标的作分包、供货或下一期的线索跟踪。" if any(c["key"] == "stage" for c in bid["checks"] if c["status"] == "不通过") else "原因见核心结论。"))
        if any(c["key"] == "quals" and c["status"] == "不通过" for c in bid["checks"]):
            steps.append("若公告允许联合体,可评估与够门槛的单位组联合体(兄弟单位业绩只能作参考,须以联合体成员身份出现)。")
    else:
        for c in bid["checks"]:
            if c["status"] in ("有条件", "不明") and c["key"] in KEY_CHECKS:
                steps.append({"source": "拿到原公告或招标文件,核对发布方、编号、截止日。", "scope": "确认国别归口(是否本单位主责)。", "stage": "入库跟踪,等采购公告发布。",
                              "time": "马上决定是否投,确定备标人手与时间表。", "quals": "按招标文件逐条对照资格;营业额、财务资源以财务部出具的数为准。",
                              "sector": "确认工程内容是否水务。"}[c["key"]])
        steps.append("在线索库里收进这一条并挂到商机,指定跟进人;" + ("参加标前会、购买招标文件。" if v == "建议投标" else "条件落实后再投。"))
        steps.append("金额、汇率按投标当日复核;报告里的折算只是参考。")
    return "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1)) + "\n"


def _pros_cons(f: dict, bid: dict) -> str:
    pros = list(f.get("pros") or [])
    cons = list(f.get("cons") or [])
    for c in bid["checks"]:
        if c["key"] == "presence" and c["status"] == "通过":
            pros.append(c["why"])
        if c["key"] == "library" and c["status"] == "通过":
            pros.append(c["why"])
        if c["status"] in ("不通过", "有条件") and c["key"] not in ("presence", "library"):
            cons.append(c["why"])
    out = ["有利因素:\n\n" + ("\n".join(f"- {_md(p)}" for p in pros) if pros else "- (原文没看出)") + "\n",
           "不利因素与风险:\n\n" + ("\n".join(f"- {_md(c)}" for c in cons) if cons else "- (没有发现)") + "\n"]
    return "\n".join(out)


def _library_chapter(result: dict) -> str:
    rows = [[_md(r.get("project_name") or r.get("title") or ""), r.get("country") or "", r.get("found_at") or "", r.get("status") or "", r.get("opportunity") or ""]
            for r in (result.get("same_url") or []) + (result.get("related_leads") or [])]
    opps = [[o.get("code") or "", _md(o.get("name_zh") or ""), o.get("stage") or ""] for o in result.get("related_opps") or []]
    out = []
    out.append("线索:\n\n" + _table(["线索", "国别", "入库", "处理状态", "商机"], rows))
    out.append("商机:\n\n" + _table(["编号", "名称", "阶段"], opps))
    return "\n".join(out)


def _header(title: str, today: date, model: str | None) -> list[str]:
    return [f"# {title}\n", f"生成日期:{today.isoformat()}  编制:助手" + (f"(模型 {model})" if model else "(未配置模型,按规则判)") + "  内部版,只供内部研判\n"]


def report_markdown(result: dict, today: date) -> str:
    """单条链接或文件的投标评估报告:结论在最前,后面是项目情况、八项检查、资格对照、有利不利、库里相关、下一步、依据链接。"""
    bid, f = result["bid"], _facts_of(result)
    refs = Refs()
    parts = _header(f"投标评估:{f['title_zh'] or f['title'] or '未命名项目'}", today, f["model"])
    parts.append("## 核心结论\n\n" + f"**{bid['verdict']}。**{_md(bid['summary'])}\n")
    if bid["conditions"] and bid["verdict"] != "不建议投标":
        parts.append("投标前要落实的:\n\n" + "\n".join(f"- {_md(c)}" for c in bid["conditions"]) + "\n")
    parts.append("## 1. 项目基本情况\n\n" + _basics(f, bid, refs))
    parts.append("## 2. 八项检查\n\n" + _table(["检查", "结果", "说明(角标是依据)"], _check_rows(bid, refs)))
    parts.append("## 3. 招标要求与我方资格对照\n\n" + _quals_chapter(bid, result.get("analysis")))
    parts.append("## 4. 有利与不利因素\n\n" + _pros_cons(f, bid))
    parts.append("## 5. 库里相关线索与商机\n\n" + _library_chapter(result))
    parts.append("## 6. 下一步\n\n" + _actions(bid, f))
    parts.append("## 7. 信源与依据\n\n" + refs.chapter())
    parts.append("## 说明\n\n结论按八项检查的规则给出,只依据原文、线索库和公开渠道查到的内容;资格对照:业绩只比单个类似合同金额,营业额、财务资源按公司资料里的近三年审计数据(没录的要财务部出数);"
                 "金额折算按当日汇率,仅供参考。投标与否由人决定。\n")
    return "\n".join(parts)


def list_report_markdown(result: dict, today: date) -> str:
    """清单型材料的总体报告:结论汇总 → 逐条结论表 → 每条的检查小节 → 依据链接。"""
    lst, a = result["list"], result.get("analysis") or {}
    items = lst["items"]
    refs = Refs()
    counts = {v: sum(1 for it in items if (it.get("bid") or {}).get("verdict") == v) for v in VERDICTS}
    nonwater = sum(1 for it in items if it.get("status") == "非水务")
    parts = _header(f"投标评估(清单):{a.get('title_zh') or result.get('title') or '上传的清单'}", today, a.get("model"))
    parts.append("## 核心结论\n\n" + f"清单共 {len(items)} 条:**建议投标 {counts['建议投标']} 条、有条件可投 {counts['有条件可投']} 条、不建议投标 {counts['不建议投标']} 条**"
                 + (f"(其中非水务 {nonwater} 条)" if nonwater else "") + "。逐条结论见下表,每条的检查与依据见后面各节。\n")
    rows = []
    for i, it in enumerate(items, 1):
        bid = it.get("bid") or {}
        o = it.get("original") if it.get("sure") else None
        url = (o or {}).get("url") or it.get("url")
        name = _md(it.get("title_zh") or it.get("title") or "") + refs.mark(url, it.get("title") or it.get("title_zh"))
        reason = "、".join(bid.get("blockers") or bid.get("conditions") or [])[:160]
        rows.append([i, name, (it.get("country") or {}).get("name") or "", it.get("deadline") or "", bid.get("verdict") or it.get("status") or "", _md(reason)])
    parts.append("## 1. 逐条结论\n\n" + _table(["序", "项目(角标回原公告)", "国别", "截止", "结论", "主要原因"], rows))
    for i, it in enumerate(items, 1):
        bid = it.get("bid")
        if not bid:
            parts.append(f"## {i + 1}. {i}. {_md(it.get('title_zh') or it.get('title') or '')}\n\n{_md(it.get('why') or '')}\n")
            continue
        meta = [x for x in ((it.get("country") or {}).get("name"), it.get("stage"), money_line(it.get("amount")) if it.get("amount") else "", it.get("funder"), f"截止 {it['deadline']}" if it.get("deadline") else "") if x]
        body = [f"**{bid['verdict']}。**{_md(bid['summary'])}\n", (" · ".join(_md(m) for m in meta) + "\n") if meta else "",
                _table(["检查", "结果", "说明(角标是依据)"], _check_rows(bid, refs)), _quals_chapter(bid, it)]
        parts.append(f"## {i + 1}. {i}. {_md(it.get('title_zh') or it.get('title') or '')}\n\n" + "\n".join(x for x in body if x))
    parts.append(f"## {len(items) + 2}. 信源与依据\n\n" + refs.chapter())
    parts.append("## 说明\n\n清单由模型从材料里拆出,原公告先在世界银行采购网找,没找到的再整国核对、查新闻;结论按八项检查的规则给出,只依据查到的内容;"
                 "资格对照:业绩只比单个类似合同金额,营业额、财务资源按公司资料里的近三年审计数据(没录的要财务部出数)。投标与否由人决定。\n")
    return "\n".join(parts)


def to_docx(md: str, out_path) -> None:
    """Markdown → Word(国别报告同一版式;角标成真超链接)。"""
    from pathlib import Path

    from core.watch import reporting
    out_path = Path(out_path)
    md_path = out_path.with_suffix(".md")
    md_path.write_text(md, encoding="utf-8")
    reporting.to_docx_styled(md_path, out_path)
