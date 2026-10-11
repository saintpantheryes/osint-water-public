"""建议与更新商机(A06、A07、E2、E3):判定之后,只对这一轮刚判定的线索。

先挂接、后新建(本体 v1.2 的去重键):线索的挂接建议(编号吻合 → 别名与名称 → 模型确认)里最可能的商机写进「建议所属商机」;
可靠的(编号吻合、监测配置指定、模型读原文确认)且「所属商机」为空时代填,随后更新该商机的事实(只填空、列表只追加、
不同的写进「待核更新」,截止日写成商机的时间节点)。都挂不上、模型认为是新项目、且过门槛(重点或市场国、核心领域、采购或立项类)的,
新建「建议」商机,编号 = 该国表里现有最大序号 + 1;没过门槛的只勾「可能是新项目」,留在「待确认归属」等人定。
"""
from __future__ import annotations

from collections import Counter

from core import tstore
from core.watch.outputs import _llm, category, sectors_of

from core import ontology as onto_mod

from .common import OPP_SECTOR, PUBLIC_STAGE_OF, Ctx, best_link, deadlines_of, lead_no

# 门槛(本体 glossary.threshold 的 categories、glossary.core_sectors 的 sectors;运行时读,中文值 → 英文键)
NEW_OPP_CATEGORIES = {onto_mod.code_of("lead_category")[v] for v in onto_mod.glossary("threshold")["categories"]}
CORE_SECTORS = {onto_mod.code_of("lead_sectors")[v] for v in onto_mod.glossary("core_sectors")["sectors"]}


def core_sectors(ctx: Ctx) -> set[str]:
    """核心领域:供水、污水、海水淡化、雨水排涝、水环境;灌溉与防洪(线索里都归「水利」)看 profile 的 sector_scope。"""
    sc = ctx.profile.sector_scope or {}
    out = set(CORE_SECTORS)
    if sc.get("irrigation_drainage") or sc.get("flood_river_basin"):
        out.add("hydraulic")
    return out


def passes_threshold(ctx: Ctx, sig: dict) -> bool:
    iso = sig.get("country")
    if not iso or ctx.tier_of(iso) not in ("重点", "市场"):
        return False
    if not set(sectors_of(sig)) & core_sectors(ctx):
        return False
    return category(sig) in NEW_OPP_CATEGORIES


def run_round(ctx: Ctx, judged: list[tuple[dict, dict]]) -> dict:
    """judged = [(线索 dict(含判定与挂接建议), 线索记录)]。→ 计数。"""
    from .collect import milestone_keys, write_milestones
    stats: Counter = Counter()
    opps = ctx.opps()
    keys = milestone_keys(ctx) if any(deadlines_of(s) for s, _ in judged) else {}
    for sig, rec in judged:
        x = _llm(sig)
        if not x or not x.get("relevant"):
            continue
        code, reliable = best_link(sig)
        lead_vals: dict = {}
        opp_rid = None
        if code and code in opps.rid_of:
            opp_rid = opps.rid_of[code]
            lead_vals["suggested_opp"] = opp_rid
            lead_vals["suggest_reliable"] = True if reliable else None
            if reliable and tstore.is_empty(ctx.base.get("lead", rec, "opportunity")):
                lead_vals["opportunity"] = opp_rid
                stats["归属"] += 1
            else:
                opp_rid = None  # 不可靠:只建议,不改商机
                stats["建议归属"] += 1
        elif x.get("new_opportunity"):
            if passes_threshold(ctx, sig):
                opp_rid = create_opportunity(ctx, sig, rec)
                lead_vals.update({"opportunity": opp_rid, "maybe_new": True, "suggest_reliable": True})
                stats["新建商机"] += 1
            else:
                lead_vals["maybe_new"] = True
                stats["可能新项目"] += 1
        if lead_vals:
            ctx.base.fill("lead", rec, lead_vals)
        if opp_rid:
            if not lead_vals.get("maybe_new"):  # 刚新建的商机事实已在行里,不必再更新
                n = update_facts(ctx, opp_rid, sig, rec)
                if n:
                    stats["更新事实"] += n
            # 已按线索建过的节点补上商机;没建过的建在商机名下
            n = write_milestones(ctx, [(sig, rec, opp_rid)], existing=keys)
            if n:
                stats["时间节点"] += n
    return dict(stats)


def facts_from(sig: dict, no: str) -> tuple[dict, dict[str, list[str]]]:
    """线索判定出的内容 → 商机的标量事实(只填空)与列表项(只追加;每项末尾带出处线索号)。"""
    x = _llm(sig) or {}
    scalars = {"name_local": x.get("project"), "public_stage": PUBLIC_STAGE_OF.get(category(sig)), "eligibility_basis": x.get("eligibility"),
               "verified": "未核实"}
    lists: dict[str, list[str]] = {}
    amt = x.get("amount")
    if x.get("funder") or amt:
        lists["financing"] = [f"{x.get('funder') or '—'} · " + (f"{amt['value']:,.0f} {amt['currency']}" if amt else "金额待核实") + f" [{no}]"]
    ids = []
    for k, v in (sig.get("fields") or {}).items():
        if k in ("project", "project_id", "notice_id", "notice_no", "reference", "id") and v:
            ids.append(f"{k}: {v} [{no}]")
    if ids:
        lists["external_ids"] = ids
    if x.get("project") and x["project"] != scalars.get("name_local"):
        lists["aliases"] = [x["project"]]
    return scalars, lists


def size_facts(ctx: Ctx, sig: dict) -> dict:
    """投资规模 + 货币(只填空;折合人民币、美元由 A21 补):表里还没这两个字段时不给;币种不在选项里的只给数字。"""
    amt = (_llm(sig) or {}).get("amount")
    b = ctx.base
    if not amt or not b.has("opportunity", "size_value") or not b.has("opportunity", "currency"):
        return {}
    choices = {c["name"] for c in (b.field("opportunity", "currency").get("options") or {}).get("choices") or []}
    cur = str(amt.get("currency") or "").upper()
    return {"size_value": amt["value"], "currency": cur if cur in choices else None}


def update_facts(ctx: Ctx, opp_rid: str, sig: dict, lead_rec: dict) -> int:
    """A07:写前读最新值;标量只填空,不同的写进「待核更新」;列表只追加;出处线索追加关联。→ 写了几处。"""
    b = ctx.base
    rec = b.record("opportunity", opp_rid)
    if not rec:
        return 0
    no = lead_no(sig["id"])
    scalars, lists = facts_from(sig, no)
    scalars.update(size_facts(ctx, sig))
    changes, diffs = b.fill("opportunity", rec, scalars)
    n = len(changes)
    upd: dict = {}
    for prop, items in lists.items():
        new = tstore.append_lines(b.get("opportunity", rec, prop), items)
        if new is not None:
            upd[prop] = new
            n += 1
    if diffs:
        new = tstore.append_review(b.get("opportunity", rec, "review_updates"), [(no, fname, v, cur) for fname, v, cur in diffs])
        if new is not None:
            upd["review_updates"] = new
            n += 1
    have = tstore.link_ids(b.get("opportunity", rec, "sources"))
    if lead_rec["id"] not in have:
        upd["sources"] = have + [lead_rec["id"]]
        n += 1
    if upd:
        b.update("opportunity", opp_rid, upd)
    return n


def create_opportunity(ctx: Ctx, sig: dict, lead_rec: dict) -> str:
    """A06:新建「建议」商机(R5:阶段只能是建议,表里的缺省值),编号 = 该国最大序号 + 1,事实带出处线索号。"""
    x = _llm(sig) or {}
    iso = str(sig.get("country")).upper()
    opps = ctx.opps()
    code = opps.next_code(iso)
    no = lead_no(sig["id"])
    scalars, lists = facts_from(sig, no)
    scalars.update(size_facts(ctx, sig))
    name = (x.get("project") or x.get("summary_zh") or sig.get("title") or code)[:120]
    why = "\n".join(x for x in [x.get("summary_zh") or "", *[f"原文:{e}" for e in x.get("evidence") or []][:3], f"出处 [{no}]"] if x)
    row = {"code": code, "name_zh": name, "country": ctx.country_rid(iso), "sources": [lead_rec["id"]], "ai_reason": why,
           "sectors": [OPP_SECTOR[s] for s in sectors_of(sig) if s in OPP_SECTOR] or None,
           **{k: v for k, v in scalars.items() if v}, **{k: "\n".join(v) for k, v in lists.items()}}
    rec = ctx.base.create("opportunity", [row])[0]
    from .common import parse_ids
    opps.add(rec["id"], code, iso, name, x.get("project"), lists.get("aliases") or [], parse_ids(row.get("external_ids")), "建议")
    ctx.log(f"  ▸ 新建建议商机 {code} {name}(出处 [{no}])")
    return rec["id"]


def run_recent(ctx: Ctx, days: int = 1) -> dict:
    """命令行补做:最近 days 天发现、模型判过、相关、还没有所属与建议所属的线索,按判定字段重做一遍建议(模型结果从表里的字段复原)。"""
    from datetime import timedelta
    from core.watch.outputs import SECTOR_ZH
    b = ctx.base
    since = (ctx.today - timedelta(days=days)).isoformat()
    flt = b.filter(b.cond("lead", "judged_by", "is", "模型"), b.cond("lead", "relevant", "is", True),
                   b.cond("lead", "opportunity", "isEmpty"), b.cond("lead", "suggested_opp", "isEmpty"))
    zh2cat = {v: k for k, v in __import__("core.jobs.common", fromlist=["CAT"]).CAT.items()}
    zh2sec = {v: k for k, v in SECTOR_ZH.items()}
    judged = []
    for rec in b.rows("lead", filter=flt):
        if (tstore.day(b.get("lead", rec, "found_at"), b.tz) or "") < since:
            continue
        no = str(b.get("lead", rec, "key") or rec["id"])
        sig = {"id": no, "source": "", "url": b.get("lead", rec, "url"), "title": b.get("lead", rec, "title"), "fields": {},
               "country": ctx.iso_of(tstore.link_id(b.get("lead", rec, "country"))), "links": [],
               "llm": {"relevant": True, "category": zh2cat.get(b.get("lead", rec, "category"), "other"),
                       "sectors": [zh2sec[x] for x in (b.get("lead", rec, "sectors") or []) if x in zh2sec],
                       "summary_zh": b.get("lead", rec, "summary_zh") or "", "project": b.get("lead", rec, "project_name"),
                       "funder": b.get("lead", rec, "funder"), "amount": None, "deadlines": [], "ledger_links": [],
                       "new_opportunity": bool(b.get("lead", rec, "maybe_new")), "evidence": []}}
        from core.watch.link import build_index, suggest as rule_links
        led = ctx.opps().ledger_like(sig["country"])
        sig["links"] = rule_links("\n".join(str(x or "") for x in (sig["title"], sig["url"], b.get("lead", rec, "excerpt"))),
                                  build_index(led) if led else [], sig["country"])
        judged.append((sig, rec))
    return run_round(ctx, judged) if judged else {}
