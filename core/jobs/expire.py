"""每天把已过期的时间节点改为「已发生」(架构方案第 9 节的后备做法)。

阶段 A 复核(2026-10-04)的结论:Teable 含 TODAY() 的公式与按 today 筛选的条件查找隔天不会自动重算——
「未过截止」、线索的「最近截止」、商机的「下一节点」都会停在建表或上次改动那天。
改一次状态就会触发重算,所以后台每天零点后第一个周期把 日期 < 今天 且 状态 ≠ 已发生 的节点改成「已发生」。
只改状态,别的字段不动;这是 R2「只填空」的既定例外(机器维护的运行状态)。
页面与报告里的「未来 N 天」「逾期」都按当前日期自己算,不依赖库里的公式值。"""
from __future__ import annotations

from datetime import timedelta

from core import ontology, tstore
from core.jobs.common import Ctx

DONE = "已发生"


def run(ctx: Ctx) -> dict:
    b = ctx.base
    today = ctx.today.isoformat()
    before = {"mode": "exactDate", "exactDate": today + "T00:00:00.000Z", "timeZone": b.tz}
    rows = b.rows("milestone", filter=b.filter(b.cond("milestone", "day", "isBefore", before)), props=["day", "status"])
    due = [r for r in rows
           if (tstore.day(b.get("milestone", r, "day"), b.tz) or "9999-12-31") < today and b.get("milestone", r, "status") != DONE]
    if not due:
        return {}
    b.update_many("milestone", [(r["id"], {"status": DONE}) for r in due])
    return {"过期节点改已发生": len(due)}


# ---------- 过期线索归档(A49) ----------
ARCHIVE_DAYS = 365  # 发布(没有发布日期按发现时间)超过一年、又没归入商机的线索算过期
REASON_DEADLINE = "截止已过"
REASON_OLD = "超过一年"
DEADLINE_KINDS = ontology.group_values("milestone_kind", "采购截止")  # 投标人要动作的截止类节点(与「未过截止」公式一致),从本体读


def archive_leads(ctx: Ctx) -> dict:
    """每天把过期线索标「已归档」:最近截止 < 今天,或发布超过一年且没归入商机。只标记,不删、不改处理状态。
    人勾掉「已归档」后「归档时间」还在,机器不再打回;表里还没这几个字段时什么也不做。"""
    b = ctx.base
    if not (b.has("lead", "archived") and b.has("lead", "archived_at")):
        return {}
    today = ctx.today
    cutoff = (today - timedelta(days=ARCHIVE_DAYS)).isoformat()
    # 线索给出的截止类节点(意向书 / 资格预审 / 投标 / PPP 标书截止):全都过了才算「截止已过」。不用表里的「最近截止」:它是含 TODAY 的汇总,过了就空、隔天又不重算
    passed: dict[str, bool] = {}  # 线索编号 → 它的截止是否全过了
    for m in b.rows("milestone", filter=b.filter(b.cond("milestone", "lead", "isNotEmpty")), props=["lead", "kind", "day"]):
        if b.get("milestone", m, "kind") not in DEADLINE_KINDS:
            continue
        lid, d = tstore.link_id(b.get("milestone", m, "lead")), tstore.day(b.get("milestone", m, "day"), b.tz)
        if lid and d:
            passed[lid] = passed.get(lid, True) and d < today.isoformat()
    rows = b.rows("lead", filter=b.filter(b.cond("lead", "archived", "is", False)),
                  props=["archived", "archived_at", "published", "found_at", "opportunity"])
    todo = []
    for r in rows:
        g = lambda p: b.get("lead", r, p)  # noqa: E731
        if g("archived") or g("archived_at"):
            continue  # 已归档,或人取消过归档
        pub = tstore.day(g("published"), b.tz) or tstore.day(g("found_at"), b.tz)
        if passed.get(r["id"]):
            todo.append((r["id"], REASON_DEADLINE))
        elif pub and pub < cutoff and not g("opportunity"):
            todo.append((r["id"], REASON_OLD))
    if not todo:
        return {}
    b.update_many("lead", [(rid, {"archived": True, "archive_reason": why, "archived_at": today.isoformat()}) for rid, why in todo])
    out = {}
    for _, why in todo:
        out[why] = out.get(why, 0) + 1
    return {"过期线索归档": len(todo), **out}
