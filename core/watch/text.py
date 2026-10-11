"""网页/附件转文本、多语种日期解析。"""
from __future__ import annotations

import io
import re
from datetime import date, datetime
from email.utils import parsedate_to_datetime

from bs4 import BeautifulSoup

_DROP = ["script", "style", "noscript", "svg", "iframe", "form"]
_CHROME = ["nav", "header", "footer", "aside"]


def soup(html: str | bytes) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def html_text(html: str | bytes, selector: str | None = None, drop_chrome: bool = True) -> str:
    """网页正文文本:去脚本与样式,可选只取 selector 区域;默认也去掉导航、页头页脚,减少页面变化监测的噪声。
    先定位 selector 区域再在区域内清理:有的网站整页包在一个 <form> 里(ASP.NET、PRADO/Atexo),先删表单就什么都不剩。"""
    s = soup(html)
    root = (s.select_one(selector) if selector else None) or s
    for t in root(_DROP + (_CHROME if drop_chrome else [])):
        t.decompose()
    return norm_lines(root.get_text("\n"))


def norm_lines(text: str) -> str:
    lines = (re.sub(r"[ \t ]+", " ", ln).strip() for ln in text.splitlines())
    return "\n".join(ln for ln in lines if ln)


# 采购公告里的自然人联系方式(邮箱、电话)在入库前去掉;原文链接仍在,需要时看原文
_EMAIL = re.compile(r"[\w.+-]+\s*@\s*[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}")  # PDF 转出的文本里 @ 前后常被断行
_PHONE_LABELLED = re.compile(
    r"\b(tel|tél|telephone|téléphone|teléfono|telefone|telefon|phone|fax|faks|mob|mobile|cell|whatsapp|тел|телефон|факс)\b"
    r"(\.?(?:\s*[/,]\s*(?:fax|faks|факс)\.?)?\s*(?:no\.?|number|номер)?\s*[:.]?\s*)(?:\+|00)?[\d(][\d\s().\-/]{5,}\d", re.IGNORECASE)
_PHONE_INTL = re.compile(r"(?<![\w+])(?:\+|00)\d{1,3}[\s.-]?\(?\d{1,4}\)?(?:[\s.-]?\d{2,4}){2,4}(?!\d)")


def scrub_contacts(text: str) -> str:
    if not text:
        return text
    text = _EMAIL.sub("[邮箱已略]", text)
    text = _PHONE_LABELLED.sub(lambda m: f"{m.group(1)}{m.group(2)}[电话已略]", text)
    return _PHONE_INTL.sub("[电话已略]", text)


def pdf_text(data: bytes) -> tuple[str, int]:
    import pdfplumber

    with pdfplumber.open(io.BytesIO(data)) as p:
        return norm_lines("\n".join((pg.extract_text() or "") for pg in p.pages)), len(p.pages)


def docx_text(data: bytes) -> str:
    from docx import Document

    d = Document(io.BytesIO(data))
    parts = [p.text for p in d.paragraphs]
    for tb in d.tables:
        for row in tb.rows:
            parts.append(" | ".join(c.text for c in row.cells))
    return norm_lines("\n".join(parts))


# ---------- 日期 ----------

_MONTHS = {
    # 英
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11,
    "dec": 12,
    # 阿塞拜疆语
    "yanvar": 1, "fevral": 2, "mart": 3, "aprel": 4, "iyun": 6, "iyul": 7, "avqust": 8, "sentyabr": 9,
    "oktyabr": 10, "noyabr": 11, "dekabr": 12,
    # 俄语(属格)
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}


def _month(word: str) -> int | None:
    w = word.lower().replace("̇", "").strip(".,")  # "İyun".lower() 带组合点
    return _MONTHS.get(w)


def parse_date(value: object) -> str | None:
    """把各信源的日期写法统一成 YYYY-MM-DD;认不出返回 None(不猜)。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    s = str(value).strip()
    if not s:
        return None
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)  # ISO(含 2026-09-24T10:00:00Z)
    if m:
        return _ok(int(m[1]), int(m[2]), int(m[3]))
    m = re.search(r"\b(\d{4})(\d{2})(\d{2})T\d{6}Z?\b", s)  # GDELT 20260924T150000Z
    if m:
        return _ok(int(m[1]), int(m[2]), int(m[3]))
    m = re.search(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{4})\b", s)  # 23/09/2026、23.09.2026(日在前)
    if m:
        return _ok(int(m[3]), int(m[2]), int(m[1]))
    m = re.search(r"\b(\d{1,2})-([^\W\d_]{3,})-(\d{4})\b", s)  # 世行采购公告 24-Aug-2026
    if m and _month(m[2]):
        return _ok(int(m[3]), _month(m[2]), int(m[1]))
    m = re.search(r"\b(\d{1,2})-([^\W\d_]{3,})-(\d{2})\b", s)  # 联合国开发署 25-Sep-26
    if m and _month(m[2]):
        return _ok(2000 + int(m[3]), _month(m[2]), int(m[1]))
    m = re.search(r"\b(\d{1,2})\s+([^\W\d_]+)\.?(?:,\s*|\s+)(\d{4})\b", s)  # 23 Sentyabr, 2026 / 12 October 2026
    if m and _month(m[2]):
        return _ok(int(m[3]), _month(m[2]), int(m[1]))
    m = re.search(r"\b([^\W\d_]+)\.?\s+(\d{1,2})(?:,\s*|\s+)(\d{4})\b", s)  # September 18, 2026 / Sep 15,2026(亚投行)
    if m and _month(m[1]):
        return _ok(int(m[3]), _month(m[1]), int(m[2]))
    try:  # RSS pubDate:Wed, 24 Sep 2026 10:00:00 +0400
        return parsedate_to_datetime(s).date().isoformat()
    except (TypeError, ValueError, IndexError):
        return None


def parse_date_infer_year(value: object, today: date) -> str | None:
    """先按 parse_date;认不出且只有"日 月"(如 Trend 列表的"19 Sentyabr 19:21")时补今年,晚于明天的算去年。"""
    iso = parse_date(value)
    if iso or value is None:
        return iso
    m = re.search(r"\b(\d{1,2})\s+([^\W\d_]+)", str(value))
    if not (m and _month(m[2])):
        return None
    iso = _ok(today.year, _month(m[2]), int(m[1]))
    if iso and iso > date.fromordinal(today.toordinal() + 1).isoformat():
        iso = _ok(today.year - 1, _month(m[2]), int(m[1]))
    return iso


def region_text(html: str | bytes, selectors: str | list[str] | None) -> str:
    """一个或多个选择器区域的正文(每个选择器取全部匹配);都没匹配到时退回全页正文。"""
    if not selectors:
        return html_text(html)
    s = soup(html)
    for t in s(_DROP):
        t.decompose()
    parts = [el.get_text("\n") for sel in ([selectors] if isinstance(selectors, str) else selectors) for el in s.select(sel)]
    return norm_lines("\n".join(parts)) if parts else html_text(html)


def _ok(y: int, m: int | None, d: int) -> str | None:
    try:
        return date(y, m or 0, d).isoformat()
    except ValueError:
        return None
