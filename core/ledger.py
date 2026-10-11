"""项目机会台账(ledger):按国别人工维护的项目清单 → 校验 + 派生字段。

一国一份:countries/{ISO3}/ledger.yaml(项目,只写公开来源事实,每条事实带来源编号)
        + 同目录 sources.yaml(来源登记:编号 → 链接/机构/日期/等级/获取方式)。
派生字段(只算不写回 YAML):
- 投标资格:reference/procurement_rules.yaml 的采购制度规则 × profile.bidder.nationality;
  项目公告里的明示条款(procurement.eligibility)优先于通用规则;
- 下一节点:最近的、投标人需要动作的未来节点(招标发布/意向书/资审/投标/PPP 标书截止);
- 最晚启动日期:下一节点日期(取所在区间的起点,保守) − profile.lead_times(使用方内部流程周期);
- 核实状态:台账人工填写,core 只汇总并提示过期未更新的计划节点。
core 不做自动评分;优先级、角色判断、非公开信息写在使用方私有的 overlay 里。
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
RULES_PATH = ROOT / "reference" / "procurement_rules.yaml"


def template_file(name: str, default: str | None = None) -> Path:
    """Word 版式模板:私有配置目录 private/templates/ 里有就用它,没有用程序自带的 core/templates/(不带任何使用方信息)。"""
    own = ROOT / "private" / "templates" / name
    return own if own.exists() else ROOT / "core" / "templates" / (default or name)
LEDGER_SCHEMA = "osint-water/ledger@1"
OVERLAY_SCHEMA = "osint-water/overlay@1"

STAGES = {
    "plan": "规划(未立项/资金未落实)",
    "preparation": "前期(可研/设计/融资洽谈)",
    "financing_signed": "融资已落实",
    "procurement": "采购进行中",
    "awarded": "已定标",
    "construction": "在建",
    "completed": "已完工",
    "on_hold": "暂停",
    "cancelled": "取消",
}
SEGMENTS = {
    "water_supply": "供水", "transmission": "输水干管", "reservoir": "水库/调蓄", "nrw": "减漏/计量",
    "wastewater": "污水", "sludge": "污泥", "reuse": "再生水", "stormwater": "雨水排涝",
    "desalination": "海水淡化", "irrigation": "灌溉", "flood": "防洪", "environment": "水环境治理",
}
MILESTONES = {
    "concept": "立项/概念审查", "board_approval": "董事会批准", "mou": "谅解备忘录", "loan_signing": "贷款/协议签署",
    "effectiveness": "协议生效", "gpn": "总采购公告(GPN)", "tender_launch": "招标发布", "eoi_deadline": "意向书截止",
    "pq_deadline": "资格预审截止", "bid_deadline": "投标截止", "rfp_deadline": "PPP 标书截止", "award": "定标/授标",
    "contract_signing": "合同签署", "construction_start": "开工", "completion": "完工", "cod": "商业运营",
    "other": "其他",
}
# 投标人需要动作的节点;最晚启动日期只对这些节点计算
ACTIONABLE = ("tender_launch", "eoi_deadline", "pq_deadline", "bid_deadline", "rfp_deadline")
MS_STATUS = {"actual": "已发生", "planned": "官方计划", "expected": "推断"}
PKG_STATUS = {"planned": "计划", "open": "在招", "closed": "已截标", "awarded": "已授标", "cancelled": "取消"}
PKG_TYPES = {"works": "工程", "goods": "货物", "consultancy": "咨询", "ppp": "PPP", "design_build": "设计施工"}
INSTRUMENTS = {
    "sovereign_loan": "主权贷款", "nonsovereign_loan": "非主权贷款", "grant": "赠款", "ta_grant": "技术援助赠款",
    "budget": "国家预算", "sovereign_fund": "国家石油基金拨款", "ppp_equity": "PPP 股权", "ppp_debt": "PPP 项目融资",
    "corporate": "企业自有资金", "unknown": "待核实",
}
NATIONALITY = {
    "open_all": "各国企业均可", "members_only": "仅限成员国企业", "tied": "限定来源国", "domestic_open": "本国采购法(外国企业可参与)",
    "per_tender": "以招标文件为准", "unknown": "待核实",
}
FUNDING_STATUS = {
    "reported": "仅见报道", "mou": "备忘录", "pipeline": "储备", "proposed": "拟议", "approved": "已批准",
    "signed": "已签署", "effective": "已生效", "disbursing": "拨付中", "completed": "已完成",
}
VERIFY_STATUS = {
    "verified": "已核实(关键事实有一手来源)", "partial": "部分核实", "unverified": "未核实(仅二手/成稿转引)",
    "conflicting": "来源冲突",
}
GRADES = {"A": "政府/国际组织/出资方一手", "B": "权威二手(行业媒体、招标库、专业机构)", "C": "媒体与企业新闻", "D": "不可用"}
VIA = {"primary_read": "读原文", "search_snippet": "检索摘要", "secondary_read": "读二手原文", "report_baseline": "成稿转引",
       "auto_extract": "系统自动写入(未经人工核对)"}
ELIG = {"eligible": "可直接投标", "presumed": "推定可投(规则待核实)", "conditional": "有条件", "restricted": "受限", "unknown": "待核实"}

_ID = re.compile(r"^[A-Z]{3}-\d{3}$")
_PD = re.compile(r"^(\d{4})(?:-(Q[1-4]|H[12]|\d{2})(?:-(\d{2}))?)?$")


class LedgerError(ValueError):
    pass


# 各层允许的字段。多出来的字段多半是流式写法 {…}/[…] 里的英文逗号把文本切断了,按错误报出。
_KEYS = {
    "item": {"id", "name_zh", "name_en", "name_local", "aliases", "brief", "location", "segments", "in_scope", "stage",
             "stage_note", "owner", "ids", "financing", "total_cost", "scope", "procurement", "milestones", "parties", "notes",
             "verification", "src"},
    "location": {"city_zh", "city_en", "detail"},
    "financing": {"funder", "instrument", "amount", "currency", "status", "date", "note", "src"},
    "total_cost": {"amount", "currency", "note", "src"},
    "scope": {"item", "qty", "unit", "note", "src"},
    "procurement": {"regime", "other_regimes", "method", "note", "packages", "eligibility"},
    "package": {"name", "type", "status", "published", "deadline", "deadline_type", "date_status", "awardee", "value",
                "note", "src"},
    "eligibility": {"status", "basis"},
    "milestone": {"type", "date", "status", "note", "src"},
    "party": {"role", "name", "note", "src"},
    "note": {"text", "src"},
    "verification": {"status", "checked_at", "method", "pending", "conflicts"},
    "event": {"name", "date", "end", "place", "note", "src"},
    "source": {"title", "publisher", "date", "grade", "via", "url", "note"},
}
_NULLABLE = {"amount", "currency", "deadline", "published", "qty", "unit", "note", "src"}


def _shape(obj: object, kind: str, where: str) -> list[str]:
    """字段名与空值检查:未知字段、或非可空字段为 null → 报错(典型成因:{} 或 [] 里的英文逗号)。"""
    if not isinstance(obj, dict):
        return [f"{where}: 应为映射,实为 {type(obj).__name__}"] if kind not in ("note",) or not isinstance(obj, str) else []
    errs = [f"{where}: 未知字段 {k!r}(流式写法里的英文逗号会切断文本,请改用中文逗号或给值加引号)"
            for k in obj if k not in _KEYS[kind]]
    errs += [f"{where}.{k}: 值为空" for k, v in obj.items() if v is None and k in _KEYS[kind] and k not in _NULLABLE]
    return errs


def _free_map(obj: object, where: str) -> list[str]:
    """owner/ids 这类自由键映射:值不许为空。"""
    if obj is None:
        return []
    if not isinstance(obj, dict):
        return [f"{where}: 应为映射"]
    return [f"{where}.{k}: 值为空(可能被英文逗号切断)" for k, v in obj.items() if v is None]


# ---------- 部分日期:2026 / 2026-H2 / 2026-Q4 / 2026-12 / 2026-12-15 ----------

@dataclass(frozen=True)
class PDate:
    raw: str
    start: date
    end: date
    precision: str  # day | month | quarter | half | year


def _month_end(y: int, m: int) -> date:
    return date(y, m, calendar.monthrange(y, m)[1])


def parse_pdate(v: object) -> PDate:
    if isinstance(v, date):
        return PDate(v.isoformat(), v, v, "day")
    s = str(v).strip()
    m = _PD.match(s)
    if not m:
        raise ValueError(f"日期格式不认识: {v!r}(可用 2026 / 2026-H2 / 2026-Q4 / 2026-12 / 2026-12-15)")
    y, mid, d = int(m[1]), m[2], m[3]
    if mid is None:
        return PDate(s, date(y, 1, 1), date(y, 12, 31), "year")
    if mid[0] == "Q":
        sm = 3 * int(mid[1]) - 2
        return PDate(s, date(y, sm, 1), _month_end(y, sm + 2), "quarter")
    if mid[0] == "H":
        sm = 1 if mid[1] == "1" else 7
        return PDate(s, date(y, sm, 1), _month_end(y, sm + 5), "half")
    mo = int(mid)
    if not 1 <= mo <= 12:
        raise ValueError(f"日期不存在: {v!r}(月份应为 01–12)")
    if d is None:
        return PDate(s, date(y, mo, 1), _month_end(y, mo), "month")
    try:
        dd = date(y, mo, int(d))
    except ValueError:
        raise ValueError(f"日期不存在: {v!r}({y} 年 {mo} 月没有 {int(d)} 日)") from None
    return PDate(s, dd, dd, "day")


# ---------- 加载与校验 ----------

@dataclass
class Ledger:
    path: Path
    country: str
    as_of: date
    fx: dict[str, float]
    items: list[dict]
    sources: dict[str, dict]
    meta: dict


def load_rules(path: str | Path = RULES_PATH) -> dict:
    rules = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    errs = []
    for k, r in (rules.get("regimes") or {}).items():
        if r.get("nationality") not in NATIONALITY:
            errs.append(f"regime {k}: nationality={r.get('nationality')!r}")
        if r.get("funder") and r["funder"] not in (rules.get("funders") or {}):
            errs.append(f"regime {k}: funder {r['funder']} 未在 funders 中登记")
        if r.get("local_registration") not in ("none", "bid", "contract", "unknown", None):
            errs.append(f"regime {k}: local_registration={r.get('local_registration')!r}")
    if errs:
        raise LedgerError("规则表校验失败:\n  " + "\n  ".join(errs))
    return rules


def load_ledger(path: str | Path, rules: dict | None = None) -> Ledger:
    p = Path(path)
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    sources = raw.get("sources")
    if sources is None:
        sp = p.with_name("sources.yaml")
        sources = (yaml.safe_load(sp.read_text(encoding="utf-8")) or {}).get("sources", {}) if sp.exists() else {}
    led = Ledger(
        path=p,
        country=str(raw.get("country", "")).upper(),
        as_of=parse_pdate(raw.get("as_of", "1970-01-01")).start,
        fx={k.upper(): float(v) for k, v in (raw.get("fx") or {"USD": 1}).items()},
        items=raw.get("opportunities") or [],
        sources=sources or {},
        meta={k: v for k, v in raw.items() if k not in ("opportunities", "sources", "fx")},
    )
    errs = validate(led, rules, raw.get("schema"))
    if errs:
        raise LedgerError(f"{p} 校验失败({len(errs)} 处):\n  " + "\n  ".join(errs))
    return led


def _refs(obj: object) -> list[str]:
    """递归收集所有 src 引用。"""
    out: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "src":
                out.extend(v if isinstance(v, list) else [v])
            else:
                out.extend(_refs(v))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(_refs(v))
    return out


def cited(led: Ledger) -> list[str]:
    """台账里实际被引用的来源编号(条目、准入门槛、活动),按登记顺序。报告附录只列这些。"""
    used = set(_refs(led.items)) | set(_refs(led.meta.get("access") or [])) | set(_refs(led.meta.get("events") or []))
    return [k for k in led.sources if k in used]


def validate(led: Ledger, rules: dict | None = None, schema: str | None = LEDGER_SCHEMA) -> list[str]:
    errs: list[str] = []
    if schema != LEDGER_SCHEMA:
        errs.append(f"schema 应为 {LEDGER_SCHEMA},实为 {schema!r}")
    if not re.fullmatch(r"[A-Z]{3}", led.country):
        errs.append(f"country 应为 ISO3,实为 {led.country!r}")
    for sid, s in led.sources.items():
        errs += _shape(s, "source", f"来源 {sid}")
        if s.get("grade") not in GRADES:
            errs.append(f"来源 {sid}: grade={s.get('grade')!r}")
        if s.get("via") not in VIA:
            errs.append(f"来源 {sid}: via={s.get('via')!r}")
        if not (s.get("url") or s.get("title")):
            errs.append(f"来源 {sid}: 至少要有 url 或 title")
    for i, x in enumerate(led.meta.get("access") or []):
        errs += _shape(x, "note", f"准入门槛[{i}]")
        errs += [f"准入门槛[{i}]: 引用了未登记的来源 {r}" for r in _refs(x) if r not in led.sources]
    for ev in led.meta.get("events") or []:
        errs += _shape(ev, "event", f"活动 {ev.get('name')}")
        try:
            parse_pdate(ev.get("date"))
        except ValueError as x:
            errs.append(f"活动 {ev.get('name')}: {x}")
        errs += [f"活动 {ev.get('name')}: 引用了未登记的来源 {r}" for r in _refs(ev) if r not in led.sources]
    seen: set[str] = set()
    for it in led.items:
        iid = str(it.get("id", "?"))
        e = lambda msg: errs.append(f"{iid}: {msg}")  # noqa: E731
        errs.extend(f"{iid}: {x}" for x in _shape(it, "item", "条目"))
        for k, kind in (("location", "location"), ("total_cost", "total_cost"), ("procurement", "procurement"),
                        ("verification", "verification")):
            if it.get(k) is not None:
                errs.extend(f"{iid}: {x}" for x in _shape(it[k], kind, k))
        for k, kind in (("financing", "financing"), ("scope", "scope"), ("milestones", "milestone"), ("parties", "party")):
            for i, x in enumerate(it.get(k) or []):
                errs.extend(f"{iid}: {m}" for m in _shape(x, kind, f"{k}[{i}]"))
        for i, x in enumerate(it.get("notes") or []):
            if isinstance(x, dict):
                errs.extend(f"{iid}: {m}" for m in _shape(x, "note", f"notes[{i}]"))
        pr_ = it.get("procurement") or {}
        for i, x in enumerate(pr_.get("packages") or []):
            errs.extend(f"{iid}: {m}" for m in _shape(x, "package", f"packages[{i}]"))
        if pr_.get("eligibility") is not None:
            errs.extend(f"{iid}: {m}" for m in _shape(pr_["eligibility"], "eligibility", "procurement.eligibility"))
        for k in ("owner", "ids"):
            errs.extend(f"{iid}: {m}" for m in _free_map(it.get(k), k))
        if not _ID.match(iid) or not iid.startswith(led.country):
            e("id 应为 '{ISO3}-三位序号'")
        if iid in seen:
            e("id 重复")
        seen.add(iid)
        for k in ("name_zh", "stage", "segments", "verification"):
            if not it.get(k):
                e(f"缺少 {k}")
        if it.get("stage") and it["stage"] not in STAGES:
            e(f"stage={it['stage']!r}")
        for sg in it.get("segments") or []:
            if sg not in SEGMENTS:
                e(f"segment={sg!r}")
        for f in it.get("financing") or []:
            if f.get("status") not in FUNDING_STATUS:
                e(f"financing {f.get('funder')}: status={f.get('status')!r}")
            if f.get("instrument") not in INSTRUMENTS:
                e(f"financing {f.get('funder')}: instrument={f.get('instrument')!r}")
            if f.get("amount") is not None and str(f.get("currency", "")).upper() not in led.fx:
                e(f"financing {f.get('funder')}: 币种 {f.get('currency')!r} 不在 fx 表")
        tc = it.get("total_cost") or {}
        if tc.get("amount") is not None and str(tc.get("currency", "")).upper() not in led.fx:
            e(f"total_cost 币种 {tc.get('currency')!r} 不在 fx 表")
        for m in it.get("milestones") or []:
            if m.get("type") not in MILESTONES:
                e(f"milestone type={m.get('type')!r}")
            if m.get("status") not in MS_STATUS:
                e(f"milestone {m.get('type')}: status={m.get('status')!r}")
            try:
                parse_pdate(m.get("date"))
            except ValueError as x:
                e(f"milestone {m.get('type')}: {x}")
        proc = it.get("procurement") or {}
        for pk in proc.get("packages") or []:
            if pk.get("status") not in PKG_STATUS:
                e(f"package {pk.get('name')}: status={pk.get('status')!r}")
            if pk.get("type") not in PKG_TYPES:
                e(f"package {pk.get('name')}: type={pk.get('type')!r}")
            for dk in ("published", "deadline"):
                if pk.get(dk) is not None:
                    try:
                        parse_pdate(pk[dk])
                    except ValueError as x:
                        e(f"package {pk.get('name')}.{dk}: {x}")
            if pk.get("deadline_type", "bid_deadline") not in ACTIONABLE:
                e(f"package {pk.get('name')}: deadline_type={pk.get('deadline_type')!r}")
        if rules is not None:
            for rg in [proc.get("regime")] + list(proc.get("other_regimes") or []):
                if rg and rg != "unknown" and rg not in (rules.get("regimes") or {}):
                    e(f"采购制度 {rg} 不在规则表")
        ov = proc.get("eligibility")
        if ov and ov.get("status") not in ELIG:
            e(f"procurement.eligibility.status={ov.get('status')!r}")
        v = it.get("verification") or {}
        if v and v.get("status") not in VERIFY_STATUS:
            e(f"verification.status={v.get('status')!r}")
        for ref in _refs(it):
            if ref not in led.sources:
                e(f"引用了未登记的来源 {ref}")
    return errs


def load_overlay(path: str | Path | None) -> dict:
    """使用方私有叠加(优先级、角色、非公开情报、建议动作)。不存在时返回空。"""
    if not path or not Path(path).exists():
        return {}
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if raw.get("schema") != OVERLAY_SCHEMA:
        raise LedgerError(f"{path}: schema 应为 {OVERLAY_SCHEMA}")
    return raw


# ---------- 派生 ----------

@dataclass(frozen=True)
class Eligibility:
    status: str  # eligible | conditional | restricted | unknown
    regime: str
    basis: str


@dataclass(frozen=True)
class Node:
    type: str
    when: PDate
    status: str
    label: str


@dataclass(frozen=True)
class Timing:
    node: Node | None
    latest_start: date | None
    lead_days: int
    lead_basis: str
    days_left: int | None


def to_usd(amount: float | None, currency: str | None, fx: dict[str, float]) -> float | None:
    if amount is None or not currency:
        return None
    rate = fx.get(str(currency).upper())
    return float(amount) * rate if rate else None


def size_usd(it: dict, fx: dict[str, float]) -> float | None:
    """项目体量(美元):优先总投资,否则已落实/拟议资金之和(不含备忘录与仅见报道)。"""
    tc = it.get("total_cost") or {}
    v = to_usd(tc.get("amount"), tc.get("currency"), fx)
    if v is not None:
        return v
    vals = [
        to_usd(f.get("amount"), f.get("currency"), fx)
        for f in it.get("financing") or []
        if f.get("status") not in ("mou", "reported")
    ]
    vals = [x for x in vals if x is not None]
    return sum(vals) if vals else None


def eligibility(it: dict, rules: dict, nationality: str | None, has_local_entity: bool = False) -> Eligibility:
    proc = it.get("procurement") or {}
    regime = proc.get("regime") or "unknown"
    ov = proc.get("eligibility")
    if ov:  # 项目文件的明示条款优先
        return Eligibility(ov["status"], regime, ov.get("basis", "项目文件明示"))
    rule = (rules.get("regimes") or {}).get(regime)
    if not rule:
        return Eligibility("unknown", regime, "采购制度未定或未收录,以招标文件为准")
    nat = rule.get("nationality")
    if not nationality:
        return Eligibility("unknown", regime, "profile 未设投标人国籍")
    status, basis = "unknown", rule.get("summary", "")
    if nat == "open_all":
        status = "eligible"
    elif nat == "members_only":
        f = (rules.get("funders") or {}).get(rule.get("funder"), {})
        if nationality in (f.get("known_members") or []):
            status, basis = "eligible", f"{basis}(投标人国籍为成员国)"
        elif nationality in (f.get("known_non_members") or []):
            status, basis = "restricted", f"{basis}(投标人国籍非成员国:可作分包/供货或借道成员国主体,以招标文件为准)"
        else:
            basis = f"{basis}(投标人国籍的成员身份未登记)"
    elif nat == "tied":
        status = "eligible" if nationality in (rule.get("tied_to") or []) else "restricted"
    elif nat in ("domestic_open", "per_tender"):
        status = "conditional"
    if status == "eligible" and rule.get("local_registration") == "bid" and not has_local_entity:
        status, basis = "conditional", f"{basis};投标前须完成当地注册/电子签名等手续"
    if rule.get("grade") == "C":  # 规则本身未核实:结论降为"推定"
        status, basis = ("presumed" if status == "eligible" else status), f"{basis}(规则未核实)"
    return Eligibility(status, regime, basis)


def _candidates(it: dict, today: date) -> list[Node]:
    out = []
    for m in it.get("milestones") or []:
        if m.get("type") in ACTIONABLE and m.get("status") != "actual":
            pd = parse_pdate(m["date"])
            if pd.end >= today:
                out.append(Node(m["type"], pd, m["status"], m.get("note") or MILESTONES[m["type"]]))
    for pk in (it.get("procurement") or {}).get("packages") or []:
        if pk.get("status") in ("planned", "open") and pk.get("deadline") is not None:
            pd = parse_pdate(pk["deadline"])
            if pd.end >= today:
                st = "planned" if pk["status"] == "open" else pk.get("date_status", "expected")
                out.append(Node(pk.get("deadline_type", "bid_deadline"), pd, st, pk.get("name", "")))
    return out


def timing(it: dict, lead_times: dict, today: date, registration_days: int = 0) -> Timing:
    cands = _candidates(it, today)
    if not cands:
        return Timing(None, None, 0, "无已知的未来节点,需跟踪", None)
    node = min(cands, key=lambda n: (n.when.start, n.when.end))
    comp = {k: int(v) for k, v in (lead_times.get(node.type) or {}).items()}
    if registration_days:
        comp["本地注册"] = registration_days
    days = sum(comp.values())
    ls = node.when.start - timedelta(days=days)
    basis = " + ".join(f"{k} {v} 天" for k, v in comp.items()) or "未设内部周期"
    return Timing(node, ls, days, basis, (ls - today).days)


def stale_milestones(it: dict, today: date) -> list[str]:
    """计划/推断节点的区间已结束但仍未改为'已发生'——台账需要更新。"""
    out = []
    for m in it.get("milestones") or []:
        if m.get("status") != "actual" and parse_pdate(m["date"]).end < today:
            out.append(f"{MILESTONES.get(m['type'], m['type'])}({m['date']},{MS_STATUS.get(m['status'], m['status'])})已过期未更新")
    return out


@dataclass(frozen=True)
class Derived:
    item: dict
    size_usd: float | None
    elig: Eligibility
    timing: Timing
    stale: list[str]


def derive(led: Ledger, rules: dict, bidder: dict, lead_times: dict, today: date) -> list[Derived]:
    nat = (bidder or {}).get("nationality")
    has_entity = bool(((bidder or {}).get("local_entity") or {}).get(led.country))
    reg_days = 0 if has_entity else int((lead_times or {}).get("local_registration", 0) or 0)
    out = []
    for it in led.items:
        el = eligibility(it, rules, nat, has_entity)
        rule = (rules.get("regimes") or {}).get(el.regime) or {}
        need_reg = rule.get("local_registration") == "bid" and not has_entity
        out.append(
            Derived(
                item=it,
                size_usd=size_usd(it, led.fx),
                elig=el,
                timing=timing(it, lead_times or {}, today, reg_days if need_reg else 0),
                stale=stale_milestones(it, today),
            )
        )
    return out


def rows(led: Ledger, ds: list[Derived], fetched_at: str) -> dict[str, list[dict]]:
    """→ opportunity(一项一行)与 opportunity_milestone(长表),供快照与 DuckDB 查询。"""
    opp, ms = [], []
    for d in ds:
        it, t = d.item, d.timing
        loc = it.get("location") or {}
        opp.append(
            {
                "id": it["id"], "iso3": led.country, "name_zh": it.get("name_zh"), "name_en": it.get("name_en"),
                "city": loc.get("city_zh") or loc.get("city_en"), "segments": ",".join(it.get("segments") or []),
                "in_scope": bool(it.get("in_scope", True)), "stage": it.get("stage"),
                "funders": ",".join(sorted({str(f.get("funder")) for f in it.get("financing") or []})),
                "size_usd": d.size_usd, "regime": d.elig.regime, "eligibility": d.elig.status,
                "next_node": t.node.type if t.node else None, "next_node_date": t.node.when.start if t.node else None,
                "latest_start": t.latest_start, "verification": (it.get("verification") or {}).get("status"),
                "checked_at": str((it.get("verification") or {}).get("checked_at") or ""),
                "n_sources": len(set(_refs(it))), "ledger_as_of": led.as_of, "fetched_at": fetched_at,
            }
        )
        for m in it.get("milestones") or []:
            pd = parse_pdate(m["date"])
            ms.append(
                {
                    "id": it["id"], "iso3": led.country, "type": m["type"], "date_raw": pd.raw,
                    "date_start": pd.start, "date_end": pd.end, "status": m["status"],
                    "src": ",".join(m.get("src") or []), "fetched_at": fetched_at,
                }
            )
    return {"opportunity": opp, "opportunity_milestone": ms}
