"""生成报告(A14):每 2 分钟看报告表里「待生成」的行;周一 09:00 自建周报、每月 1 日 09:30 自建月报(期间去重,重启不重复建);
每天采集完成后自建当天的日报(core.jobs.brief,正文另写进「简报正文」,「线索分析」页顶部显示)。

同事要一份报告:在报告表新建一行,选类型(国别报告要选范围国别,项目报告要选范围商机;周报、月报可只填期间或留空),生成状态留「待生成」。
这里按类型从库里取数(国别、商机、线索、跟进、时间节点)写成 Markdown,按成稿模板转 Word 传到「文件」,写「文件名」(标识,重跑不重复建)、
「期间」、「引用的线索」与「写进的商机」,生成状态改「完成」;出错改「失败」并把原因记日志。
引用的线索 = 正文直接列出的线索 ∪ 写进的商机当时的所属线索(快照,R9)。模型配好时加分析段落(摘要、动态、机会研判、行动建议、风险),
每条结论标线索号或商机编号;没配就只出数据章节。
容器启动时把超过 30 分钟仍是「生成中」的改回「待生成」,重启不丢活。
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from core import ontology as onto_mod
from core import geo, tstore
from core.country_facts import METRICS, tone
from core.watch import reporting
from core.zh_name import expand_acronyms, zh_name

from . import brief
from .common import Ctx, now_iso

WEEKLY_AT, MONTHLY_AT = dtime(9, 0), dtime(9, 30)
STALE_MIN = 30
UPCOMING_DAYS = 30
# 我方阶段的显示顺序与「跟踪中的商机」口径,都照本体(docs/ontology/ontology.yaml 的 taxonomies.stage 与 glossary.tracked);tests/test_ontology.py 核对
STAGE_ORDER = onto_mod.values("stage")  # 运行时读本体(v2.2);改阶段只改 ontology.yaml
TRACKED_STAGES = set(onto_mod.glossary_stages("tracked"))
RECENT_LEADS_DAYS = 90
FIELDS = {k: v for k, v in reporting.FIELDS.items()}
SYSTEM = reporting.SYSTEM
_to_docx = reporting.to_docx_styled  # 测试里换掉


class ReportError(Exception):
    pass


# ---------- 周期自建 ----------

def week_period(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def ensure_periodic(ctx: Ctx, now: datetime) -> dict:
    """周一 09:00 以后建上周的周报行、每月 1 日 09:30 以后建上月的月报行(同类型同期间只建一次)。→ 建了几行。"""
    b = ctx.base
    want: list[tuple[str, str]] = []
    if now.weekday() == 0 and now.time() >= WEEKLY_AT:
        want.append(("周报", week_period(now.date() - timedelta(days=7)), None))
    if now.day == 1 and now.time() >= MONTHLY_AT:
        prev = now.date().replace(day=1) - timedelta(days=1)
        want.append(("月报", prev.strftime("%Y-%m"), None))
    if not want:
        return {}
    have = {(b.get("report", r, "kind"), b.get("report", r, "period")) for r in b.rows("report", props=["kind", "period"])}
    rows = [{"kind": k, "period": p, "day": now.date().isoformat(), "edition": "内部版", "gen_status": "待生成", **({"country": rid} if rid else {})}
            for k, p, rid in want if (k, p) not in have]
    if rows:
        b.create("report", rows)
        for r in rows:
            ctx.log(f"  ▸ 自建{r['kind']} {r['period']}(待生成)")
    return {"自建报告": len(rows)} if rows else {}


def ensure_daily(ctx: Ctx, today: date) -> dict:
    """采集完成后建当天的日报行(E10;同一天只建一次,人在表里要过的也算)。→ 建了几行。"""
    b = ctx.base
    have = any(b.get("report", r, "period") == today.isoformat() or tstore.day(b.get("report", r, "day"), b.tz) == today.isoformat()
               for r in b.rows("report", filter=b.filter(b.cond("report", "kind", "is", "日报")), props=["kind", "period", "day"]))
    if have:
        return {}
    b.create("report", [{"kind": "日报", "period": today.isoformat(), "day": today.isoformat(), "edition": "内部版", "gen_status": "待生成"}])
    ctx.log(f"  ▸ 自建日报 {today}(待生成)")
    return {"自建报告": 1}


def reset_stale(ctx: Ctx) -> int:
    """「生成中」超过 30 分钟的(上次进程中途没了)改回「待生成」。"""
    b = ctx.base
    n = 0
    for r in b.rows("report", filter=b.filter(b.cond("report", "gen_status", "is", "生成中")), props=["gen_status"]):
        ts = str(r.get("lastModifiedTime") or r.get("createdTime") or "")
        try:
            when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            when = None
        if when is None or (datetime.now(when.tzinfo) - when) >= timedelta(minutes=STALE_MIN):
            b.update("report", r["id"], {"gen_status": "待生成"})
            n += 1
    return n


# ---------- 入口 ----------

def pending(ctx: Ctx) -> list[dict]:
    b = ctx.base
    return b.rows("report", filter=b.filter(b.cond("report", "gen_status", "is", "待生成")))


def run(ctx: Ctx, limit: int = 5) -> dict:
    rows = pending(ctx)[:limit]
    if not rows:
        return {}
    b = ctx.base
    done = failed = 0
    for rec in rows:
        b.update("report", rec["id"], {"gen_status": "生成中"})
        try:
            name = generate(ctx, rec)
            done += 1
            ctx.log(f"  ▸ 报告完成:{name}")
        except Exception as e:  # noqa: BLE001  一份失败不影响其余;原因记日志,表里标失败
            failed += 1
            b.update("report", rec["id"], {"gen_status": "失败"})
            ctx.log(f"  ✗ 报告失败(记录 {rec['id']}):{type(e).__name__}: {e}")
    return {"报告完成": done, "报告失败": failed}


def generate(ctx: Ctx, rec: dict) -> str:
    """一份报告:取数 → Markdown → Word → 传附件 → 回写。→ 文件名。"""
    b = ctx.base
    kind = str(b.get("report", rec, "kind") or "")
    if kind not in ("周报", "月报", "国别报告", "项目报告", "日报", "招标监测"):
        raise ReportError(f"类型「{kind}」不会生成")
    today = ctx.today
    summary = None
    if kind == "日报":
        period = str(b.get("report", rec, "period") or "").strip() or (tstore.day(b.get("report", rec, "day"), b.tz) or today.isoformat())
        try:
            d = date.fromisoformat(period)
        except ValueError:
            raise ReportError(f"日报的期间要是日期(YYYY-MM-DD),现在是「{period}」") from None
        md, leads, opps, scope = brief.build_daily(ctx, d)
        summary = md
        upd = {"period": scope}
    elif kind in ("周报", "月报"):
        start, end, period = _window(kind, str(b.get("report", rec, "period") or ""), today)
        iso = ctx.iso_of(tstore.link_id(b.get("report", rec, "country")))
        md, leads, opps, scope = build_periodic(ctx, kind, start, end, iso)
        upd = {"period": period}
    elif kind == "招标监测":
        from . import tender_monitor
        iso = ctx.iso_of(tstore.link_id(b.get("report", rec, "country")))
        if not iso:
            raise ReportError("招标监测要选「范围国别」")
        start, end = tender_monitor.window(str(b.get("report", rec, "period") or ""), today)
        md, leads, opps, scope = tender_monitor.build(ctx, iso, start, end)
        upd = {"period": tender_monitor.period_text(iso, start, end)}
    elif kind == "国别报告":
        iso = ctx.iso_of(tstore.link_id(b.get("report", rec, "country")))
        if not iso:
            raise ReportError("国别报告要选「范围国别」")
        md, leads, opps, scope = build_country(ctx, iso, internal=str(b.get("report", rec, "edition") or "") != "公开版")
        upd = {}
    else:
        opp_rid = tstore.link_id(b.get("report", rec, "opportunity"))
        if not opp_rid:
            raise ReportError("项目报告要选「范围商机」")
        return _generate_project(ctx, rec, opp_rid)
    edition = b.get("report", rec, "edition")
    if kind in ("国别报告", "项目报告") and str(edition or "") != "公开版":
        extra = company_chapter(ctx, kind, scope)  # 内部版附我方资质与业绩(A23);公开版不附
        md += extra.replace("## 附:", "## 附录 C ") if kind == "国别报告" else extra
    if kind != "日报":
        md = expand_acronyms(md)  # 银行缩写第一次出现时补中文全称
    day = tstore.day(b.get("report", rec, "day"), b.tz) or today.isoformat()
    name = reporting.slug("_".join(x for x in (kind, scope, day, str(edition or "")) if x)) + ".docx"
    out_dir = ctx.cache.root.parent / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / (name[:-5] + ".md")
    md_path.write_text(md, encoding="utf-8")
    tpl_rid, tpl_path = pick_template(ctx, rec, kind, out_dir.parent / "templates")
    if tpl_rid and not tstore.link_id(b.get("report", rec, "template")):
        upd["template"] = tpl_rid  # 没指定时把实际套用的模板填上,回头看得出这份用的哪个
    try:
        fp = _to_docx(md_path, out_dir / name, template=tpl_path) if tpl_path else _to_docx(md_path, out_dir / name)
    except Exception as e:  # noqa: BLE001  转不成 Word 就传 Markdown,报告不丢
        ctx.log(f"  ! 转 Word 失败,改传 Markdown:{type(e).__name__}: {e}")
        fp, name = md_path, md_path.name
    ctx.api.upload_file(b.t["report"], rec["id"], b.fid("report", "file"), fp)
    if summary is not None:
        if b.has("report", "summary"):
            upd["summary"] = summary
        elif fp is not md_path:  # 库里还没有「简报正文」字段:把 Markdown 也传上去,页面读它
            ctx.api.upload_file(b.t["report"], rec["id"], b.fid("report", "file"), md_path)
    upd.update({"key": name, "day": day, "gen_status": "完成", "cites_leads": sorted(leads), "cites_opps": sorted(opps)})
    b.update("report", rec["id"], upd)
    return name


def _generate_project(ctx: Ctx, rec: dict, opp_rid: str) -> str:
    """项目报告:按《项目分析报告》模板填(内部版中文报领导,公开版英文发伙伴),见 opp_report。"""
    from . import opp_report

    b = ctx.base
    edition = str(b.get("report", rec, "edition") or "内部版")
    internal = edition != "公开版"
    day = tstore.day(b.get("report", rec, "day"), b.tz) or ctx.today.isoformat()
    out_dir = ctx.cache.root.parent / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    tpl_rid, tpl_path = pick_template(ctx, rec, "项目报告", out_dir.parent / "templates", edition=edition)
    o = b.record("opportunity", opp_rid)
    code = str(b.get("opportunity", o, "code") or opp_rid) if o else opp_rid
    name = reporting.slug("_".join(x for x in ("项目报告", code, day, edition) if x)) + ".docx"
    fp, leads, code, md = opp_report.render(ctx, opp_rid, internal, out_dir / name, tpl_path)
    if internal:
        md += company_chapter(ctx, "项目报告", code)  # Markdown 留底附我方资质与业绩(A23);Word 里由模型写进判断与资格对照
    md_path = out_dir / (name[:-5] + ".md")
    md_path.write_text(md, encoding="utf-8")
    if fp is None:  # 模板拿不到:传 Markdown,报告不丢
        ctx.log("  ! 项目报告没出成 Word,改传 Markdown")
        fp, name = md_path, md_path.name
    ctx.api.upload_file(b.t["report"], rec["id"], b.fid("report", "file"), fp)
    upd = {"key": name, "day": day, "gen_status": "完成", "cites_leads": sorted(leads), "cites_opps": [opp_rid]}
    if tpl_rid and not tstore.link_id(b.get("report", rec, "template")):
        upd["template"] = tpl_rid
    b.update("report", rec["id"], upd)
    return name


# ---------- 我方资质与业绩(公司资料表,A23) ----------

def _parent_sectors(fine: list[str]) -> set[str]:
    """商机领域(细分 12 类)→ 上级线索领域(粗分 7 类);认不出的原样保留。"""
    parent = {v["v"]: str(v.get("parent") or v["v"]) for g in onto_mod.taxonomy("opp_sectors")["groups"] for v in g["values"]}
    return {parent.get(str(x), str(x)) for x in fine or []}


def company_rows(ctx: Ctx, country_rid: str | None, sectors: set[str] | None) -> list[dict]:
    """要附进报告的公司资料:勾了「写进报告」;归属为本单位(空也算);适用国别含该国或为空(全球通用);适用领域与给定领域有交集或为空(各领域都适用)。
    sectors 为 None 表示不按领域筛(国别报告)。库里还没建「公司资料」表 → 空。"""
    b = ctx.base
    if "company" not in b.t or not b.has("company", "for_report"):
        return []
    try:
        rows = b.rows("company", filter=b.filter(b.cond("company", "for_report", "is", True)))
    except tstore.TStoreError as e:
        ctx.log(f"  ! 读不到公司资料表,报告不附:{e}")
        return []
    has_owner = b.has("company", "owner")
    out = []
    for r in rows:
        if has_owner and str(b.get("company", r, "owner") or "本单位") != "本单位":
            continue  # 兄弟单位、合作方的业绩只做记录,不当本单位业绩附进报告(v2.28)
        cs = tstore.link_ids(b.get("company", r, "countries"))
        if cs and country_rid not in cs:
            continue
        ss = set(b.get("company", r, "sectors") or [])
        if sectors is not None and ss and not (ss & sectors):
            continue
        out.append(r)
    kinds = {k: i for i, k in enumerate(onto_mod.values("company_kind"))}
    out.sort(key=lambda r: (kinds.get(str(b.get("company", r, "kind")), 99), -(b.get("company", r, "year") or 0), str(b.get("company", r, "name") or "")))
    return out


def company_chapter(ctx: Ctx, kind: str, scope: str) -> str:
    """国别报告 / 项目报告内部版末尾的「附:我方资质与相关业绩」;没有可附的行 → 空字符串。scope 是 generate 算出的范围(ISO3 或商机编号)。"""
    b = ctx.base
    if kind == "国别报告":
        country_rid, sectors = ctx.country_rid(scope), None
    else:
        o = next(iter(b.rows("opportunity", filter=b.filter(b.cond("opportunity", "code", "is", scope)), props=["code", "country", "sectors"])), None)
        if not o:
            return ""
        country_rid = tstore.link_id(b.get("opportunity", o, "country"))
        sectors = _parent_sectors(b.get("opportunity", o, "sectors") or []) or None
    rows = company_rows(ctx, country_rid, sectors)
    if not rows:
        return ""
    g = lambda r, p: b.get("company", r, p)  # noqa: E731
    quals = [r for r in rows if g(r, "kind") == "资质"]
    recs = [r for r in rows if g(r, "kind") == "业绩"]
    others = [r for r in rows if g(r, "kind") not in ("资质", "业绩")]
    parts = ["\n## 附:我方资质与相关业绩\n", "按「公司资料」表里勾了「写进报告」、适用国别与领域匹配的条目列出;仅限内部版。\n"]
    if quals:
        parts.append("### 资质\n\n" + "\n".join(f"- {g(r, 'name')}" + (f"({g(r, 'year')})" if g(r, "year") else "") + (f":{_cell(g(r, 'content'))}" if g(r, "content") else "") for r in quals) + "\n")
    if recs:
        parts.append("### 相关业绩\n\n" + _table(["项目", "国别", "领域", "年份", "金额", "概况"],
                                            [[g(r, "name"), _cell(g(r, "countries")) or "全球", _cell(g(r, "sectors")), g(r, "year"), g(r, "amount"), (g(r, "content") or "")[:160]] for r in recs]))
    if others:
        parts.append("### 其他\n\n" + "\n".join(f"- {g(r, 'name')}" + (f":{_cell(g(r, 'content'))}" if g(r, "content") else "") for r in others) + "\n")
    return "\n".join(parts)


# ---------- 报告模板 ----------

def pick_template(ctx: Ctx, rec: dict, kind: str, cache_dir: Path, edition: str = "") -> tuple[str | None, Path | None]:
    """这份报告套哪个模板:报告行选了的 → 适用类型含该类型且勾了「缺省」的(多个取最近改的)→ 适用类型含该类型的任一个 → 都没有用程序自带的。
    给了版本时先在名称合这个版本的模板里挑:公开版认名称里的「公开版」「英文」「English」,内部版认其余的(项目报告中英文两套模板靠名称区分)。
    → (模板记录编号, 下载到本机的 .docx 路径);没有或下载失败 → (None, None),照常用自带模板。"""
    b = ctx.base
    if "template" not in b.t or not b.has("report", "template"):
        return None, None  # 库里还没建「报告模板」表(先跑 python -m core.schema)
    try:
        rows = b.rows("template", props=["name", "kinds", "file", "is_default"])
    except tstore.TStoreError as e:
        ctx.log(f"  ! 读不到报告模板表,用自带模板:{e}")
        return None, None
    want = tstore.link_id(b.get("report", rec, "template"))
    row = next((r for r in rows if r["id"] == want), None) if want else None
    if row is None:
        when = lambda r: str(r.get("lastModifiedTime") or r.get("createdTime") or "")  # noqa: E731
        fit = [r for r in rows if kind in (b.get("template", r, "kinds") or []) and b.get("template", r, "file")]
        if edition:
            pub = lambda r: bool(re.search("公开版|英文|English", str(b.get("template", r, "name") or ""), re.IGNORECASE))  # noqa: E731
            fit = [r for r in fit if pub(r) == (edition == "公开版")] or fit
        pool = [r for r in fit if b.get("template", r, "is_default")] or fit
        row = max(pool, key=when) if pool else None
    if row is None:
        return None, None
    files = b.get("template", row, "file") or []
    if not files:
        ctx.log(f"  ! 报告模板「{b.get('template', row, 'name')}」没有文件,用自带模板")
        return None, None
    att = files[-1]
    cache_dir.mkdir(parents=True, exist_ok=True)
    fp = cache_dir / (reporting.slug(str(att.get("id") or att.get("token") or att.get("name") or "tpl")) + ".docx")
    if not fp.exists():
        try:
            fp.write_bytes(_download(ctx, att))
        except Exception as e:  # noqa: BLE001  模板拿不到不耽误出报告
            ctx.log(f"  ! 下载报告模板失败,用自带模板:{type(e).__name__}: {e}")
            return None, None
    return row["id"], fp


def _download(ctx: Ctx, att: dict) -> bytes:
    """附件 → 字节:用附件的下载地址(presignedUrl)。表格的附件地址一律只取路径、补上表格的网址:
    换域名前存下的签名地址带着旧网址,从容器里走旧网址既绕远也可能打不开。"""
    url = str(att.get("presignedUrl") or att.get("url") or "")
    if not url:
        raise ReportError("附件没有下载地址")
    u = urlsplit(url)
    if url.startswith("/") or u.path.startswith("/api/attachments/"):
        url = ctx.api.url + u.path + (("?" + u.query) if u.query else "")
    r = ctx.api.http.get(url, follow_redirects=True)
    r.raise_for_status()
    return r.content


def _window(kind: str, period: str, today: date) -> tuple[date, date, str]:
    """期间 → (起, 止, 规范化的期间)。周报 2026-W40 = 该 ISO 周;月报 2026-09 = 该月;没填时周报取最近 7 天、月报取上个月。"""
    if kind == "周报":
        m = re.fullmatch(r"(\d{4})-W(\d{1,2})", period.strip())
        if m:
            start = date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
            return start, start + timedelta(days=6), week_period(start)
        return today - timedelta(days=6), today, week_period(today)
    m = re.fullmatch(r"(\d{4})-(\d{1,2})", period.strip())
    if m:
        start = date(int(m.group(1)), int(m.group(2)), 1)
    else:
        start = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return start, end, start.strftime("%Y-%m")


# ---------- 取数 ----------

def _leads_between(ctx: Ctx, start: date, end: date, iso: str | None) -> list[dict]:
    b = ctx.base
    out = []
    for r in b.rows("lead", filter=b.filter(b.cond("lead", "relevant", "is", True))):
        d = tstore.day(b.get("lead", r, "found_at"), b.tz) or ""
        if not (start.isoformat() <= d <= end.isoformat()):
            continue
        if iso and ctx.iso_of(tstore.link_id(b.get("lead", r, "country"))) != iso:
            continue
        out.append(r)
    out.sort(key=lambda r: (tstore.day(b.get("lead", r, "found_at"), b.tz) or ""), reverse=True)
    return out


def _opps(ctx: Ctx, iso: str | None = None) -> list[dict]:
    b = ctx.base
    rows = b.rows("opportunity")
    if iso:
        rows = [r for r in rows if ctx.iso_of(tstore.link_id(b.get("opportunity", r, "country"))) == iso]
    return rows


def _followups_between(ctx: Ctx, start: date, end: date, opp_rids: set[str] | None) -> list[dict]:
    b = ctx.base
    out = []
    for r in b.rows("followup"):
        d = tstore.day(b.get("followup", r, "day"), b.tz) or ""
        oid = tstore.link_id(b.get("followup", r, "opportunity"))
        if start.isoformat() <= d <= end.isoformat() and (opp_rids is None or oid in opp_rids):
            out.append(r)
    out.sort(key=lambda r: tstore.day(b.get("followup", r, "day"), b.tz) or "")
    return out


def _milestones(ctx: Ctx, start: date, end: date, opp_rids: set[str] | None = None, lead_rids: set[str] | None = None) -> list[dict]:
    b = ctx.base
    out = []
    for r in b.rows("milestone"):
        d = tstore.day(b.get("milestone", r, "day"), b.tz) or ""
        if not (start.isoformat() <= d <= end.isoformat()):
            continue
        oid, lid = tstore.link_id(b.get("milestone", r, "opportunity")), tstore.link_id(b.get("milestone", r, "lead"))
        if opp_rids is not None and oid not in opp_rids and (lead_rids is None or lid not in lead_rids):
            continue
        out.append(r)
    out.sort(key=lambda r: tstore.day(b.get("milestone", r, "day"), b.tz) or "")
    return out


# ---------- 排版 ----------

def _cell(x: object) -> str:
    if x is None:
        return ""
    if isinstance(x, dict):
        x = x.get("title") or x.get("id") or ""
    if isinstance(x, list):
        x = "、".join(_cell(i) for i in x)
    if isinstance(x, bool):
        return "是" if x else "否"
    return str(x).replace("|", "/").replace("\n", " ").strip()


def _table(head: list[str], body: list[list[object]]) -> str:
    if not body:
        return "(无)\n"
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    lines += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in body]
    return "\n".join(lines) + "\n"


_SRC_URL = re.compile(r"[(\uff08]([^()\uff08\uff09]*?)\s+(https?://[^\s()\uff08\uff09]+)[)\uff09]")  # 「(维基百科 https://…)」:来源名 + 网址
_BARE_URL = re.compile(r"(?<![(<])https?://[^\s()\uff08\uff09<>]+")


def _md_text(s: str) -> str:
    """普通文字放进 Markdown 链接文字前,把会被当成格式的符号转义。"""
    return re.sub(r"([\\`*_\[\]<>#])", r"\\\1", s)


def _host(url: str) -> str:
    m = re.match(r"https?://(?:www\.)?([^/?#]+)", url)
    return m.group(1) if m else "链接"


def _links(text: str) -> str:
    """库里存的「…(来源名 网址)」「… · 网址」→ Markdown 链接:来源名是可点的字,网址本身不再印出来。"""
    text = _SRC_URL.sub(lambda m: f"([{_md_text(m.group(1).strip() or _host(m.group(2)))}](<{m.group(2)}>))", text)
    return _BARE_URL.sub(lambda m: f"[{_host(m.group(0))}](<{m.group(0)}>)", text)


def _bullets(text: object) -> str:
    """多行字段(基础信息、指标、机构、法规……)→ 一行一条的列表,网址变成可点的来源名。"""
    lines = [x.strip() for x in str(text or "").splitlines() if x.strip()]
    return "\n".join("- " + _links(x.lstrip("-•· ").strip()) for x in lines) + "\n"


# ---------- 国别概况的版式(要点 → 关键指标卡 → 基本情况表 → 分组指标表 → 机构、国企、法规表) ----------

_YEAR = re.compile(r"^(.*?)\s*[(\uff08](\d{4}(?:-\d{2})?)[)\uff09]\s*$")
KPI_CAPTION = reporting.KPI_CAPTION  # Word 里按表格标题认出关键指标卡,换成大字版式
BAD_LABEL = reporting.BAD_MARK  # 「超警戒线」:负面指标写明越过了哪条线,Word 里整格标红
# 关键指标:顺序与国别概况页一致(portal/country.html 的 KEY)
KEY_METRICS = ((r"^GDP\s*\(现价", "GDP"), (r"^人均 GDP(?!\s*\(PPP)", "人均 GDP"), (r"实际增速", "GDP 实际增速"), (r"^总人口", "总人口"),
               (r"^城镇化率", "城镇化率"), (r"^安全管理饮水", "安全管理饮水覆盖率"), (r"^淡水压力", "淡水压力(取水/可再生)"), (r"^政府总债务", "政府总债务/GDP"))
GROUP_OF = {m.label: m.group for m in METRICS}
GROUPS = list(dict.fromkeys(m.group for m in METRICS))


def fact_rows(text: object) -> list[dict]:
    """「名称:值(年份)(来源 网址)」一行一条 → [{k, v, year, src, url, bad}]。"""
    out = []
    for line in (x.strip() for x in str(text or "").splitlines()):
        if not line:
            continue
        src = url = ""
        m = _SRC_URL.search(line)
        if m and m.end() == len(line.rstrip()):
            src, url, line = m.group(1).strip(), m.group(2), line[:m.start()].rstrip()
        else:
            m = re.search(r"[(\uff08]([^()\uff08\uff09]*(?:WDI|IMF|维基|世界银行|世行|联合国)[^()\uff08\uff09]*)[)\uff09]$", line)
            if m:
                src, line = m.group(1).strip(), line[:m.start()].rstrip()
        i = min([j for j in (line.find(":"), line.find("\uff1a")) if j > 0] or [-1])
        k, v = (line[:i].strip(), line[i + 1:].strip()) if i > 0 else ("", line)
        y = _YEAR.match(v)
        v, year = (y.group(1).strip(), y.group(2)) if y else (v, "")
        out.append({"k": k, "v": v, "year": year, "src": src, "url": url, "bad": tone(k, v) if k else None})
    return out


def _src_md(r: dict) -> str:
    if r["url"]:
        return f"[{_md_text(r['src'] or _host(r['url']))}](<{r['url']}>)"
    return _md_text(r["src"])


def _val_md(r: dict) -> str:
    """数值:负面的加「(超警戒线:门槛)」(Word 里整格标红)。"""
    return _md_text(r["v"]) + (f"({BAD_LABEL}:{_md_text(r['bad'])})" if r["bad"] else "")


def _kpis(rows: list[dict]) -> list[tuple[str, dict]]:
    out = []
    for pat, label in KEY_METRICS:
        r = next((x for x in rows if re.search(pat, x["k"])), None)
        if r:
            out.append((label, r))
    return out


def kpi_table(kpis: list[tuple[str, dict]], per_row: int = 4) -> str:
    """关键指标卡:一行名称(带年份)、一行数值,每行 4 个;负面的名称后写明「超警戒线:门槛」,整张卡标红。"""
    if not kpis:
        return ""
    lines = []
    for k in range(0, len(kpis), per_row):
        part = kpis[k:k + per_row] + [("", None)] * (per_row - len(kpis[k:k + per_row]))
        lines.append("| " + " | ".join((f"{_md_text(lb)}({r['year']})" if r and r["year"] else _md_text(lb)) + (f" {BAD_LABEL}:{_md_text(r['bad'])}" if r and r["bad"] else "") for lb, r in part) + " |")
        if k == 0:
            lines.append("|" + "---|" * per_row)
        lines.append("| " + " | ".join(_kpi_value(r) if r else "" for _, r in part) + " |")
    return "\n".join(lines) + f"\n\nTable: {KPI_CAPTION}\n"


def _kpi_value(r: dict) -> str:
    """「1,470 亿美元」→ 数字大号、单位小一号(Word 里单位用 KPI Unit 样式);标红跟着名称行。"""
    m = re.match(r"^\s*(-?[\d,]+(?:\.\d+)?)\s*(.*)$", r["v"].replace("−", "-"))
    num, unit = (m.group(1), m.group(2).strip()) if m else (r["v"], "")
    if unit == "%":
        num, unit = num + "%", ""
    out = _md_text(num) + (f" [{_md_text(unit)}]{{custom-style=\"KPI Unit\"}}" if unit else "")
    return out


def key_points(g, rows: list[dict]) -> list[str]:
    """几句话的要点:经济、财政、水务各一句,再把标红的指标单列一句(只用表里有的数,没有的不提)。"""
    by = lambda pat: next((x for x in rows if re.search(pat, x["k"])), None)  # noqa: E731
    gdp, pc, gr, inf = by(r"^GDP\s*\(现价"), by(r"^人均 GDP(?!\s*\(PPP)"), by(r"实际增速"), by(r"通胀")
    debt, ext = by(r"政府总债务"), by(r"外债/GNI")
    pop, urb = by(r"^总人口"), by(r"^城镇化率")
    ws, ss, stress = by(r"安全管理饮水"), by(r"安全管理卫生"), by(r"淡水压力")
    inc = str(g("income_group") or "").split(";")[0].strip()
    out = []
    eco = [f"GDP {gdp['v']}" if gdp else "", f"人均 {pc['v']}" + (f"({inc})" if inc else "") if pc else "",
           f"实际增速 {gr['v']}" if gr else "", f"通胀 {inf['v']}" if inf else ""]
    if any(eco):
        out.append("经济:" + "，".join(x for x in eco if x) + "。")
    peo = [f"总人口 {pop['v']}" if pop else "", f"城镇化率 {urb['v']}" if urb else ""]
    if any(peo):
        out.append("人口:" + "，".join(x for x in peo if x) + "。")
    fis = [f"政府债务占 GDP {debt['v']}" if debt else "", f"外债占 GNI {ext['v']}" if ext else ""]
    if any(fis):
        out.append("财政与外债:" + "，".join(x for x in fis if x) + "。")
    wat = [f"安全管理饮水覆盖 {ws['v']}" if ws else "", f"安全管理卫生设施覆盖 {ss['v']}" if ss else "", f"淡水压力 {stress['v']}" if stress else ""]
    if any(wat):
        out.append("水务:" + "，".join(x for x in wat if x) + "。")
    bad = [f"{x['k']} {x['v']}(警戒线:{x['bad']})" for x in rows if x["bad"]]
    if bad:
        out.append(f"{BAD_LABEL}的指标:" + ";".join(bad) + "。")
    return out


def _dots_table(text: object, head: list[str]) -> str:
    """「名称 · 英文名 · 说明 · 网址」一行一条 → 表格,网址变成可点的「网站」。"""
    rows = []
    for line in (x.strip() for x in str(text or "").splitlines()):
        if not line:
            continue
        parts = [x.strip() for x in re.split(r"\s+·\s+", line)]
        url = parts.pop() if parts and re.match(r"https?://", parts[-1]) else ""
        cells = [_md_text(x) for x in parts[:len(head) - 1]]
        if len(parts) > len(head) - 1:
            cells[-1] += " · " + " · ".join(_md_text(x) for x in parts[len(head) - 1:])
        cells += [""] * (len(head) - 1 - len(cells))
        rows.append(cells + [f"[{_host(url)}](<{url}>)" if url else ""])
    return _table(head, rows) if rows else ""


TITLE_ZH_SYSTEM = ("你是水务与国际工程领域的翻译。把每条招标、采购或项目公告的标题译成简洁准确的中文:"
                   "机构与银行用通用中文译名,项目编号、合同号保留原文,不要解释。")
TITLE_ZH_GUIDE = '\n输出格式:只输出一个 JSON 对象 {"items": [{"i": 序号, "zh": "中文标题"}, …]},不要任何其他文字。\n'
TITLE_ZH_SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": {
    "type": "object", "properties": {"i": {"type": "integer"}, "zh": {"type": "string"}}, "required": ["i", "zh"], "additionalProperties": False}}},
    "required": ["items"], "additionalProperties": False}
TITLE_ZH_BATCH = 40


_CJK = re.compile(r"[\u4e00-\u9fff]")


def _zh_name(ctx: Ctx, r: dict, orig: str, n: int = 80) -> tuple[str, str]:
    """core.zh_name 的取名顺序,但项目名、摘要里没有汉字(模型偶尔照抄英文原名)的不算中文名。"""
    b = ctx.base
    pn, sz = (str(b.get("lead", r, p) or "") for p in ("project_name", "summary_zh"))
    return zh_name(orig, sz if _CJK.search(sz) else None, pn if _CJK.search(pn) else None, n)


def _orig_title(ctx: Ctx, r: dict, n: int = 120) -> str:
    return (str(ctx.base.get("lead", r, "title") or "").strip().splitlines() or [""])[0][:n]


def translate_titles(ctx: Ctx, leads: list[dict]) -> dict[str, str]:
    """没有中文项目名与中文摘要的线索,请模型把原题译成中文(报告用,不写回表格);译过的存在本机缓存,下次不再花钱。
    没配模型或调用失败时返回已缓存的,其余线索照旧用「要点」。→ {原题: 中文}"""
    want = []
    for r in leads:
        t = _orig_title(ctx, r, 300)
        if t and _zh_name(ctx, r, t)[1] not in ("项目名", "摘要"):
            want.append(t)
    return translate_texts(ctx, want)


def translate_texts(ctx: Ctx, texts: list[str]) -> dict[str, str]:
    """一组外文标题(线索原题、商机名)→ {原文: 中文};已含汉字的不译;缓存与 translate_titles 同一个文件。"""
    fp = ctx.cache.root.parent / "title_zh.json"
    try:
        done: dict[str, str] = json.loads(fp.read_text(encoding="utf-8")) if fp.exists() else {}
    except (OSError, ValueError):
        done = {}
    want = []
    for t in texts:
        t = (t or "").strip()
        if t and t not in done and t not in want and not _CJK.search(t):
            want.append(t)
    clf = ctx.clf
    if want and clf is not None and getattr(clf, "enabled", False) and hasattr(clf, "ask"):
        for k in range(0, len(want), TITLE_ZH_BATCH):
            part = want[k:k + TITLE_ZH_BATCH]
            out = clf.ask(TITLE_ZH_SYSTEM, json.dumps([{"i": i, "t": t} for i, t in enumerate(part)], ensure_ascii=False),
                          TITLE_ZH_SCHEMA, TITLE_ZH_GUIDE)
            if not out or out.get("_error"):
                ctx.log(f"  ! 线索标题翻译没成功,其余用要点:{(out or {}).get('_error') or '模型未返回'}")
                break
            for it in out.get("items") or []:
                try:
                    i, zh = int(it.get("i")), str(it.get("zh") or "").strip()
                except (TypeError, ValueError, AttributeError):
                    continue
                if 0 <= i < len(part) and zh:
                    done[part[i]] = zh
        try:
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(json.dumps(done, ensure_ascii=False, indent=0), encoding="utf-8")
        except OSError:
            pass
    return done


def _lead_title(ctx: Ctx, r: dict, zh_of: dict[str, str]) -> tuple[str, str]:
    """→ (标题格, 中文名的来源):中文标题在前、可点到原文网页,原题跟在括号里。"""
    b = ctx.base
    orig = _orig_title(ctx, r)
    name, how = _zh_name(ctx, r, orig, 400)
    if how not in ("项目名", "摘要") and zh_of.get(_orig_title(ctx, r, 300)):
        name, how = zh_of[_orig_title(ctx, r, 300)], "译名"
    elif how == "要点":
        name = "要点:" + name
    name = name[:70] + "…" if len(name) > 72 else name
    url = str(b.get("lead", r, "url") or "").strip()
    text = _md_text(name or orig[:80])
    cell_ = f"[{text}](<{url}>)" if url.startswith(("http://", "https://")) else text
    if name and orig and orig[:80] != name:
        cell_ += f"(原文:{_md_text(orig[:80])}{'…' if len(orig) > 80 else ''})"
    return cell_, how


def _lead_rows(ctx: Ctx, leads: list[dict], with_country: bool = True) -> list[list[object]]:
    b = ctx.base
    zh_of = translate_titles(ctx, leads)
    out = []
    for r in leads:
        g = lambda p: b.get("lead", r, p)  # noqa: E731
        title, how = _lead_title(ctx, r, zh_of)
        summ = str(g("summary_zh") or "").strip()
        if how == "摘要":  # 标题已用了摘要首行,摘要栏只放其余部分
            summ = "\n".join(summ.splitlines()[1:]).strip()
        row = [g("key"), g("category"), title, tstore.day(g("published") or g("found_at"), b.tz), tstore.day(g("nearest_deadline"), b.tz), summ[:120]]
        if with_country:
            row.insert(1, _cell(g("country")))
        out.append(row)
    return out


def _opp_rows(ctx: Ctx, opps: list[dict]) -> list[list[object]]:
    b = ctx.base
    g = lambda r, p: b.get("opportunity", r, p)  # noqa: E731
    return [[g(r, "code"), g(r, "name_zh"), _cell(g(r, "country")), g(r, "stage"), g(r, "public_stage"), g(r, "next_milestone"),
             (g(r, "financing") or "").split("\n")[0]] for r in opps]


def _ms_rows(ctx: Ctx, ms: list[dict], opp_code: dict[str, str]) -> list[list[object]]:
    b = ctx.base
    g = lambda r, p: b.get("milestone", r, p)  # noqa: E731
    return [[tstore.day(g(r, "day"), b.tz), g(r, "kind"), opp_code.get(tstore.link_id(g(r, "opportunity")) or "", "") or _cell(g(r, "opportunity")),
             _cell(g(r, "lead")), g(r, "status"), g(r, "label")] for r in ms]


def _fu_rows(ctx: Ctx, fus: list[dict], opp_code: dict[str, str]) -> list[list[object]]:
    b = ctx.base
    g = lambda r, p: b.get("followup", r, p)  # noqa: E731
    return [[tstore.day(g(r, "day"), b.tz), opp_code.get(tstore.link_id(g(r, "opportunity")) or "", ""), _cell(g(r, "author")),
             g(r, "stage_change"), (g(r, "content") or "")[:120], g(r, "next_step")] for r in fus]


def _analysis(ctx: Ctx, kind: str, dataset: str, known: set[str]) -> tuple[dict, str]:
    clf = ctx.clf
    if clf is None or not getattr(clf, "enabled", False) or not hasattr(clf, "write"):
        return {}, "本报告未使用模型,分析段落从略;摘要按统计生成。"
    system = SYSTEM.format(bidder=(ctx.profile.bidder or {}).get("nationality") or "未设", kind=kind)
    out = clf.write(system, dataset, FIELDS)
    if not out or out.get("_error"):
        return {}, f"模型分析未成功({(out or {}).get('_error') or '模型未返回'}),分析段落从略。"
    out, bad = reporting._check_cites(out, known)
    note = f"分析段落由模型({clf.label})根据本期数据撰写,方括号内为线索号或商机编号,请以原文为准。"
    if bad:
        note += f"其中 {len(bad)} 个引用编号在本期数据里没有找到,已标注\"未核到\",相关表述须核实。"
    return out, note


def _compose(title: str, kind: str, ctx: Ctx, chapters: list[tuple[str, str]], known: set[str]) -> str:
    """封面标题 → 摘要(模型或统计)→ 数据章节 → 分析章节 → 说明。"""
    data_md = "\n".join(f"## {h}\n\n{body}" for h, body in chapters)
    ana, note = _analysis(ctx, kind, data_md, known)
    parts = [f"# {title}\n", f"生成日期:{ctx.today.isoformat()}\n"]
    if ana.get("summary"):
        parts.append("## 摘要\n\n" + reporting._plain(ana["summary"]) + "\n")
    else:
        parts.append("## 摘要\n\n" + chapters[0][1].split("\n\n")[0] + "\n")
    n = 0
    for h, body in chapters:
        n += 1
        parts.append(f"## {n}. {h}\n\n{body}")
    for key, h in (("dynamics", "本期动态分析"), ("opportunities", "重点机会研判"), ("actions", "行动建议"), ("risks", "风险提示")):
        if ana.get(key):
            n += 1
            parts.append(f"## {n}. {h}\n\n{reporting._plain(ana[key])}\n")
    parts.append(f"## 说明\n\n{note}\n数据来自部门共用表格(线索、商机、跟进记录、时间节点、国别),生成于 {now_iso()}。\n")
    return "\n".join(parts)


def _cites(ctx: Ctx, leads: list[dict], opps: list[dict]) -> tuple[set[str], set[str]]:
    """引用的线索 = 列出的线索 ∪ 写进的商机当时的所属线索;写进的商机。"""
    b = ctx.base
    lead_rids = {r["id"] for r in leads}
    opp_rids = {r["id"] for r in opps}
    for r in opps:
        for ld in b.rows("lead", filter=b.filter(b.cond("lead", "opportunity", "is", r["id"])), props=["key"]) if b.has("lead", "opportunity") else []:
            lead_rids.add(ld["id"])
    return lead_rids, opp_rids


def _known(ctx: Ctx, leads: list[dict], opps: list[dict]) -> set[str]:
    b = ctx.base
    return {str(b.get("lead", r, "key") or "") for r in leads} | {str(b.get("opportunity", r, "code") or "") for r in opps}


# ---------- 三种报告 ----------

def build_periodic(ctx: Ctx, kind: str, start: date, end: date, iso: str | None) -> tuple[str, set[str], set[str], str]:
    b = ctx.base
    leads = _leads_between(ctx, start, end, iso)
    all_opps = _opps(ctx, iso)
    code_of = {r["id"]: str(b.get("opportunity", r, "code") or "") for r in all_opps}
    new_opps = [r for r in all_opps if start.isoformat() <= str(r.get("createdTime") or "")[:10] <= end.isoformat()]
    fus = _followups_between(ctx, start, end, set(code_of))
    changed = {tstore.link_id(b.get("followup", r, "opportunity")) for r in fus if b.get("followup", r, "stage_change")}
    touched = [r for r in all_opps if r["id"] in changed or r in new_opps]
    tracked = {r["id"] for r in all_opps if b.get("opportunity", r, "stage") in TRACKED_STAGES}  # 与页面同一口径(本体 glossary.tracked)
    ms = _milestones(ctx, ctx.today, ctx.today + timedelta(days=UPCOMING_DAYS), tracked, {r["id"] for r in leads})
    by_c = Counter(_cell(b.get("lead", r, "country")) for r in leads)
    by_k = Counter(str(b.get("lead", r, "category") or "") for r in leads)
    scope_zh = geo.name_zh(iso) if iso else "全部国别"
    overview = (f"本期({start} 至 {end}){scope_zh}新增相关线索 {len(leads)} 条;新建商机 {len(new_opps)} 个;阶段有变化的商机 {len(changed)} 个;"
                f"未来 {UPCOMING_DAYS} 天有 {len(ms)} 个时间节点。\n\n"
                + "按国别:" + ("、".join(f"{k} {v}" for k, v in by_c.most_common()) or "无") + "\n\n"
                + "按类别:" + ("、".join(f"{k} {v}" for k, v in by_k.most_common()) or "无") + "\n")
    chapters = [
        ("本期概览", overview),
        ("本期线索", _table(["线索号", "国别", "类别", "标题(点开看原文)", "发布", "最近截止", "摘要"], _lead_rows(ctx, leads))),
        ("商机进展", "新建或阶段有变化的商机:\n\n" + _table(["编号", "名称", "国别", "我方阶段", "公开进度", "下一节点", "资金"], _opp_rows(ctx, touched))
         + "\n本期跟进:\n\n" + _table(["日期", "商机", "记录人", "阶段变更", "内容", "下一步"], _fu_rows(ctx, fus, code_of))),
        (f"未来 {UPCOMING_DAYS} 天时间节点", _table(["日期", "类型", "商机", "线索", "状态", "说明"], _ms_rows(ctx, ms, code_of))),
    ]
    title = f"{scope_zh}水务市场{kind}({start} 至 {end})"
    md = _compose(title, kind, ctx, chapters, _known(ctx, leads, touched))
    cl, co = _cites(ctx, leads, touched)
    return md, cl, co, (iso or "") + (start.strftime("%Y-W%V") if kind == "周报" else start.strftime("%Y-%m"))


def build_country(ctx: Ctx, iso: str, internal: bool = True) -> tuple[str, set[str], set[str], str]:
    """国别报告:章节照阿塞拜疆成稿(core.jobs.country_report)。"""
    from . import country_report
    return country_report.build_country(ctx, iso, internal)


__all__ = ["run", "generate", "ensure_periodic", "ensure_daily", "reset_stale", "pending", "Path"]
