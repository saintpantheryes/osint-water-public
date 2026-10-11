"""系统内提醒页与周报(Markdown;周报另出 Word)。

提醒页(每次运行覆盖):data 分支根目录 README.md(总览,GitHub 打开分支即显示)+ inbox/{ISO3}.md(分国全量)。
周报:reports/weekly/{ISO3}_周报_{年-W周}.md/.docx。
"""
from __future__ import annotations

from datetime import date, timedelta

from core import geo
from core.ledger import ACTIONABLE, ELIG, MILESTONES, PKG_TYPES, Derived, Ledger, parse_pdate
from core.report import cell, table
from core.watch.link import firm, foreign, item_country
from core.watch.rules import SECTORS, term_sectors
from core import ontology as onto_mod

CAT_ZH = {
    "tender": "招标", "eoi": "意向书", "prequalification": "资格预审", "gpn": "总采购公告", "spn": "特定采购公告",
    "award": "授标/短名单", "financing": "融资", "plan": "立项/设计/计划", "project_news": "项目动态", "policy": "政策",
    "other": "其他", "page": "页面变化",
}
PROCUREMENT = {"tender", "eoi", "prequalification", "gpn", "spn", "award", "financing"}
SECTOR_ZH = dict(onto_mod.codes("lead_sectors"))  # 线索领域:英文键 → 中文(本体 taxonomies.lead_sectors,运行时读)
# 台账的细分领域 → 线索的领域
SEGMENT_SECTOR = {"water_supply": "water_supply", "transmission": "water_supply", "nrw": "water_supply", "wastewater": "wastewater",
                  "sludge": "wastewater", "reuse": "wastewater", "reservoir": "hydraulic", "irrigation": "hydraulic",
                  "flood": "hydraulic", "desalination": "desalination", "stormwater": "stormwater", "environment": "environment"}
MANUAL = "manual"  # 人工提交的线索的信源(core.watch.manual.SOURCE)
LEDGER = "ledger"  # 正式商机的台账出处补成的线索的信源(core.ledger_leads)
DEADLINE_ZH = {"bid_deadline": "投标截止", "eoi_deadline": "意向书截止", "pq_deadline": "资格预审截止",
               "rfp_deadline": "PPP 标书截止", "other": "截止"}


def _llm(sig: dict) -> dict | None:
    x = sig.get("llm")
    return x if x and not x.get("_error") else None


def relevant(sig: dict) -> bool:
    """判定相关。人工提交的(网址、粘贴文字、上传文件)一律算相关:使用方的流程图里"用户自己上传的链接不再需要判定",
    模型只给它写摘要、定国别与领域、认台账项目(自动写进台账仍要模型判为相关,见 core.ledger_auto)。
    台账出处补成的线索(core.ledger_leads)写进台账时核对过,也算相关。"""
    if sig.get("source") in (MANUAL, LEDGER):
        return True
    x = _llm(sig)
    return bool(x["relevant"]) if x else bool((sig.get("rules") or {}).get("candidate"))


def category(sig: dict) -> str:
    if sig.get("change") == "changed" and sig.get("kind") == "page":
        return "page"
    x = _llm(sig)
    return (x or {}).get("category") or (sig.get("rules") or {}).get("stage") or "other"


def sectors_of(sig: dict) -> list[str]:
    """领域:模型判定的优先;其次规则命中的词;旧线索按记下的命中词补;都没有记为 other。"""
    x = _llm(sig)
    if x and x.get("sectors"):
        return [k for k in x["sectors"] if k in SECTORS]
    r = sig.get("rules") or {}
    if r.get("sectors"):
        return r["sectors"]
    ts, out = term_sectors(), []
    for t in r.get("water_terms") or []:
        for k in ts.get(t, []):
            if k not in out:
                out.append(k)
    return out or ["other"]


def sectors_zh(sig: dict) -> str:
    return "、".join(SECTOR_ZH.get(k, k) for k in sectors_of(sig))


def deadlines(sig: dict) -> list[tuple[str, str]]:
    """→ [(日期, 类型中文)]:模型抽取的在前(类型更具体),信源结构化字段的日期模型没抽到时补上。"""
    out = [(d["date"], DEADLINE_ZH.get(d.get("type"), "截止")) for d in (_llm(sig) or {}).get("deadlines") or []]
    dl = sig.get("fields", {}).get("deadline")
    if dl and all(d != dl for d, _ in out):
        out.append((dl, "截止"))
    return out


def summary(sig: dict) -> str:
    x = _llm(sig)
    if x and x.get("summary_zh"):
        return x["summary_zh"]
    if sig.get("summary_zh"):  # 台账出处补成的线索:按出处登记写的
        return sig["summary_zh"]
    ex = (sig.get("excerpt") or "").replace("\n", " ")
    return ex[:90] + ("…" if len(ex) > 90 else "")


RANK = {"high": 3, "medium": 2, "low": 1}


def links_of(sig: dict) -> list[dict]:
    """线索的台账挂接:规则(编号/名称匹配、监测配置)+ 模型判断。规则按名称挂上、模型读原文后也认的,
    置信度取两者较高者,标 by: rule+llm。按名称或模型挂上的别国台账项目不算(2026-09-27 以前入库的线索里有)。"""
    confirm = {lk["item"]: lk for lk in (_llm(sig) or {}).get("ledger_links") or [] if lk.get("item")}
    out = []
    for lk in sig.get("links") or []:
        if not firm(lk) and foreign(item_country(lk["item"]), sig.get("country")):
            continue
        m = confirm.get(lk["item"]) if lk.get("by") == "rule" else None
        if m:
            best = max(lk["confidence"], m.get("confidence") or "low", key=lambda c: RANK.get(c, 0))
            lk = {**lk, "confidence": best, "by": "rule+llm"}
        out.append(lk)
    return out


def _links(sig: dict) -> str:
    return "、".join(("●" if lk["confidence"] == "high" else "○") + lk["item"] for lk in links_of(sig)) or "—"


def _next_deadline(sig: dict, today: date) -> str:
    fut = sorted(d for d, _ in deadlines(sig) if d >= today.isoformat())
    return fut[0] if fut else "—"


def _sig_rows(sigs: list[dict], today: date, with_country: bool) -> list[list]:
    return [[s["first_seen"][:10], s.get("published") or "—", s.get("source_name") or s["source"],
             *([geo.name_zh(s.get("country"))] if with_country else []), sectors_zh(s), CAT_ZH.get(category(s), category(s)),
             f"[{cell(s['title'])[:80]}]({s['url']})", _next_deadline(s, today), _links(s), summary(s), s["id"][:8]]
            for s in sigs]


def signal_tables(sigs: list[dict], today: date, with_country: bool = False) -> list[str]:
    head = ["发现", "发布", "来源", *(["国别"] if with_country else []), "领域", "类别", "标题", "截止", "涉及台账项目", "摘要", "线索号"]
    sigs = sorted(sigs, key=lambda s: (s.get("published") or s["first_seen"][:10], s["first_seen"]), reverse=True)
    proc = [s for s in sigs if category(s) in PROCUREMENT]
    other = [s for s in sigs if category(s) not in PROCUREMENT]
    out = ["**采购与融资公告**\n", table(head, _sig_rows(proc, today, with_country)) if proc else "无。", "",
           "**项目与政策动态、页面变化**\n", table(head, _sig_rows(other, today, with_country)) if other else "无。", ""]
    return out


def deadline_rows(led: Ledger | None, ds: list[Derived], sigs: list[dict], today: date, days: int) -> list[list]:
    end = today + timedelta(days=days)
    rows = []
    elig = {d.item["id"]: ELIG[d.elig.status] for d in ds}
    for it in (led.items if led else []):
        for pk in (it.get("procurement") or {}).get("packages") or []:
            if pk.get("deadline") and pk.get("status") in ("open", "planned"):
                d = parse_pdate(pk["deadline"]).start
                if today <= d <= end:
                    rows.append([d.isoformat(), (d - today).days, f"{PKG_TYPES.get(pk.get('type'), '')}:{pk.get('name')}",
                                 f"{it['id']} {it['name_zh']}", elig.get(it["id"], "—"), "台账"])
        for m in it.get("milestones") or []:
            if m.get("type") in ACTIONABLE and m.get("status") in ("planned", "expected"):
                d = parse_pdate(m["date"]).start
                if today <= d <= end and parse_pdate(m["date"]).precision == "day":
                    rows.append([d.isoformat(), (d - today).days, MILESTONES.get(m["type"]), f"{it['id']} {it['name_zh']}",
                                 elig.get(it["id"], "—"), "台账"])
    ledger_dates = {(r[0], r[3].split(" ")[0]) for r in rows}
    for s in sigs:
        if not relevant(s):
            continue
        strong = [lk["item"] for lk in links_of(s) if lk["confidence"] == "high"]
        for dt, kind in deadlines(s):
            if any((dt, i) in ledger_dates for i in strong):
                continue  # 台账里已有同一天的节点
            try:
                d = date.fromisoformat(dt)
            except ValueError:
                continue
            if today <= d <= end:
                # 同一公告常在几个信源各出现一次(业主网站、世行接口……):挂到同一台账项目、同一天截止的只留一行
                anchor = strong[0] if strong else f"[{cell(s['title'])[:60]}]({s['url']})"
                rows.append([dt, (d - today).days, kind, f"[{cell(s['title'])[:60]}]({s['url']})", "—", f"线索 {s['id'][:8]}",
                             anchor])
    def pref(r: list) -> int:  # 同一天同一项目只留一行:台账标段行 > 台账节点行 > 线索里类型具体的 > 只写"截止"的
        if r[5] == "台账":
            return 1 if r[2] in MILESTONES.values() else 0
        return 3 if r[2] == "截止" else 2

    seen, out = set(), []
    for r in sorted(rows, key=lambda r: (r[0], r[6] if len(r) > 6 else r[3], pref(r))):
        k = (r[0], r[6] if len(r) > 6 else r[3])
        if k not in seen:
            seen.add(k)
            out.append(r[:6])
    return out


def _url_key(u: object) -> str:
    return str(u or "").strip().rstrip("/")


def suggestion_pairs(sigs: list[dict], led: Ledger | None, reviewed: set[str] | frozenset = frozenset(),
                     settled: set[str] | frozenset = frozenset()) -> tuple[dict[str, list[dict]], list[dict]]:
    """建议更新台账的线索 → ({台账编号: [线索]}, [可能的新项目线索])。
    原文网址已登记为台账来源的(已写进台账)、在界面上核对过的(reviewed:线索号前 8 位)不再列出;
    settled(线索号前 8 位):已处理或忽略的线索——人看过、没设跟踪进展,不再列为可能的新项目(要跟的项目设跟踪进展即自动入账)。"""
    names = {it["id"]: it["name_zh"] for it in (led.items if led else [])}
    known = {_url_key(s.get("url")) for s in (led.sources.values() if led else []) if s.get("url")}
    by_item: dict[str, list[dict]] = {}
    new_ops = []
    for s in sigs:
        if not relevant(s) or s["id"][:8] in reviewed or (s.get("url") and _url_key(s["url"]) in known):
            continue
        for lk in links_of(s):
            # 只收可靠的挂接:按编号匹配、监测配置指定、或模型读原文后判断;只按名称/别名的弱匹配只在线索表里显示
            if lk["item"] in names and (lk["confidence"] == "high" or "llm" in lk.get("by", "")):
                by_item.setdefault(lk["item"], []).append(s)
        if (_llm(s) or {}).get("new_opportunity") and not s.get("links") and s["id"][:8] not in settled:
            new_ops.append(s)
    return by_item, new_ops


def expired(sig: dict, today: date) -> bool:
    """线索给的截止日都已过去(没有截止日的不算过期)。"""
    days = [d for d, _ in deadlines(sig) if d]
    return bool(days) and max(days) < today.isoformat()


def ledger_suggestions(sigs: list[dict], led: Ledger | None, reviewed: set[str] | frozenset = frozenset()) -> list[str]:
    names = {it["id"]: it["name_zh"] for it in (led.items if led else [])}
    by_item, new_ops = suggestion_pairs(sigs, led, reviewed)
    out = []
    for iid in sorted(by_item):
        out.append(f"- **{iid} {names[iid]}**:" + ";".join(f"[{cell(s['title'])[:50]}]({s['url']})({summary(s)[:60]})" for s in by_item[iid][:5]))
    if new_ops:
        out.append("- **可能的新商机(台账里没有)**:" + ";".join(f"[{cell(s['title'])[:50]}]({s['url']})({summary(s)[:60]})" for s in new_ops[:10]))
    return out or ["无。"]


STATUS_ZH = {"ok": "正常", "empty": "无条目", "error": "出错", "blocked": "被拒(403/429,已停)"}


def source_rows(state: dict, source_ids: list[str], names: dict[str, str]) -> list[list]:
    rows = []
    for sid in source_ids:
        st = state.get("sources", {}).get(sid, {})
        rows.append([names.get(sid, sid), STATUS_ZH.get(st.get("status"), st.get("status") or "未运行"), (st.get("last_ok") or "—")[:16],
                     st.get("items", "—"), st.get("new", "—"), st.get("error") or ""])
    return rows


def render_inbox(label: str, sigs: list[dict], led: Ledger | None, ds: list[Derived], state: dict, source_ids: list[str],
                 source_names: dict[str, str], today: date, cfg: dict, notes: list[str],
                 reviewed: set[str] | frozenset = frozenset()) -> str:
    rel = [s for s in sigs if relevant(s)]
    dl = deadline_rows(led, ds, rel, today, cfg["deadline_days"])
    stale = [(d.item["id"], d.item["name_zh"], x) for d in ds for x in d.stale]
    L = [f"# {label} · 系统提醒\n",
         f"> 自动生成 {today} · 最近 {cfg['inbox_days']} 天相关线索 {len(rel)} 条 · 截止临近 {len(dl)} 项"
         + "".join(f"\n> {n}" for n in notes) + "\n",
         "## 一、新线索(待处理)\n"]
    L += signal_tables(rel, today)
    L += [f"## 二、截止临近(未来 {cfg['deadline_days']} 天)\n",
          table(["日期", "剩余天数", "事项", "项目/线索", "投标资格", "来源"], dl) if dl else "无。", "",
          "## 三、建议更新台账\n", *ledger_suggestions(rel, led, reviewed), "",
          "## 四、过期未更新的计划节点\n", *([f"- {i} {n}:{x}" for i, n, x in stale] or ["无。"]), "",
          "## 五、信源状态\n", table(["信源", "状态", "最近成功", "本次条目", "本次新增", "说明"], source_rows(state, source_ids, source_names)), "",
          "---", ("台账标记:● 可靠(按项目编号匹配,或模型读原文后确认)○ 需核对(只按名称/别名匹配)。处理过的线索可在 `watch/triage.yaml` 里标记"
                  " `线索号: {status: done}` 或 `{status: ignore}`,下次运行起不再出现在本页。"
                  "\"建议更新台账\"在本机界面的\"台账\"页逐条处理:\"更新台账\"核对原文后写入,\"不用更新\"以后不再提示。")]
    return "\n".join(L) + "\n"


def render_global(sigs: list[dict], state: dict, source_ids: list[str], source_names: dict[str, str], today: date, cfg: dict,
                  notes: list[str]) -> str:
    """全球提醒页:profile 以外国别的线索(来自全球信源),按大洲分组;截止临近;全球信源状态。"""
    rel = [s for s in sigs if relevant(s)]
    dl = deadline_rows(None, [], rel, today, cfg["deadline_days"])
    L = ["# 全球 · 系统提醒\n",
         f"> 自动生成 {today} · 最近 {cfg['inbox_days']} 天其他国别的相关线索 {len(rel)} 条(重点国别的见各自提醒页)"
         + "".join(f"\n> {n}" for n in notes) + "\n", "## 一、新线索(按大洲)\n"]
    for cont in geo.CONTINENTS:
        sub = [s for s in rel if geo.continent(s.get("country")) == cont]
        if sub:
            L += [f"### {cont}({len(sub)} 条)\n", *signal_tables(sub, today, with_country=True)]
    if not rel:
        L.append("无。\n")
    L += [f"## 二、截止临近(未来 {cfg['deadline_days']} 天)\n",
          table(["日期", "剩余天数", "事项", "项目/线索", "投标资格", "来源"], dl) if dl else "无。", "",
          "## 三、全球信源状态\n", table(["信源", "状态", "最近成功", "本次条目", "本次新增", "说明"], source_rows(state, source_ids, source_names)), "",
          "---", "处理过的线索可在 `watch/triage.yaml` 里标记,或在本机界面的线索看板上标记。"]
    return "\n".join(L) + "\n"


def render_digest(label: str, start: date, end: date, sigs: list[dict], led: Ledger | None, ds: list[Derived],
                  state: dict, source_ids: list[str], source_names: dict[str, str], cfg: dict, notes: list[str],
                  reviewed: set[str] | frozenset = frozenset(), progress: str | None = None) -> str:
    """周报。progress:「商机跟踪进展」一节(core.opportunity.progress_md),放在最前面——周期性报告是跟踪进展用的。"""
    rel = [s for s in sigs if relevant(s)]
    cats: dict[str, int] = {}
    for s in rel:
        cats[category(s)] = cats.get(category(s), 0) + 1
    dl = deadline_rows(led, ds, rel, end, cfg["deadline_days"])
    n = iter("一二三四五六七")
    L = [f"# {label}水务项目机会周报({start} 至 {end})\n",
         f"> 自动生成 · 本周采集线索 {len(sigs)} 条,判定相关 {len(rel)} 条" + "".join(f"\n> {n}" for n in notes) + "\n"]
    if progress is not None:
        L += [f"## {next(n)}、商机跟踪进展\n", progress, ""]
    L += [f"## {next(n)}、本周要点\n",
          "- 相关线索按类别:" + (";".join(f"{CAT_ZH.get(k, k)} {v} 条" for k, v in sorted(cats.items(), key=lambda x: -x[1])) or "无"),
          f"- 未来 {cfg['deadline_days']} 天内截止:{len(dl)} 项", ""]
    L += [f"## {next(n)}、本周线索\n", *signal_tables(rel, end),
          f"## {next(n)}、截止临近(未来 {cfg['deadline_days']} 天)\n",
          table(["日期", "剩余天数", "事项", "项目/线索", "投标资格", "来源"], dl) if dl else "无。", "",
          f"## {next(n)}、建议更新台账\n", *ledger_suggestions(rel, led, reviewed), "",
          f"## {next(n)}、信源运行情况\n", table(["信源", "状态", "最近成功", "本次条目", "本次新增", "说明"], source_rows(state, source_ids, source_names)), ""]
    return "\n".join(L) + "\n"


def render_overview(rows: list[list], generated: str, notes: list[str]) -> str:
    """data 分支根目录 README:各国一行,链接到分国提醒页与最新周报。"""
    return "\n".join([
        "# Water Tender Watch · 系统提醒总览\n",
        f"> 自动生成 {generated}。本分支由定时任务维护,请勿手工改动(`watch/triage.yaml` 除外)。" + "".join(f"\n> {n}" for n in notes) + "\n",
        table(["国别", "近期相关线索", "其中采购与融资", "截止临近", "信源异常", "提醒页", "最新周报", "最新月报"], rows), "",
        "说明:线索由系统自动采集与判定(规则初筛 + 大模型),台账由人确认;截止日与资格以原文为准。", ""])
