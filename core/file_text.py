"""用户上传的文件转正文:PDF、Word 直接取文字;截图、照片和扫描版 PDF 用文字识别(tesseract)。

助手「看链接」用:网站不让程序读时,用户把下载的 PDF 或截图传上来,按它分析。
文字识别要主机上装 tesseract 和语言包(worker.Dockerfile 里装);没装时给出原因,不影响其余功能。
"""
from __future__ import annotations

import io
import re
import shutil
import subprocess

from core.watch.text import docx_text, norm_lines, pdf_text, scrub_contacts

MAX_BYTES = 12_000_000
OCR_PAGES = 8  # 扫描版 PDF 只识别前几页:公告的要点(业主、资金、截止)都在前面,也免得一份长文件占住后台
OCR_TIMEOUT = 90
OCR_LANGS = ("eng", "chi_sim", "fra", "spa", "por", "rus", "ara")  # 多边开发银行公告与中文截图常见的文字
MIN_TEXT = 40
KINDS = "PDF、Word(.docx)或图片(截图、照片)"


class FileError(ValueError):
    """文件读不了:原因直接给页面显示。"""


def kind_of(data: bytes) -> str | None:
    """按文件头认类型(不信文件名):pdf、docx、image。"""
    if data[:5] == b"%PDF-":
        return "pdf"
    if data[:4] == b"PK\x03\x04" and b"word/" in data[:200_000]:
        return "docx"
    if data[:8] == b"\x89PNG\r\n\x1a\n" or data[:3] == b"\xff\xd8\xff" or data[:6] in (b"GIF87a", b"GIF89a") \
            or (data[:4] == b"RIFF" and data[8:12] == b"WEBP") or data[:2] == b"BM":
        return "image"
    return None


def _langs() -> str:
    try:
        out = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return "eng"
    have = {ln.strip() for ln in out.splitlines()[1:]}
    return "+".join(x for x in OCR_LANGS if x in have) or "eng"


def ocr_image(img) -> str:
    """一张 PIL 图片 → 文字。"""
    if not shutil.which("tesseract"):
        raise FileError("服务器上还没装文字识别,暂时看不了图片和扫描件;可以先把文字复制给我")
    img = img.convert("L")
    if max(img.size) < 1600:  # 手机截图字小:放大一些识别得准
        f = 1600 / max(img.size)
        img = img.resize((int(img.width * f), int(img.height * f)))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    try:
        r = subprocess.run(["tesseract", "stdin", "stdout", "-l", _langs(), "--psm", "3"], input=buf.getvalue(),
                           capture_output=True, timeout=OCR_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise FileError("文字识别太久没完成,图片可能太大") from e
    if r.returncode != 0:
        raise FileError("文字识别出错:" + r.stderr.decode("utf-8", "ignore").strip()[:200])
    return r.stdout.decode("utf-8", "ignore")


def _open_image(data: bytes):
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = 60_000_000
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        return img
    except Exception as e:  # noqa: BLE001  图片坏了、太大
        raise FileError(f"图片打不开({type(e).__name__})") from e


def _cjk_join(text: str) -> str:
    """文字识别会在中文字之间加空格:去掉汉字与汉字之间的空格。"""
    return re.sub(r"(?<=[㐀-鿿　-〿＀-￯]) (?=[㐀-鿿　-〿＀-￯])", "", text)


def file_text(data: bytes) -> tuple[str, str]:
    """文件 → (正文, 读法说明)。读不到字时抛 FileError。"""
    if not data:
        raise FileError("文件是空的")
    if len(data) > MAX_BYTES:
        raise FileError("文件太大(超过 12 MB);可以只截要点那几页")
    k = kind_of(data)
    how = ""
    if k == "pdf":
        try:
            text, _ = pdf_text(data)
        except Exception as e:  # noqa: BLE001
            raise FileError(f"PDF 打不开({type(e).__name__}),可能加了密码") from e
        if len(text.strip()) < MIN_TEXT:  # 扫描件:没有文字层,逐页识别
            import pdfplumber

            parts = []
            with pdfplumber.open(io.BytesIO(data)) as p:
                for pg in p.pages[:OCR_PAGES]:
                    parts.append(ocr_image(pg.to_image(resolution=200).original))
                more = len(p.pages) > OCR_PAGES
            text = "\n".join(parts)
            how = f"PDF 是扫描件,用文字识别读了前 {OCR_PAGES} 页" if more else "PDF 是扫描件,用文字识别读的"
    elif k == "docx":
        try:
            text = docx_text(data)
        except Exception as e:  # noqa: BLE001
            raise FileError(f"Word 文件打不开({type(e).__name__})") from e
    elif k == "image":
        text = ocr_image(_open_image(data))
        how = "图片用文字识别读的,个别字可能认错"
    else:
        raise FileError(f"只能看 {KINDS}")
    text = scrub_contacts(norm_lines(_cjk_join(text)))
    if len(text.strip()) < MIN_TEXT:
        raise FileError("文件里没读到文字" + ("(图片可能太模糊)" if k == "image" else ""))
    return text, how
