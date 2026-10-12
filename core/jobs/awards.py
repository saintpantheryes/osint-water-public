"""A28 导入已中标项目:按私有配置仓的 data/awards.csv.gz 把已授出的合同建进「已中标项目」表;只新建、只填空(R2),去重按编号。
首批数据由 scripts/fetch_wb_awards.py 从世界银行公开的合同授予数据取(那个脚本用这里的 classify / quantity 归类、摘工程量);
别的来源整理成同样的列(COLS)也能导入。城市:合同名称(或所属项目名)里出现了该国某座规模以上城市的英文名才挂,几座都出现取人口最多的。
供应商(v2.15):winners 列是 JSON 列表 [{name, iso3, country_zh, ext}],每家一行建进「供应商」表(标识 = contractor_key(名称),同名的并成一家),
再把合同的「中标方」关联到这几家;同样只新建、只填空。
后台每个周期另有 translate:给没有中文名的行请模型译中文名(配了模型时,一次 TRANSLATE_BATCH 条)。
命令行:python -m core.jobs.awards [--file 路径] [--dry-run]。库里还没建这张表时跳过。"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import math
import os
import re
import sys
from pathlib import Path

from core import tstore

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "private" / "data" / "awards.csv.gz"
OID = "award"
COLS = ["key", "iso3", "name", "sector", "contract_type", "price", "currency", "awarded", "quantity", "winner", "winner_country",
        "project", "funder", "method", "source", "url", "winners"]
TEXT = ("name", "sector", "contract_type", "currency", "awarded", "quantity", "winner", "winner_country", "project", "funder", "method", "source", "url")

# 类别(lead_sectors):按合同名称里的词归,先中先得(海水淡化 → 污水 → 雨水排涝 → 供水 → 水利 → 水环境);英、法、西、葡、越南文都认。
# 只有泛泛的「水」字样的算其他;一个都不中的不是水务合同(fetch 时丢掉)。
SECTOR_WORDS: list[tuple[str, str]] = [
    ("海水淡化", r"desalin|dessal|desalaci|khử mặn"),
    ("污水", r"sewer|sewage|waste ?water|\bw?wtp\b|\bstp\b|\bfstp\b|sanitation|latrine|toilet|septic|f[a]?ecal|sludge|assainissement|eaux us[ée]es|"
            r"alcantarillado|aguas residuales|saneamiento|esgot|nước thải|vệ sinh"),
    ("雨水排涝", r"drain|storm ?water|caniveau|drenaje|pluvial|thoát nước|inundaci[oó]n urbana"),
    ("供水", r"water supply|drinking|potable|\bwtp\b|water treatment|distribution (?:network|pipe|line)|transmission (?:main|pipe|line)|borehole|tube ?wells?|"
            r"water (?:storage|tank|scheme|meter|connection|kiosk|point|pipe|network|system|works|utilit)|piped water|eau potable|adduction|\baep\b|forages?|branchements?|"
            r"agua potable|abastecimiento|acueducto|abastecimento|água|cấp nước|nước sạch|non-revenue|\bnrw\b"),
    ("水利", r"irrigat|\bdams?\b|canal|embankment|levee|\bdykes?\b|\bdikes?\b|\bweirs?\b|barrage|flood|river ?bank|reservoir|hồ chứa|riego|irriga[çc]|"
            r"thủy lợi|đê|hydraulique agricole|périmètre"),
    ("水环境", r"river restoration|lake|wetland|watershed|catchment|water pollution|water quality|basin management|ecolog|rivière|cours d'eau"),
    ("其他", r"\bwater\b|\beau\b|\bagua\b|\bnước\b|hydro"),
]
_SECTOR_RE = [(s, re.compile(p, re.I)) for s, p in SECTOR_WORDS]

# 工程量:合同名称里带单位的数量(原文照摘,最多 4 处)
_QTY = re.compile(r"(?<![\w.+/:-])(\d[\d,.]*\s?(?:km|kms|kilom[eè]tres?|kilometers?|m3/d(?:ay)?|m³/d(?:ay)?|m3/j|m3/h|m3\b|m³|cubic met(?:er|re)s?|mld|ml/d|l/s|"
                  r"ha\b|hectares?|tube ?wells?|boreholes?|wells?|forages?|connections?|branchements?|latrines?|villages?|reservoirs?|"
                  r"(?:water )?tanks?|pumping stations?|stations? de pompage|bornes? fontaines?|(?:water )?kiosks?|meters?|m[eè]tres|lm\b|rm\b|m\b))", re.I)

# 合同类型「运营」:工程、非咨询服务类合同里,名称写明是运营维护、委托运营、管理合同、租赁经营的(咨询、货物不算)
_OPS = re.compile(r"operation(?:s)? (?:and|&) maintenance|\bO ?& ?M\b|\bO&M\b|management contract|affermage|gestion d[ée]l[ée]gu[ée]e|"
                  r"exploitation et (?:de )?maintenance|operaci[oó]n y mantenimiento|opera[çc][ãa]o e manuten[çc][ãa]o|"
                  r"performance[- ]based (?:contract|management|operation)|lease contract|operate and maintain", re.I)
TYPE_ZH = {"Works": "工程", "Goods": "货物", "Consultant Services": "咨询服务", "Non-consulting Services": "非咨询服务"}
METHOD_ZH = {"Request for Bids": "公开招标(RFB)", "Request for Quotations": "询价(RFQ)", "Direct Selection": "直接选定",
             "Quality And Cost-Based Selection": "质量和费用选择(QCBS)", "Consultant Qualification Selection": "咨询顾问资格选择(CQS)",
             "Least Cost Selection": "最低费用选择(LCS)", "Quality Based Selection": "质量选择(QBS)", "Fixed Budget Selection": "固定预算选择(FBS)",
             "Request for Proposals": "建议书邀请(RFP)", "Community Driven Development": "社区主导", "UN Agencies (Direct)": "联合国机构(直接)",
             "E-Auctions": "电子反拍", "Force Account": "自营工程", "Public Private Partnership": "公私合营(PPP)",
             "Individual Consultant Selection": "个人咨询顾问选择", "Non-Profit Organizations": "非营利组织(直接)"}

CITY_MIN_LEN = 5  # 城市英文名短于这个的不认(Ica、Bo 这类会误中)


def classify(text: str | None) -> str | None:
    """合同名称 → 类别;不是水务合同返回 None。"""
    t = str(text or "")
    return next((s for s, rx in _SECTOR_RE if rx.search(t)), None)


def quantity(text: str | None, most: int = 4) -> str:
    """合同名称里带单位的数量,「、」隔开;「Lot 1」这类编号、年份不算。"""
    out: list[str] = []
    for m in _QTY.finditer(str(text or "")):
        q = re.sub(r"\s+", " ", m.group(1)).strip()
        num = re.match(r"[\d,.]+", q).group(0).replace(",", "")
        if re.fullmatch(r"\d+\s?m", q, re.I) and len(num) < 3:  # 「5m」多半是宽度、深度,不是长度
            continue
        try:
            if float(num.rstrip(".")) == 0:
                continue
        except ValueError:
            continue
        if q.lower() not in (x.lower() for x in out):
            out.append(q)
        if len(out) >= most:
            break
    return "、".join(out)


def contract_type(category: str | None, name: str | None) -> str:
    """世行采购类别 + 合同名称 → 合同类型(award_contract_type);认不出的类别返回空串。"""
    zh = TYPE_ZH.get(str(category or "").strip(), "")
    t = str(name or "")
    side = re.search(r"(?:includ\w*|with|plus|and)\s+(?:the\s+)?(?:O ?& ?M|operation)|\b(?:design|build|construct|fabricat|install)", t, re.I)  # 设计建设(含运维)仍是工程
    return "运营" if zh in ("工程", "非咨询服务") and _OPS.search(t) and not side else zh


# 供应商标识:名称大写、去标点、去开头的 M/S 与结尾的公司后缀(可连着几个),空白并成一个
_SUFFIX = {"LLC", "LTD", "LIMITED", "CO", "COMPANY", "CORP", "CORPORATION", "INC", "INCORPORATED", "JSC", "OJSC", "CJSC", "PJSC", "OAO", "ZAO",
           "OOO", "TOO", "SA", "SAS", "SARL", "SRL", "SPA", "SAU", "SL", "SAC", "LTDA", "LDA", "CIA", "PVT", "PRIVATE", "PLC", "LLP", "GMBH",
           "AG", "BV", "NV", "AB", "AS", "PTY", "PT", "TBK", "BHD", "SDN", "KG", "EIRL", "SE", "AND", "Ş", "ŞTI", "STI", "AŞ", "A", "S", "L", "C"}
_PREFIX = {"LLC", "OOO", "TOO", "JSC", "OJSC", "CJSC", "PT", "CV", "SARL", "SA", "ETS", "STE"}
_PHRASES = re.compile(r"\b(?:GESELLSCHAFT MIT BESCHR[ÄA]NKTER HAFTUNG|LIMITED LIABILITY (?:COMPANY|PARTNERSHIP)|(?:OPEN |CLOSED |PUBLIC )?JOINT[ -]STOCK COMPANY|"
                      r"ANON[İI]M [ŞS][İI]RKET[İI]|L[İI]M[İI]TED [ŞS][İI]RKET[İI]|SANAY[İI] VE T[İI]CARET|C[ÔO]NG TY (?:C[ỔO] PH[ẦA]N|TNHH(?: MTV)?)|"
                      r"PRIVATE LIMITED|SOCI[ÉE]T[ÉE] ANONYME|SOLE PROPRIETORSHIP)\b")


def contractor_key(name: str | None) -> str:
    t = str(name or "").upper().replace("&", " AND ")
    t = re.sub(r"^\s*(?:M\s*[/.]?\s*S\b\.?|MESSRS\.?)\s*", "", t)  # M/S、M/s.、Messrs
    t = re.sub(r"\b([A-Z])\.(?=[A-Z]\.)", r"\1", t)  # S.A. / P.L.C. → SA / PLC
    t = re.sub(r"(?<=\b[A-Z])\.(?=\s|$)", "", t)
    t = _PHRASES.sub(" ", t)
    words = re.sub(r"[^\w]+", " ", t).split()
    while len(words) > 1 and words[0] in _PREFIX:
        words.pop(0)
    while len(words) > 1 and words[-1] in _SUFFIX:
        words.pop()
    return " ".join(words)


def winners_of(row: dict) -> list[dict]:
    """数据文件一行的 winners 列 → [{name, iso3, country_zh, ext}];没有或读不了时为空。"""
    try:
        xs = json.loads(row.get("winners") or "[]")
    except ValueError:
        return []
    return [x for x in xs if isinstance(x, dict) and str(x.get("name") or "").strip()] if isinstance(xs, list) else []


def read_rows(path: Path) -> list[dict]:
    raw = path.read_bytes()
    text = gzip.decompress(raw).decode("utf-8") if raw[:2] == b"\x1f\x8b" else raw.decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def write_rows(path: Path, rows: list[dict]) -> None:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLS, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    data = buf.getvalue().encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(data, mtime=0) if path.suffix == ".gz" else data)


def _num(v: str | None) -> float | None:
    try:
        x = float(str(v).replace(",", "")) if v not in (None, "") else None
    except ValueError:
        return None
    return x if x is None or math.isfinite(x) else None


class CityIndex:
    """城市表:国别记录编号 → [(英文名正则, 人口, 城市记录编号)],人口从多到少。"""

    def __init__(self, b: tstore.Base):
        self.by_country: dict[str, list[tuple[re.Pattern, float, str]]] = {}
        if "city" not in b.t:
            return
        for r in b.rows("city", props=["name_en", "country", "population"]):
            name, crid = str(b.get("city", r, "name_en") or "").strip(), tstore.link_id(b.get("city", r, "country"))
            if crid and len(name) >= CITY_MIN_LEN:
                rx = re.compile(r"(?<!\w)" + re.escape(name) + r"(?!\w)", re.I)
                self.by_country.setdefault(crid, []).append((rx, float(b.get("city", r, "population") or 0), r["id"]))
        for v in self.by_country.values():
            v.sort(key=lambda x: -x[1])

    def find(self, country_rid: str, *texts: str | None) -> str | None:
        for t in texts:
            if t:
                hit = next((rid for rx, _, rid in self.by_country.get(country_rid, []) if rx.search(t)), None)
                if hit:
                    return hit
        return None


def values(row: dict, country_rid: str, city_rid: str | None) -> dict:
    """数据文件的一行 → 本体属性的值(空的不写)。"""
    v: dict = {k: (str(row.get(k) or "").strip() or None) for k in TEXT}
    v["price"] = _num(row.get("price"))
    v["country"], v["city"] = country_rid, city_rid
    if v["price"] is not None:
        v["price"] = round(v["price"], 2)
    return {k: x for k, x in v.items() if x is not None}


def run(b: tstore.Base, path: Path | None = None, dry_run: bool = False, log=print) -> dict:
    """→ {新建, 补空, 挂上城市, 跳过(国别表里没有)};表没建或没有数据文件时跳过。"""
    path = path or DATA
    if OID not in b.t:
        return {"跳过": "库里还没有「已中标项目」表(先跑建表命令)"}
    if not path.exists():
        return {"跳过": f"没有数据文件 {path}"}
    rows = read_rows(path)
    countries = {str(b.get("country", r, "key")): r["id"] for r in b.rows("country", props=["key"]) if b.get("country", r, "key")}
    cities = CityIndex(b)
    props = ["key", "country", "city", *[p for p in (*TEXT, "price", "winners") if b.has(OID, p)]]
    have = {str(b.get(OID, r, "key")): r for r in b.rows(OID, props=props) if b.get(OID, r, "key")}
    allowed = {p: {c["name"] for c in (b.field(OID, p).get("options") or {}).get("choices") or []} for p in ("sector", "contract_type", "currency") if b.has(OID, p)}
    with_firms = b.has(OID, "winners") and "contractor" in b.t
    firms, n_firm_new, n_firm_fill = _contractors(b, rows, countries, dry_run) if with_firms else ({}, 0, 0)
    new, ups, n_city, skipped, seen = [], [], 0, 0, set()
    for row in rows:
        key = str(row.get("key") or "").strip()
        crid = countries.get(str(row.get("iso3") or "").strip())
        if not key or key in seen:
            continue
        seen.add(key)
        if not crid:
            skipped += 1
            continue
        city = cities.find(crid, row.get("name"), (row.get("project") or "").split(" ", 1)[-1])
        want = {k: x for k, x in values(row, crid, city).items() if k in ("country", "city") or b.has(OID, k)}
        ids = list(dict.fromkeys(firms[k] for k in (contractor_key(w["name"]) for w in winners_of(row)) if firms.get(k)))
        if ids:
            want["winners"] = ids
        for p, ok in allowed.items():  # 选项里没有的值不写(建表时选项来自本体)
            if p in want and want[p] not in ok:
                want.pop(p)
        rec = have.get(key)
        if rec is None:
            new.append({"key": key, **want})
            n_city += bool(city)
            continue
        cur = {p: b.get(OID, rec, p) for p in want}
        changes, _ = tstore.fill_empty({**cur, "winners": tstore.link_ids(cur.get("winners"))} if "winners" in cur else cur, want)
        if changes:
            ups.append((rec["id"], changes))
            n_city += "city" in changes
    if not dry_run:
        b.create(OID, new)
        b.update_many(OID, ups)
    out = {"新建": len(new), "补空": len(ups), "挂上城市": n_city, "国别表里没有": skipped}
    if with_firms:
        out.update({"新建供应商": n_firm_new, "供应商补空": n_firm_fill})
    log(f"  award:新建 {len(new)},补空 {len(ups)},挂上城市 {n_city},国别表里没有的跳过 {skipped}"
        + (f";供应商新建 {n_firm_new},补空 {n_firm_fill}" if with_firms else "") + ("(只算,没写)" if dry_run else ""))
    return out


def _contractors(b: tstore.Base, rows: list[dict], countries: dict, dry_run: bool) -> tuple[dict[str, str | None], int, int]:
    """把数据文件里出现的供应商建进「供应商」表(国别表里没有的合同也不建它的单位)。
    → ({标识: 记录编号(只算不写时新建的是 None)}, 新建几家, 补空几家)"""
    C = "contractor"
    want: dict[str, dict] = {}
    for row in rows:
        if not countries.get(str(row.get("iso3") or "").strip()):
            continue
        for w in winners_of(row):
            k = contractor_key(w["name"])
            if not k:
                continue
            v = want.setdefault(k, {"name": " ".join(str(w["name"]).split()), "ext": [], "nationality": None, "nationality_code": None})
            v["nationality"] = v["nationality"] or str(w.get("country_zh") or "").strip() or None
            v["nationality_code"] = v["nationality_code"] or str(w.get("iso3") or "").strip() or None
            if w.get("ext") and w["ext"] not in v["ext"]:
                v["ext"].append(str(w["ext"]))
    props = [p for p in ("key", "name", "nationality", "nationality_code", "ext_ids") if b.has(C, p)]
    have = {str(b.get(C, r, "key")): r for r in b.rows(C, props=props) if b.get(C, r, "key")}
    new, ups = [], []
    for k, v in want.items():
        vals = {"name": v["name"], "nationality": v["nationality"], "nationality_code": v["nationality_code"], "ext_ids": ",".join(v["ext"][:20]) or None}
        vals = {p: x for p, x in vals.items() if x and b.has(C, p)}
        rec = have.get(k)
        if rec is None:
            new.append({"key": k, **vals})
            continue
        changes, _ = tstore.fill_empty({p: b.get(C, rec, p) for p in vals}, vals)
        if changes:
            ups.append((rec["id"], changes))
    ids: dict[str, str | None] = {k: r["id"] for k, r in have.items()}
    if dry_run:
        ids.update({x["key"]: None for x in new})
    else:
        ids.update({x["key"]: r["id"] for x, r in zip(new, b.create(C, new))})
        b.update_many(C, ups)
    return ids, len(new), len(ups)


ZH_SYSTEM = ("你是水务与国际工程领域的翻译。每份合同:zh 把名称译成简洁准确的中文(一般 40 字以内),工程内容、地名、标段号照译,"
             "机构用通用中文译名,合同号保留原文,不要解释;qty 写名称里明说了的主要工程量(数量 + 中文单位,如「配水管网 17 km」「水表 5 万只」,"
             "几项用「、」隔开),名称里没写数量的写空串,不要猜。")
ZH_GUIDE = '\n输出格式:只输出一个 JSON 对象 {"items": [{"i": 序号, "zh": "中文名", "qty": "工程量或空串"}, …]},不要任何其他文字。\n'
ZH_SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": {
    "type": "object", "properties": {"i": {"type": "integer"}, "zh": {"type": "string"}, "qty": {"type": "string"}}, "required": ["i", "zh", "qty"],
    "additionalProperties": False}}}, "required": ["items"], "additionalProperties": False}
TRANSLATE_BATCH = 40


def translate(b: tstore.Base, clf, limit: int = TRANSLATE_BATCH, log=print) -> int:
    """没有中文名的行请模型译一批,工程量空着的顺带从名称里摘;只填空。没配模型、表没建或字段没有时什么也不做。→ 写了几条"""
    if clf is None or not getattr(clf, "enabled", False) or not hasattr(clf, "ask") or OID not in b.t or not b.has(OID, "name_zh"):
        return 0
    it = b.api.iter_records(b.t[OID], filter=b.filter(b.cond(OID, "name_zh", "isEmpty"), b.cond(OID, "name", "isNotEmpty")),
                            projection=[b.fid(OID, p) for p in ("name", "quantity") if b.has(OID, p)], take=limit)
    rows = [r for _, r in zip(range(limit), it)]  # 只要第一页
    if not rows:
        return 0
    names = [str(b.get(OID, r, "name") or "")[:300] for r in rows]
    out = clf.ask(ZH_SYSTEM, json.dumps([{"i": i, "t": t} for i, t in enumerate(names)], ensure_ascii=False), ZH_SCHEMA, ZH_GUIDE)
    if not out or out.get("_error"):
        log(f"  ! 已中标项目中文名没译成:{(out or {}).get('_error') or '模型未返回'}")
        return 0
    ups = []
    for it in out.get("items") or []:
        try:
            i, zh = int(it.get("i")), str(it.get("zh") or "").strip()
        except (TypeError, ValueError, AttributeError):
            continue
        if 0 <= i < len(rows) and zh:
            v = {"name_zh": zh[:200]}
            qty = str(it.get("qty") or "").strip()
            if qty and b.has(OID, "quantity") and not b.get(OID, rows[i], "quantity"):
                v["quantity"] = qty[:200]
            ups.append((rows[i]["id"], v))
    b.update_many(OID, ups)
    return len(ups)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m core.jobs.awards", description="A28 导入已中标项目(只新建、只填空)")
    ap.add_argument("--file", type=Path, default=DATA, help=f"数据文件(缺省 {DATA})")
    ap.add_argument("--dry-run", action="store_true", help="只算要建、要补多少,不写")
    a = ap.parse_args(argv)
    b = tstore.Base(tstore.Client(*tstore.credentials(admin=not os.environ.get("TEABLE_TOKEN"))))  # 机器账号的令牌;只有管理员令牌时用它
    print(run(b, a.file, a.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
