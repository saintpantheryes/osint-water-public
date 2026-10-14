"""取世界银行 2019 年以前的合同授予记录,挑出水务合同,写成 A28 的数据文件(core/jobs/awards.py 的 COLS)。

fetch_wb_awards.py 用的 STEP 数据(DS00005)从 2019 年起;更早的合同(多是事前审查的大合同,2000 年前后起)在世行项目网站的
合同检索接口 search.worldbank.org/api/contractdata 里,一份合同一条,有签约日期、金额(美元)、各家中标方及注册国、采购方式、项目所属行业。
像阿塞拜疆,世行的水务贷款 2017 年前就花完了,STEP 数据里一份也没有。

挑法同 fetch_wb_awards:项目行业里有供水、污水、排水、灌溉、防洪的,合同名称归得进一个类别(awards.classify);别的项目的合同要过
awards.classify_strict。早年的合同名称常常很短(「NETWORK REHABILITATION …」「MILL-MUGAN SIPHON COMPLETION」),所以水务项目里
名称归不了类的,工程(不是办公楼、培训之类)、管材泵阀之类的货物、设计监理之类的咨询,按项目名称归类(项目名称也归不了的不收)。金额 ≥ --min-usd(缺省 10 万美元),不是个人咨询顾问合同,签约日期早于 --before(缺省 2019-01-01,之后的归 STEP 数据)。
国别按借款国(地区性项目没有国别的丢掉)。编号与 STEP 数据同一套(WB-合同号),两边都有的导入时按编号只算一份。

python scripts/fetch_wb_old_awards.py --countries AZE[,KGZ…|all] [--out private/data/awards_wb_old.csv.gz] [--min-usd 100000] [--before 2019-01-01] [--cache 目录]
只读公开接口,一页 500 条;--countries all 要翻全部约 49 万条(约一千页)。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import geo  # noqa: E402
from core.jobs import awards  # noqa: E402

API = "https://search.worldbank.org/api/contractdata?format=json&rows={rows}&os={os}{where}"
PAGE = 500
PROJECT_URL = "https://projects.worldbank.org/en/projects-operations/project-detail/{}"
OUT = awards.DATA.with_name("awards_wb_old.csv.gz")
WATER_SECTOR = re.compile(r"water|sanit|sewer|irrigat|flood", re.I)
# 水务项目里名称归不了类、按项目名称归的:工程排除这些;货物、咨询只收这些
NOT_WORKS = re.compile(r"\boffices?\b|training|universit|bazaa?r", re.I)
WATER_GOODS = re.compile(r"\bpipes?\b|pipeline|fittings?|valves?|pumps?|\bmeters?\b|chlorinat|water treatment", re.I)
WATER_CS = re.compile(r"supervision|\bdesign\b|feasibility|construction management|management of construction|\bCMF\b", re.I)


def _get(url: str, tries: int = 4) -> dict:
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "osint-water"}), timeout=120) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            if k == tries - 1:
                raise
            time.sleep(2 ** (k + 1))
    return {}


def fetch(iso2: str | None = None, cache: Path | None = None, workers: int = 6) -> list[dict]:
    """一个借款国(ISO2)的全部合同;不给国别就是全部。有缓存目录时每页存一份,下次不再取。"""
    where = f"&countrycode={iso2}" if iso2 else ""
    total = int(_get(API.format(rows=1, os=0, where=where)).get("total") or 0)

    def page(i: int) -> list[dict]:
        fp = cache / f"{iso2 or 'all'}_p{i}.json" if cache else None
        if fp and fp.exists():
            return json.loads(fp.read_text(encoding="utf-8"))
        rows = _get(API.format(rows=PAGE, os=i * PAGE, where=where)).get("contract") or []
        if fp:
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        return rows

    with ThreadPoolExecutor(workers) as ex:
        return [r for part in ex.map(page, range((total + PAGE - 1) // PAGE)) for r in part]


def _day(s: str | None) -> str:
    from datetime import datetime

    try:
        return datetime.strptime(str(s or "").strip(), "%d-%b-%Y").date().isoformat()
    except ValueError:
        return ""


def _fix(s: str) -> str:
    """世行数据里有的名称是把 UTF-8 当 cp1252 读坏的(「TTÅž INÅžAAT」= TTŞ İNŞAAT),能还原就还原。"""
    s = " ".join(str(s or "").replace("_x000D_", " ").split())
    if re.search(r"[ÃÄÅ][\x80-\xbf\u0152-\u2122]", s):
        try:
            return s.encode("cp1252").decode("utf-8")
        except UnicodeError:
            pass
    return s


def sector_of(name: str, project: str, group: str, water: bool) -> str | None:
    """合同名称(早年名称太短时看项目名称)→ 类别;不是水务合同返回 None。"""
    if not water:
        return awards.classify_strict(name)
    hit = awards.classify(name)
    if hit or awards.NOT_WATER.search(name):
        return hit
    ok = {"CW": not NOT_WORKS.search(name), "GO": bool(WATER_GOODS.search(name)), "CS": bool(WATER_CS.search(name))}.get(group, False)
    if not ok:
        return None
    if re.search(r"irrigat|drainage|water users", project, re.I):
        return "水利"
    return "供水" if re.search(r"water supply", project, re.I) else awards.classify(project)  # 「供水与卫生」项目里名称看不出的多是供水


def _usd(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _supplier_iso(code: str, borrower: str | None) -> str | None:
    """供应商注册国;世行数据里写成朝鲜(KP)的是录错的,同 fetch_wb_awards._supplier_code。"""
    if code.upper() in ("KP", "PRK"):
        return borrower if borrower in ("COD", "COG") else "KOR"
    return geo.iso3(code) if code else None


def _zh(iso: str | None, name: str | None) -> str:
    zh = geo.name_zh(iso) if iso else ""
    return zh if zh and zh != "未识别" else str(name or "").strip()


def pick(raw: list[dict], min_usd: float = 100_000, before: str = "2019-01-01", today: date | None = None) -> list[dict]:
    """接口的行 → 数据文件的行(一份合同一行)。"""
    taken = (today or date.today()).isoformat()
    out, seen = [], set()
    for r in raw:
        no = str(r.get("contr_id") or r.get("id") or "").strip()
        method = " ".join(str(r.get("procu_meth_text") or "").split())
        firms_raw = [s for s in (r.get("suppinfo") or []) if isinstance(s, dict) and str(s.get("name") or "").strip()]
        if not no or no in seen or method == "Individual Consultant Selection" or not firms_raw:
            continue
        if all(_fix(s["name"]).upper() == "INDIVIDUAL CONSULTANT" for s in firms_raw):
            continue
        signed = _day(r.get("contr_sgn_date"))
        if not signed or signed >= before:
            continue
        name = _fix(r.get("contr_desc"))
        water = any(WATER_SECTOR.search(str(s)) for s in (r.get("sector") or []))
        sector = sector_of(name, str(r.get("project_name") or ""), str(r.get("procurement_group") or ""), water)
        iso = geo.iso3(r.get("countrycode")) or geo.iso3(r.get("countryshortname"))
        price = _usd(r.get("total_contr_amnt")) or sum(_usd(s.get("supplier_contr_amount")) for s in firms_raw)
        if not sector or not iso or price < min_usd:
            continue
        seen.add(no)
        firms: dict[str, dict] = {}
        for s in firms_raw:
            nm = _fix(s["name"])
            if nm not in firms:
                iso_w = _supplier_iso(str(s.get("country") or "").strip(), iso)
                sid = str(s.get("id") or "").strip()
                firms[nm] = {"name": nm, "iso3": iso_w or "", "country_zh": _zh(iso_w, s.get("countryshortname")), "ext": f"WB:{sid}" if sid else ""}
        pid = str(r.get("projectid") or "").strip()
        out.append({"key": f"WB-{no}", "iso3": iso, "name": name, "sector": sector,
                    "contract_type": awards.contract_type(r.get("procurement_group_desc"), name),
                    "price": f"{price:.2f}", "currency": "USD", "awarded": signed, "quantity": awards.quantity(name),
                    "winner": "、".join(firms), "winner_country": "、".join(dict.fromkeys(f["country_zh"] for f in firms.values() if f["country_zh"])),
                    "project": f"{pid} {str(r.get('project_name') or '').strip()}".strip(), "funder": "世界银行",
                    "method": awards.METHOD_ZH.get(method, method),
                    "source": f"世界银行合同授予记录(2019 年前,{taken} 取)",
                    "url": PROJECT_URL.format(pid) if pid else "", "winners": json.dumps(list(firms.values()), ensure_ascii=False)})
    out.sort(key=lambda x: (x["iso3"], x["awarded"]))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="世界银行 2019 年前的合同授予记录 → 已中标项目数据文件")
    ap.add_argument("--countries", required=True, help="借款国 ISO3,逗号隔开;all 是全部")
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--min-usd", type=float, default=100_000)
    ap.add_argument("--before", default="2019-01-01", help="只收这天以前签的(之后的在 STEP 数据里)")
    ap.add_argument("--cache", type=Path, default=None, help="原始页存到这个目录(再跑不重取)")
    a = ap.parse_args(argv)
    if a.countries.strip().lower() == "all":
        raw = fetch(None, a.cache)
    else:
        raw = []
        for c in a.countries.split(","):
            iso = geo.iso3(c.strip())
            iso2 = (geo.countries().get(iso) or {}).get("iso2") if iso else None
            if not iso2:
                raise SystemExit(f"认不出国别:{c}")
            raw += fetch(iso2, a.cache)
    rows = pick(raw, a.min_usd, a.before)
    awards.write_rows(a.out, rows)
    print(f"原始 {len(raw)} 条 → {a.before} 前的水务合同 {len(rows)} 份,写到 {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
