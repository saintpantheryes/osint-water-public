"""取欧洲复兴开发银行(EBRD)电子采购门户 ECEPP 的「授标公告」(Contract Award Notice),挑出水务合同,写成 A28 的数据文件(core/jobs/awards.py 的 COLS)。

ECEPP 的公告检索页一次给出全部公告(约 4 千条,2015 年起,授标公告约 1 千多条);每条授标公告的详情页写着签约日期、合同金额与币种、
中标单位(联合体列出各家及所在国)、业主、项目编号与名称。EBRD 在中亚、高加索、东欧、土耳其、北非放贷,
世界银行的数据没有这些合同。分三步,每步的结果存进缓存目录,断了再跑接着做:

  list    检索页 → 缓存/notices.jsonl(只留授标公告)
  detail  逐条打开授标公告详情页 → 缓存/detail/<编号>.html(已有的跳过)
  out     详情 → 数据文件(缺省 private/data/awards_ebrd.csv.gz)

挑法:项目名称或业主像水务的(供水、污水、灌溉、防洪、水务公司),合同名称、说明或项目名称归得进一个类别(awards.classify),
名称里没有车辆、房屋之类明显不是水务工程的字样(awards.NOT_WATER);别的项目里的合同要过 awards.classify_strict。
金额折合 ≥ --min-usd(缺省 10 万美元)。一条公告列了几个标段(Lot)的,每个标段一行;同一标段登在几条公告上的只收一次。

python scripts/fetch_ebrd_awards.py list   --cache 目录
python scripts/fetch_ebrd_awards.py detail --cache 目录
python scripts/fetch_ebrd_awards.py out    --cache 目录 [--out private/data/awards_ebrd.csv.gz] [--min-usd 100000]
只做公开检索与下载,不登录。
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import geo  # noqa: E402
from core.jobs import awards  # noqa: E402

BASE = "https://ecepp.ebrd.com/delta/"
LIST_URL = BASE + "noticeSearchResults.html"
DETAIL_URL = BASE + "viewNotice.html?displayNoticeId={}"
OUT = awards.DATA.with_name("awards_ebrd.csv.gz")
SOURCE = "欧洲复兴开发银行采购门户(ECEPP)授标公告"

# 项目或业主像水务的:供水、污水、灌溉、防洪、各国水务公司的叫法(vodokanal、vodovod、apă-canal、su ve kanalizasyon……)
WATER_PROJECT = re.compile(r"water|waste ?water|sewer|sanita|vodokanal|vodovod|kanaliz|suvtaminot|apa[- ]?canal|\bapa\b|\bsu ve\b|\bViK\b|"
                           r"irrigat|flood|drainage|\bw?wtp\b|desalin|водоканал|водоснаб|канализ", re.I)
NOT_WATER_PROJECT = re.compile(r"hydro ?power|\bHPP\b|hydroelectric|district heating|heating", re.I)
TYPE_EN = {"Works": "Works", "Goods": "Goods", "Consultancy": "Consultant Services", "Consultancy Services": "Consultant Services",
           "Non-consultancy Services": "Non-consulting Services", "Services": "Non-consulting Services"}
METHOD_ZH = {"Open Tender Single Stage": "公开招标(单阶段)", "Open Tender Two Stage": "公开招标(两阶段)",
             "Open Tender Single - Two Envelope": "公开招标(单阶段双信封)", "Open Tender with Prequalification": "资格预审后公开招标",
             "Consultancy Single Stage Open": "咨询公开选择", "Consultancy Competitive Selection": "咨询竞争性选择",
             "Direct Contracting": "直接签约", "Limited Tender": "限制性招标", "Shopping": "询价"}


def _text(fragment: str) -> list[str]:
    """一段 HTML → 去掉标签后的非空行。"""
    t = re.sub(r"<br\s*/?>|</?address>|</?p>|</?div[^>]*>", "\n", fragment, flags=re.I)
    t = html.unescape(re.sub(r"<[^>]+>", "", t))
    return [" ".join(x.split()) for x in t.split("\n") if x.strip()]


def _field(page: str, label: str) -> str:
    m = re.search(r"<b>\s*" + re.escape(label) + r":?\s*</b>\s*(?:<br\s*/?>)?(.*?)<br", page, re.S | re.I)
    if m:
        return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", m.group(1))).split())
    m = re.search(re.escape(label) + r":?\s*</[^>]+>\s*(?:<[^>]+>\s*)*([^<]+)", page, re.I)
    return " ".join(html.unescape(m.group(1)).split()) if m else ""


def _money(s: str) -> tuple[float, str]:
    """「201,450.00 EUR」「EUR 2,352,875.53」「€ 1,137,050.9」→ (金额, 币种)。"""
    s = html.unescape(str(s or "")).replace("€", " EUR ")
    m = re.search(r"([A-Z]{3})\s*([\d,]+(?:\.\d+)?)|([\d,]+(?:\.\d+)?)\s*([A-Z]{3})", s)
    if not m:
        return 0.0, ""
    cur, num = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
    try:
        return float(num.replace(",", "")), cur
    except ValueError:
        return 0.0, cur


def _firms_from_details(lines: list[str]) -> list[dict]:
    """「Lead Partner Details: / Other Partner Details:」下的各家:名称一行、地址若干行,以「城市, 国别, 邮编」一行收尾。"""
    out, cur = [], None
    for ln in lines:
        if re.match(r"(?:Lead|Other) Partner Details", ln, re.I):
            cur = None
            continue
        if cur is None:
            cur = {"name": ln, "iso3": ""}
            out.append(cur)
            continue
        m = re.fullmatch(r"[^,]*,\s*([^,]+?)\s*,\s*[\w -]*", ln)  # 收尾行「城市, 国别, 邮编」
        iso = geo.iso3(m.group(1)) if m else None
        if iso:
            cur["iso3"] = iso
            cur = None
    return out


def _clean_name(s: str) -> str:
    s = " ".join(html.unescape(s).replace("“", '"').replace("”", '"').replace("«", '"').replace("»", '"').split())
    s = s.strip(" ,;")
    if s.count('"') % 2:  # 联合体拆开后落单的引号:「LLC "Profit-Express」「CJSC Kaynar"」
        s = s[:-1].rstrip() if s.endswith('"') else s + '"'
    return s


def parse_detail(page: str) -> dict:
    """授标公告详情页 → {project, project_id, country, client, type, method, sector, title, description, signed, lots: [...]}。
    lots 里每项 {lot, name, firms: [{name, iso3}], price, currency, eur}。"""
    page = re.sub(r"<script.*?</script>|<style.*?</style>", "", page, flags=re.S | re.I)
    d = {k: _field(page, lab) for k, lab in [("project", "Project Name"), ("project_id", "EBRD Project ID"), ("country", "Country"),
                                               ("client", "Client Name"), ("type", "Type of Procurement"), ("method", "Procurement Method"),
                                               ("sector", "Business Sector"), ("title", "Contract Title"), ("description", "Contract Description"),
                                               ("signed", "Date of Contract Signature"), ("value", "Signed Contract Value")]}
    i = page.find("1. Contract Details")
    j = page.find("3. Other Information")
    body = page[i:j if j > i else len(page)]
    lots = []
    blocks = re.split(r"<b>\s*Lot\s+(\w+)\s*</b>", body)
    if len(blocks) > 1:
        for k in range(1, len(blocks), 2):
            lines = _text(re.split(r"<b>\s*2\. Contracting Party", blocks[k + 1])[0])
            if not lines:
                continue
            price_line = next((x for x in lines if x.startswith("Contract Price:")), "")
            eur_line = next((x for x in lines if x.startswith("Contract Price (Euro")), "")
            price, cur = _money(price_line.split(":", 1)[-1])
            eur, _ = _money(eur_line.split(":", 1)[-1])
            at = next((n for n, x in enumerate(lines) if x.startswith("Address:")), 0)
            name = _clean_name(lines[at - 1] if at else lines[0])  # 有的标段名会续到第一行(「4 (pickup type).」),中标单位是地址的上一行
            if "JVCA Details:" in lines:
                firms = _firms_from_details(lines[lines.index("JVCA Details:") + 1:])
            else:
                head = lines[:next((n for n, x in enumerate(lines) if x.startswith("Contract Price")), len(lines))]
                iso = next((c for c in (geo.iso3(x) for x in reversed(head[1:])) if c), None)
                firms = [{"name": name, "iso3": iso or ""}]
            lots.append({"lot": blocks[k], "name": name, "firms": [dict(f, name=_clean_name(f["name"])) for f in firms if f["name"]],
                         "price": price, "currency": cur, "eur": eur})
    else:
        m = re.search(r"<b>\s*2\. Contracting Party\s*</b>(.*?)<b>\s*JVCA Partners", body, re.S | re.I)
        party = _text(m.group(1)) if m else []
        name = _clean_name(party[0]) if party else ""
        iso = next((c for c in (geo.iso3(x) for x in reversed(party[1:])) if c), None)
        firms = []
        jm = re.search(r"JVCA Partners:?\s*</b>(.*?)(?:<b>|$)", body, re.S | re.I)
        if jm and "No JVCA partners" not in jm.group(1):
            for pm in re.finditer(r"Lead Partner:\s*(Yes|No)\s*<br\s*/?>\s*(.*?)<address>(.*?)</address>", jm.group(1), re.S | re.I):
                addr = _text(pm.group(3))
                f = {"name": _clean_name(_text(pm.group(2))[0]) if _text(pm.group(2)) else "",
                     "iso3": next((c for c in (geo.iso3(x) for x in reversed(addr)) if c), None) or ""}
                if f["name"]:
                    firms.insert(0, f) if pm.group(1).lower() == "yes" else firms.append(f)
        if not firms and name:
            firms = [{"name": name, "iso3": iso or ""}]
        price, cur = _money(d["value"])
        if name or firms:
            lots.append({"lot": "", "name": name, "firms": firms, "price": price, "currency": cur, "eur": 0.0})
    d["lots"] = lots
    return d


def is_water(d: dict) -> str | None:
    """详情 → 类别;不是水务合同返回 None。"""
    proj = f"{d.get('project') or ''} {d.get('client') or ''}"
    title, desc = str(d.get("title") or ""), str(d.get("description") or "")
    if WATER_PROJECT.search(proj) and not NOT_WATER_PROJECT.search(proj):
        if awards.NOT_WATER.search(title):
            return None
        return awards.classify(title) or awards.classify(desc) or awards.classify(d.get("project"))
    return awards.classify_strict(title)


def _day(s: str | None) -> str:
    for fmt in ("%d/%m/%Y", "%d/%m/%Y %H:%M"):
        try:
            return datetime.strptime(str(s or "").strip(), fmt).date().isoformat()
        except ValueError:
            pass
    return ""


def _country_zh(iso: str | None) -> str:
    zh = geo.name_zh(iso) if iso else ""
    return "" if not zh or zh == "未识别" else zh


def rows_from(notices: list[dict], details: dict[str, dict], min_usd: float = 100_000, rates=None, today: date | None = None) -> list[dict]:
    """授标公告 + 详情 → 数据文件的行(一个标段一行)。rates:core.fx.Rates(折美元比门槛)。"""
    if rates is None:
        from core.fx import Rates
        rates = Rates()
    out, seen = [], set()
    stamp = f"{SOURCE}({(today or date.today()).isoformat()} 取)"
    for n in sorted(notices, key=lambda x: int(x["id"])):
        d = details.get(str(n["id"]))
        if not d:
            continue
        sector = is_water(d)
        iso = geo.iso3(d.get("country")) or geo.iso3(str(n.get("title") or "").split(":")[0])
        if not sector or not iso:
            continue
        many = len(d["lots"]) > 1
        for lot in d["lots"]:
            usd = rates.to_usd(lot["price"], lot["currency"]) if lot["price"] else None
            if usd is None and lot["eur"]:
                usd = rates.to_usd(lot["eur"], "EUR")
            firms = [f for f in lot["firms"] if f.get("name")]
            if not firms or not usd or usd < min_usd:
                continue
            dup = (str(d.get("project_id") or ""), awards.contractor_key(firms[0]["name"]), round(usd, -2))
            if dup in seen:  # 同一标段登在几条公告上
                continue
            seen.add(dup)
            name = str(d.get("title") or "").strip() or str(n.get("title") or "").split(":", 1)[-1].strip()
            if many and lot["lot"]:
                name = re.sub(r"[\s,;-]*\bLot\(s\):.*$", "", name, flags=re.I) + f"(Lot {lot['lot']})"
            for f in firms:
                if awards.CHINESE_FIRM.search(f["name"]):
                    f["iso3"] = "CHN"
                f["country_zh"], f["ext"] = _country_zh(f.get("iso3")), ""
            method = str(d.get("method") or "").strip()
            pid, pname = str(d.get("project_id") or "").strip(), str(d.get("project") or "").strip()
            out.append({"key": f"EBRD-{n['id']}" + (f"-{lot['lot']}" if many and lot["lot"] else ""), "iso3": iso, "name": name, "sector": sector,
                        "contract_type": awards.contract_type(TYPE_EN.get(str(d.get("type") or "").strip(), ""), name),
                        "price": f"{lot['price']:.2f}", "currency": lot["currency"] or "EUR",
                        "awarded": _day(d.get("signed")) or _day(str(n.get("pub") or "")[:10]), "quantity": awards.quantity(name),
                        "winner": "、".join(f["name"] for f in firms),
                        "winner_country": "、".join(dict.fromkeys(f["country_zh"] for f in firms if f["country_zh"])),
                        "project": f"{pid} {pname}".strip(), "funder": "欧洲复兴开发银行", "method": METHOD_ZH.get(method, method),
                        "buyer": str(d.get("client") or "").strip(), "place": f"{pname} {d.get('client') or ''}".strip(),
                        "source": stamp, "url": DETAIL_URL.format(n["id"]), "winners": json.dumps(firms, ensure_ascii=False)})
    out.sort(key=lambda x: (x["iso3"], x["awarded"]))
    return out


def list_notices(cache: Path, log=print) -> int:
    """检索页(一次给出全部公告)→ 缓存/notices.jsonl,只留授标公告。"""
    from core.watch import fetch

    page = fetch.get(LIST_URL, timeout=180, max_bytes=60_000_000).text
    out = []
    for row in re.findall(r"<tr>(.*?)</tr>", page, re.S):
        m = re.search(r'displayNoticeId=(\d+)">([^<]*)</a>', row)
        cells = [" ".join(html.unescape(re.sub(r"<[^>]+>", " ", c)).split()) for c in re.findall(r"<td>(.*?)</td>", row, re.S)]
        if not m or len(cells) < 4 or "award" not in cells[1].lower():
            continue
        out.append({"id": m.group(1), "title": " ".join(html.unescape(m.group(2)).split()), "type": cells[1], "pub": cells[3],
                    "meta": cells[-1]})
    cache.mkdir(parents=True, exist_ok=True)
    with open(cache / "notices.jsonl", "w", encoding="utf-8") as f:
        for n in out:
            f.write(json.dumps(n, ensure_ascii=False) + "\n")
    log(f"授标公告 {len(out)} 条")
    return len(out)


def fetch_details(cache: Path, log=print) -> int:
    from core.watch import fetch

    ddir = cache / "detail"
    ddir.mkdir(parents=True, exist_ok=True)
    done = 0
    for n in load_notices(cache):
        fp = ddir / f"{n['id']}.html"
        if fp.exists():
            continue
        try:
            fp.write_text(fetch.get(DETAIL_URL.format(n["id"]), timeout=90).text, encoding="utf-8")
            done += 1
        except Exception as e:  # noqa: BLE001
            log(f"  {n['id']} 没取到:{e}")
    return done


def load_notices(cache: Path) -> list[dict]:
    return [json.loads(ln) for ln in open(cache / "notices.jsonl", encoding="utf-8")]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="EBRD 授标公告 → 已中标项目数据文件")
    ap.add_argument("step", choices=["list", "detail", "out"])
    ap.add_argument("--cache", type=Path, required=True, help="缓存目录(清单、详情页)")
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--min-usd", type=float, default=100_000)
    a = ap.parse_args(argv)
    if a.step == "list":
        list_notices(a.cache)
    elif a.step == "detail":
        print(f"取了 {fetch_details(a.cache)} 条")
    else:
        details = {f.stem: parse_detail(f.read_text(encoding="utf-8")) for f in (a.cache / "detail").glob("*.html")}
        rows = rows_from(load_notices(a.cache), details, a.min_usd)
        awards.write_rows(a.out, rows)
        print(f"授标公告详情 {len(details)} 条 → 水务合同 {len(rows)} 份,写到 {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
