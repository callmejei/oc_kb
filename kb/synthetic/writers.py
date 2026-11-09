"""Render DocContent to DOCX, PDF (single column, two column, scanned image-only), PPTX, XLSX.

All outputs are byte-reproducible: fixed document metadata, reportlab ``invariant=1``, and Office
zip containers rewritten with a fixed timestamp. Reruns of the seed therefore converge.
"""

from __future__ import annotations

import io
import os
import random
import re
import zipfile
from datetime import datetime
from pathlib import Path

import reportlab
from docx import Document
from docx.shared import Pt
from openpyxl import Workbook
from openpyxl.styles import Font
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from pptx import Presentation
from pptx.util import Inches
from pptx.util import Pt as PptPt
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    FrameBreak,
    NextPageTemplate,
    PageTemplate,
    Paragraph,
    Spacer,
    TableStyle,
)
from reportlab.platypus import Table as RLTable

from synthetic.content import DocContent, Section

FIXED_DT = datetime(2026, 1, 15, 9, 0, 0)
ZIP_DT = (2026, 1, 15, 9, 0, 0)
AUTHOR = "Synthetic Generator"
FONT_DIR = Path(reportlab.__file__).parent / "fonts"


def normalize_zip(path: Path) -> None:
    """Rewrite an OOXML container with fixed timestamps and sorted-stable order."""
    with zipfile.ZipFile(path) as z:
        items = [(i.filename, z.read(i.filename)) for i in z.infolist()]
    # openpyxl forces dcterms:modified = now() inside save(); pin it like every other timestamp
    fixed = FIXED_DT.strftime("%Y-%m-%dT%H:%M:%SZ").encode()
    items = [
        (
            n,
            re.sub(rb"(<dcterms:modified[^>]*>)[^<]*(</dcterms:modified>)", rb"\g<1>" + fixed + rb"\g<2>", d)
            if n == "docProps/core.xml"
            else d,
        )
        for n, d in items
    ]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
        for name, data in items:
            info = zipfile.ZipInfo(name, date_time=ZIP_DT)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            out.writestr(info, data)
    path.write_bytes(buf.getvalue())


def _walk(sections: list[Section], depth: int = 1):  # type: ignore[no-untyped-def]
    for s in sections:
        yield s, depth
        yield from _walk(s.subsections, depth + 1)


# ---------------------------------------------------------------------------- DOCX
def write_docx(c: DocContent, path: Path) -> None:
    doc = Document()
    cp = doc.core_properties
    cp.author = cp.last_modified_by = AUTHOR
    cp.created = cp.modified = FIXED_DT
    cp.title = c.title
    cp.revision = c.version
    doc.add_heading(c.title, level=0)
    p = doc.add_paragraph(c.subtitle)
    p.runs[0].font.size = Pt(9)
    for s, depth in _walk(c.sections):
        doc.add_heading(s.heading, level=min(depth, 3))
        for para in s.paragraphs:
            doc.add_paragraph(para)
        for b in s.bullets:
            doc.add_paragraph(b, style="List Bullet")
        if s.table:
            doc.add_paragraph(s.table.caption).runs[0].bold = True
            t = doc.add_table(rows=1, cols=len(s.table.header))
            t.style = "Table Grid"
            for i, h in enumerate(s.table.header):
                t.rows[0].cells[i].text = h
                t.rows[0].cells[i].paragraphs[0].runs[0].bold = True
            for row in s.table.rows:
                cells = t.add_row().cells
                for i, v in enumerate(row):
                    cells[i].text = v
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    normalize_zip(path)


# ---------------------------------------------------------------------------- PDF (text)
def _flowables(c: DocContent) -> list:  # type: ignore[type-arg]
    ss = getSampleStyleSheet()
    out: list = [Paragraph(c.title, ss["Title"]), Paragraph(c.subtitle, ss["Italic"]), Spacer(1, 4 * mm)]  # type: ignore[type-arg]
    for s, depth in _walk(c.sections):
        out.append(Paragraph(s.heading, ss["Heading2" if depth == 1 else "Heading3"]))
        out.extend(Paragraph(p, ss["BodyText"]) for p in s.paragraphs)
        out.extend(Paragraph(f"&bull; {b}", ss["BodyText"]) for b in s.bullets)
        if s.table:
            out.append(Paragraph(f"<b>{s.table.caption}</b>", ss["BodyText"]))
            small = ss["BodyText"].clone("cell", fontSize=7.5, leading=9)
            data = [[Paragraph(f"<b>{h}</b>", small) for h in s.table.header]] + [
                [Paragraph(v, small) for v in r] for r in s.table.rows
            ]
            t = RLTable(data, repeatRows=1)
            t.setStyle(
                TableStyle(
                    [
                        ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8e8e8")),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ]
                )
            )
            out.extend([t, Spacer(1, 3 * mm)])
    return out


def _footer(canvas: Canvas, doc: BaseDocTemplate) -> None:
    canvas.saveState()
    canvas.setFont("Helvetica", 7)
    canvas.drawString(15 * mm, 10 * mm, f"{doc.title} - synthetic document - page {doc.page}")
    canvas.restoreState()


def write_pdf(c: DocContent, path: Path, columns: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = BaseDocTemplate(
        str(path),
        pagesize=A4,
        title=c.title,
        author=AUTHOR,
        subject=c.subtitle,
        creator=AUTHOR,
        invariant=1,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=18 * mm,
    )
    w, h = A4
    if columns == 1:
        frames = [Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="one")]
        doc.addPageTemplates([PageTemplate("one", frames, onPage=_footer)])
        story = _flowables(c)
    else:
        gap = 8 * mm
        cw = (doc.width - gap) / 2
        head_h = 30 * mm
        first = [
            Frame(doc.leftMargin, h - doc.topMargin - head_h, doc.width, head_h, id="head"),
            Frame(doc.leftMargin, doc.bottomMargin, cw, doc.height - head_h, id="c1"),
            Frame(doc.leftMargin + cw + gap, doc.bottomMargin, cw, doc.height - head_h, id="c2"),
        ]
        later = [
            Frame(doc.leftMargin, doc.bottomMargin, cw, doc.height, id="l1"),
            Frame(doc.leftMargin + cw + gap, doc.bottomMargin, cw, doc.height, id="l2"),
        ]
        doc.addPageTemplates(
            [PageTemplate("first", first, onPage=_footer), PageTemplate("later", later, onPage=_footer)]
        )
        fl = _flowables(c)
        story = [*fl[:3], NextPageTemplate("later"), FrameBreak(), *fl[3:]]
    doc.build(story)


# ---------------------------------------------------------------------------- PDF (scanned)
def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_DIR / "Vera.ttf"), size)


def _wrap(text: str, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines


def write_scanned_pdf(c: DocContent, path: Path, seed: str) -> None:
    """Image-only PDF (no text layer): rendered at 150 dpi, slightly skewed and noisy, like a scan."""
    rnd = random.Random(seed)  # noqa: S311
    lines: list[tuple[str, int]] = [(c.title, 30), (c.subtitle, 16), ("", 16)]
    for s, _ in _walk(c.sections):
        lines.append((s.heading, 22))
        for p in s.paragraphs + s.bullets:
            lines.extend((ln, 17) for ln in _wrap(p, 88))
        if s.table:
            lines.append((s.table.caption, 17))
            lines.extend((" | ".join(r), 16) for r in [s.table.header, *s.table.rows])
        lines.append(("", 12))
    width, height = 1240, 1754  # A4 at 150 dpi
    pages: list[Image.Image] = []
    page, y = None, 0
    for text, size in lines:
        if page is None or y > height - 140:
            page = Image.new("L", (width, height), 250)
            pages.append(page)
            y = 110
        ImageDraw.Draw(page).text((110, y), text, fill=25, font=_font(size))
        y += int(size * 1.55)
    path.parent.mkdir(parents=True, exist_ok=True)
    cv = Canvas(str(path), pagesize=A4, invariant=1)
    cv.setTitle(c.title)
    cv.setAuthor(AUTHOR)
    for img in pages:
        img = img.rotate(rnd.uniform(-0.8, 0.8), fillcolor=250, resample=Image.Resampling.BICUBIC)
        px = img.load()
        for _ in range(4000):
            px[rnd.randrange(width), rnd.randrange(height)] = rnd.choice([90, 140, 200])  # type: ignore[index]
        img = img.filter(ImageFilter.GaussianBlur(0.5))
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=False)
        from reportlab.lib.utils import ImageReader

        cv.drawImage(ImageReader(io.BytesIO(buf.getvalue())), 0, 0, width=A4[0], height=A4[1])
        cv.showPage()
    cv.save()


# ---------------------------------------------------------------------------- PPTX
def write_pptx(c: DocContent, path: Path) -> None:
    prs = Presentation()
    cp = prs.core_properties
    cp.author = cp.last_modified_by = AUTHOR
    cp.created = cp.modified = FIXED_DT
    cp.title = c.title
    cp.revision = c.version
    s0 = prs.slides.add_slide(prs.slide_layouts[0])
    s0.shapes.title.text = c.title
    s0.placeholders[1].text = c.subtitle
    for s in c.sections:
        if s.table:
            sl = prs.slides.add_slide(prs.slide_layouts[5])
            sl.shapes.title.text = f"{s.heading}: {s.table.caption}"
            rows, cols = len(s.table.rows) + 1, len(s.table.header)
            tbl = sl.shapes.add_table(rows, cols, Inches(0.5), Inches(1.6), Inches(9), Inches(0.4 * rows)).table
            for j, hcell in enumerate(s.table.header):
                tbl.cell(0, j).text = hcell
            for i, r in enumerate(s.table.rows, start=1):
                for j, v in enumerate(r):
                    tbl.cell(i, j).text = v
        sl = prs.slides.add_slide(prs.slide_layouts[1])
        sl.shapes.title.text = s.heading
        tf = sl.placeholders[1].text_frame
        items = s.paragraphs + s.bullets
        tf.text = items[0] if items else ""
        for it in items[1:]:
            para = tf.add_paragraph()
            para.text = it
        for para in tf.paragraphs:
            for run in para.runs:
                run.font.size = PptPt(14)
        sl.notes_slide.notes_text_frame.text = f"Speaker notes for {s.heading} (synthetic)."
    path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(path))
    normalize_zip(path)


# ---------------------------------------------------------------------------- XLSX
def write_xlsx(c: DocContent, path: Path) -> None:
    wb = Workbook()
    wb.properties.creator = wb.properties.lastModifiedBy = AUTHOR
    wb.properties.created = wb.properties.modified = FIXED_DT
    wb.properties.title = c.title
    ws = wb.active
    ws.title = "Schedule"
    ws.append([c.title])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append([c.subtitle])
    ws.append([])
    tables = [s.table for s, _ in _walk(c.sections) if s.table]
    if not tables:  # retention-standard style schedule
        from synthetic.content import Table

        tables = [
            Table(
                "Retention periods by record class (synthetic)",
                ["Record class", "Retention class", "Retention period", "Legal hold eligible"],
                [
                    ["Customer correspondence", "R7Y", "7 years", "Yes"],
                    ["Credit files", "R10Y", "10 years after closure", "Yes"],
                    ["Marketing material", "R2Y", "2 years", "No"],
                    ["Board papers", "PERM", "Permanent", "Yes"],
                    ["System logs", "R1Y", "1 year", "Yes"],
                ],
            )
        ]
    for t in tables:
        ws.append([t.caption])
        ws.cell(ws.max_row, 1).font = Font(bold=True)
        ws.append(t.header)
        for cell in ws[ws.max_row]:
            cell.font = Font(bold=True)
        for r in t.rows:
            ws.append(r)
        ws.append([])
    notes = wb.create_sheet("Notes")
    for s, _ in _walk(c.sections):
        for p in s.paragraphs:
            notes.append([s.heading, p])
    for col in ("A", "B", "C", "D"):
        ws.column_dimensions[col].width = 34
    notes.column_dimensions["B"].width = 120
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    normalize_zip(path)


def write(fmt: str, content: DocContent, path: Path, seed: str) -> None:
    if fmt == "docx":
        write_docx(content, path)
    elif fmt == "pdf":
        write_pdf(content, path, 1)
    elif fmt == "pdf2col":
        write_pdf(content, path, 2)
    elif fmt == "pdfscan":
        write_scanned_pdf(content, path, seed)
    elif fmt == "pptx":
        write_pptx(content, path)
    elif fmt == "xlsx":
        write_xlsx(content, path)
    else:
        raise ValueError(fmt)
    os.utime(path, (FIXED_DT.timestamp(), FIXED_DT.timestamp()))
