from __future__ import annotations

import csv
import asyncio
import hashlib
import io
import unicodedata
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Iterable

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from openpyxl import load_workbook
from pypdf import PdfReader
import httpx


MAX_DOCUMENT_BYTES = 15 * 1024 * 1024
MAX_DOCUMENT_CHARS = 360_000
MAX_DOCUMENT_CHUNKS = 200
CHUNK_CHARS = 1_700
MAX_PDF_PAGES = 100
MAX_SHEET_ROWS = 10_000
MAX_SHEET_CELLS = 50_000
MAX_SHEETS = 30
MAX_ZIP_UNCOMPRESSED = 50 * 1024 * 1024
MAX_ZIP_ENTRIES = 4_000


class DocumentError(ValueError):
    def __init__(self, code: str):
        self.code = code
        messages = {
            "empty": "Fayl bo‘sh ekan.",
            "size": "Fayl 15 MB dan katta. Kichikroq nusxasini yuboring.",
            "unsupported": "PDF, DOCX, XLSX, CSV, TXT yoki MD fayl yuboring.",
            "type_mismatch": "Fayl kengaytmasi va haqiqiy formati mos kelmadi.",
            "invalid": "Fayl buzilgan yoki noto‘g‘ri formatda.",
            "encrypted": "Parol bilan himoyalangan faylni ocholmayman.",
            "too_many_pages": "PDF 100 sahifadan oshmasligi kerak.",
            "too_many_rows": "Jadval 10 000 qatordan oshmasligi kerak.",
            "too_many_cells": "Jadvalda 50 000 dan ortiq katak bor.",
            "too_many_sheets": "Excel faylida 30 tadan ortiq varaq bor.",
            "archive_limit": "Office faylining ichki hajmi xavfsiz chegaradan oshdi.",
            "no_text": "Faylda o‘qiladigan matn topilmadi.",
            "scanned_pdf": "PDF skanerlangan; sahifa rasmlarini aniqroq yoki alohida yuboring.",
            "ocr_unavailable": "Skanerlangan PDFni OCR qilish uchun server vositasi mavjud emas.",
            "scan_pages": "Skanerlangan PDF 20 sahifadan oshmasligi kerak.",
            "ocr_timeout": "Skanerlangan PDFni OCR qilish uzoq davom etdi. Kichikroq fayl yuboring.",
            "text_limit": "Fayl matni indekslash chegarasidan katta.",
        }
        super().__init__(messages.get(code, "Faylni o‘qiy olmadim."))


@dataclass(frozen=True)
class DocumentBlock:
    location: str
    text: str


@dataclass(frozen=True)
class ParsedDocument:
    filename: str
    media_type: str
    sha256: str
    blocks: list[DocumentBlock]
    scanned_pages: list[int] = field(default_factory=list)


def _clean_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = "".join(char for char in text if char in "\n\t" or char.isprintable())
    return "\n".join(" ".join(line.split()) for line in text.splitlines()).strip()


def _safe_zip(data: bytes, *, expected: set[str]) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        entries = archive.infolist()
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise DocumentError("invalid") from exc
    if len(entries) > MAX_ZIP_ENTRIES or not expected.issubset(set(archive.namelist())):
        archive.close()
        raise DocumentError("invalid")
    total = 0
    for entry in entries:
        path = PurePosixPath(entry.filename)
        if path.is_absolute() or ".." in path.parts or entry.flag_bits & 0x1:
            archive.close()
            raise DocumentError("invalid")
        total += entry.file_size
        if total > MAX_ZIP_UNCOMPRESSED:
            archive.close()
            raise DocumentError("archive_limit")
        if entry.file_size > 1_000_000 and entry.file_size / max(1, entry.compress_size) > 100:
            archive.close()
            raise DocumentError("archive_limit")
    return archive


def _pdf_blocks(data: bytes) -> tuple[list[DocumentBlock], list[int]]:
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise DocumentError("encrypted")
        if not 1 <= len(reader.pages) <= MAX_PDF_PAGES:
            raise DocumentError("too_many_pages")
        blocks = []
        scanned_pages = []
        for index, page in enumerate(reader.pages, start=1):
            text = _clean_text(page.extract_text() or "")
            if text:
                blocks.append(DocumentBlock(f"PDF, sahifa {index}", text))
            else:
                scanned_pages.append(index)
        if not blocks:
            raise DocumentError("scanned_pdf")
        return blocks, scanned_pages
    except DocumentError:
        raise
    except Exception as exc:
        raise DocumentError("invalid") from exc


def _docx_blocks(data: bytes) -> list[DocumentBlock]:
    archive = _safe_zip(data, expected={"[Content_Types].xml", "word/document.xml"})
    try:
        if any(name.casefold().endswith("vbaproject.bin") for name in archive.namelist()):
            raise DocumentError("unsupported")
        document = Document(io.BytesIO(data))
    except DocumentError:
        raise
    except Exception as exc:
        raise DocumentError("invalid") from exc
    finally:
        archive.close()
    blocks: list[DocumentBlock] = []
    paragraph_number = 0
    table_number = 0
    for child in document.element.body.iterchildren():
        if child.tag.endswith("}p"):
            paragraph_number += 1
            paragraph = Paragraph(child, document)
            text = _clean_text(paragraph.text)
            if text:
                style = str(getattr(paragraph.style, "name", "") or "")[:60]
                label = f"Bo‘lim: {text[:100]}" if style.casefold().startswith("heading") else f"Abzats {paragraph_number}"
                blocks.append(DocumentBlock(label, text))
        elif child.tag.endswith("}tbl"):
            table_number += 1
            table = Table(child, document)
            for row_number, row in enumerate(table.rows, start=1):
                cells = [_clean_text(cell.text) for cell in row.cells]
                rendered = " | ".join(f"Ustun {index}: {value}" for index, value in enumerate(cells, 1) if value)
                if rendered:
                    blocks.append(DocumentBlock(f"Jadval {table_number}, qator {row_number}", rendered))
    return blocks


def _xlsx_blocks(data: bytes) -> list[DocumentBlock]:
    archive = _safe_zip(data, expected={"[Content_Types].xml", "xl/workbook.xml"})
    archive.close()
    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise DocumentError("invalid") from exc
    blocks: list[DocumentBlock] = []
    cells_seen = 0
    rows_seen = 0
    try:
        if len(workbook.worksheets) > MAX_SHEETS:
            raise DocumentError("too_many_sheets")
        for sheet in workbook.worksheets:
            if sheet.max_row and sheet.max_row > MAX_SHEET_ROWS:
                raise DocumentError("too_many_rows")
            if sheet.max_column and sheet.max_column > 500:
                raise DocumentError("too_many_cells")
            for row_number, row in enumerate(sheet.iter_rows(values_only=True), start=1):
                rows_seen += 1
                if rows_seen > MAX_SHEET_ROWS:
                    raise DocumentError("too_many_rows")
                rendered = []
                for column_number, value in enumerate(row, start=1):
                    cells_seen += 1
                    if cells_seen > MAX_SHEET_CELLS:
                        raise DocumentError("too_many_cells")
                    text = _clean_text(value)
                    if text:
                        rendered.append(f"{_column_name(column_number)}{row_number}: {text}")
                if rendered:
                    blocks.append(DocumentBlock(f"Varaq {sheet.title}, qator {row_number}", " | ".join(rendered)))
    except DocumentError:
        raise
    except Exception as exc:
        raise DocumentError("invalid") from exc
    finally:
        workbook.close()
    return blocks


def _column_name(value: int) -> str:
    name = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        name = chr(65 + remainder) + name
    return name


def _csv_blocks(data: bytes) -> list[DocumentBlock]:
    try:
        text = data.decode("utf-8-sig", errors="strict")
        sample = text[:8192]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        rows = list(csv.reader(io.StringIO(text), dialect))
    except (UnicodeDecodeError, csv.Error) as exc:
        raise DocumentError("invalid") from exc
    if len(rows) > MAX_SHEET_ROWS:
        raise DocumentError("too_many_rows")
    if any(len(row) > 500 for row in rows):
        raise DocumentError("too_many_cells")
    headers = [" ".join(value.split()) for value in rows[0]] if rows else []
    blocks = []
    for row_number, row in enumerate(rows, start=1):
        rendered = []
        for column_number, value in enumerate(row, start=1):
            value = _clean_text(value)
            if value:
                label = headers[column_number - 1] if column_number <= len(headers) and headers[column_number - 1] else f"Ustun {_column_name(column_number)}"
                rendered.append(f"{label}: {value}")
        if rendered:
            blocks.append(DocumentBlock(f"CSV qatori {row_number}", " | ".join(rendered)))
    return blocks


def _plain_text_blocks(data: bytes) -> list[DocumentBlock]:
    try:
        text = data.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise DocumentError("invalid") from exc
    if not text.strip():
        return []
    lines = text.splitlines()
    return [DocumentBlock(f"Matn, qator {start + 1}–{min(start + 25, len(lines))}", "\n".join(lines[start:start + 25]))
            for start in range(0, len(lines), 25) if "\n".join(lines[start:start + 25]).strip()]


def chunk_document_blocks(blocks: Iterable[DocumentBlock]) -> list[dict[str, str]]:
    chunks: list[dict[str, str]] = []
    pending_text: list[str] = []
    pending_locations: list[str] = []
    pending_chars = 0

    def flush() -> None:
        nonlocal pending_chars
        if pending_text:
            locations = pending_locations
            location = locations[0] if len(locations) == 1 else f"{locations[0]} – {locations[-1]}"
            chunks.append({"location": location[:120], "text": "\n".join(pending_text)[:CHUNK_CHARS]})
        pending_text.clear()
        pending_locations.clear()
        pending_chars = 0

    for block in blocks:
        text = _clean_text(block.text)
        while text:
            if len(text) > CHUNK_CHARS:
                split_at = text.rfind(" ", 0, CHUNK_CHARS)
                if split_at < CHUNK_CHARS // 2:
                    split_at = CHUNK_CHARS
                part, text = text[:split_at].strip(), text[split_at:].strip()
            else:
                part, text = text, ""
            if not part:
                continue
            if pending_chars + len(part) + 1 > CHUNK_CHARS:
                flush()
            pending_text.append(part)
            if not pending_locations or pending_locations[-1] != block.location:
                pending_locations.append(block.location)
            pending_chars += len(part) + 1
            if len(chunks) >= MAX_DOCUMENT_CHUNKS:
                raise DocumentError("text_limit")
    flush()
    total_chars = sum(len(chunk["text"]) for chunk in chunks)
    if total_chars > MAX_DOCUMENT_CHARS:
        raise DocumentError("text_limit")
    if not chunks:
        raise DocumentError("no_text")
    return chunks


def parse_document(filename: str, mime_type: str | None, data: bytes) -> ParsedDocument:
    name = " ".join(str(filename).replace("\\", "/").split("/")[-1].split())[:160]
    if not data:
        raise DocumentError("empty")
    if len(data) > MAX_DOCUMENT_BYTES:
        raise DocumentError("size")
    suffix = PurePosixPath(name).suffix.casefold()
    scanned_pages: list[int] = []
    if suffix == ".pdf" and data.startswith(b"%PDF-"):
        media_type = "application/pdf"
        blocks, scanned_pages = _pdf_blocks(data)
    elif suffix == ".docx" and data.startswith(b"PK"):
        media_type, blocks = "application/vnd.openxmlformats-officedocument.wordprocessingml.document", _docx_blocks(data)
    elif suffix == ".xlsx" and data.startswith(b"PK"):
        media_type, blocks = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", _xlsx_blocks(data)
    elif suffix == ".csv":
        media_type, blocks = "text/csv", _csv_blocks(data)
    elif suffix in {".txt", ".md"}:
        media_type, blocks = ("text/markdown" if suffix == ".md" else "text/plain"), _plain_text_blocks(data)
    else:
        raise DocumentError("unsupported")
    if mime_type:
        supplied = str(mime_type).split(";", 1)[0].strip().casefold()
        aliases = {
            "application/pdf": {"application/pdf", "application/x-pdf"},
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": {
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "application/zip"},
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "application/zip", "application/vnd.ms-excel"},
            "text/csv": {"text/csv", "application/csv", "application/vnd.ms-excel"},
            "text/plain": {"text/plain"},
            "text/markdown": {"text/markdown", "text/plain"},
        }
        if supplied not in {"application/octet-stream", "binary/octet-stream", *aliases.get(media_type, {media_type})}:
            raise DocumentError("type_mismatch")
    if not blocks:
        raise DocumentError("no_text")
    chunks = chunk_document_blocks(blocks)
    return ParsedDocument(name, media_type, hashlib.sha256(data).hexdigest(),
                          [DocumentBlock(chunk["location"], chunk["text"]) for chunk in chunks],
                          scanned_pages)


__all__ = ["DocumentBlock", "DocumentError", "ParsedDocument", "chunk_document_blocks", "parse_document",
           "download_telegram_document", "ocr_scanned_pdf", "MAX_DOCUMENT_BYTES", "MAX_DOCUMENT_CHUNKS"]


async def download_telegram_document(bot, file_id: str, file_size: int | None) -> bytes:
    if file_size is not None and file_size > MAX_DOCUMENT_BYTES:
        raise DocumentError("size")
    file_info = await bot.get_file(file_id)
    if not file_info.file_path:
        raise DocumentError("invalid")
    file_url = bot.session.api.file_url(bot.token, file_info.file_path)
    chunks: list[bytes] = []
    total = 0
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20), follow_redirects=False,
                                     trust_env=False) as client:
            async with client.stream("GET", file_url) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes(65536):
                    total += len(chunk)
                    if total > MAX_DOCUMENT_BYTES:
                        raise DocumentError("size")
                    chunks.append(chunk)
    except DocumentError:
        raise
    except (httpx.HTTPError, OSError) as exc:
        raise DocumentError("invalid") from exc
    if not total:
        raise DocumentError("empty")
    return b"".join(chunks)


async def ocr_scanned_pdf(data: bytes, pages: list[int] | None = None) -> list[dict[str, str]]:
    try:
        page_count = len(PdfReader(io.BytesIO(data), strict=False).pages)
    except Exception as exc:
        raise DocumentError("invalid") from exc
    if not 1 <= page_count <= MAX_PDF_PAGES:
        raise DocumentError("too_many_pages")
    requested_pages = sorted(set(pages or range(1, page_count + 1)))
    if len(requested_pages) > 20 or any(page < 1 or page > page_count for page in requested_pages):
        raise DocumentError("scan_pages")
    from app.speech.ocr import MAX_IMAGE_BYTES, recognize_image

    blocks = []
    deadline = asyncio.get_running_loop().time() + 90
    try:
        with TemporaryDirectory(prefix="assistant-pdf-ocr-") as temporary:
            root = Path(temporary)
            pdf_path = root / "input.pdf"
            pdf_path.write_bytes(data)
            for page_number in requested_pages:
                output_prefix = root / f"page-{page_number}"
                process = await asyncio.create_subprocess_exec(
                    "pdftoppm", "-f", str(page_number), "-l", str(page_number), "-singlefile",
                    "-png", "-r", "110", "-scale-to", "1800", str(pdf_path), str(output_prefix),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                try:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise asyncio.TimeoutError
                    await asyncio.wait_for(process.wait(), timeout=min(25, remaining))
                except asyncio.TimeoutError as exc:
                    process.kill()
                    await process.wait()
                    raise DocumentError("ocr_timeout") from exc
                image_path = output_prefix.with_suffix(".png")
                if process.returncode != 0 or not image_path.is_file():
                    raise DocumentError("invalid")
                if image_path.stat().st_size > MAX_IMAGE_BYTES:
                    raise DocumentError("size")
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise DocumentError("ocr_timeout")
                try:
                    result = await asyncio.wait_for(recognize_image(image_path.read_bytes()),
                                                    timeout=min(25, remaining))
                except asyncio.TimeoutError as exc:
                    raise DocumentError("ocr_timeout") from exc
                if result.text:
                    blocks.append(DocumentBlock(f"PDF OCR, sahifa {page_number}", result.text))
                image_path.unlink(missing_ok=True)
    except FileNotFoundError as exc:
        raise DocumentError("ocr_unavailable") from exc
    if not blocks:
        raise DocumentError("no_text")
    return chunk_document_blocks(blocks)
