"""助手复核记录(2026-10-19 使用方:「助手给出结论,需要人工复核的地方,能否增加"已复核""帮我复核""忽略此项"之类按钮?」)。

看一条链接或一份文件的结果里,「要核实」的每一条和「库里可能是同一项目」的每条线索后面各有三个按钮:
  要核实:已复核 / 帮我复核 / 忽略此项;库里线索:是同一项目 / 帮我复核 / 不是同一项目。
点选是页面上的操作,不是说给模型听的话:记进 Teable「助手复核记录」表(review,一条一行,按材料 + 条目算的编号找),同一份材料再看时照旧显示,
投标评估(core/bid_check.assess)按点选结果重算(确认是同一项目的进「库里已有」;助手核出的截止日等事实补进去)。
「帮我复核」由助手自己去查(整国世行公告、新闻、库里)再请模型给结论——不把「先核实」推回给人(使用方 10-16 的规则);
没查到也给一个有依据的判断并写明把握。表没建时点选只在这一页有效(页面会说明)。
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime

from core import geo

KINDS = ("要核实", "库里线索")
STATUS = ("已复核", "已忽略", "是同一项目", "不是同一项目", "助手已核")
HUMAN_STATUS = {"要核实": ("已复核", "已忽略"), "库里线索": ("是同一项目", "不是同一项目")}
FACT_KEYS = ("deadline", "stage", "category", "amount", "requirements", "original", "sure")  # 助手核出来、能改投标评估的事实(original / sure:找到的原公告,信源检查按它算)
ROW_PROPS = ("key", "material", "project", "kind", "item", "lead_id", "status", "conclusion", "evidence", "facts", "who", "at")


def material_of(url: str | None, final_url: str | None, upload: bytes | None) -> str:
    """一份材料的标识:有网址按最终网址;只传了文件按文件内容的指纹(同一份文件再传认得出)。"""
    u = (final_url or url or "").strip()
    if u:
        return u[:500]
    if upload is not None:
        return "file:" + hashlib.sha1(upload).hexdigest()[:16]
    return ""


def item_key(material: str, kind: str, text: str) -> str:
    """一条待复核项的编号:材料 + 条目类型 + 条目文字(库里线索用线索的记录编号)。"""
    return "R" + hashlib.sha1(f"{material}|{kind}|{(text or '').strip()}".encode()).hexdigest()[:16]


def keys_for(result: dict) -> dict[str, dict]:
    """结果里所有能点的条目:编号 → {kind, item, lead_id}。"""
    mat = result.get("material") or ""
    out: dict[str, dict] = {}
    a = result.get("analysis") or {}
    for v in a.get("verify") or []:
        out[item_key(mat, "要核实", v)] = {"kind": "要核实", "item": v}
    for lead in result.get("related_leads") or []:
        if lead.get("id"):
            out[item_key(mat, "库里线索", lead["id"])] = {"kind": "库里线索", "item": lead.get("title") or "", "lead_id": lead["id"]}
    return out


# ---------- 表 ----------

def available(db) -> bool:
    return db is not None and "review" in getattr(db, "t", {})


def _row(db, r: dict) -> dict:
    g = lambda p: db.get("review", r, p) if db.has("review", p) else None  # noqa: E731
    out = {p: g(p) for p in ROW_PROPS}
    out["id"] = r.get("id")
    try:
        out["facts"] = json.loads(out["facts"]) if out.get("facts") else {}
    except ValueError:
        out["facts"] = {}
    out["evidence"] = _ev_list(out.get("evidence"))
    return out


def _ev_list(text: str | list | None) -> list[dict]:
    if isinstance(text, list):
        return [e for e in text if isinstance(e, dict) and e.get("url")]
    out = []
    for ln in str(text or "").splitlines():
        name, _, url = ln.strip().rpartition(" ")
        if url.startswith("http"):
            out.append({"name": name or url, "url": url})
    return out


def load(db, material: str) -> dict[str, dict]:
    """这份材料已记的复核:编号 → 行。表没建、没材料标识时空。"""
    if not material or not available(db):
        return {}
    rows = db.rows("review", filter=db.filter(db.cond("review", "material", "is", material)))
    out = {}
    for r in rows:
        row = _row(db, r)
        if row.get("key") and row.get("status") in STATUS:
            out[row["key"]] = row
    return out


def save(db, rec: dict, who: str = "", now: datetime | None = None) -> dict:
    """记一条点选(同编号已有就改):→ 存进去的行。"""
    now = now or datetime.now()
    vals = {"key": rec["key"], "material": str(rec.get("material") or "")[:500], "project": str(rec.get("project") or "")[:200],
            "kind": rec["kind"], "item": str(rec.get("item") or "")[:2000], "lead_id": str(rec.get("lead_id") or "")[:40],
            "status": rec["status"], "conclusion": str(rec.get("conclusion") or "")[:4000],
            "evidence": "\n".join(f"{e.get('name') or ''} {e['url']}".strip() for e in (rec.get("evidence") or []) if e.get("url"))[:4000],
            "facts": json.dumps({k: v for k, v in (rec.get("facts") or {}).items() if v not in (None, "", [], {})}, ensure_ascii=False) if rec.get("facts") else "",
            "who": who[:200], "at": now.isoformat(timespec="seconds")}
    vals = {k: v for k, v in vals.items() if db.has("review", k)}
    old = db.rows("review", filter=db.filter(db.cond("review", "key", "is", rec["key"])))
    if old:
        db.update("review", old[0]["id"], vals)
        rid = old[0]["id"]
    else:
        rid = db.create("review", [vals])[0]["id"]
    return {**rec, "id": rid, "who": who, "at": vals.get("at")}


# ---------- 按点选重算投标评估 ----------

def rebid(result: dict, reviews: dict[str, dict], today: date, records: dict | None, scope) -> dict | None:
    """按复核结果重算投标评估:确认是同一项目的线索算「库里已有」,否定的从相关里去掉;助手核出的事实(截止日、阶段……)补进去。
    结果里没有评估依据(bid_facts)时给 None(旧结果)。"""
    from core import bid_check
    facts = result.get("bid_facts")
    if not isinstance(facts, dict):
        return None
    facts = dict(facts)
    related = list(result.get("related_leads") or [])
    by_id = {r.get("id"): r for r in related if r.get("id")}
    have: list[dict] = []
    for rv in reviews.values():
        if rv.get("kind") == "库里线索":
            lead = by_id.get(rv.get("lead_id"))
            if lead is None:
                continue
            if rv.get("status") == "是同一项目":
                have.append(lead)
            if rv.get("status") in ("是同一项目", "不是同一项目", "已忽略"):
                related = [r for r in related if r.get("id") != lead.get("id")]
        elif rv.get("kind") == "要核实" and rv.get("status") == "助手已核":
            for k, v in (rv.get("facts") or {}).items():
                if k in FACT_KEYS and v not in (None, "", [], {}):
                    facts[k] = v
    lib = {"same_url": result.get("same_url") or [], "in_library": have, "related_leads": related, "related_opps": result.get("related_opps") or []}
    return bid_check.assess(facts, today, records, scope, lib)


def apply(result: dict, reviews: dict[str, dict], today: date, records: dict | None, scope, saved: bool) -> dict:
    """看完一条链接后把已记的复核挂到结果上(页面按它显示按钮状态),并按它重算评估。"""
    known = keys_for(result)
    mine = {k: v for k, v in reviews.items() if k in known}
    out = {**result, "reviews": {k: _public(v) for k, v in mine.items()}, "review_saved": saved}
    if mine:
        bid = rebid(out, mine, today, records, scope)
        if bid is not None:
            out["bid"] = bid
    return out


def _public(rv: dict) -> dict:
    return {k: rv.get(k) for k in ("key", "kind", "item", "lead_id", "status", "conclusion", "evidence", "facts", "who", "at", "confidence") if rv.get(k) not in (None, "")}


# ---------- 帮我复核 ----------

HELP_GUIDE = """
输出格式:只输出一个 JSON 对象,不要任何其他文字。字段:
- "found":true / false,公开渠道或库里的材料能不能把这一点查清
- "conclusion":一两句中文结论。查清了写结论和依据(哪份公告、哪条新闻);没查清也要给判断:按原文、按这类出资方的常规怎么看、建议按什么准备,并说明是推断
- "confidence":把握,取 高 / 中 / 低
- "facts":核出来的事实(没核出的填 null):{"deadline": 截止日 YYYY-MM-DD, "stage": 进展(立项或规划 / 融资 / 招标 / 授标 / 在建 / 建成 / 搁置后重启 / 其他),
  "category": 类别(招标 / 意向书 / 资格预审 / 总采购公告 / 特定采购公告 / 授标或短名单 / 融资 / 立项或规划 / 项目动态 / 政策 / 其他), "amount": 金额或规模(原文写法),
  "requirements": {"similar_usd": 数字或 null, "turnover_usd": 数字或 null, "financial_usd": 数字或 null, "years": 数字或 null, "specific": 一句话或 null} 或 null}
- "evidence":用到的材料序号数组(下面「公开渠道」「库里」里标的 [n])
"""
_S = {"type": "string"}
_N = {"anyOf": [{"type": "string"}, {"type": "null"}]}
_NUM = {"anyOf": [{"type": "number"}, {"type": "null"}]}
HELP_SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"}, "conclusion": _S, "confidence": {"type": "string", "enum": ["高", "中", "低"]},
        "facts": {"type": "object", "properties": {"deadline": _N, "stage": _N, "category": _N, "amount": _N,
                                                  "requirements": {"anyOf": [{"type": "object", "properties": {"similar_usd": _NUM, "turnover_usd": _NUM, "financial_usd": _NUM, "years": _NUM, "specific": _N},
                                                                              "required": ["similar_usd", "turnover_usd", "financial_usd", "years", "specific"], "additionalProperties": False}, {"type": "null"}]}},
                  "required": ["deadline", "stage", "category", "amount", "requirements"], "additionalProperties": False},
        "evidence": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["found", "conclusion", "confidence", "facts", "evidence"], "additionalProperties": False,
}
LEAD_GUIDE = """
输出格式:只输出一个 JSON 对象,不要任何其他文字。字段:
- "relation":取 同一项目 / 同项目别的标段 / 不同项目
- "why":一句中文,说明判断依据(项目名、地点、出资方、编号、阶段对得上或对不上的地方)
"""
LEAD_SCHEMA = {"type": "object", "properties": {"relation": {"type": "string", "enum": ["同一项目", "同项目别的标段", "不同项目"]}, "why": _S},
               "required": ["relation", "why"], "additionalProperties": False}


def _ctx_text(ctx: dict) -> str:
    iso = ctx.get("country")
    lines = [f"项目:{ctx.get('title') or ''}", f"国别:{geo.name_zh(iso) if iso else '不明'}", f"出资方:{ctx.get('funder') or '不明'}",
             f"摘要:{ctx.get('summary') or ''}", f"进展:{ctx.get('stage') or '不明'};类别:{ctx.get('category') or '不明'};截止日:{ctx.get('deadline') or '原文没写'}",
             f"原文里的资格门槛:{json.dumps(ctx.get('requirements'), ensure_ascii=False) if ctx.get('requirements') else '原文没写'}",
             f"原文网址:{ctx.get('url') or '(上传的文件,没有网址)'}"]
    return "\n".join(lines)


def _system(today: date) -> str:
    return (f"你是水务项目情报分析员,今天是 {today.isoformat()}。同事看一条项目公告时有一点要核实,请你根据下面给的材料把它核清楚。"
            "只依据给的材料判断,不要编造;材料不够时也要给出有依据的判断并说明是推断。用中文写。")


def help_verify(item: str, ctx: dict, today: date, clf=None, verify=None, lib_rows: list[dict] | None = None) -> dict:
    """「帮我复核」一条要核实的点:助手先查(verify = link_check.verify_outside:整国世行公告按编号、名字比对,新闻旁证,出资方检索链接),
    再请模型据此给结论。→ {status: 助手已核, conclusion, confidence, facts, evidence, notes}"""
    from core.link_check import verify_outside
    verify = verify or verify_outside
    iso = ctx.get("country")
    notes: list[str] = []
    hits: list[dict] = []
    links: list[dict] = []
    found_original: dict | None = None  # 整国比对里对上了编号或名字的原公告:信源检查也按它算
    try:
        chk = verify({"ref": ctx.get("ref"), "names": ctx.get("names") or [], "query": ctx.get("news_query"), "title": ctx.get("title"), "funder": ctx.get("funder")}, iso)
        if chk.get("sure") and (chk.get("original") or {}).get("url"):
            found_original = {k: chk["original"].get(k) for k in ("kind", "title", "url")}
        for h in ([chk.get("original")] if chk.get("original") else []) + list(chk.get("candidates") or []) + list(chk.get("news") or []):
            if h and h.get("url") and h["url"] not in {x["url"] for x in hits}:
                hits.append({k: h.get(k) for k in ("kind", "title", "url", "date", "deadline", "text")})
        links = chk.get("links") or []
        notes += chk.get("notes") or []
    except Exception as e:  # noqa: BLE001
        notes.append(f"公开渠道没查成({type(e).__name__})")
    lib_rows = lib_rows or []
    ev_all = [{"name": h.get("title") or h.get("kind") or "公开渠道", "url": h["url"]} for h in hits] + \
             [{"name": r.get("title") or "库里线索", "url": r.get("url")} for r in lib_rows if r.get("url")]
    if clf is None or not getattr(clf, "enabled", False):
        concl = ("没配模型,助手只做了检索:" + (f"在公开渠道找到 {len(hits)} 条可能相关的材料(见依据),请对照看。" if hits else "公开渠道里没找到能核这一点的材料。")
                 + (" 可到出资方网站按下面的链接找。" if links else ""))
        return {"status": "助手已核", "conclusion": concl, "confidence": "低", "facts": {}, "evidence": ev_all[:4] + links[:2], "notes": notes}
    parts = [_ctx_text(ctx), f"\n要核实的点:{item}", "\n公开渠道查到的材料:" if hits else "\n公开渠道查到的材料:(没有)"]
    for i, h in enumerate(hits, 1):
        parts.append(f"[{i}] {h.get('kind') or ''} {h.get('date') or ''} {h.get('title') or ''}" + (f" 截止 {h['deadline']}" if h.get("deadline") else "")
                     + f" {h['url']}" + (f"\n    {str(h.get('text') or '')[:400]}" if h.get("text") else ""))
    if lib_rows:
        parts.append("\n库里可能相关的线索:")
        for j, r in enumerate(lib_rows, len(hits) + 1):
            parts.append(f"[{j}] {r.get('found_at') or ''} {r.get('title') or ''} {r.get('summary_zh') or ''} {r.get('url') or ''}")
    out = clf.ask(_system(today), "\n".join(parts), HELP_SCHEMA, HELP_GUIDE)
    if not out or out.get("_error") or not isinstance(out.get("conclusion"), str):
        notes.append("模型没有给出结论" + (f"({out['_error']})" if out and out.get("_error") else ""))
        concl = "助手查了公开渠道" + (f",找到 {len(hits)} 条可能相关的材料(见依据)" if hits else ",没找到能核这一点的材料") + ",但模型这次没给出结论,请稍后再点一次。"
        return {"status": "助手已核", "conclusion": concl, "confidence": "低", "facts": {}, "evidence": ev_all[:4] + links[:2], "notes": notes}
    facts = {k: v for k, v in (out.get("facts") or {}).items() if k in FACT_KEYS and v not in (None, "", [], {})}
    if facts.get("deadline") and not _is_date(facts["deadline"]):
        facts.pop("deadline")
    if found_original:
        facts.update(original=found_original, sure=True)
    idx = [i for i in (out.get("evidence") or []) if isinstance(i, int) and 1 <= i <= len(ev_all)]
    ev = [ev_all[i - 1] for i in idx] or ev_all[:3]
    conf = out.get("confidence") if out.get("confidence") in ("高", "中", "低") else ("中" if out.get("found") else "低")
    concl = str(out["conclusion"]).strip()
    if not out.get("found"):
        concl = ("公开渠道里没查到," if not hits else "") + concl + (" 可按下面的链接到出资方网站再找。" if links and not hits else "")
    return {"status": "助手已核", "conclusion": concl[:2000], "confidence": conf, "facts": facts, "evidence": (ev + ([] if hits else links[:2]))[:5], "notes": notes,
            "model": out.get("_model")}


def help_lead(lead: dict, ctx: dict, today: date, clf=None) -> dict:
    """「帮我复核」库里的一条可能相关的线索:请模型把它和这份材料对一对 → 是同一项目 / 不是同一项目(别的标段算「助手已核」,仍列作相关)。"""
    if clf is None or not getattr(clf, "enabled", False):
        return {"status": "助手已核", "conclusion": "没配模型,比不了;请按两边的项目名、地点、出资方看一眼。", "confidence": "低", "facts": {}, "evidence": [], "notes": ["没配模型"]}
    user = (_ctx_text(ctx) + "\n\n库里的这条线索:\n" + f"标题:{lead.get('title') or ''}\n中文项目名:{lead.get('project_name') or ''}\n摘要:{lead.get('summary_zh') or ''}\n"
            f"国别:{lead.get('country') or ''};收录日期:{lead.get('found_at') or ''};所属商机:{lead.get('opportunity') or '(无)'}\n网址:{lead.get('url') or ''}\n\n"
            "请判断这条线索讲的和上面这份材料是不是同一个项目(同一个项目的另一个标段、另一阶段的公告算「同项目别的标段」)。")
    out = clf.ask(_system(today), user, LEAD_SCHEMA, LEAD_GUIDE)
    if not out or out.get("_error") or out.get("relation") not in ("同一项目", "同项目别的标段", "不同项目"):
        return {"status": "助手已核", "conclusion": "模型这次没给出判断,请稍后再点一次。", "confidence": "低", "facts": {}, "evidence": [], "notes": ["模型没有给出判断"]}
    rel, why = out["relation"], str(out.get("why") or "").strip()
    status = {"同一项目": "是同一项目", "不同项目": "不是同一项目", "同项目别的标段": "助手已核"}[rel]
    return {"status": status, "conclusion": f"{rel}:{why}"[:2000], "confidence": "中", "facts": {}, "evidence": [{"name": lead.get("title") or "线索", "url": lead["url"]}] if lead.get("url") else [],
            "notes": [], "model": out.get("_model")}


def _is_date(s: object) -> bool:
    try:
        date.fromisoformat(str(s)[:10])
        return True
    except ValueError:
        return False
