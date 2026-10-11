"""每天把已过期的时间节点改为「已发生」(架构方案第 9 节的后备做法)。

阶段 A 复核(2026-10-04)的结论:Teable 含 TODAY() 的公式与按 today 筛选的条件查找隔天不会自动重算——
「未过截止」、线索的「最近截止」、商机的「下一节点」都会停在建表或上次改动那天。
改一次状态就会触发重算,所以后台每天零点后第一个周期把 日期 < 今天 且 状态 ≠ 已发生 的节点改成「已发生」。
只改状态,别的字段不动;这是 R2「只填空」的既定例外(机器维护的运行状态)。
页面与报告里的「未来 N 天」「逾期」都按当前日期自己算,不依赖库里的公式值。"""
from __future__ import annotations

from core import tstore
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
