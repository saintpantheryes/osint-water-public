"""每日简报(日报,A14 的一种;本体 V12、E10):每天采集完成后,把当天的线索按几个维度数一遍,写成几百字的中文 Markdown。

口径与「线索分析」页、首页同一套(R14):线索总数 = 首页「线索」卡;待处理 = 处理状态为待处理;待确认归属 = glossary.to_assign;
跟踪中的商机 = glossary.tracked;资金来源按本体 groupings.funders 归组;采购类 = 本体 lead_category 的「采购类」组。
不依赖模型:统计与规则就能写;ctx.clf 配好时多一段「模型研判」(每条结论标线索号,核对过的才算)。
"""
from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
from statistics import median

from core import ontology as onto_mod
from core import tstore
from core.watch import reporting
from core.zh_name import zh_name

from .common import Ctx

TRACKED_STAGES = set(onto_mod.glossary_stages("tracked"))  # 本体 glossary.tracked(运行时读)
# 线索类别里的「采购类」组(本体 taxonomies.lead_category 第一组),本体里的顺序即「值得看」的优先级;其余类别按本体顺序接在后面
PROCUREMENT = onto_mod.group_values("lead_category", "采购类")
CATEGORY_PRIORITY = PROCUREMENT + tuple(v for v in onto_mod.values("lead_category") if v not in PROCUREMENT)
WEEK = 7
TOP = 5
PICKS = 6
UPCOMING_DAYS = 7


def funder_groups(onto_raw: dict) -> tuple[list[tuple[str, list[str]]], str, str]:
    """本体 groupings.funders → ([(组名, [样式小写])], 其他, 未填)。"""
    g = onto_raw["groupings"]["funders"]
    return [(x["v"], [str(p).lower() for p in x["patterns"]]) for x in g["groups"]], g["other"], g["empty"]


def funder_group(text: object, groups: list[tuple[str, list[str]]], other: str, empty: str) -> list[str]:
    t = str(text or "").strip().lower()
    if not t:
        return [empty]
    hit = [name for name, pats in groups if any(p in t for p in pats)]
    return hit or [other]


def to_assign(b: tstore.Base, r: dict) -> bool:
    """待确认归属(glossary.to_assign):所属商机为空,且(有建议所属、不是已忽略)或(可能新项目、待处理)。"""
    g = lambda p: b.get("lead", r, p)  # noqa: E731
    if tstore.link_id(g("opportunity")):
        return False
    st = str(g("status") or "")
    return bool(tstore.link_id(g("suggested_opp")) and st != "已忽略") or bool(g("maybe_new") and st == "待处理")


def _top(counter: Counter, n: int = TOP, unit: str = "") -> str:
    items = counter.most_common()
    if not items:
        return "无"
    s = "、".join(f"{k} {v}{unit}" for k, v in items[:n])
    rest = items[n:]
    if rest:
        s += f";其余 {len(rest)} 项 {sum(v for _, v in rest)}{unit}"
    return s


def _title(b: tstore.Base, r: dict, n: int = 60) -> str:
    """线索在简报里的名字:中文名称(项目名 → 中文摘要 → 从原题挑的「要点」,见 core.zh_name);
    只有要点或连要点都没有时把原题也带上,读的人能对上原文。"""
    g = lambda p: b.get("lead", r, p)
    full = (str(g("title") or "").strip().splitlines() or [""])[0]
    name, how = zh_name(full, g("summary_zh"), g("project_name"), 1000)
    name, orig = _short(name, n), _short(full, n)
    if how in ("项目名", "摘要"):
        return name
    return f"{name}(原题:{orig})" if name else orig


def build_daily(ctx: Ctx, today: date | None = None) -> tuple[str, set[str], set[str], str]:
    """→ (Markdown, 引用的线索记录编号, 写进的商机记录编号, 期间 YYYY-MM-DD)。"""
    b = ctx.base
    today = today or ctx.today
    tz = b.tz
    d0 = today.isoformat()
    week_start = (today - timedelta(days=WEEK - 1)).isoformat()
    groups, other, empty = funder_groups(b.onto.raw)

    leads = b.rows("lead", props=["key", "title", "relevant", "category", "sectors", "country", "funder", "found_at", "status", "opportunity",
                                   "suggested_opp", "maybe_new", "judged_by", "source", "summary_zh", "project_name", "amount", "origin"]
                  + (["amount_cny"] if b.has("lead", "amount_cny") else []))
    g = lambda r, p: b.get("lead", r, p) if p != "amount_cny" or b.has("lead", p) else None  # noqa: E731
    day_of = {r["id"]: (tstore.day(g(r, "found_at"), tz) or "") for r in leads}
    today_leads = [r for r in leads if day_of[r["id"]] == d0]
    week_leads = [r for r in leads if week_start <= day_of[r["id"]] <= d0]
    rel = [r for r in today_leads if g(r, "relevant") is True]
    proc = [r for r in rel if str(g(r, "category") or "") in PROCUREMENT]
    pending = sum(1 for r in leads if str(g(r, "status") or "") == "待处理")  # 与首页卡片同一条件
    assign = sum(1 for r in leads if to_assign(b, r))
    # 比较基准:前 7 天(不含今天)每天新增条数的中位数。不用平均数:新加信源、批量导入那天一下进来上千条,平均数会被拉高,
    # 让平常的一天看着像「明显低于」
    past = Counter(day_of[r["id"]] for r in leads)
    week_mid = median(past.get((today - timedelta(days=i)).isoformat(), 0) for i in range(1, WEEK + 1))

    opps = b.rows("opportunity", props=["code", "name_zh", "stage", "country"])
    og = lambda r, p: b.get("opportunity", r, p)  # noqa: E731
    code_of = {r["id"]: str(og(r, "code") or "") for r in opps}
    tracked = [r for r in opps if str(og(r, "stage") or "") in TRACKED_STAGES]
    suggested = [r for r in opps if str(og(r, "stage") or "建议") == "建议"]
    new_opps = [r for r in opps if str(r.get("createdTime") or "")[:10] == d0 or tstore.day(r.get("createdTime"), tz) == d0]
    fus = [r for r in b.rows("followup", props=["day", "opportunity", "stage_change"]) if tstore.day(b.get("followup", r, "day"), tz) == d0 and b.get("followup", r, "stage_change")]
    changed = [(code_of.get(tstore.link_id(b.get("followup", r, "opportunity")) or "", "?"), str(b.get("followup", r, "stage_change"))) for r in fus]
    tracked_ids = {r["id"] for r in tracked}
    end = (today + timedelta(days=UPCOMING_DAYS)).isoformat()
    ms = []
    for r in b.rows("milestone", props=["day", "kind", "opportunity", "lead", "status", "label"]):
        d = tstore.day(b.get("milestone", r, "day"), tz) or ""
        oid = tstore.link_id(b.get("milestone", r, "opportunity"))
        if d0 <= d <= end and (oid in tracked_ids or tstore.link_id(b.get("milestone", r, "lead")) in {x["id"] for x in rel}):
            ms.append((d, str(b.get("milestone", r, "kind") or ""), code_of.get(oid or "", "") or _cell(b.get("milestone", r, "lead")), r))
    ms.sort(key=lambda x: x[0])

    # ---- 今日概览
    trend = ""
    if week_mid > 0:
        ratio = len(today_leads) / week_mid
        trend = "明显高于" if ratio >= 1.5 else "高于" if ratio >= 1.15 else "明显低于" if ratio <= 0.5 else "低于" if ratio <= 0.85 else "接近"
        trend = f",{trend}前 {WEEK} 天的一般水平(每天 {week_mid:g} 条,取中位数)"
    over = [f"- 线索累计 {len(leads)} 条;今日新增 {len(today_leads)} 条(相关 {len(rel)} 条,其中采购类 {len(proc)} 条){trend}。",
            f"- 待处理 {pending} 条,待确认归属 {assign} 条;跟踪中的商机 {len(tracked)} 个,AI 推荐的商机 {len(suggested)} 个。"]
    uploads = sum(1 for r in today_leads if str(g(r, "origin") or "") == "用户上传")
    if uploads:
        over.append(f"- 今日新增里 {uploads} 条是同事上传的。")

    # ---- 分布(今日相关线索;一条没有时看近 7 天)
    dist_src, dist_label = (rel, "今日相关线索") if rel else ([r for r in week_leads if g(r, "relevant") is True], f"近 {WEEK} 天相关线索")
    dist = []
    if dist_src:
        by_c = Counter(_cell(g(r, "country")) or "未定国别" for r in dist_src)
        by_k = Counter(str(g(r, "category") or "未分类") for r in dist_src)
        by_s = Counter(s for r in dist_src for s in (g(r, "sectors") or ["未分领域"]))
        by_f = Counter(x for r in dist_src for x in funder_group(g(r, "funder"), groups, other, empty))
        dist = [f"- 国别:{_top(by_c)}", f"- 类别:{_top(by_k)}", f"- 领域:{_top(by_s)}", f"- 资金来源:{_top(by_f)}"]
    else:
        dist = ["- 今日与近 7 天都没有判为相关的新线索。"]

    # ---- 值得看的新线索:采购类优先,再按类别优先级;同类里有金额的在前
    prio = {k: i for i, k in enumerate(CATEGORY_PRIORITY)}
    picks = sorted(rel, key=lambda r: (prio.get(str(g(r, "category") or ""), 99), 0 if g(r, "amount") else 1, day_of[r["id"]]))[:PICKS]
    pick_lines = [f"- {_cell(g(r, 'country')) or '未定国别'} · {g(r, 'category') or '未分类'} · {_title(b, r)}"
                  + (f" · {_amount(g(r, 'amount'), g(r, 'amount_cny'))}" if g(r, "amount") else "") + f" [{g(r, 'key')}]" for r in picks]
    if not pick_lines:
        pick_lines = ["- 今日没有新的相关线索。"]

    # ---- 商机动态
    # 机器每天按新线索推荐商机(阶段「建议」),和人立项跟踪的分开说,免得把推荐当成新立项
    new_ai = [r for r in new_opps if str(og(r, "stage") or "建议") == "建议"]
    new_set = [r for r in new_opps if r not in new_ai]
    opp_list = lambda rs: "(" + "、".join(f"{og(r, 'code')} {og(r, 'name_zh') or ''}".strip() for r in rs[:TOP]) + ("等" if len(rs) > TOP else "") + ")" if rs else ""  # noqa: E731
    opp_lines = [f"- 今日 AI 新推荐商机 {len(new_ai)} 个{opp_list(new_ai)};新立项跟踪 {len(new_set)} 个{opp_list(new_set)}。"]
    if changed:
        opp_lines.append("- 阶段有变化:" + "、".join(f"{c} → {s}" for c, s in changed[:TOP]) + ("等" if len(changed) > TOP else "") + "。")
    if ms:
        opp_lines.append(f"- 未来 {UPCOMING_DAYS} 天时间节点 {len(ms)} 个:" + "、".join(f"{d} {k}({who})" for d, k, who, _ in ms[:TOP]) + ("等" if len(ms) > TOP else "") + "。")
    else:
        opp_lines.append(f"- 未来 {UPCOMING_DAYS} 天没有跟踪中商机的时间节点。")

    # ---- 信源运行(最近采集是今天的信源)
    src_lines = []
    ran = [(str(b.get("source", r, "name") or k), str(b.get("source", r, "last_status") or "")) for k, r in ctx.sources().items()
           if tstore.day(b.get("source", r, "last_run"), tz) == d0]
    if ran:
        st = Counter(s or "未知" for _, s in ran)
        bad = [n for n, s in ran if s in ("出错", "被拒")]
        src_lines.append(f"- 今日采集信源 {len(ran)} 个:" + "、".join(f"{k} {v}" for k, v in st.most_common()) + "。")
        if bad:
            src_lines.append("- 没抓成的:" + "、".join(bad[:TOP]) + ("等" if len(bad) > TOP else "") + "。")
    else:
        src_lines.append("- 今日还没有信源完成采集。")

    chapters = [("今日概览", "\n".join(over)), (dist_label + "分布", "\n".join(dist)), ("值得看的新线索", "\n".join(pick_lines)),
                ("商机动态", "\n".join(opp_lines)), ("信源运行", "\n".join(src_lines))]
    known = {str(g(r, "key") or "") for r in rel} | {code_of[r["id"]] for r in opps}
    ana, note = _insight(ctx, chapters, known)
    parts = [f"# 线索日报 {d0}\n"]
    for h, body in chapters:
        parts.append(f"## {h}\n\n{body}\n")
    if ana:
        parts.append(f"## 模型研判\n\n{ana}\n")
    parts.append(f"## 说明\n\n{note}口径与首页、「线索分析」页一致:新增按发现时间(当天,{tz});待处理 = 处理状态为待处理;待确认归属 = 没有所属商机、有机器建议或像是新项目的。\n")
    cite_leads = {r["id"] for r in picks}
    for _, _, _, r in ms:
        lid = tstore.link_id(b.get("milestone", r, "lead"))
        if lid:
            cite_leads.add(lid)
    cite_opps = {r["id"] for r in new_opps} | {tstore.link_id(b.get("followup", r, "opportunity")) for r in fus} - {None}
    return "\n".join(parts), cite_leads, cite_opps, d0


def _insight(ctx: Ctx, chapters: list[tuple[str, str]], known: set[str]) -> tuple[str, str]:
    """配了模型时写 3–5 句研判;没配、出错都只给说明,不影响日报。"""
    clf = ctx.clf
    if clf is None or not getattr(clf, "enabled", False) or not hasattr(clf, "write"):
        return "", "本报按统计与规则生成,未使用模型。"
    data = "\n".join(f"## {h}\n\n{body}" for h, body in chapters)
    system = reporting.SYSTEM.format(bidder=(ctx.profile.bidder or {}).get("nationality") or "未设", kind="每日线索简报")
    try:
        out = clf.write(system, data, {"insight": "研判:3–5 句话,只说今天的数据里最值得管理层注意的事(新机会、临近截止、异常),每条结论句末标线索号或商机编号;数据不够就说数据不够"})
    except Exception as e:  # noqa: BLE001  模型出错不影响日报
        return "", f"模型研判没成功({type(e).__name__}),本报按统计与规则生成。"
    if not out or out.get("_error") or not out.get("insight"):
        return "", f"模型研判没成功({(out or {}).get('_error') or '模型未返回'}),本报按统计与规则生成。"
    fixed, bad = reporting._check_cites({"insight": out["insight"]}, known)
    note = f"「模型研判」由模型({clf.label})根据上面的统计撰写,方括号内为线索号或商机编号;"
    if bad:
        note += f"其中 {len(bad)} 个编号在今日数据里没找到,已标「未核到」;"
    return reporting._plain(fixed["insight"]), note


def _short(s: str, n: int) -> str:
    """截到 n 个字,留头留尾:同一项目的不同标段(如 TRONÇON 1 / 2)常常只有结尾不同,只留开头读着像重复。"""
    s = (s or "").strip()
    return s if len(s) <= n else f"{s[:n - 25].rstrip()}…{s[-24:].lstrip()}"


def _amount(raw: object, cny: object) -> str:
    """原文金额后面带上折合人民币(万元,汇率任务 A21 填的);原币就是人民币或没折算的只写原文。"""
    s = str(raw).strip()
    try:
        w = float(cny)
    except (TypeError, ValueError):
        return s
    if w <= 0 or "CNY" in s.upper() or "RMB" in s.upper():
        return s
    return f"{s}(约 {w:,.0f} 万元)" if w >= 1 else f"{s}(约 {w * 10000:,.0f} 元)"


def _cell(x: object) -> str:
    if x is None:
        return ""
    if isinstance(x, dict):
        return str(x.get("title") or x.get("id") or "")
    if isinstance(x, list):
        return "、".join(_cell(i) for i in x)
    return str(x).strip()


__all__ = ["build_daily", "funder_group", "funder_groups", "to_assign", "PROCUREMENT", "TRACKED_STAGES"]
