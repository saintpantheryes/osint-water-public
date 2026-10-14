"""A37 的数据:按法规清单取原文、转文字(扫描件做文字识别)、按条拆开,写私有配置仓 data/law_clauses.csv.gz。

清单 data/law_sources.csv 一部法规一行:law_key, url, file, language, pages
  url    原文网址(PDF 或网页);从服务器打不开的留空,人在本机下载后放进 file 写的位置
  file   本地原文(相对数据目录,如 law_src/MAR/arrete_1607-06.pdf);有就用它,没有再按 url 下载到这里
  language  法文 / 俄文 / 英文 / 阿塞拜疆文 / 吉尔吉斯文 / 阿拉伯文(文字识别选语言包、认条号用)
  pages  只取这几页(如 1-12;双语公报只要其中一种文字的页),可空
法规本身的信息(名称、文号、类别……)写在 data/laws.csv,两份按 law_key 对上。

拆条:一行以「Article 12 / Art. 12 / Статья 12 / Maddə 12 / 12-берене / المادة 12 / Article premier」开头的起一条,
「ANNEXE / Приложение / Əlavə / Annex」起一条附表,「Tableau / Таблица / Cədvəl / Table」起一条表;「Chapitre / Titre / Глава / Fəsil / Bölmə / Chapter」行记作所在章节。
没有条号的短文件(部令附表、技术规范)整份一条。页码按 PDF 的页(取文字时逐页取)。

用法:python scripts/fetch_laws.py [--dir 私有配置仓/data] [--only LAW-MAR-1607-06]
要 pdftotext / pdftoppm(poppler)与 tesseract(+ fra、rus、eng、aze、kir、ara 语言包;没有的语言包退到 eng)。
"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import re
import shutil
import subprocess
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.jobs.laws import CLAUSE_COLS  # noqa: E402

DATA = ROOT / "private" / "data"
csv.field_size_limit(10_000_000)  # 没拆开的长附表一格可能很大
UA = "Mozilla/5.0 (compatible; osint-water law fetcher)"
LANG_OCR = {"法文": "fra", "俄文": "rus", "英文": "eng", "阿塞拜疆文": "aze", "吉尔吉斯文": "kir+rus", "阿拉伯文": "ara"}
ART = re.compile(r"^\s*(?:"
                 r"(?:ARTICLE|Article|ART\.|Art\.|CLAUSE|Clause)\s+(?P<fr>premier|PREMIER|1er|\d+(?:[.\-–]\d+)*(?:\s?(?:bis|ter|quater))?)"
                 r"|(?:Статья|СТАТЬЯ|Стаття)\s+(?P<ru>\d+(?:[.\-]\d+)*)"
                 r"|(?:Maddə|MADDƏ|Madde)\s+(?P<az>\d+(?:[.\-]\d+)*)"
                 r"|(?P<kg>\d+)-(?:берене|статья)"
                 r"|(?:المادة|الفصل)\s+(?P<ar>[\d٠-٩]+|\S+)"
                 r")\s*[.:\-–—)]?\s*(?P<rest>.*)$")
ANNEX = re.compile(r"^\s*(?P<a>ANNEXE|Annexe|ANNEX|Annex|ПРИЛОЖЕНИЕ|Приложение|Əlavə|ƏLAVƏ|Тиркеме|TABLEAU|Tableau|Таблица|Cədvəl|Table)\b\s*(?:n°\s*|№\s*)?(?P<n>[\w\-.]*)\s*[.:\-–—]?\s*(?P<rest>.*)$")
HEAD = re.compile(r"^\s*(?:CHAPITRE|Chapitre|TITRE|Titre|Section|SECTION|ГЛАВА|Глава|РАЗДЕЛ|Раздел|Fəsil|FƏSİL|Bölmə|BÖLMƏ|Chapter|CHAPTER|Глава|Бөлүм)\b.{0,160}$")
POINT = re.compile(r"^\s*(?P<n>\d{1,3})\.\s+(?P<rest>\S.*)$")  # 没有条号的规则、指南按「1. 2. 3.」编的点
CHUNK = 6000  # 一条太长(附表、整份没拆开的)按段切开,检索和翻译都按这个粒度
LABEL = {"fr": "第 {} 条", "ru": "第 {} 条", "az": "第 {} 条", "kg": "第 {} 条", "ar": "第 {} 条"}


class _Text(HTMLParser):
    """网页 → 正文行(去掉脚本、样式、导航)。"""
    SKIP = {"script", "style", "nav", "header", "footer", "noscript"}

    def __init__(self):
        super().__init__()
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        if tag in ("p", "br", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table"):
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.out.append(re.sub(r"\s+", " ", data))  # 源码里的换行只是排版;分行看块标签


def _pages_arg(spec: str) -> tuple[int, int] | None:
    m = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+))?\s*", spec or "")
    return (int(m.group(1)), int(m.group(2) or m.group(1))) if m else None


def _two_columns(t: str) -> bool:
    """一页里过半的长行中间有一大段空白、两边都有字 → 双栏。表格行也有空白,但少有过半。"""
    lines = [x for x in t.splitlines() if len(x.strip()) > 50]
    gap = [x for x in lines if re.search(r"\S {4,}\S", x.strip()) and re.search(r"\w{3,}.* {4,}.*\w{3,}", x)]
    return len(lines) >= 12 and len(gap) > 0.5 * len(lines)


def pdf_pages(path: Path, lang: str, pages: tuple[int, int] | None = None, log=print) -> list[tuple[int, str]]:
    """PDF → [(页码, 文字)];一页文字层少于 40 个字的用 tesseract 识别(扫描件)。"""
    n = int(re.search(r"Pages:\s+(\d+)", subprocess.run(["pdfinfo", str(path)], capture_output=True, text=True).stdout).group(1))
    lo, hi = pages or (1, n)
    ocr_lang = LANG_OCR.get(lang, "eng")
    have = set(subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True).stdout.split()) if shutil.which("tesseract") else set()
    ocr_lang = "+".join(x for x in ocr_lang.split("+") if x in have) or ("eng" if "eng" in have else "")
    out, ocrd = [], 0
    for p in range(lo, min(hi, n) + 1):
        t = subprocess.run(["pdftotext", "-q", "-layout", "-f", str(p), "-l", str(p), str(path), "-"], capture_output=True, text=True).stdout
        if _two_columns(t):  # 公报常见双栏:按版面取会把左右两栏拼成一行,改按阅读顺序取
            t = subprocess.run(["pdftotext", "-q", "-f", str(p), "-l", str(p), str(path), "-"], capture_output=True, text=True).stdout
        if len(re.sub(r"\s", "", t)) < 40 and ocr_lang:
            with tempfile.TemporaryDirectory() as d:
                subprocess.run(["pdftoppm", "-r", "300", "-gray", "-f", str(p), "-l", str(p), "-png", str(path), f"{d}/pg"], check=True)
                img = next(Path(d).glob("pg*.png"))
                t = subprocess.run(["tesseract", str(img), "-", "-l", ocr_lang, "--psm", "6"], capture_output=True, text=True).stdout
                ocrd += 1
        out.append((p, t))
    if ocrd:
        log(f"    文字识别 {ocrd} 页({ocr_lang})")
    return out


def html_pages(raw: bytes) -> list[tuple[int, str]]:
    m = re.search(rb"charset=[\"']?([\w\-]+)", raw[:4000], re.I)  # 阿塞拜疆官网的是 windows-1251 / 1254
    try:
        text = raw.decode(m.group(1).decode() if m else "utf-8")
    except (LookupError, UnicodeDecodeError):
        text = raw.decode("utf-8", errors="replace")
    p = _Text()
    p.feed(text)
    return [(0, re.sub(r"\n\s*\n+", "\n", "".join(p.out)))]


def _clean(t: str) -> str:
    t = re.sub(r"[ \t ]+", " ", t)
    t = re.sub(r"(\w)-\n(\w)", r"\1\2", t)  # 行末断字
    return "\n".join(x.strip() for x in t.splitlines()).strip()


def split(pages: list[tuple[int, str]], law_key: str) -> list[dict]:
    """[(页码, 文字)] → 条文行(CLAUSE_COLS)。认不出任何条号的整份一条(短的部令、附表)。"""
    items, cur, heading = [], None, ""
    for pg, text in pages:
        for line in text.splitlines():
            s = line.strip()
            if not s or re.fullmatch(r"[-–—\d\s/]{1,8}", s):  # 空行、孤立页码
                if cur is not None and cur["lines"] and cur["lines"][-1] != "":
                    cur["lines"].append("")
                continue
            m, a = ART.match(s), ANNEX.match(s)
            if m and len(s) < 400:
                k = next(g for g in ("fr", "ru", "az", "kg", "ar") if m.group(g))
                num = m.group(k)
                num = "1" if num.lower() in ("premier", "1er") else num
                cur = {"label": LABEL[k].format(num), "heading": heading, "page": str(pg or ""), "lines": [s]}
                items.append(cur)
                continue
            if a and len(s) < 120:
                kind = "表" if a.group("a").lower() in ("tableau", "таблица", "cədvəl", "table") else "附表"
                cur = {"label": (kind + " " + a.group("n").strip(".")).strip(), "heading": heading, "page": str(pg or ""), "lines": [s]}
                items.append(cur)
                continue
            if HEAD.match(s) and (cur is None or len(s) < 120):
                heading = s[:160]
            if cur is None:  # 第一条之前:序言、标题
                cur = {"label": "序言", "heading": "", "page": str(pg or ""), "lines": []}
                items.append(cur)
            cur["lines"].append(s)
    if sum(1 for x in items if x["label"].startswith("第 ")) < 3 and sum(len(" ".join(x["lines"])) for x in items) > CHUNK:
        items = _points(pages) or items
    labels = [x["label"] for x in items]
    items = [x for i, x in enumerate(items) if not (len(" ".join(x["lines"])) < 200 and x["label"] in labels[i + 1:])]  # 卷首目录里的条号行
    items = [part for it in items for part in _chunks(it)]
    rows, seen = [], {}
    for i, it in enumerate(x for x in items if _clean("\n".join(x["lines"]))):
        lab = it["label"]
        seen[lab] = seen.get(lab, 0) + 1
        if seen[lab] > 1 and lab != "序言":
            lab = f"{lab}(第 {seen[lab]} 处)"  # 修订法里同一条号出现几次(被修订条文的引文)
        rows.append({"key": f"{law_key}-{i + 1:04d}", "law_key": law_key, "seq": i + 1, "label": lab, "heading": it["heading"],
                     "text": _clean("\n".join(it["lines"])), "text_zh": "", "page": it["page"]})
    if len(rows) == 1:
        rows[0]["label"] = "全文"
    return rows


def _points(pages: list[tuple[int, str]]) -> list[dict]:
    """按「1. 2. 3.」顺序编号拆(号接得上上一点才算新的一点;章节标题另有自己的编号,并进上一点)。拆不出 3 点 → []。"""
    items, cur, n, most = [], None, 0, 0
    for pg, text in pages:
        for line in text.splitlines():
            s = line.strip()
            if not s:
                continue
            a = ANNEX.match(s)
            if a and len(s) < 120:  # 决议正文的「1. 批准……」之后,附件(规则本身、附表)重新编号
                n = 0
                cur = {"label": ("附件 " + a.group("n").strip(".")).strip(), "heading": "", "page": str(pg or ""), "lines": [s]}
                items.append(cur)
                continue
            m = POINT.match(s)
            if m and int(m.group("n")) == n + 1:
                n += 1
                most = max(most, n)
                cur = {"label": f"第 {n} 点", "heading": "", "page": str(pg or ""), "lines": [s]}
                items.append(cur)
                continue
            if cur is None:
                cur = {"label": "序言", "heading": "", "page": str(pg or ""), "lines": []}
                items.append(cur)
            cur["lines"].append(s)
    return items if most >= 3 else []


def _chunks(it: dict) -> list[dict]:
    """一条超过 CHUNK 字的按行切成几段,标「(第 k 段)」;页码取每段开头所在页(段内只记开头)。"""
    if sum(len(x) + 1 for x in it["lines"]) <= CHUNK:
        return [it]
    out, cur, size = [], [], 0
    for x in it["lines"]:
        if cur and size + len(x) > CHUNK and (x == "" or size > CHUNK * 1.3 or x[:1].isupper() or x[:1].isdigit()):
            out.append(cur)
            cur, size = [], 0
        cur.append(x)
        size += len(x) + 1
    if cur:
        out.append(cur)
    return [{**it, "label": f"{it['label']}(第 {k + 1} 段)", "lines": ls} for k, ls in enumerate(out)]


def fetch(src: dict, data: Path, log=print) -> list[tuple[int, str]]:
    f = data / src["file"] if src.get("file") else None
    if f is None or not f.exists():
        if not src.get("url"):
            raise RuntimeError("没有本地原文也没有网址")
        r = httpx.get(src["url"], headers={"User-Agent": UA}, follow_redirects=True, timeout=120)
        r.raise_for_status()
        if f is not None:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(r.content)
        raw, ctype = r.content, r.headers.get("content-type", "")
    else:
        raw, ctype = f.read_bytes(), ""
    if raw[:5] == b"%PDF-":
        with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
            tmp.write(raw)
            tmp.flush()
            return pdf_pages(Path(tmp.name), src.get("language") or "", _pages_arg(src.get("pages") or ""), log)
    if "html" in ctype or raw.lstrip()[:1] == b"<":
        return html_pages(raw)
    return [(0, raw.decode("utf-8", errors="replace"))]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dir", type=Path, default=DATA)
    ap.add_argument("--only", nargs="*", help="只取这几部(law_key)")
    a = ap.parse_args(argv)
    srcs = list(csv.DictReader(io.open(a.dir / "law_sources.csv", encoding="utf-8")))
    out_path = a.dir / "law_clauses.csv.gz"
    keep = []
    if out_path.exists():
        with gzip.open(out_path, "rt", encoding="utf-8") as fh:
            keep = [r for r in csv.DictReader(fh)]
    done = {s["law_key"] for s in srcs if not a.only or s["law_key"] in a.only}
    rows = [r for r in keep if r["law_key"] not in done]
    for s in srcs:
        if s["law_key"] not in done:
            continue
        print(f"{s['law_key']}:", flush=True)
        try:
            got = split(fetch(s, a.dir), s["law_key"])
        except Exception as e:  # noqa: BLE001  一部取不到不影响别的
            print(f"  ! 没取成:{type(e).__name__}: {e}")
            rows += [r for r in keep if r["law_key"] == s["law_key"]]
            continue
        print(f"  {len(got)} 条,{sum(len(r['text']) for r in got)} 字")
        rows += got
    with gzip.open(out_path, "wt", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, CLAUSE_COLS)
        w.writeheader()
        w.writerows(rows)
    print(f"写了 {out_path}:{len(rows)} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
