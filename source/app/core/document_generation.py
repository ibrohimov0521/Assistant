from __future__ import annotations

import io
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches, Pt, RGBColor
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from pptx import Presentation
from pptx.dml.color import RGBColor as PptxColor
from pptx.enum.text import PP_ALIGN
from pptx.util import Inches as PptxInches, Pt as PptxPt
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer,
                                Table, TableStyle)


MAX_DOCUMENT_CHARS = 30_000
MAX_SECTIONS = 18
MAX_TABLE_ROWS = 200
MAX_TABLE_COLUMNS = 12
MAX_FILE_BYTES = 15 * 1024 * 1024

FORMATS = {
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
    "pdf": ("application/pdf", ".pdf"),
    "pptx": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"),
    "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
}
DOCUMENT_TYPES = {"general", "letter", "proposal", "technical_spec", "meeting_minutes",
                  "guide", "presentation", "table"}


class DocumentGenerationError(ValueError):
    pass


def _text(value: Any, limit: int) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = "".join(ch for ch in text if ch in "\n\t" or ch.isprintable())
    return "\n".join(line.rstrip() for line in text.splitlines()).strip()[:limit]


def _slug(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).strip().casefold()
    value = re.sub(r"[^\w.-]+", "_", value, flags=re.UNICODE).strip("._-")
    return (value[:72] or "qoralama")


def _normalize(args: dict[str, Any]) -> dict[str, Any]:
    fmt = str(args.get("format") or "docx").lower()
    document_type = str(args.get("document_type") or "general")
    title = _text(args.get("title"), 180)
    style = str(args.get("style") or "formal").strip().lower()
    if style not in {"formal", "simple", "compact"}:
        raise DocumentGenerationError("Uslub formal, simple yoki compact bo‘lishi kerak.")
    if fmt not in FORMATS:
        raise DocumentGenerationError("Format DOCX, PDF, PPTX yoki XLSX bo‘lishi kerak.")
    if document_type not in DOCUMENT_TYPES:
        raise DocumentGenerationError("Hujjat turi qo‘llab-quvvatlanmaydi.")
    if not title:
        raise DocumentGenerationError("Hujjat sarlavhasi kerak.")
    raw_sections = args.get("sections")
    if not isinstance(raw_sections, list) or len(raw_sections) > MAX_SECTIONS:
        raise DocumentGenerationError("Hujjat 18 tagacha bo‘limdan iborat bo‘lishi mumkin.")
    sections: list[dict[str, str]] = []
    total = len(title)
    for row in raw_sections:
        if not isinstance(row, dict):
            raise DocumentGenerationError("Bo‘lim ma’lumoti noto‘g‘ri.")
        heading = _text(row.get("heading"), 120)
        body = _text(row.get("body"), 6000)
        if not heading and not body:
            continue
        if not body:
            raise DocumentGenerationError("Har bir bo‘limda mazmun bo‘lishi kerak.")
        total += len(heading) + len(body)
        sections.append({"heading": heading, "body": body})
    raw_headers = args.get("table_headers")
    raw_rows = args.get("table_rows")
    if not isinstance(raw_headers, list) or not isinstance(raw_rows, list):
        raise DocumentGenerationError("Jadval sarlavha va qatorlari noto‘g‘ri.")
    headers = [_text(value, 100) for value in raw_headers[:MAX_TABLE_COLUMNS]]
    if any(not value for value in headers):
        raise DocumentGenerationError("Jadval ustun sarlavhalari bo‘sh bo‘lmasin.")
    if len(raw_headers) > MAX_TABLE_COLUMNS or len(raw_rows) > MAX_TABLE_ROWS:
        raise DocumentGenerationError("Jadval ko‘pi bilan 12 ustun va 200 qatordan iborat bo‘lishi mumkin.")
    rows: list[list[str]] = []
    for row in raw_rows:
        if not isinstance(row, list) or len(row) != len(headers):
            raise DocumentGenerationError("Jadval qatori ustunlar soniga mos kelmadi.")
        values = [_text(value, 500) for value in row]
        total += sum(map(len, values))
        rows.append(values)
    if headers and not rows:
        raise DocumentGenerationError("Jadvalda kamida bitta ma’lumot qatori bo‘lsin.")
    if rows and not headers:
        raise DocumentGenerationError("Jadval sarlavhalari ko‘rsatilmagan.")
    raw_missing = args.get("missing_fields")
    if not isinstance(raw_missing, list) or len(raw_missing) > 20:
        raise DocumentGenerationError("Yetishmayotgan maydonlar ro‘yxati noto‘g‘ri.")
    missing = list(dict.fromkeys(_text(value, 120) for value in raw_missing if _text(value, 120)))
    total += sum(map(len, missing))
    if total > MAX_DOCUMENT_CHARS:
        raise DocumentGenerationError("Hujjat mazmuni 30 000 belgidan oshmasligi kerak.")
    if not sections and not rows:
        raise DocumentGenerationError("Hujjatga kiritiladigan mazmun topilmadi.")
    return {"format": fmt, "document_type": document_type, "title": title, "style": style,
            "sections": sections, "table_headers": headers, "table_rows": rows,
            "missing_fields": missing}


def _safe_xlsx_cell(value: str) -> str:
    if value.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _make_docx(data: dict[str, Any], created: datetime) -> bytes:
    doc = Document()
    section = doc.sections[0]
    compact = data["style"] == "compact"
    section.top_margin = Inches(0.5 if compact else 0.7)
    section.bottom_margin = Inches(0.45 if compact else 0.65)
    normal = doc.styles["Normal"]
    normal.font.name = "Aptos"
    normal.font.size = Pt(9.5 if compact else 11 if data["style"] == "simple" else 10.5)
    normal.font.color.rgb = RGBColor(31, 41, 55) if data["style"] == "simple" else RGBColor(43, 52, 64)
    title = doc.add_heading(data["title"], level=0)
    title.alignment = WD_ALIGN_PARAGRAPH.LEFT if data["style"] == "simple" else WD_ALIGN_PARAGRAPH.CENTER
    for run in title.runs:
        run.font.size = Pt(20 if compact else 24 if data["style"] == "simple" else 26)
    date_line = doc.add_paragraph(f"Tayyorlangan sana: {created:%Y-%m-%d %H:%M} ({created.tzname() or 'mahalliy vaqt'})")
    date_line.alignment = WD_ALIGN_PARAGRAPH.CENTER
    date_line.runs[0].font.color.rgb = RGBColor(100, 116, 139)
    if data["missing_fields"]:
        doc.add_heading("Aniqlashtirilishi kerak", level=1)
        for field in data["missing_fields"]:
            doc.add_paragraph(f"[[{field}]]", style="List Bullet")
    for item in data["sections"]:
        if item["heading"]:
            doc.add_heading(item["heading"], level=1)
        for raw_line in item["body"].splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("- ") or line.startswith("• "):
                doc.add_paragraph(line[2:].strip(), style="List Bullet")
            elif re.match(r"^\d+[.)]\s+", line):
                doc.add_paragraph(re.sub(r"^\d+[.)]\s+", "", line), style="List Number")
            else:
                doc.add_paragraph(line)
    if data["table_headers"]:
        doc.add_heading("Jadval", level=1)
        table = doc.add_table(rows=1, cols=len(data["table_headers"]))
        table.style = "Light Shading Accent 1"
        for cell, value in zip(table.rows[0].cells, data["table_headers"]):
            cell.text = value
        for values in data["table_rows"]:
            cells = table.add_row().cells
            for cell, value in zip(cells, values):
                cell.text = value
    footer = section.footer.paragraphs[0]
    footer.text = "Yordamchi tayyorlagan qoralama · yuborishdan oldin tekshiring"
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def _pdf_font() -> str:
    candidates = (
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed.ttf"),
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("C:/Windows/Fonts/calibri.ttf"),
    )
    font_path = next((path for path in candidates if path.is_file()), None)
    if font_path is None:
        raise DocumentGenerationError("PDF uchun o‘zbek belgilarini qo‘llaydigan shrift serverda topilmadi.")
    if "AssistantUnicode" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("AssistantUnicode", str(font_path)))
    return "AssistantUnicode"


def _make_pdf(data: dict[str, Any], created: datetime) -> bytes:
    font = _pdf_font()
    out = io.BytesIO()
    compact = data["style"] == "compact"
    margin = 16 if compact else 24 if data["style"] == "simple" else 22
    doc = SimpleDocTemplate(out, pagesize=A4, rightMargin=margin * mm, leftMargin=margin * mm,
                            topMargin=(16 if compact else 24 if data["style"] == "simple" else 20) * mm,
                            bottomMargin=(16 if compact else 24 if data["style"] == "simple" else 20) * mm,
                            title=data["title"], author="Shaxsiy yordamchi")
    base = getSampleStyleSheet()
    title_style = ParagraphStyle("UzbekTitle", parent=base["Title"], fontName=font,
                                 fontSize=18 if compact else 20 if data["style"] == "simple" else 22,
                                 leading=23 if compact else 26 if data["style"] == "simple" else 28,
                                 alignment=TA_CENTER if data["style"] == "formal" else TA_LEFT,
                                 spaceAfter=(5 if compact else 7 if data["style"] == "simple" else 9) * mm)
    heading_style = ParagraphStyle("UzbekHeading", parent=base["Heading2"], fontName=font,
                                   fontSize=14, leading=18, textColor=colors.HexColor("#12304A"),
                                   spaceBefore=4 * mm, spaceAfter=2 * mm)
    body_style = ParagraphStyle("UzbekBody", parent=base["BodyText"], fontName=font,
                                fontSize=9 if compact else 11 if data["style"] == "simple" else 10.5,
                                leading=12 if compact else 16 if data["style"] == "simple" else 15,
                                spaceAfter=(1.5 if compact else 3 if data["style"] == "simple" else 2.5) * mm)
    small_style = ParagraphStyle("UzbekSmall", parent=body_style, fontSize=8.5,
                                 textColor=colors.HexColor("#64748B"), alignment=TA_CENTER)
    flow: list[Any] = [Paragraph(escape(data["title"]), title_style),
                       Paragraph(escape(f"Tayyorlangan sana: {created:%Y-%m-%d %H:%M} ({created.tzname() or 'mahalliy vaqt'})"), small_style),
                       Spacer(1, 4 * mm)]
    if data["missing_fields"]:
        flow.append(Paragraph("Aniqlashtirilishi kerak", heading_style))
        for field in data["missing_fields"]:
            flow.append(Paragraph(escape(f"• [[{field}]]"), body_style))
    for item in data["sections"]:
        if item["heading"]:
            flow.append(Paragraph(escape(item["heading"]), heading_style))
        for raw_line in item["body"].splitlines():
            line = raw_line.strip()
            if line:
                flow.append(Paragraph(escape(line).replace("\t", "    "), body_style))
    if data["table_headers"]:
        flow.append(Paragraph("Jadval", heading_style))
        matrix = [data["table_headers"], *data["table_rows"]]
        table = Table([[Paragraph(escape(_safe_xlsx_cell(value)), body_style) for value in row]
                       for row in matrix], repeatRows=1, hAlign="LEFT")
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#12304A")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#CBD5E1")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ]))
        flow.extend([table, Spacer(1, 3 * mm)])
    flow.append(Spacer(1, 6 * mm))
    flow.append(Paragraph("Qoralama · yuborishdan oldin tekshiring", small_style))
    doc.build(flow)
    return out.getvalue()


def _make_pptx(data: dict[str, Any], created: datetime) -> bytes:
    presentation = Presentation()
    presentation.slide_width = PptxInches(13.333)
    presentation.slide_height = PptxInches(7.5)
    title_slide = presentation.slides.add_slide(presentation.slide_layouts[0])
    title_slide.shapes.title.text = data["title"]
    subtitle = title_slide.placeholders[1]
    subtitle.text = f"Qoralama · {created:%Y-%m-%d} · tekshirish uchun"
    for paragraph in subtitle.text_frame.paragraphs:
        paragraph.font.size = PptxPt(18)
        paragraph.font.color.rgb = PptxColor(89, 105, 122)
    content = data["sections"] or ([{"heading": "Jadval", "body": "Jadval ma’lumoti keyingi slaydda."}]
                                   if data["table_headers"] else [])
    for item in content:
        slide = presentation.slides.add_slide(presentation.slide_layouts[1])
        slide.shapes.title.text = item["heading"] or data["title"]
        body = slide.placeholders[1].text_frame
        body.clear()
        for index, raw_line in enumerate(item["body"].splitlines()):
            line = raw_line.strip().lstrip("-• ").strip()
            if not line:
                continue
            paragraph = body.paragraphs[0] if index == 0 and not body.paragraphs[0].text else body.add_paragraph()
            paragraph.text = line[:650]
            paragraph.level = 0 if raw_line.lstrip().startswith(("-", "•")) else 0
            base_size = 17 if data["style"] == "compact" else 20 if data["style"] == "simple" else 22
            paragraph.font.size = PptxPt(base_size if len(line) < 100 else max(15, base_size - 4))
            paragraph.font.color.rgb = PptxColor(43, 52, 64)
        if data["missing_fields"] and len(presentation.slides) == 2:
            paragraph = body.add_paragraph()
            paragraph.text = "Tekshirish kerak: " + ", ".join(f"[[{value}]]" for value in data["missing_fields"])
            paragraph.font.size = PptxPt(16)
            paragraph.font.color.rgb = PptxColor(180, 83, 9)
    if data["table_headers"]:
        slide = presentation.slides.add_slide(presentation.slide_layouts[5])
        slide.shapes.title.text = "Jadval"
        rows, cols = len(data["table_rows"]) + 1, len(data["table_headers"])
        shape = slide.shapes.add_table(rows, cols, PptxInches(0.45), PptxInches(1.35),
                                       PptxInches(12.4), PptxInches(5.5))
        table = shape.table
        for col, header in enumerate(data["table_headers"]):
            cell = table.cell(0, col)
            cell.text = header[:100]
            cell.fill.solid()
            cell.fill.fore_color.rgb = PptxColor(18, 48, 74)
            for paragraph in cell.text_frame.paragraphs:
                paragraph.font.bold = True
                paragraph.font.size = PptxPt(13)
                paragraph.font.color.rgb = PptxColor(255, 255, 255)
        for row_no, row in enumerate(data["table_rows"], start=1):
            for col, value in enumerate(row):
                table.cell(row_no, col).text = value[:200]
                for paragraph in table.cell(row_no, col).text_frame.paragraphs:
                    paragraph.font.size = PptxPt(11)
    out = io.BytesIO()
    presentation.save(out)
    return out.getvalue()


def _make_xlsx(data: dict[str, Any], created: datetime) -> bytes:
    workbook = Workbook()
    accent = {"formal": "12304A", "simple": "2563EB", "compact": "334155"}[data["style"]]
    sheet = workbook.active
    sheet.title = "Qoralama"
    sheet.merge_cells("A1:B1")
    sheet["A1"] = data["title"]
    sheet["A1"].font = Font(name="Aptos Display", size=16, bold=True, color="FFFFFF")
    sheet["A1"].fill = PatternFill("solid", fgColor=accent)
    sheet["A1"].alignment = Alignment(vertical="center")
    sheet.row_dimensions[1].height = 30
    sheet.append(["Tayyorlangan sana", created.strftime("%Y-%m-%d %H:%M %Z")])
    if data["missing_fields"]:
        sheet.append(["Tekshirish kerak", "; ".join(f"[[{value}]]" for value in data["missing_fields"])])
    sheet.append(["Bo‘lim", "Mazmun"])
    header_row = sheet.max_row
    for cell in sheet[header_row]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=accent)
    for item in data["sections"]:
        sheet.append([_safe_xlsx_cell(item["heading"] or "Mazmun"), _safe_xlsx_cell(item["body"])])
    sheet.column_dimensions["A"].width = 28
    sheet.column_dimensions["B"].width = 88
    for row in sheet.iter_rows(min_row=header_row + 1):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    sheet.freeze_panes = f"A{header_row + 1}"
    sheet.auto_filter.ref = f"A{header_row}:B{sheet.max_row}"
    if data["table_headers"]:
        table_sheet = workbook.create_sheet("Ma'lumotlar")
        table_sheet.append([_safe_xlsx_cell(value) for value in data["table_headers"]])
        for cell in table_sheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor=accent)
        for row in data["table_rows"]:
            table_sheet.append([_safe_xlsx_cell(value) for value in row])
        table_sheet.freeze_panes = "A2"
        table_sheet.auto_filter.ref = table_sheet.dimensions
        for col in range(1, len(data["table_headers"]) + 1):
            values = [len(str(table_sheet.cell(row, col).value or "")) for row in range(1, table_sheet.max_row + 1)]
            table_sheet.column_dimensions[get_column_letter(col)].width = min(42, max(14, max(values, default=0) + 2))
    out = io.BytesIO()
    workbook.save(out)
    return out.getvalue()


def generate_document(args: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    """Create an in-memory, owner-facing document draft from validated model output."""
    data = _normalize(args)
    if now.tzinfo is None:
        raise ValueError("Hujjat yaratilish vaqti vaqt mintaqasiga ega bo‘lishi kerak.")
    created = now.astimezone(now.tzinfo)
    fmt = data["format"]
    if fmt == "docx":
        content = _make_docx(data, created)
    elif fmt == "pdf":
        content = _make_pdf(data, created)
    elif fmt == "pptx":
        content = _make_pptx(data, created)
    else:
        content = _make_xlsx(data, created)
    if not content or len(content) > MAX_FILE_BYTES:
        raise DocumentGenerationError("Tayyor fayl hajmi xavfsiz chegaradan oshdi.")
    mime, extension = FORMATS[fmt]
    filename = f"{_slug(data['title'])}{extension}"
    return {"filename": filename, "mime_type": mime, "content": content,
            "format": fmt, "title": data["title"], "missing_fields": data["missing_fields"]}


__all__ = ["DocumentGenerationError", "generate_document"]
