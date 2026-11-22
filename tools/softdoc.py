"""生成软著申请用的「源程序」文档（docx），再用 LibreOffice 转 PDF。

规则：
- A4，每页正好 50 行
- 页眉：软件全称 + 版本号；页脚：第 N 页
- 全文连续行号
- 超过版面宽度的行自动折行（按显示宽度：中文=2，英文=1）
- 超过 60 页时，只取「前 30 页 + 后 30 页」
"""

from __future__ import annotations

import os
import sys

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Mm, Pt

SOFTWARE_NAME = "笔记本温度联动空气净化器风速控制系统"
VERSION = "V1.0"
LINES_PER_PAGE = 50
PAGES_FRONT = 30
PAGES_BACK = 30
MAX_WIDTH = 100          # 显示宽度上限（中文算 2）
FONT_SIZE = 8.5
LINE_PT = 14.0

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES = ["app.py", "bridge.py", "config.py", "sampler.py",
         "miio_lan.py", "sensors.py", "tray.py", "fake_purifier.py"]


def display_width(s: str) -> int:
    return sum(2 if ord(c) > 0x2E80 else 1 for c in s)


def collect() -> list:
    lines = []
    for name in FILES:
        path = os.path.join(BASE, name)
        if not os.path.exists(path):
            continue
        lines.append("# " + "=" * 20 + " 文件：" + name + " " + "=" * 20)
        with open(path, encoding="utf-8") as fh:
            for raw in fh.read().splitlines():
                text = raw.rstrip().expandtabs(4)
                if not text:
                    lines.append("")
                    continue
                # 超宽折行
                while display_width(text) > MAX_WIDTH:
                    cut, w = 0, 0
                    while cut < len(text) and w + (2 if ord(text[cut]) > 0x2E80 else 1) <= MAX_WIDTH:
                        w += 2 if ord(text[cut]) > 0x2E80 else 1
                        cut += 1
                    lines.append(text[:cut])
                    text = "    " + text[cut:]
                lines.append(text)
    return lines


def set_font(run, name="Consolas", east="宋体", size=FONT_SIZE):
    run.font.name = name
    run.font.size = Pt(size)
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    rfonts.set(qn("w:ascii"), name)
    rfonts.set(qn("w:hAnsi"), name)
    rfonts.set(qn("w:eastAsia"), east)


def add_page_field(par, size=8):
    run = par.add_run()
    set_font(run, size=size)
    fld_begin = OxmlElement("w:fldChar")
    fld_begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = "PAGE"
    fld_end = OxmlElement("w:fldChar")
    fld_end.set(qn("w:fldCharType"), "end")
    run._r.append(fld_begin)
    run._r.append(instr)
    run._r.append(fld_end)


def build(lines: list, out_path: str) -> int:
    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Mm(210), Mm(297)
    sec.top_margin, sec.bottom_margin = Mm(22), Mm(18)
    sec.left_margin, sec.right_margin = Mm(20), Mm(18)
    sec.header_distance = Mm(10)
    sec.footer_distance = Mm(8)

    # 页眉
    hp = sec.header.paragraphs[0]
    hp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    hr = hp.add_run("%s  %s  源程序" % (SOFTWARE_NAME, VERSION))
    set_font(hr, size=9)

    # 页脚：第 N 页
    fp = sec.footer.paragraphs[0]
    fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r1 = fp.add_run("第 ")
    set_font(r1, size=8)
    add_page_field(fp)
    r2 = fp.add_run(" 页")
    set_font(r2, size=8)

    style = doc.styles["Normal"]
    style.paragraph_format.space_before = Pt(0)
    style.paragraph_format.space_after = Pt(0)
    style.paragraph_format.line_spacing_rule = WD_LINE_SPACING.EXACTLY
    style.paragraph_format.line_spacing = Pt(LINE_PT)

    total_pages = (len(lines) + LINES_PER_PAGE - 1) // LINES_PER_PAGE
    if total_pages > PAGES_FRONT + PAGES_BACK:
        head = lines[:PAGES_FRONT * LINES_PER_PAGE]
        tail = lines[-PAGES_BACK * LINES_PER_PAGE:]
        shown = head + tail
    else:
        shown = lines

    for idx, text in enumerate(shown):
        p = doc.add_paragraph()
        pf = p.paragraph_format
        pf.space_before = Pt(0)
        pf.space_after = Pt(0)
        pf.line_spacing_rule = WD_LINE_SPACING.EXACTLY
        pf.line_spacing = Pt(LINE_PT)
        if idx and idx % LINES_PER_PAGE == 0:
            pf.page_break_before = True
        run = p.add_run("%05d  %s" % (idx + 1, text if text else " "))
        set_font(run)

    doc.save(out_path)
    return len(shown)


if __name__ == "__main__":
    src = collect()
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(BASE, "docs", "软著-源程序.docx")
    shown = build(src, out)
    print("源码总行数:", len(src))
    print("实际写入行数:", shown, "->", shown // LINES_PER_PAGE, "页")
    print("输出:", out)
