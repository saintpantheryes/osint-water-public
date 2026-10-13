"""取摩洛哥公共采购网(marchespublics.gov.ma)的「结果公告」(résultats définitifs),挑出水务合同,写成 A28 的数据文件(core/jobs/awards.py 的 COLS)。

世界银行的数据只有它自己贷款里按项目管理的合同;摩洛哥的水务投资大多是国家电力与饮用水办公室(ONEE)、各大区多功能服务公司(SRM)、
流域机构与农业部门自己招标,结果公告都登在这个门户上(近三年约 4 万条,水务约一成多)。门户只给公告清单,
中标单位与金额在每条公告的附件里(多为扫描件),所以分四步,每步的结果存进缓存目录,断了再跑接着做:

  list  全部结果公告的清单(每页 500 条)→ 缓存/notices.jsonl
  text  标题过水务词的(classify)逐条打开详情页,下载结果公告附件:有文字层的取文字;扫描件先转正再文字识别(tesseract),
        另存每页的小图(人核对用)→ 缓存/text/<编号>.json、缓存/img/
  read  从正文读出中标单位与金额:用 profile 里配置的模型(watch.llm),按 READ_SYSTEM 只读原文写着的,读不准的不给;
        也可以用别的读法(如看小图)写进 缓存/read/<编号>.json,格式同 read_one 的返回
  out   读出的结果 → 数据文件(缺省 private/data/awards_mar.csv.gz):金额 ≥ --min-mad(缺省 100 万迪拉姆,约 10 万美元)、
        单位名称读得出的才收;同一份结果被几条公告共用(一张表登了几个标)时按单位与金额只收一次

python scripts/fetch_ma_awards.py list --cache 目录
python scripts/fetch_ma_awards.py text --cache 目录 [--workers 8]
python scripts/fetch_ma_awards.py read --cache 目录 --profile private/profile.yaml
python scripts/fetch_ma_awards.py out  --cache 目录 [--out private/data/awards_mar.csv.gz] [--min-mad 1000000]
只做公开检索与下载,不登录。
"""
from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.jobs import awards  # noqa: E402

BASE = "https://www.marchespublics.gov.ma/index.php"
LIST_URL = BASE + "?page=entreprise.EntrepriseAdvancedSearch&AvisAttribution&domaineActivite="  # 参数为空时直接出全部结果公告
DETAIL_URL = BASE + "?page=entreprise.EntrepriseDetailConsultation&refConsultation={ref}&orgAcronyme={org}"
_DL = re.compile(r"EntrepriseDownloadAvisJAL&refConsultation=(\d+)&orgAcronyme=(\w+)&idAvis=(\d+)")
_R = "ctl0$CONTENU_PAGE$resultSearch$"
PAGE_SIZE = 500
OUT = awards.DATA.with_name("awards_mar.csv.gz")
MIN_MAD = 1_000_000
SOURCE = "摩洛哥公共采购网结果公告"

# ---------- 哪些公告是水务合同:按标题里的法文词,先中先得;NOT 里的字样一律不收 ----------
WORDS: list[tuple[str, str]] = [
    ("海水淡化", r"dessalement|desalination"),
    ("污水", r"assainissement liquide|eaux us[ée]es|[ée]puration|\bSTEP\b|r[ée]seaux? d.assainissement|collecteurs? d.assainissement|"
            r"branchements? d.assainissement|regards? d.assainissement|conduites? d.assainissement|hydrocur|[ée]missaire|boues"),
    ("雨水排涝", r"pluvia|lutte contre les inondations"),
    ("水利", r"irrigu|irrigation|hydro-? ?agricole|\bPMH\b|s[ée]guias?|khettaras?|\bcanal\b|\bcanaux\b"),
    ("供水", r"eau potable|\bAEP\b|adduct|forages? (?:d.exploitation|de reconnaissance|d.eau|profonds?)|r[ée]alisation (?:de|des) forages?|"
            r"compteurs? d.eau|points? d.eau|ch[âa]teaux? d.eau|r[ée]servoirs?|stations? de (?:pompage|traitement|reprise)|"
            r"conduites? d.eau|potabilisation|traitement (?:de l.|des )eau"),
    ("水利", r"barrages?|\bseuils?\b|digues?|crues|inondations|hydrolog|recharge (?:artificielle )?de la nappe|nappes? phr[ée]atique"),
]
_WORDS = [(s, re.compile(p, re.I)) for s, p in WORDS]
NOT = re.compile(r"reboisement|r[ée]g[ée]n[ée]ration|arganier|foresti|\bvoie\b|\bpistes?\b|chauss[ée]e|[ée]clairage|t[ée]l[ée]phon|lignes? (?:HTA|BT|MT)\b|"
                 r"\bHTA\b|branchements? [ée]lectrique|coupures?|factur|h[ôo]pita|\bCHU\b|assainissement (?:foncier|juridique|de la situation)|"
                 r"miniers?|hydrocarbures|hydroponi|toilettes|gardiennage|nettoyage des locaux|assurance|informatique|gr[êe]le|\bR[NPR] ?\d|\bPK ?\d|"
                 r"mosqu[ée]e|lotissement|\bport\b|portuaire|protection civile|berces?\b|bateaux|embarcations", re.I)


def classify(title: str | None) -> str | None:
    """公告标题 → 类别(线索领域);不是水务合同返回 None。道路、学校、电力之类的字样(awards.NOT_WATER 与 NOT)一律不收。"""
    t = str(title or "")
    if awards.NOT_WATER.search(t) or NOT.search(t):
        return None
    return next((s for s, rx in _WORDS if rx.search(t)), None)


# ---------- 金额(迪拉姆的写法:2.086.236,00 / 520 518,00 / 1 772 820 DH)----------
_AMT = re.compile(r"(?<![\d.,/])(\d{1,3}(?:[ .  ]\d{3}){1,3}|\d{5,10})\s?[,.]\s?(\d{2})(?![\d/])|"
                  r"(?<![\d.,/])(\d{1,3}(?:[ .  ]\d{3}){1,3})\s*(?=(?:DH|Dhs|MAD|dirhams))", re.I)
_NOT_PRICE = re.compile(r"estimati|estimation|caution|cautionnement|capital|plafond|budget|prix d.acquisition", re.I)
NO_AWARD = re.compile(r"infructu|annul|sans suite", re.I)


def amounts(text: str) -> list[float]:
    """正文里像中标金额的数(1 万到 50 亿);预估、保证金、注册资本旁边的不算。"""
    out = []
    text = re.sub(r"(?<![A-Za-z\d|])[Il](?=[ .]\d{3}[ .,])", "1", text or "")  # 文字识别常把打头的 1 认成 I、l
    for m in _AMT.finditer(text):
        if _NOT_PRICE.search(text[max(0, m.start() - 60):m.start()]):
            continue
        raw, dec = (m.group(3), "00") if m.group(3) else (m.group(1), m.group(2))
        v = float(re.sub(r"\D", "", raw) + "." + dec)
        if 10_000 <= v <= 5e9:
            out.append(v)
    return out


def worth_reading(text: str, min_mad: float = MIN_MAD) -> bool:
    """值得交给 read 的:有正文;不是只写了流标、作废;读得出的金额里至少一笔够门槛(一笔都读不出的也交)。"""
    if len(re.sub(r"\s", "", text or "")) < 80:
        return False
    ams = amounts(text)
    if not ams:
        return not NO_AWARD.search(text)
    return max(ams) >= min_mad


# ---------- 写进数据文件的几个列 ----------
_CONSULT = re.compile(r"[ée]tudes?|assistance technique|suivi|contr[ôo]le|ma[îi]trise d.[œo]euvre|conseil|audit|expertise|diagnostic|"
                      r"sch[ée]ma directeur|lev[ée]s|topograph|g[ée]otechni|elaboration|[ée]laboration", re.I)
_CATEGORY = {"Travaux": "Works", "Fournitures": "Goods"}


def contract_type(category: str | None, title: str | None) -> str:
    """门户的类别(工程、供货、服务)+ 标题 → 合同类型;服务里写明研究、技术协助、监理、勘测的算咨询服务,运营维护的算运营。"""
    cat = str(category or "").strip()
    wb = _CATEGORY.get(cat) or ("Consultant Services" if cat == "Services" and _CONSULT.search(str(title or "")) else
                                "Non-consulting Services" if cat == "Services" else "")
    return awards.contract_type(wb, title) if wb else ""


METHOD_ZH = {"Appel d'offres ouvert": "公开招标", "Appel d'offres ouvert simplifié": "简化公开招标", "Appel d'offres restreint": "限制性招标",
             "Concours": "设计竞赛", "Procédure négociée": "议标", "Consultation architecturale": "建筑设计竞选"}


def method_zh(proc: str | None) -> str:
    p = " ".join(str(proc or "").split())
    for k, v in METHOD_ZH.items():
        if p.lower().startswith(k.lower()):
            return f"{v}({p})"
    return p


FUNDERS = [("世界银行", r"banque mondiale|\bBIRD\b|world bank"), ("非洲开发银行", r"banque africaine de d[ée]veloppement|\bBAD\b|\bAfDB\b"),
           ("欧洲投资银行", r"banque europ[ée]enne d.investissement|\bBEI\b"), ("德国复兴信贷银行", r"\bKfW\b"), ("法国开发署", r"\bAFD\b|agence fran[çc]aise de d[ée]veloppement"),
           ("日本国际协力机构", r"\bJICA\b"), ("阿拉伯经济社会发展基金", r"\bFADES\b|fonds arabe"), ("伊斯兰开发银行", r"banque islamique|\bBID\b"),
           ("欧盟", r"union europ[ée]enne"), ("沙特发展基金", r"fonds saoudien"), ("科威特基金", r"fonds koweitien")]
_FUNDERS = [(z, re.compile(p, re.I)) for z, p in FUNDERS]


def funders_of(text: str | None) -> str:
    """附件里写明的外部出资方(「financé par la KfW」之类);没写的留空(多数是业主自己的预算)。"""
    return "、".join(z for z, rx in _FUNDERS if rx.search(text or ""))


_MA_FORM = re.compile(r"\bS\.?A\.?R\.?L\b|\bSARL\s?AU\b", re.I)


def firm_country(name: str) -> tuple[str, str]:
    """供应商注册国:名称看得出的才写(中国企业的名称、摩洛哥常见的有限责任公司 SARL);别的留空,人补。"""
    if awards.CHINESE_FIRM.search(name or ""):
        return "中国", "CHN"
    if _MA_FORM.search(name or ""):
        return "摩洛哥", "MAR"
    return "", ""


def _day(s: str | None) -> str:
    try:
        return datetime.strptime(str(s or "").strip(), "%d/%m/%Y").date().isoformat()
    except ValueError:
        return ""


def rows_from(notices: list[dict], reads: dict[str, dict], texts: dict[str, str] | None = None, min_mad: float = MIN_MAD,
              today: date | None = None) -> list[dict]:
    """清单 + 读出的结果 → 数据文件的行(一个中标的标段一行)。"""
    texts = texts or {}
    out, seen = [], set()
    stamp = f"{SOURCE}({(today or date.today()).isoformat()} 取)"
    for n in notices:
        r = reads.get(str(n.get("ref_cons")))
        sector = classify(n.get("title"))
        if not r or r.get("status") != "awarded" or not sector:
            continue
        lots = [x for x in r.get("lots") or [] if str(x.get("winner") or "").strip() and _num(x.get("amount"))]
        for i, lot in enumerate(lots):
            names = [" ".join(w.split()) for w in re.split(r"\s+/\s+", str(lot["winner"])) if w.strip()]
            price, cur = _num(lot["amount"]), str(lot.get("currency") or "MAD").strip().upper() or "MAD"
            if cur == "MAD" and price < min_mad:
                continue
            dup = (str(n.get("buyer") or "").strip().upper(), awards.contractor_key(names[0]), round(price))
            if dup in seen:  # 一张结果表登了几个标,挂在几条公告上:只收一次
                continue
            seen.add(dup)
            label = str(lot.get("lot") or "").strip()
            many = len(lots) > 1
            name = " ".join(str(n.get("title") or "").split()) + (f"(Lot {label})" if many and label and label.lower() != "unique" else "")
            firms = []
            for w in names:
                zh, iso = firm_country(w)
                firms.append({"name": w, "iso3": iso, "country_zh": zh, "ext": ""})
            out.append({"key": f"MA-{n['ref_cons']}" + (f"-{i + 1}" if many else ""), "iso3": "MAR", "name": name, "sector": sector,
                        "contract_type": contract_type(n.get("category"), n.get("title")), "price": f"{price:.2f}", "currency": cur,
                        "awarded": _day(n.get("published")), "quantity": awards.quantity(n.get("title")),
                        "winner": "、".join(names), "winner_country": "、".join(dict.fromkeys(f["country_zh"] for f in firms if f["country_zh"])),
                        "project": "", "funder": funders_of(texts.get(str(n["ref_cons"]))), "method": method_zh(n.get("procedure")),
                        "buyer": " ".join(str(n.get("buyer") or "").split()), "place": str(n.get("place") or "").strip(),
                        "source": stamp, "url": DETAIL_URL.format(ref=n["ref_cons"], org=n["org"]), "winners": json.dumps(firms, ensure_ascii=False)})
    out.sort(key=lambda x: x["awarded"])
    return out


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# ---------- read:从正文读中标结果(模型)----------
READ_SYSTEM = ("你读摩洛哥公共采购的「结果公告」(résultat définitif)正文,正文多是扫描件的文字识别结果,表格的列会断开、夹杂乱码。"
               "列出真正授出的标段:中标单位(concurrent / soumissionnaire retenu、attributaire、titulaire)与中标金额(montant de l'offre retenue)。"
               "流标、作废(infructueux、annulé、sans suite、néant)的标段不列;都流标了 status 写 no_award。"
               "单位名称照原文抄,只改明显的识别错(空格、个别字母);联合体各家用「 / 」隔开。金额是摩洛哥写法(空格或点分千位、逗号分分):"
               "2.086.236,00 → 2086236.00;用中标金额,不要预估金额、保证金、最低额;框架合同有最低、最高时取最高。"
               "名称或金额读不准的标段不列,status 写 unclear(别的标段读得准时仍写 awarded)。不要猜。")
READ_GUIDE = ('\n输出格式:只输出一个 JSON 对象 {"status": "awarded|no_award|unclear", "lots": [{"lot": "标段号或 unique", "winner": "单位名称", '
              '"amount": 数字, "tax": "TTC|HT|", "currency": "MAD"}]},不要任何其他文字。\n')
READ_SCHEMA = {"type": "object", "properties": {
    "status": {"type": "string", "enum": ["awarded", "no_award", "unclear"]},
    "lots": {"type": "array", "items": {"type": "object", "properties": {
        "lot": {"type": "string"}, "winner": {"type": "string"}, "amount": {"type": "number"}, "tax": {"type": "string"}, "currency": {"type": "string"}},
        "required": ["lot", "winner", "amount", "tax", "currency"], "additionalProperties": False}}},
    "required": ["status", "lots"], "additionalProperties": False}


def read_one(clf, notice: dict, text: str) -> dict | None:
    """一条公告的正文 → {status, lots, by};模型没给、出错时 None。金额要在正文里找得到(防止模型编数)。"""
    user = f"公告标题:{notice.get('title')}\n采购方:{notice.get('buyer')}\n正文:\n{text[:6000]}"
    out = clf.ask(READ_SYSTEM, user, READ_SCHEMA, READ_GUIDE)
    if not out or out.get("_error") or out.get("status") not in ("awarded", "no_award", "unclear"):
        return None
    have = {round(v) for v in amounts(text)}
    lots = [x for x in out.get("lots") or [] if isinstance(x, dict) and round(_num(x.get("amount"))) in have]
    status = out["status"] if len(lots) == len(out.get("lots") or []) else ("awarded" if lots else "unclear")
    return {"status": status, "lots": lots, "by": out.get("_model") or getattr(clf, "label", "模型")}


# ---------- list / text:门户上取 ----------
def _session():
    from core.watch import fetch
    return fetch.session(timeout=90)


def list_notices(cache: Path, log=print) -> int:
    """全部结果公告的清单(新的在前)→ 缓存/notices.jsonl。"""
    from core.watch import collectors as C
    from core.watch.text import soup

    def rows(page) -> list[dict]:
        res = []
        for a in soup(page.content).find_all("a", href=C._ATEXO_DETAIL):
            tr = a.find_parent("tr")
            tds = tr.find_all("td", recursive=False) if tr else []
            if len(tds) < 5:
                continue
            ref, org = C._ATEXO_DETAIL.search(a["href"]).groups()
            head, body, place = tds[1], tds[2], tds[3]
            m = C._DMY.search(head.get_text(" "))
            res.append({"ref_cons": ref, "org": org,
                        "title": C._text(body.select_one("[id$=infosBullesObjet]")) or C._after_label(body.select_one("[id$=panelBlocObjet]")),
                        "reference": C._text(body.select_one("[id$=_reference]")), "buyer": C._after_label(body.select_one("[id$=panelBlocDenomination]")),
                        "procedure": C._text(head.select_one("[id$=type_procedure]")), "category": C._text(head.select_one("[id$=panelBlocCategorie]")),
                        "place": C._text(place.select_one("[id$=infosLieuExecution]")), "published": m.group(0) if m else None})
        return res

    cache.mkdir(parents=True, exist_ok=True)
    out = cache / "notices.jsonl"
    n_all = 0
    with _session() as ses, open(out, "w", encoding="utf-8") as fo:
        page = ses.get(LIST_URL)
        action, data = C._form(page)
        data.update({_R + "listePageSizeTop": str(PAGE_SIZE), "PRADO_POSTBACK_TARGET": _R + "listePageSizeTop"})
        page = ses.post(action, data)
        total = C._atexo_count(page) or 0
        for p in range(1, (total + PAGE_SIZE - 1) // PAGE_SIZE + 1):
            if p > 1:
                for k in range(4):
                    try:
                        action, data = C._form(page, _R + "DefaultButtonTop")
                        data.update({_R + "numPageTop": str(p), _R + "listePageSizeTop": str(PAGE_SIZE)})
                        page = ses.post(action, data)
                        break
                    except Exception:  # noqa: BLE001  门户偶尔断连:等一会儿重试
                        if k == 3:
                            raise
                        time.sleep(5 * (k + 1))
            rs = rows(page)
            for r in rs:
                fo.write(json.dumps(r, ensure_ascii=False) + "\n")
            n_all += len(rs)
            log(f"  第 {p} 页 {len(rs)} 条(到 {rs[-1]['published'] if rs else '—'})")
            if not rs:
                break
    log(f"结果公告 {n_all} 条(门户显示 {total} 条)→ {out}")
    return n_all


def _ocr_page(img, save: Path | None) -> str:
    """一页扫描件:先认方向转正(tesseract 的方向检测,认不出就不转),存小图,再识别法文。"""
    buf = io.BytesIO()
    img.save(buf, "PNG")
    try:
        osd = subprocess.run(["tesseract", "stdin", "stdout", "--psm", "0"], input=buf.getvalue(), capture_output=True, timeout=60).stdout.decode()
        m, c = re.search(r"Rotate: (\d+)", osd), re.search(r"Orientation confidence: ([\d.]+)", osd)
        if m and c and float(c.group(1)) >= 1.0 and int(m.group(1)):
            img = img.rotate(-int(m.group(1)), expand=True)
            buf = io.BytesIO()
            img.save(buf, "PNG")
    except (OSError, subprocess.SubprocessError):
        pass
    if save:
        small = img.copy()
        small.thumbnail((1400, 1400))
        small.save(save, optimize=True)
    try:
        r = subprocess.run(["tesseract", "stdin", "stdout", "-l", "fra", "--psm", "3"], input=buf.getvalue(), capture_output=True, timeout=120)
        return r.stdout.decode("utf-8", "ignore")
    except subprocess.TimeoutExpired:
        return ""


def attachment_text(data: bytes, tag: str, img_dir: Path, pages: int = 2) -> tuple[str, str]:
    """附件 → (读法, 正文):生成的 PDF 取文字层;扫描件(页上有大图)逐页识别。前 pages 页。"""
    import pdfplumber
    from PIL import Image

    if data[:5] == b"%PDF-":
        with pdfplumber.open(io.BytesIO(data)) as p:
            pgs = p.pages[:pages]
            scanned = any(max([im.get("srcsize", (0, 0))[0] for im in pg.images] or [0]) >= 1200 for pg in pgs)
            text = "\n".join(pg.extract_text(layout=True) or "" for pg in pgs)
            if len(re.sub(r"\s", "", text)) > 150 and not scanned:
                for k, pg in enumerate(pgs):
                    pg.to_image(resolution=100).original.convert("L").save(img_dir / f"{tag}-{k + 1}.png", optimize=True)
                return "pdf-text", text
            return "pdf-ocr", "\n\f".join(_ocr_page(pg.to_image(resolution=300).original.convert("L"), img_dir / f"{tag}-{k + 1}.png")
                                         for k, pg in enumerate(pgs))
    try:
        img = Image.open(io.BytesIO(data)).convert("L")
    except Exception:  # noqa: BLE001  Word、压缩包等:不读
        return "other", ""
    img.thumbnail((3600, 3600))
    return "img-ocr", _ocr_page(img, img_dir / f"{tag}-1.png")


def fetch_texts(cache: Path, workers: int = 8, log=print) -> int:
    """水务候选公告的附件正文 → 缓存/text/<编号>.json(已有的跳过)。"""
    from core.watch.text import soup

    if not shutil.which("tesseract"):
        raise SystemExit("要先装 tesseract 和法文语言包(tesseract-ocr-fra)")
    tdir, idir = cache / "text", cache / "img"
    tdir.mkdir(parents=True, exist_ok=True)
    idir.mkdir(parents=True, exist_ok=True)
    seen, todo = set(), []
    for ln in open(cache / "notices.jsonl", encoding="utf-8"):
        n = json.loads(ln)
        if n["ref_cons"] not in seen and classify(n.get("title")) and not (tdir / f"{n['ref_cons']}.json").exists():
            seen.add(n["ref_cons"])
            todo.append(n)
    log(f"要读附件的水务公告 {len(todo)} 条")

    def one(n: dict) -> str:
        err = ""
        for k in range(3):
            try:
                with _session() as ses:
                    s = soup(ses.get(DETAIL_URL.format(ref=n["ref_cons"], org=n["org"])).content)
                    files = []
                    for h in list(dict.fromkeys(a["href"] for a in s.find_all("a", href=_DL)))[:3]:
                        fid = _DL.search(h).group(3)
                        kind, text = attachment_text(ses.get(BASE + "?" + h.split("?", 1)[1]).content, f"{n['ref_cons']}_{fid}", idir)
                        files.append({"id": fid, "kind": kind, "text": text})
                (tdir / f"{n['ref_cons']}.json").write_text(json.dumps({**n, "files": files}, ensure_ascii=False), encoding="utf-8")
                return "ok"
            except Exception as e:  # noqa: BLE001
                err = f"{type(e).__name__}: {e}"[:200]
                time.sleep(5 * (k + 1))
        return err

    done = 0
    with ThreadPoolExecutor(workers) as ex:
        for i, res in enumerate(ex.map(one, todo), 1):
            done += res == "ok"
            if res != "ok" or i % 50 == 0:
                log(f"  {i}/{len(todo)} {res}")
    return done


def load_texts(cache: Path) -> dict[str, str]:
    out = {}
    for f in (cache / "text").glob("*.json"):
        d = json.loads(f.read_text(encoding="utf-8"))
        out[str(d["ref_cons"])] = "\n".join(x.get("text") or "" for x in d.get("files") or [])
    return out


def load_notices(cache: Path) -> list[dict]:
    seen, out = set(), []
    for ln in open(cache / "notices.jsonl", encoding="utf-8"):
        n = json.loads(ln)
        if n["ref_cons"] not in seen:
            seen.add(n["ref_cons"])
            out.append(n)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="摩洛哥公共采购网结果公告 → 已中标项目数据文件")
    ap.add_argument("step", choices=["list", "text", "read", "out"])
    ap.add_argument("--cache", type=Path, required=True, help="缓存目录(清单、正文、小图、读出的结果)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--profile", help="read 用:profile.yaml(读其中 watch.llm 的模型配置)")
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--min-mad", type=float, default=MIN_MAD)
    a = ap.parse_args(argv)
    if a.step == "list":
        list_notices(a.cache)
    elif a.step == "text":
        print(f"读了 {fetch_texts(a.cache, a.workers)} 条")
    elif a.step == "read":
        from core.profile import load_profile
        from core.watch.llm import Classifier, LLMConfig

        cfg = (load_profile(a.profile).watch or {}).get("llm") if a.profile else None
        clf = Classifier(LLMConfig.from_profile(cfg, max_calls=10 ** 6, thinking=False))
        if not clf.enabled:
            raise SystemExit(clf.note or "没有可用的模型")
        rdir = a.cache / "read"
        rdir.mkdir(exist_ok=True)
        texts, n_done = load_texts(a.cache), 0
        for n in load_notices(a.cache):
            ref = str(n["ref_cons"])
            if ref in texts and not (rdir / f"{ref}.json").exists() and worth_reading(texts[ref], a.min_mad):
                r = read_one(clf, n, texts[ref])
                if r:
                    (rdir / f"{ref}.json").write_text(json.dumps(r, ensure_ascii=False), encoding="utf-8")
                    n_done += 1
        print(f"读出 {n_done} 条")
    else:
        reads = {f.stem: json.loads(f.read_text(encoding="utf-8")) for f in (a.cache / "read").glob("*.json")}
        rows = rows_from(load_notices(a.cache), reads, load_texts(a.cache), a.min_mad)
        awards.write_rows(a.out, rows)
        print(f"读出结果 {len(reads)} 条 → 中标 {len(rows)} 份(≥ {a.min_mad:,.0f} 迪拉姆),写到 {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
