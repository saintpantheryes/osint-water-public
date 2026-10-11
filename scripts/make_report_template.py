"""从成稿 Word 做报告样式模板:保留样式、页面设置、页眉页脚与编号,删掉正文和图片(模板里不留成稿内容)。

用法:python scripts/make_report_template.py <成稿.docx> private/templates/报告模板_阿塞拜疆成稿.docx
      python scripts/make_report_template.py --outline <模板.docx> <输出.docx>   正文换成国别报告的章节目录(样式、页面不变)
报告生成时(core/watch/reporting.py)从模板读版式:各样式的字体字号、行距、缩进、段前段后,页面尺寸与页边距;
国别报告的章节来自 core/jobs/country_report.OUTLINE,--outline 把同一份目录写进模板正文,打开模板就看得到报告的结构。
"""
from __future__ import annotations

import sys
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn

IMAGE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"


def main(src: str, dst: str) -> None:
    d = Document(src)
    body = d.element.body
    for el in list(body):
        if el.tag != qn("w:sectPr"):  # 最后一节的页面设置、页眉页脚引用保留
            body.remove(el)
    p = d.add_paragraph("(报告样式模板:取自成稿的样式、页面设置与页眉页脚;正文为空)")
    p.style = d.styles["Normal"]
    part = d.part
    for rid, rel in list(part.rels.items()):  # 正文删掉后,图片关系无人引用,一并去掉
        if rel.reltype == IMAGE:
            del part.rels[rid]
    cp = d.core_properties  # 文档属性里的作者、标题等不带进模板
    cp.author = cp.last_modified_by = cp.title = cp.subject = cp.keywords = cp.comments = cp.category = ""
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    d.save(dst)
    print(f"{src} → {dst} ({Path(dst).stat().st_size:,} 字节)")


def outline(src: str, dst: str) -> None:
    """模板正文换成国别报告的章节目录:pandoc 以模板为 reference-doc 出 Word(样式、页面设置、页眉页脚都取自模板)。"""
    import tempfile

    import pypandoc

    from core.jobs.country_report import outline_md

    md = ("---\ntitle: \"××国水务市场分析报告\"\nsubtitle: \"内部资料　仅供参考\"\ndate: \"××××年××月\"\n---\n\n"
          "(报告样式模板:样式、页面设置与页眉页脚取自一份国别市场分析成稿。下面是国别报告的章节目录,系统按这个结构生成报告;"
          "括号里是每节放什么。改样式直接在本文件里改样式并上传到「报告模板」表;改章节需改程序 core/jobs/country_report.OUTLINE。)\n\n"
          + outline_md())
    with tempfile.TemporaryDirectory() as tmp:
        mp = Path(tmp) / "outline.md"
        mp.write_text(md, encoding="utf-8")
        pypandoc.convert_file(str(mp), "docx", format="markdown", outputfile=dst,
                              extra_args=["--quiet", "-M", "lang=zh-CN", f"--reference-doc={src}"])
    print(f"{src} → {dst}(正文为国别报告章节目录,{Path(dst).stat().st_size:,} 字节)")


if __name__ == "__main__":
    if sys.argv[1] == "--outline":
        outline(sys.argv[2], sys.argv[3])
    else:
        main(sys.argv[1], sys.argv[2])
