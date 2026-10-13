"""取亚洲开发银行(ADB)公开的「已授合同」(Contracts Awarded),挑出水务合同,写成 A28 的数据文件(core/jobs/awards.py 的 COLS)。

数据:adb.org「Tenders」页的检索服务(searchstax,约 1.3 万条,2015 年起;每条有合同名称、中标单位及地址、合同金额(美元)、
执行机构、项目编号与名称、领域)。检索服务的地址和公开访问令牌写在 adb.org 页面的脚本里,每次运行先从页面上找。
世界银行的数据没有亚行贷款的合同,亚行在南亚、东南亚、中亚、太平洋岛国放贷多。

挑法:项目名称像水务的(供水、污水、灌溉、防洪……,同 fetch_ebrd_awards.WATER_PROJECT),合同名称或项目名称归得进一个类别
(awards.classify),合同名称里没有车辆、房屋之类字样(awards.NOT_WATER);别的项目里的合同要过 awards.classify_strict。
金额 ≥ --min-usd(缺省 10 万美元)。同一份合同登了两次的(同一项目、同样金额)只收一次。

python scripts/fetch_adb_awards.py [--cache 目录] [--out private/data/awards_adb.csv.gz] [--min-usd 100000]
只做公开检索,不登录。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urljoin

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import geo  # noqa: E402
from core.jobs import awards  # noqa: E402

PAGE_URL = "https://www.adb.org/projects/tenders"
PROJECT_URL = "https://www.adb.org/projects/{}/main"
OUT = awards.DATA.with_name("awards_adb.csv.gz")
SOURCE = "亚洲开发银行已授合同"
ROWS = 500
WATER_PROJECT = re.compile(r"water|waste ?water|sewer|sanita|irrigat|flood|drainage|\bw?wtp\b|desalin|river|canal|reservoir|\bdams?\b", re.I)
NOT_WATER_PROJECT = re.compile(r"hydro ?power|\bHPP\b|hydroelectric|waterway|port\b|inland water transport", re.I)
_JV = re.compile(r"^\s*(?:(?:the\s+)?(?:joint venture|JV|consortium)\s*(?:of|:|between)?\s+)|\(\s*JV\s*\)|\bJV\b|joint venture|consortium", re.I)
_ASSOC = re.compile(r"\s+in\s+(?:association|assn\.?|asso\.?)\s+with\s+|\s+in\s+JV\s+with\s+", re.I)


def discover(log=print) -> tuple[str, str]:
    """从 adb.org 招标页的脚本里找检索服务地址与公开令牌。"""
    from core.watch import fetch

    page = fetch.get(PAGE_URL, timeout=90).text
    for src in re.findall(r'<script[^>]+src="([^"]+\.js[^"]*)"', page):
        url = urljoin(PAGE_URL, src.replace("&amp;", "&"))
        if "adb.org" not in url:
            continue
        try:
            js = fetch.get(url, timeout=90).text
        except Exception:  # noqa: BLE001
            continue
        m = re.search(r'searchURL:"(https://[^"]+/tenders-\d+/emselect)"[^}]*?searchAuth:"([0-9a-f]{20,})"', js)
        if m:
            return m.group(1), m.group(2)
    raise SystemExit("adb.org 页面上没找到招标检索服务的地址(页面改版了?)")


def fetch_all(cache: Path | None = None, log=print) -> list[dict]:
    import httpx

    from core.storage import USER_AGENT

    url, token = discover(log)
    headers = {"Authorization": f"Token {token}", "User-Agent": USER_AGENT}
    docs, start, total = [], 0, None
    with httpx.Client(headers=headers, timeout=120) as c:
        while total is None or start < total:
            fp = cache / f"p{start:06d}.json" if cache else None
            if fp and fp.exists():
                d = json.loads(fp.read_text(encoding="utf-8"))
            else:
                r = c.get(url, params={"q": "*", "fq": 'sm_fct_type:"Contracts Awarded"', "rows": ROWS, "start": start, "wt": "json",
                                       "language": "en", "sort": "ds_date_posted asc, id asc", "fl": "*"})
                r.raise_for_status()
                d = r.json()
                if fp:
                    fp.parent.mkdir(parents=True, exist_ok=True)
                    fp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
            total = int(d["response"]["numFound"])
            docs += d["response"]["docs"]
            start += ROWS
    log(f"亚行已授合同 {len(docs)} 条(检索服务报 {total} 条)")
    return docs


def _one(doc: dict, field: str) -> str:
    v = doc.get(field)
    if isinstance(v, list):
        v = v[0] if v else ""
    return " ".join(str(v or "").split())


def is_water(title: str, project: str) -> str | None:
    if WATER_PROJECT.search(project) and not NOT_WATER_PROJECT.search(project):
        if awards.NOT_WATER.search(title):
            return None
        return awards.classify(title) or awards.classify(project)
    return awards.classify_strict(title)


def split_firms(name: str) -> list[str]:
    """「JV of A and B」「A in association with B」「A & B & C (JV)」→ 各家;不像联合体的不拆。"""
    out = []
    for p in (x for x in _ASSOC.split(name) if x.strip()):
        q = " ".join(re.sub(r"\(\s*JV\s*\)|^\s*(?:the\s+)?(?:joint venture|JV|consortium)\s*(?:of|:|between|-)?\s+", " ", p, flags=re.I).split())
        if _JV.search(p):
            q = re.sub(r",\s*(?=(?:Ltd|Inc|LLC|S\.?A\.?|Co\b|Limited|GmbH|JSC|A\.?S\.?|Pvt|Private|PLC)\b)", " ", q, flags=re.I)  # 「Co., Ltd」不拆
            bits = [re.sub(r"^and\s+", "", b.strip(" ,."), flags=re.I) for b in re.split(r"\s+&\s+|\s*[/;,]\s*|\s+and\s+", q, flags=re.I)]
            bits = [b for b in bits if len(b) >= 2 and b.lower() not in ("and", "&")]  # 「ENGINEERING&TEC.」这种不拆
            if len(bits) > 1:
                out += bits
                continue
        out.append(q.strip(" ,."))
    return [x for x in out if x]


# 个人咨询顾问(世行的数据按采购方式剔除;亚行没有采购方式,按「职位名 + 人名」认)
_POST = re.compile(r"\b(?:specialist|expert|advis[eo]r|coordinator|team leader|consultant \(individual\)|individual consultant)\b", re.I)
_FIRM = re.compile(r"\b(?:LTD|LLC|LLP|INC|GMBH|CONSULT\w*|ENGINEER\w*|GROUP|CO|CORP\w*|COMPANY|S\.?A\.?|INSTITUTE|UNIVERSITY|ASSOC\w*|JV|"
                   r"CONSORTIUM|SERVICES|INTERNATIONAL|INTL|FOUNDATION|FUND|CENTER|CENTRE|AGENCY|LIMITED|PVT|BV|AB|AS|AG|SARL|PLC|NGO)\b", re.I)


def is_individual(title: str, name: str) -> bool:
    return bool(_POST.search(title)) and not _FIRM.search(name) and len(name.split()) <= 4 and not re.search(r"\d", name)


def _country(name: str, location: str) -> str:
    if awards.CHINESE_FIRM.search(name):
        return "CHN"
    m = re.search(r"\(([^()]+)\)\s*$", name)
    found = geo.detect(m.group(1)) if m else []
    found = found or geo.detect(location)
    return found[-1] if found else ""


def _ctype(doc: dict, title: str) -> str:
    if _one(doc, "ss_fct_group") == "consulting":
        cat = "Consultant Services"
    elif re.search(r"\bsuppl(?:y|ies)\b|procurement of|purchase|equipment|vehicles?|\bmeters?\b|materials|chemicals|software|machinery|"
                   r"excavators?|loaders?|\bpumps?\b|\bpipes?\b|instruments?|laborator", title, re.I):
        cat = "Goods"  # 亚行把工程和货物都归在 goods 下:名称写明供货的算货物,其余算工程
    else:
        cat = "Works"
    return awards.contract_type(cat, title)


def rows_from(docs: list[dict], min_usd: float = 100_000, today: date | None = None) -> list[dict]:
    out, seen = [], set()
    stamp = f"{SOURCE}({(today or date.today()).isoformat()} 取)"
    for d in sorted(docs, key=lambda x: (x.get("ds_date_posted") or "", x.get("id") or "")):
        title, project = _one(d, "tm_X3b_en_title"), _one(d, "tm_X3b_en_project_title")
        iso = geo.iso3(_one(d, "tm_X3b_en_country"))
        sector = is_water(title, project)
        try:
            price = float(_one(d, "sm_price") or 0)
        except ValueError:
            price = 0.0
        name = _one(d, "tm_X3b_en_contractor_name")
        if not sector or not iso or price < min_usd or not name or is_individual(title, name):
            continue
        pno = _one(d, "tm_X3b_en_project_number")
        dup = (pno, round(price))
        if dup in seen:  # 同一份合同登了两次
            continue
        seen.add(dup)
        loc = _one(d, "tm_X3b_en_location")
        firms = []
        for n in split_firms(name):
            c = _country(n, loc)
            zh = geo.name_zh(c) if c else ""
            firms.append({"name": n, "iso3": c, "country_zh": "" if zh == "未识别" else zh, "ext": ""})
        node = re.search(r"node/(\d+)", str(d.get("id") or d.get("ss_url") or ""))
        out.append({"key": f"ADB-{node.group(1) if node else pno + '-' + str(round(price))}", "iso3": iso, "name": title, "sector": sector,
                    "contract_type": _ctype(d, title), "price": f"{price:.2f}", "currency": "USD",
                    "awarded": str(d.get("ds_date_posted") or "")[:10], "quantity": awards.quantity(title),
                    "winner": "、".join(f["name"] for f in firms),
                    "winner_country": "、".join(dict.fromkeys(f["country_zh"] for f in firms if f["country_zh"])),
                    "project": f"{pno} {project}".strip(), "funder": "亚洲开发银行", "method": "",
                    "buyer": _one(d, "tm_X3b_en_executing_agency"), "place": f"{project} {_one(d, 'tm_X3b_en_executing_agency')}".strip(),
                    "source": stamp, "url": PROJECT_URL.format(pno) if pno else "", "winners": json.dumps(firms, ensure_ascii=False)})
    out.sort(key=lambda x: (x["iso3"], x["awarded"]))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="亚行已授合同 → 已中标项目数据文件")
    ap.add_argument("--cache", type=Path, default=None, help="检索结果每页存到这个目录(再跑不重取)")
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--min-usd", type=float, default=100_000)
    a = ap.parse_args(argv)
    docs = fetch_all(a.cache)
    rows = rows_from(docs, a.min_usd)
    awards.write_rows(a.out, rows)
    print(f"亚行已授合同 {len(docs)} 条 → 水务合同 {len(rows)} 份,写到 {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
