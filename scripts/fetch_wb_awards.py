"""取世界银行公开的合同授予数据,挑出水务合同,写成 A28 的数据文件(core/jobs/awards.py 的 COLS)。

数据:世界银行 Finances One「Contract Awards in Investment Project Financing」(DS00005,STEP 系统,借款国录入,2019 年起),
公开接口 datacatalogapi.worldbank.org,一页 1000 条,全量约 30 万条、300 MB,取一次几分钟。
挑法:项目属水务实践(project_global_practice 含 Water)的,合同名称归得进一个类别(core.jobs.awards.classify);别的项目(农业、防灾、城市等,
世行把灌溉、防洪、农村供水常归在这些下面)里的合同要过更严的 awards.classify_strict(只收供水、污水、海水淡化、水利、雨水排涝,道路、学校等字样的不收);
金额 ≥ --min-usd(缺省 10 万美元)、
不是个人咨询顾问合同;同一合同号的多行(联合体各家)并成一行,金额相同的只算一次、不同的相加。国别按借款国(地区性项目没有国别的丢掉)。
每家供应商另记进 winners 列(名称、注册国、世行供应商编号),导入时建成「供应商」;合同类型按 awards.contract_type(运营维护类合同记「运营」)。

python scripts/fetch_wb_awards.py [--out private/data/awards.csv.gz] [--min-usd 100000] [--cache 目录]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import geo  # noqa: E402
from core.jobs import awards  # noqa: E402

API = "https://datacatalogapi.worldbank.org/dexapps/fone/api/apiservice?datasetId=DS00005&resourceId=RS00005&top={top}&skip={skip}&type=json"
PAGE = 1000
PROJECT_URL = "https://projects.worldbank.org/en/projects-operations/project-detail/{}"


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


def fetch_all(cache: Path | None = None, workers: int = 8) -> list[dict]:
    """全量取下来(有缓存目录时每页存一份,下次不再取)。"""
    total = int(_get(API.format(top=1, skip=0)).get("count") or 0)

    def page(i: int) -> list[dict]:
        fp = cache / f"p{i}.json" if cache else None
        if fp and fp.exists():
            return json.loads(fp.read_text(encoding="utf-8"))["data"]
        d = _get(API.format(top=PAGE, skip=i * PAGE))
        if fp:
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        return d.get("data") or []

    with ThreadPoolExecutor(workers) as ex:
        return [r for part in ex.map(page, range((total + PAGE - 1) // PAGE)) for r in part]


def _day(s: str | None) -> str:
    try:
        return datetime.strptime(str(s or "").strip(), "%d-%b-%Y").date().isoformat()
    except ValueError:
        return ""


def _country_zh(code: str | None, name: str | None) -> str:
    iso = geo.iso3(code) if code else None
    zh = geo.name_zh(iso) if iso else ""
    return zh if zh and zh != "未识别" else str(name or "").strip()


def pick(raw: list[dict], min_usd: float = 100_000) -> list[dict]:
    """原始行 → 数据文件的行(一份合同一行)。"""
    groups: dict[str, list[dict]] = {}
    for r in raw:
        if " ".join(str(r.get("procurement_method") or "").split()) == "Individual Consultant Selection":
            continue
        no = str(r.get("wb_contract_number") or "").strip()
        if no:
            groups.setdefault(no, []).append(r)
    out = []
    for no, rs in groups.items():
        r0 = rs[0]
        name = " ".join(str(r0.get("contract_description") or "").split())
        water = "Water" in str(r0.get("project_global_practice") or "")
        sector = awards.classify(name) if water else awards.classify_strict(name)
        iso = geo.iso3(r0.get("borrower_country_code"))
        if not sector or not iso:
            continue
        amounts = {round(float(x.get("supplier_contract_amount_usd") or 0), 2) for x in rs}
        price = sum(amounts)
        if price < min_usd:
            continue
        winners = list(dict.fromkeys(" ".join(str(x.get("supplier") or "").split()) for x in rs if x.get("supplier")))
        firms: dict[str, dict] = {}
        for x in rs:
            nm = " ".join(str(x.get("supplier") or "").split())
            if nm and nm not in firms:
                iso_w = geo.iso3(x.get("supplier_country_code")) if x.get("supplier_country_code") else None
                sid = str(x.get("supplier_id") or "").strip()
                firms[nm] = {"name": nm, "iso3": iso_w or "", "country_zh": _country_zh(x.get("supplier_country_code"), x.get("supplier_country")),
                             "ext": f"WB:{sid}" if sid else ""}
        wc = list(dict.fromkeys(_country_zh(x.get("supplier_country_code"), x.get("supplier_country")) for x in rs))
        method = " ".join(str(r0.get("procurement_method") or "").split())
        pid = str(r0.get("project_id") or "").strip()
        out.append({"key": f"WB-{no}", "iso3": iso, "name": name, "sector": sector,
                    "contract_type": awards.contract_type(r0.get("procurement_category"), name),
                    "price": f"{price:.2f}", "currency": "USD", "awarded": _day(r0.get("contract_signing_date")), "quantity": awards.quantity(name),
                    "winner": "、".join(winners), "winner_country": "、".join(x for x in wc if x),
                    "project": f"{pid} {str(r0.get('project_name') or '').strip()}".strip(), "funder": "世界银行",
                    "method": awards.METHOD_ZH.get(method, method),
                    "source": f"世界银行合同授予数据(STEP,{_day(r0.get('as_of_date')) or '取数日'} 取)",
                    "url": PROJECT_URL.format(pid) if pid else "", "winners": json.dumps(list(firms.values()), ensure_ascii=False)})
    out.sort(key=lambda x: (x["iso3"], x["awarded"]), reverse=False)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="世界银行合同授予数据 → 已中标项目数据文件")
    ap.add_argument("--out", type=Path, default=awards.DATA)
    ap.add_argument("--min-usd", type=float, default=100_000)
    ap.add_argument("--cache", type=Path, default=None, help="原始页存到这个目录(再跑不重取)")
    a = ap.parse_args(argv)
    raw = fetch_all(a.cache)
    rows = pick(raw, a.min_usd)
    awards.write_rows(a.out, rows)
    print(f"原始 {len(raw)} 行 → 水务合同 {len(rows)} 份,写到 {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
