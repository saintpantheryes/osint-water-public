"""从世界银行公开的文件库与项目库取一国的水务研究资料与项目事件(A31、A33 的数据文件)。
研究资料:该国水务相关项目(名称或行业有供水、污水、灌溉、防洪等)每个取最新的评估文件(没有的取政策贷款文件、项目文件或项目信息文件)、
完成报告、一份主要的环境社会评估;另收标题有水务字样的行业研究、国别气候与发展报告。
事件:每个水务项目的批准(承诺额)与关闭。
只换掉这几个国家的 WB- 开头的行,别的行(综述 SUM-、摘自原文的事件 EV-、别的国家)原样保留。
用法:python scripts/fetch_wb_studies.py MAR AZE KGZ [--studies private/data/studies.csv.gz] [--events private/data/events.csv.gz] [--since 2005]"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import geo  # noqa: E402
from core.jobs.studies import EVENT_COLS, EVENTS, STUDIES, STUDY_COLS, read_rows, write_rows  # noqa: E402

API = "https://search.worldbank.org/api/v2/"
WATER_NAME = re.compile(r"water|sanitat|wastewater|sewer|irrigat|drainage|flood|hydromet|\bbasin\b|\bdams?\b|\beau\b|assainissement", re.I)
WATER_SECTOR = re.compile(r"Water Supply|Sanitation|Irrigation|Flood|Wastewater|Water Resource|Hydropower", re.I)
NOT_WATER = re.compile(r"solid waste|transport|highway|road|rail|energy efficiency|education|health|judicial|employment|\bIDP\b|public investment|maroc vert", re.I)
STUDY_TYPES = {"Working Paper", "Report", "Publication", "Country Climate and Development Report", "Public Expenditure Review",
               "Policy Research Working Paper", "Working Paper (Numbered Series)", "Other Environmental Study", "Other Agricultural Study", "ESMAP Paper"}
STUDY_TITLE = re.compile(r"water|sanitat|irrigat|drought|flood|urban|climate and development", re.I)
LANG = {"English": "英文", "French": "法文", "Russian": "俄文", "Arabic": "阿拉伯文", "Spanish": "西班牙文", "Portuguese": "葡萄牙文", "Chinese": "中文"}


def get(path: str, **q) -> dict:
    url = API + path + "?" + urllib.parse.urlencode({"format": "json", **q})
    for i in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "osint-water"}), timeout=120) as r:
                return json.load(r)
        except Exception:  # noqa: BLE001 — 网络抖动重试
            if i == 3:
                raise
            time.sleep(2 ** i)
    return {}


def clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def water_share(p: dict) -> float:
    """项目行业构成里水务类(供水、排水、灌溉、防洪等)所占的百分比;行业字段有时是纯文字,那种不计。"""
    tot = 0.0
    for i in range(1, 6):
        s = p.get(f"sector{i}")
        if isinstance(s, dict) and WATER_SECTOR.search(str(s.get("Name") or "")):
            tot += usd(s.get("Percent"))
    return tot


def is_water_project(p: dict) -> bool:
    """名字里有水务字样,或水务行业占到四分之一以上。"""
    name = clean(p.get("project_name"))
    if NOT_WATER.search(name) and not re.search(r"water|sanitat|irrigat|flood", name, re.I):
        return False
    return bool(WATER_NAME.search(name)) or water_share(p) >= 25


def iso_day(v) -> str:
    """「2016-12-31T00:00:00Z」「12/31/2016 12:00:00 AM」→ 2016-12-31;认不出返回空。"""
    s = str(v or "").strip()
    if re.match(r"\d{4}-\d{2}-\d{2}", s):
        return s[:10]
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}" if m else ""


def doc_kind(docty: str) -> str | None:
    """世行文件类型 → 研究资料类型(study_kind);不收的返回 None。"""
    t = clean(docty)
    if re.fullmatch(r"(Project|Program) Appraisal Document|Program Document|Project Paper|Project Information Document|"
                    r"Program Information Document|Project Information and Integrated Safeguards Data Sheet", t):
        return "项目评估"
    if re.search(r"Implementation Completion and Results|Project Performance Assessment", t):
        return "完成报告"
    if re.search(r"^Environmental Assessment$|Environmental and Social (Management Framework|Systems Assessment)|Technical Assessment", t):
        return "环境社会评估"
    if t in STUDY_TYPES:
        return "行业研究"
    return None


APPRAISAL_RANK = [r"Appraisal Document", r"Program Document", r"Project Paper", r"Information"]


def pick(docs: list[dict]) -> list[dict]:
    """一个项目的文件 → 最新的评估文件一份、完成报告一份、环境社会评估一份(英文优先,定稿优先)。"""
    def best(cands: list[dict]) -> dict | None:
        if not cands:
            return None
        en = [d for d in cands if "English" in str(d.get("lang") or "")] or cands
        en.sort(key=lambda d: (not re.search(r"draft|concept", d.get("display_title") or "", re.I), d.get("docdt") or ""), reverse=True)
        return en[0]
    out = []
    for rank in APPRAISAL_RANK:
        hit = best([d for d in docs if doc_kind(d.get("docty")) == "项目评估" and re.search(rank, d.get("docty") or "") and d.get("pdfurl")])
        if hit:
            out.append(hit)
            break
    for kind in ("完成报告", "环境社会评估"):
        hit = best([d for d in docs if doc_kind(d.get("docty")) == kind and d.get("pdfurl")])
        if hit:
            out.append(hit)
    return out


def study_row(iso3: str, d: dict, project: str = "") -> dict:
    lang = clean(d.get("lang")).split(",")[0]
    return {"key": f"WB-{d['id']}", "iso3": iso3, "name": clean(d.get("display_title")), "kind": doc_kind(d.get("docty")),
            "publisher": "世界银行", "published": str(d.get("docdt") or "")[:10], "language": LANG.get(lang, lang), "project": project,
            "url": str(d.get("pdfurl") or d.get("url") or "").replace("http://", "https://")}


def usd(v) -> float:
    try:
        return float(str(v or "0").replace(",", ""))
    except ValueError:
        return 0.0


def money(n: float) -> str:
    return f"{n / 1e8:.2f}".rstrip("0").rstrip(".") + " 亿美元" if n >= 1e8 else f"{n / 1e4:,.0f} 万美元" if n else ""


def project_events(iso3: str, p: dict) -> list[dict]:
    pid, name = p["id"], clean(p.get("project_name"))
    url = f"https://projects.worldbank.org/en/projects-operations/project-detail/{pid}"
    out = []
    amt = money(usd(p.get("totalcommamt")))
    if iso_day(p.get("boardapprovaldate")):
        out.append({"key": f"WB-{pid}-批准", "iso3": iso3, "day": iso_day(p["boardapprovaldate"]), "kind": "项目批准",
                    "title": f"世界银行批准「{name}」" + (f",{amt}" if amt else ""), "source": f"世界银行项目库 {pid}", "url": url})
    if iso_day(p.get("closingdate")) and str(p.get("status")) == "Closed":
        out.append({"key": f"WB-{pid}-关闭", "iso3": iso3, "day": iso_day(p["closingdate"]), "kind": "项目完成",
                    "title": f"世界银行项目「{name}」关闭", "source": f"世界银行项目库 {pid}", "url": url})
    return out


def fetch(iso3: str, since: str, log=print) -> tuple[list[dict], list[dict]]:
    iso2 = (geo.countries().get(iso3) or {}).get("iso2")
    if not iso2:
        raise SystemExit(f"认不出国别码 {iso3}")
    d = get("projects", rows=500, countrycode_exact=iso2, strdate=f"{since}-01-01", enddate="2100-12-31",
            fl="id,project_name,boardapprovaldate,closingdate,status,totalcommamt,sector1,sector2,sector3,sector4,sector5")
    projects = [p for p in (d.get("projects") or {}).values() if isinstance(p, dict) and is_water_project(p)]
    studies, events = [], []
    for p in projects:
        events += project_events(iso3, p)
        docs = [x for k, x in (get("wds", rows=300, proid=p["id"], fl="id,docty,docdt,display_title,pdfurl,url,lang").get("documents") or {}).items()
                if k != "facets" and isinstance(x, dict)]
        for doc in pick(docs):
            studies.append(study_row(iso3, doc, f"{p['id']} {clean(p.get('project_name'))}"))
    seen = set()
    for term in ("water", "sanitation", "irrigation", "drought"):
        q = get("wds", rows=200, countrycode_exact=iso2, qterm=term, strdate=f"{since}-01-01", enddate="2100-12-31",
                fl="id,docty,docdt,display_title,pdfurl,url,lang,projectid")
        for k, doc in (q.get("documents") or {}).items():
            title = clean(doc.get("display_title")) if isinstance(doc, dict) else ""
            if k == "facets" or doc_kind(doc.get("docty")) != "行业研究" or not STUDY_TITLE.search(title) or not doc.get("pdfurl"):
                continue
            if "English" not in str(doc.get("lang") or "") or title.lower() in seen:
                continue
            seen.add(title.lower())
            studies.append(study_row(iso3, doc))
    log(f"{iso3}:水务项目 {len(projects)} 个,研究资料 {len(studies)} 份,项目事件 {len(events)} 条")
    return studies, events


KEEP_EDITS = ("title", "name_zh", "summary", "city_en")  # 人改过的中文标题、要点、城市,重取时留着


def merge(path: Path, cols: list[str], isos: set[str], fresh: list[dict]) -> int:
    old = read_rows(path) if path.exists() else []
    was = {r.get("key"): r for r in old}
    keep = [r for r in old if not (r.get("iso3") in isos and str(r.get("key") or "").startswith("WB-"))]
    keys = {r.get("key") for r in keep}
    for r in fresh:
        prev = was.get(r["key"]) or {}
        r.update({c: prev[c] for c in KEEP_EDITS if c in cols and prev.get(c)})
    rows = keep + [r for r in fresh if r["key"] not in keys]
    write_rows(path, sorted(rows, key=lambda r: (r.get("iso3") or "", r.get("key") or "")), cols)
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("iso3", nargs="+", help="国别码(ISO3),如 MAR AZE KGZ")
    ap.add_argument("--studies", type=Path, default=STUDIES)
    ap.add_argument("--events", type=Path, default=EVENTS)
    ap.add_argument("--since", default="2005", help="只看这一年以后批准的项目与发布的文件")
    a = ap.parse_args(argv)
    isos = {x.upper() for x in a.iso3}
    studies, events = [], []
    for iso in sorted(isos):
        s, e = fetch(iso, a.since)
        studies += s
        events += e
    print("研究资料共", merge(a.studies, STUDY_COLS, isos, studies), "行 →", a.studies)
    print("水务事件共", merge(a.events, EVENT_COLS, isos, events), "行 →", a.events)
    return 0


if __name__ == "__main__":
    sys.exit(main())
