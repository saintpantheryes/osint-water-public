"""阶段变更记跟进(A18、R6、E4):每 2 分钟。

不另存状态:比较商机的「我方阶段」与它最近一条带「阶段变更」的跟进(没有的视为「建议」,即初始阶段)。
只在「阶段修改人」不是机器账号、且阶段与最近一条阶段变更不同时记一条跟进:日期 = 阶段修改时间,记录人 = 阶段修改人,
内容「阶段从 X 改为 Y」(没有过阶段变更、直接以「跟踪中」等新建的商机记「立项为 X」),阶段变更 = 新阶段。
机器建的建议商机、迁移进来的商机(迁移时已写出阶段变更跟进)都不会多记;两次检查之间连改两次只记最后一次。
表里没有「阶段修改人 / 时间」字段(阶段 A 核验不过时 schema 跳过)就按记录的最后修改人 / 时间。
"""
from __future__ import annotations

from core import tstore

from .common import Ctx

INITIAL = "建议"


def last_changes(ctx: Ctx) -> dict[str, tuple[str, str]]:
    """商机记录编号 → (最近一条阶段变更, 其日期):按日期、再按建的先后取最后一条。"""
    b = ctx.base
    out: dict[str, tuple[str, str]] = {}
    flt = b.filter(b.cond("followup", "stage_change", "isNotEmpty"))
    rows = b.rows("followup", filter=flt, props=["opportunity", "day", "stage_change"])
    rows.sort(key=lambda r: (tstore.day(b.get("followup", r, "day"), b.tz) or "", r.get("createdTime") or ""))
    for r in rows:
        oid = tstore.link_id(b.get("followup", r, "opportunity"))
        st = b.get("followup", r, "stage_change")
        if oid and st:
            out[oid] = (str(st), tstore.day(b.get("followup", r, "day"), b.tz) or "")
    return out


def run(ctx: Ctx) -> dict:
    b = ctx.base
    me = (ctx.base.me() or {}).get("id")
    has_track = b.has("opportunity", "阶段修改人") and b.has("opportunity", "阶段修改时间")
    last = last_changes(ctx)
    props = ["code", "stage"] + (["阶段修改人", "阶段修改时间"] if has_track else [])
    made = 0
    rows = []
    for r in b.rows("opportunity", props=props):
        stage = str(b.get("opportunity", r, "stage") or INITIAL)
        prev = last.get(r["id"], (INITIAL, ""))[0]
        if stage == prev:
            continue
        who = tstore.link_id(b.get("opportunity", r, "阶段修改人")) if has_track else None
        when = (b.get("opportunity", r, "阶段修改时间") if has_track else None) or r.get("lastModifiedTime")
        if not who:  # 建行时就填了阶段(没改过):按建行的人与时间
            who, when = tstore.link_id(r.get("createdBy")), r.get("createdTime") or when
            if not who:
                who = tstore.link_id(r.get("lastModifiedBy"))
        if not who or who == me:
            continue  # 机器自己建的、改的不记;认不出是谁的也不记(等下次)
        content = f"阶段从「{prev}」改为「{stage}」" if r["id"] in last or stage == INITIAL else f"立项为「{stage}」"
        rows.append({"opportunity": r["id"], "day": tstore.day(when, b.tz) or ctx.today.isoformat(), "author": who, "content": content,
                     "stage_change": stage})
    if rows:
        b.create("followup", rows)
        made = len(rows)
        for x in rows:
            ctx.log(f"  ▸ 跟进:{x['content']}(商机记录 {x['opportunity']})")
    return {"阶段变更记跟进": made} if made else {}
