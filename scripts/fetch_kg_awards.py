"""取吉尔吉斯斯坦国家采购门户(zakupki.gov.kg)公开的「中标单位」表,挑出水务合同,写成 A28 的数据文件(core/jobs/awards.py 的 COLS)。

门户的中标单位表(winners.xhtml)公开、不用登录,2015 年起约 77 万行,每行:公告编号、采购名称、中标单位、各标段名称与
计划金额、报价、合同金额(索姆)、合同号、签约日期。表不能按关键词筛,只能整表翻页(每页 1000 行,三路并行约 3 小时);
业主不在表里,在每条公告的详情页(view.xhtml?id=公告编号后 9 位)。分三步,每步的结果存进缓存目录,断了再跑接着做:

  crawl   翻整张表 → 缓存/winners/p<起始行>.jsonl(已有的页跳过)
  buyers  水务、够门槛的那几百条公告逐条打开详情页,取业主 → 缓存/buyers.jsonl
  out     → 数据文件(缺省 private/data/awards_kgz.csv.gz)

挑法:采购名称与标段名称有俄文或吉尔吉斯文的水务词(WORDS:供水、污水、灌溉渠、防洪堤、泥石流……),没有学校、道路、
电力、药品之类字样(NOT);合同金额折合 ≥ --min-usd(缺省 10 万美元,约 870 万索姆);合同金额不到计划金额 5% 或超过 5 倍的
(多半是单价或外币)不收;同一公告、同一中标单位、同一合同号的只收一次。

python scripts/fetch_kg_awards.py crawl  --cache 目录 [--workers 3]
python scripts/fetch_kg_awards.py buyers --cache 目录
python scripts/fetch_kg_awards.py out    --cache 目录 [--out private/data/awards_kgz.csv.gz] [--min-usd 100000]
只做公开检索,不登录;同时最多 3 个请求,每路每页之间停 1~2 秒。
"""
from __future__ import annotations

import argparse
import html
import json
import random
import re
import sys
import threading
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.jobs import awards  # noqa: E402

BASE = "https://zakupki.gov.kg/popp/view/order/"
WINNERS = BASE + "winners.xhtml"
VIEW = BASE + "view.xhtml?id={}"
OUT = awards.DATA.with_name("awards_kgz.csv.gz")
SOURCE = "吉尔吉斯斯坦国家采购门户中标单位表"
ROWS = 1000
MAX_CONCURRENT = 3
_SEM = threading.BoundedSemaphore(MAX_CONCURRENT)

# 俄文、吉尔吉斯文的水务词,先中先得;「арык」只认小写(村名「Жаңы-Арык」不算),「селе」要带词尾(泥石流,不是「в селе」村里)
WORDS: list[tuple[str, str]] = [
    ("海水淡化", r"опреснен"),
    ("污水", r"канализ|сточн\w* вод|очистн\w* сооружен|водоотведен|\bКОС\b|фекальн|ассенизац|септик"),
    ("雨水排涝", r"ливнев|дренаж"),
    ("供水", r"водопровод|водоснабж|питьев\w* вод|водозабор|скважин|резервуар\w* (?:чист|питьев|вод)|насосн\w* станц|водоразбор|водомер|"
            r"при?бор\w* учета воды|водовод|ичүүчү суу|таза суу|суу түтүг|суу менен камсыз"),
    ("水利", r"ирригац|орошен|(?-i:арык)|\bканал(?:а|ов|ы|е|ом|у|ам|ами)?\b|магистральн\w* канал|дамб|берегоукреп|берегозащит|"
            r"селе(?:в|защит|пропуск|отвод|хранил)|\bсель\b|паводк|водохранилищ|гидротехн|мелиорац|\bГТС\b|головн\w* сооружен|"
            r"водовыпуск|гидропост|\bрусл(?:о|а|е)\b|сугат|сугаруу"),
]
_WORDS = [(s, re.compile(p, re.I)) for s, p in WORDS]
NOT = re.compile(r"школ|мектеп|мектеб|больниц|оорукан|\bФАП\b|детск\w* (?:сад|площад|игр)|дорог|асфальт|улиц|тротуар|\bмост|сквер|парк\b|"
                 r"канцеляр|\bГСМ\b|топлив|бензин|компьютер|автомоб|автотранспорт|мебел|продукт|питани|медикамент|лекарств|одежд|спорт|"
                 r"электроснабж|электрич|\bЛЭП\b|освещен|газоснаб|газопровод|МРТ|аппарат\w* (?:МРТ|рентген)|котельн|отоплен|"
                 r"здани|отделени|корпус|общежит|кабинет|жарыктандыр|көчө|кочо|"
                 r"передач\w* данных|интернет|телеви|телепрограмм|телеканал|радио|канал\w* связи|видеонаблюд|программн\w* обеспеч", re.I)  # 「канал」也是电视、数据通道
_FORMS = [(r"Общество с ограниченной ответственностью", "ОсОО"), (r"Открытое акционерное общество", "ОАО"),
          (r"Закрытое акционерное общество", "ЗАО"), (r"Индивидуальный предприниматель", "ИП")]
_KG_FORM = re.compile(r"\bОсОО\b|\bОАО\b|\bЗАО\b|\bИП\b|\bГП\b|\bМП\b|\bОсОО\b|Общество с ограниченной ответственностью|"
                      r"Открытое акционерное общество|Закрытое акционерное общество|Индивидуальный предприниматель", re.I)


def classify(text: str | None) -> str | None:
    """采购名称(加各标段名称)→ 类别;不是水务合同返回 None。"""
    t = str(text or "")
    if NOT.search(t):
        return None
    return next((s for s, rx in _WORDS if rx.search(t)), None)


def firm_country(name: str) -> tuple[str, str]:
    """中标单位注册国:名称看得出的才写(中国企业;吉尔吉斯斯坦的 ОсОО、ОАО、ИП 等);别的留空。"""
    if awards.CHINESE_FIRM.search(name or "") or re.search(r"Китай|\bКНР\b", name or ""):
        return "中国", "CHN"
    if _KG_FORM.search(name or ""):
        return "吉尔吉斯斯坦", "KGZ"
    return "", ""


# ---------- crawl:翻整张表 ----------
def num(s: str | None) -> float | None:
    """「1,234,567.5」或「1 234 567,5」→ 数。"""
    s = str(s or "").strip().replace(" ", " ").replace(" ", " ")
    if re.fullmatch(r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\d+(?:\.\d+)?", s):
        return float(s.replace(",", ""))
    if re.fullmatch(r"-?\d{1,3}(?:\s\d{3})+(?:,\d+)?|-?\d+,\d+", s):
        return float(re.sub(r"\s", "", s).replace(",", "."))
    return None


def _iso(s: str | None) -> str | None:
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(str(s or "").strip(), fmt).date().isoformat()
        except ValueError:
            pass
    return None


def _text(s: str) -> str:
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s))).strip()


def _multi(td: str) -> list[str]:
    """各标段的值以 <br /><br /> 隔开;空的也留位,标段才对得齐。"""
    td = re.sub(r"<span>.*?</span>", "", td, count=1, flags=re.S)  # 手机版的列名
    parts = [_text(x) for x in re.split(r"<br\s*/?>\s*<br\s*/?>", td)]
    while parts and parts[-1] == "":
        parts.pop()
    return parts


def parse_rows(xml: str) -> list[dict]:
    """表格的一页(局部刷新的响应)→ 行。"""
    out = []
    for tr in re.split(r'(?=<tr[^>]*data-ri=")', xml)[1:]:
        ri = int(re.search(r'data-ri="(\d+)"', tr).group(1))
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if len(tds) < 10:
            continue
        one = [_text(re.sub(r"<span>.*?</span>", "", td, count=1, flags=re.S)) for td in tds]
        lots, pl, of, cp = (_multi(tds[i]) for i in (4, 5, 6, 7))
        no = one[1]
        rec = {"offset": ri, "notice_no": no, "purchase_name": one[2], "winner": one[3], "contract_no": one[8],
               "signed_date": _iso(one[9]), "lots": []}
        if len(lots) == len(pl) == len(of) == len(cp):
            for a, b, c, d in zip(lots, pl, of, cp):
                m = re.match(r"(L\d+(?:-\d+)?)\s*:\s*(.*)", a, re.S)
                rec["lots"].append({"lot_no": m.group(1) if m else None, "lot_name": m.group(2) if m else a,
                                    "planned": num(b), "offered": num(c), "contract": num(d)})
        for k, raw in (("planned_total", pl), ("contract_total", cp)):
            vals = [v for v in (num(x) for x in raw) if v is not None]
            rec[k] = sum(vals) if vals else None
        out.append(rec)
    return out


class _Client:
    """一个 cookie、一个 JSF 视图状态(ViewState)。"""

    def __init__(self):
        import httpx

        from core.storage import USER_AGENT
        self.c = httpx.Client(headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}, timeout=90, follow_redirects=True)
        self.vs = self.action = None
        self.total = 0

    def get(self, url: str) -> str:
        with _SEM:
            r = self.c.get(url)
        r.raise_for_status()
        return r.text

    def open(self) -> int:
        h = self.get(WINNERS)
        m = re.search(r'name="javax\.faces\.ViewState"[^>]*value="([^"]+)"', h)
        a = re.search(r'<form id="form"[^>]*action="([^"]+)"', h)
        if not m or not a:
            raise RuntimeError("中标单位表页面没有表单(页面改版了?)")
        self.vs, self.action = m.group(1), urljoin(WINNERS, html.unescape(a.group(1)))
        t = re.search(r"rowCount:(\d+)", h)
        self.total = int(t.group(1)) if t else 0
        return self.total

    def page(self, first: int, rows: int) -> list[dict]:
        """[first, first+rows) 这一段;first 必须是 rows 的整数倍,否则服务器给的行会错位。"""
        if not self.vs:
            self.open()
        d = {"javax.faces.partial.ajax": "true", "javax.faces.source": "table", "javax.faces.partial.execute": "table",
             "javax.faces.partial.render": "table", "table": "table", "table_pagination": "true", "table_first": str(first),
             "table_rows": str(rows), "table_skipChildren": "true", "table_encodeFeature": "true", "form": "form",
             "javax.faces.ViewState": self.vs}
        with _SEM:
            r = self.c.post(self.action, data=d, headers={"Faces-Request": "partial/ajax", "X-Requested-With": "XMLHttpRequest"})
        x = r.text
        if "<partial-response" not in x or "ViewExpired" in x:
            self.vs = None
            raise RuntimeError("表格翻页没回来")
        m = re.search(r'ViewState:0"><!\[CDATA\[([^\]]+)\]\]>', x)
        if m:
            self.vs = m.group(1)
        return [r for r in parse_rows(x) if first <= r["offset"] < first + rows]


def _page(cli: _Client, first: int, rows: int, log=print) -> list[dict]:
    for k in range(6):
        try:
            recs = cli.page(first, rows)
            have = {r["offset"] for r in recs}
            for g in sorted(set(range(first, min(first + rows, cli.total))) - have)[:50]:  # 大页偶尔少最后一行:单取补上
                recs += [r for r in cli.page(g, 1) if r["offset"] == g]
            return sorted(recs, key=lambda r: r["offset"])
        except Exception as e:  # noqa: BLE001
            log(f"  第 {first} 行起那页第 {k + 1} 次没取到:{e}")
            if k >= 1 and rows >= 250 and rows % 2 == 0 and "timed out" in str(e).lower():
                half = rows // 2
                return _page(cli, first, half, log) + _page(cli, first + half, half, log)
            time.sleep(min(300, 5 * 3 ** k))
            try:
                cli.open()
            except Exception:  # noqa: BLE001
                pass
    raise RuntimeError(f"第 {first} 行起那页取不到")


def crawl(cache: Path, workers: int = 3, log=print) -> int:
    wdir = cache / "winners"
    wdir.mkdir(parents=True, exist_ok=True)
    total = _Client().open()
    starts = [s for s in range(0, total + 5 * ROWS, ROWS) if not (wdir / f"p{s:07d}.jsonl").exists()]
    log(f"表里 {total} 行;还要取 {len(starts)} 页")
    lock, it, done = threading.Lock(), iter(starts), [0]

    def work():
        cli = _Client()
        cli.open()
        while True:
            with lock:
                first = next(it, None)
            if first is None:
                return
            recs = _page(cli, first, ROWS, log)
            if not recs:
                continue
            tmp = wdir / f"p{first:07d}.jsonl.tmp"
            tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs), encoding="utf-8")
            tmp.replace(wdir / f"p{first:07d}.jsonl")
            with lock:
                done[0] += 1
            time.sleep(1 + random.random())

    ts = [threading.Thread(target=work, daemon=True) for _ in range(max(1, min(workers, MAX_CONCURRENT)))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return done[0]


def load_rows(cache: Path) -> list[dict]:
    out = []
    for f in sorted((cache / "winners").glob("p*.jsonl")):
        out += [json.loads(ln) for ln in open(f, encoding="utf-8") if ln.strip()]
    return out


# ---------- buyers:业主 ----------
def _pairs(h: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in re.finditer(r'<span class="label">([^<]*)</span>\s*<span class="text">(.*?)</span>\s*</', h, re.S):
        out.setdefault(_text(m.group(1)), _text(m.group(2)))
    return out


def buyer(cli: _Client, notice_no: str) -> dict:
    h = cli.get(VIEW.format(str(notice_no)[-9:]))
    p = _pairs(h)
    return {"notice_no": notice_no, "matches": str(notice_no) in h,
            "buyer": p.get("The procuring entity") or p.get("Закупающая организация") or "",
            "address": p.get("Actual address") or p.get("Фактический адрес") or ""}


# 城市表里吉尔吉斯斯坦的城市(按 GeoNames 英文名);「Ошской области」「Жалал-Абадской области」是州,不算
CITIES = [("Bishkek", re.compile(r"Бишкек", re.I)), ("Osh", re.compile(r"(?:\bг\.|\bгород\w*|\bшаар\w*)\s*Ош\b(?!ск)", re.I)),
          ("Manas", re.compile(r"(?:\bг\.|\bгород\w*|\bшаар\w*)\s*(?:Манас|Жалал-?\s?Абад\b(?!ск))", re.I))]


def city_of(*texts: str) -> str:
    """采购名称、业主、地址里写明的城市 → 英文名(给 A28 认城市);没写的空。"""
    t = " ".join(x for x in texts if x)
    return next((en for en, rx in CITIES if rx.search(t)), "")


def contract_type(text: str) -> str:
    """采购名称 → 合同类型:设计、监理、咨询算咨询服务,采购、供货算货物,建设、改造、修缮算工程;认不出的留空。"""
    if re.search(r"\bПСД\b|проектн|проектирован|технадзор|технического надзор|авторск\w* надзор|консультац|экспертиз|изыскан", text, re.I):
        return "咨询服务"
    if re.search(r"приобретен|покупк|поставк|закупк|сатып алуу", text, re.I) and not re.search(r"строительств|реконструкц|ремонт", text, re.I):
        return "货物"
    if re.search(r"строительств|реконструкц|ремонт|устройств|прокладк|бурени|восстановлен|расширен|реабилит|ребилит|сооружен|монтаж|модерниз|"
                 r"\bработ|дамб|курулуш|оңдоо", text, re.I):
        return "工程"
    return ""


def candidates(rows: list[dict], min_kgs: float) -> list[dict]:
    """水务、够门槛、金额不离谱的行(同一公告、中标单位、合同号只留一行)。"""
    out, seen = [], set()
    for r in rows:
        text = " ".join([r.get("purchase_name") or ""] + [x.get("lot_name") or "" for x in r.get("lots") or []])
        price, plan = r.get("contract_total") or 0, r.get("planned_total") or 0
        if price < min_kgs or (plan and not 0.05 * plan <= price <= 5 * plan) or not classify(text):
            continue
        k = (r["notice_no"], awards.contractor_key(r.get("winner")), r.get("contract_no"))
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def fetch_buyers(cache: Path, min_kgs: float, log=print) -> int:
    fp = cache / "buyers.jsonl"
    have = {json.loads(ln)["notice_no"] for ln in open(fp, encoding="utf-8")} if fp.exists() else set()
    todo = list(dict.fromkeys(r["notice_no"] for r in candidates(load_rows(cache), min_kgs) if r["notice_no"] not in have))
    log(f"要取业主的公告 {len(todo)} 条")
    cli = _Client()
    with open(fp, "a", encoding="utf-8") as f:
        for n in todo:
            try:
                f.write(json.dumps(buyer(cli, n), ensure_ascii=False) + "\n")
                f.flush()
            except Exception as e:  # noqa: BLE001
                log(f"  {n} 没取到:{e}")
            time.sleep(0.5 + random.random())
    return len(todo)


# ---------- out ----------
def rows_from(rows: list[dict], buyers: dict[str, dict], min_kgs: float, today: date | None = None) -> list[dict]:
    today = today or date.today()
    stamp = f"{SOURCE}({today.isoformat()} 取)"
    out = []
    for r in candidates(rows, min_kgs):
        lots = r.get("lots") or []
        text = " ".join([r["purchase_name"]] + [x.get("lot_name") or "" for x in lots])
        name = r["purchase_name"]
        if len(lots) == 1 and lots[0].get("lot_name") and lots[0]["lot_name"].strip() != name.strip():
            name = f"{name}({lots[0]['lot_name'].strip()})"
        signed = r.get("signed_date") or ""
        if not ("2015-01-01" <= signed <= today.isoformat()):  # 门户里有 1011 年、未来日期这类笔误
            no = r["notice_no"]
            signed = f"20{no[:2]}-{no[2:4]}-{no[4:6]}" if no[:6].isdigit() else ""
        winner = " ".join(str(r.get("winner") or "").replace("”", '"').replace("“", '"').split())
        for long, short in _FORMS:  # 「Общество с ограниченной ответственностью」→「ОсОО」,同一家的几种写法才并得上
            winner = re.sub(long, short, winner, flags=re.I)
        zh, iso = firm_country(winner)
        b = buyers.get(r["notice_no"]) or {}
        tail = re.sub(r"\W+", "", str(r.get("contract_no") or ""))[:20] or awards.contractor_key(winner)[:20]
        out.append({"key": f"KG-{r['notice_no']}-{tail}", "iso3": "KGZ", "name": name, "sector": classify(text),
                    "contract_type": contract_type(text), "price": f"{r['contract_total']:.2f}", "currency": "KGS", "awarded": signed,
                    "quantity": "", "winner": winner, "winner_country": zh, "project": "", "funder": "", "method": "",
                    "buyer": b.get("buyer") or "", "place": city_of(text, b.get("buyer") or "", b.get("address") or ""),
                    "source": stamp, "url": VIEW.format(str(r["notice_no"])[-9:]),
                    "winners": json.dumps([{"name": winner, "iso3": iso, "country_zh": zh, "ext": ""}], ensure_ascii=False)})
    out.sort(key=lambda x: x["awarded"])
    return out


def _min_kgs(min_usd: float) -> float:
    from core.fx import Rates
    usd = Rates().to_usd(1_000_000, "KGS")
    return min_usd / usd * 1_000_000 if usd else min_usd * 87.5


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="吉尔吉斯斯坦国家采购门户中标单位表 → 已中标项目数据文件")
    ap.add_argument("step", choices=["crawl", "buyers", "out"])
    ap.add_argument("--cache", type=Path, required=True, help="缓存目录(表格各页、业主)")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--min-usd", type=float, default=100_000)
    a = ap.parse_args(argv)
    if a.step == "crawl":
        print(f"取了 {crawl(a.cache, a.workers)} 页")
    elif a.step == "buyers":
        print(f"取了 {fetch_buyers(a.cache, _min_kgs(a.min_usd))} 条")
    else:
        fp = a.cache / "buyers.jsonl"
        buyers = {}
        if fp.exists():
            for ln in open(fp, encoding="utf-8"):
                b = json.loads(ln)
                if b.get("matches", b.get("notice_matches", True)):
                    buyers[b["notice_no"]] = {"buyer": b.get("buyer") or "", "address": b.get("address") or b.get("buyer_address") or ""}
        rows = load_rows(a.cache)
        out = rows_from(rows, buyers, _min_kgs(a.min_usd))
        awards.write_rows(a.out, out)
        print(f"中标单位表 {len(rows)} 行 → 水务合同 {len(out)} 份,写到 {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
