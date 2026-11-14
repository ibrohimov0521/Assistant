from __future__ import annotations

import difflib
import mimetypes
import re
import stat
import unicodedata
import zipfile
from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import PurePosixPath
from typing import Any

from app.core.documents import (MAX_DOCUMENT_BYTES, MAX_ZIP_UNCOMPRESSED, DocumentBlock,
                                DocumentError, ParsedDocument, ocr_scanned_pdf, parse_document)


MAX_COMPARE_ARCHIVE_BYTES = 15 * 1024 * 1024
MAX_COMPARE_FILES = 2
MAX_DIFF_ENTRIES = 200
MAX_REPORT_CHARS = 30_000
_ALLOWED_SUFFIXES = {".pdf", ".docx", ".xlsx", ".csv", ".txt", ".md"}
_NUMBER = r"\d(?:[\d\s\u00a0,.]*\d)?"
_CURRENCY = r"(?:UZS|USD|EUR|RUB|GBP|JPY|KZT|TRY|so['’ʻ`]?m|sum|сум|dollar(?:s)?)"
_AMOUNT_RE = re.compile(
    rf"(?:[$€£¥]\s*{_NUMBER}|\b{_CURRENCY}\s*{_NUMBER}|\b{_NUMBER}\s*{_CURRENCY}\b)",
    re.IGNORECASE,
)
_MONTHS = ("yanvar|fevral|mart|aprel|may|iyun|iyul|avgust|sentabr|oktabr|noyabr|dekabr|"
           "january|february|march|april|may|june|july|august|september|october|november|december|"
           "января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря")
_DATE_RE = re.compile(
    rf"(?i)(?<!\d)(?:\d{{4}}[./-]\d{{1,2}}[./-]\d{{1,2}}|"
    rf"\d{{1,2}}[./-]\d{{1,2}}[./-]\d{{2,4}}|\d{{1,2}}\s+(?:{_MONTHS})\s+\d{{4}})(?!\d)"
)
_DURATION_RE = re.compile(
    r"(?i)\b\d[\d.,]*\s*(?:ta\s+)?(?:ish\s+kun(?:i|lari)?|bank\s+kun(?:i|lari)?|"
    r"kun(?:i|lari)?|days?|weeks?|oy(?:ga|lik)?|months?|yil(?:ga)?|years?)\b"
)


class DocumentCompareError(ValueError):
    pass


@dataclass(frozen=True)
class Change:
    kind: str
    old: DocumentBlock | None = None
    new: DocumentBlock | None = None


def _safe_archive_documents(data: bytes) -> list[tuple[str, bytes]]:
    if not data:
        raise DocumentCompareError("ZIP arxiv bo‘sh.")
    if len(data) > MAX_COMPARE_ARCHIVE_BYTES:
        raise DocumentCompareError("Taqqoslash arxivi 15 MB dan katta.")
    try:
        archive = zipfile.ZipFile(BytesIO(data))
        entries = archive.infolist()
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise DocumentCompareError("ZIP arxivni ocholmadim.") from exc
    try:
        if len(entries) != MAX_COMPARE_FILES:
            raise DocumentCompareError("ZIP ichida aynan 2 ta hujjat bo‘lishi kerak: avval eski, keyin yangi.")
        total_size = 0
        filenames: set[str] = set()
        result: list[tuple[str, bytes]] = []
        for entry in entries:
            name = entry.filename
            path = PurePosixPath(name)
            mode = entry.external_attr >> 16
            if (not name or "\\" in name or path.is_absolute() or ".." in path.parts
                    or entry.is_dir() or entry.flag_bits & 0x1 or stat.S_ISLNK(mode)):
                raise DocumentCompareError("ZIP ichida xavfsiz bo‘lmagan fayl yo‘li yoki havola bor.")
            suffix = PurePosixPath(path.name).suffix.casefold()
            if suffix not in _ALLOWED_SUFFIXES:
                raise DocumentCompareError("ZIP ichida PDF, DOCX, XLSX, CSV, TXT yoki MD fayllari bo‘lishi mumkin.")
            filename = PurePosixPath(path.name).name[:160]
            if filename.casefold() in filenames:
                raise DocumentCompareError("ZIP ichida bir xil nomli hujjatlar bor; fayl nomlarini farqlang.")
            filenames.add(filename.casefold())
            if entry.file_size > MAX_DOCUMENT_BYTES:
                raise DocumentCompareError(f"{filename} 15 MB dan katta.")
            total_size += entry.file_size
            if total_size > MAX_ZIP_UNCOMPRESSED:
                raise DocumentCompareError("ZIP ichidagi jami ochilgan hajm 50 MB dan oshdi.")
            if entry.file_size > 1_000_000 and entry.file_size / max(1, entry.compress_size) > 100:
                raise DocumentCompareError("ZIP siqish nisbati xavfsiz chegaradan oshdi.")
            try:
                with archive.open(entry, "r") as stream:
                    content = stream.read(MAX_DOCUMENT_BYTES + 1)
            except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                raise DocumentCompareError(f"{filename} faylini arxivdan o‘qiy olmadim.") from exc
            if len(content) != entry.file_size or len(content) > MAX_DOCUMENT_BYTES:
                raise DocumentCompareError(f"{filename} faylining hajmi yoki ichki ma’lumoti yaroqsiz.")
            result.append((filename, content))
        return result
    finally:
        archive.close()


async def _parse_comparison_document(filename: str, content: bytes) -> ParsedDocument:
    mime_type = mimetypes.guess_type(filename)[0]
    try:
        parsed = parse_document(filename, mime_type, content)
    except DocumentError as exc:
        if exc.code != "scanned_pdf" or not filename.casefold().endswith(".pdf"):
            raise DocumentCompareError(str(exc)) from exc
        try:
            scanned = await ocr_scanned_pdf(content)
        except (DocumentError, OSError, RuntimeError, ValueError) as ocr_error:
            raise DocumentCompareError(str(ocr_error)) from ocr_error
        parsed = ParsedDocument(filename, "application/pdf", sha256(content).hexdigest(),
                                 [DocumentBlock(item["location"], item["text"]) for item in scanned])
        return parsed
    if parsed.scanned_pages:
        try:
            scanned = await ocr_scanned_pdf(content, pages=parsed.scanned_pages)
        except (DocumentError, OSError, RuntimeError, ValueError) as exc:
            raise DocumentCompareError(str(exc)) from exc
        parsed = ParsedDocument(parsed.filename, parsed.media_type, parsed.sha256,
                                [*parsed.blocks,
                                 *(DocumentBlock(item["location"], item["text"]) for item in scanned)])
    if PurePosixPath(filename).suffix.casefold() in {".txt", ".md"}:
        lines = content.decode("utf-8-sig", errors="strict").splitlines()
        blocks = [DocumentBlock(f"Matn qatori {line_number}", line.strip())
                  for line_number, line in enumerate(lines, start=1) if line.strip()]
        parsed = ParsedDocument(parsed.filename, parsed.media_type, parsed.sha256, blocks)
    return parsed


def _normalized(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _changes(old: list[DocumentBlock], new: list[DocumentBlock]) -> list[Change]:
    old_keys = [_normalized(item.text) for item in old]
    new_keys = [_normalized(item.text) for item in new]
    matcher = difflib.SequenceMatcher(None, old_keys, new_keys, autojunk=False)
    changes: list[Change] = []
    for opcode, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if opcode == "equal":
            continue
        old_group = old[old_start:old_end]
        new_group = new[new_start:new_end]
        if opcode == "replace":
            paired = min(len(old_group), len(new_group))
            for index in range(paired):
                if _normalized(old_group[index].text) != _normalized(new_group[index].text):
                    similarity = difflib.SequenceMatcher(
                        None, old_keys[old_start + index], new_keys[new_start + index],
                        autojunk=False).ratio()
                    if similarity >= 0.35:
                        changes.append(Change("changed", old_group[index], new_group[index]))
                    else:
                        changes.append(Change("removed", old_group[index], None))
                        changes.append(Change("added", None, new_group[index]))
            changes.extend(Change("removed", item, None) for item in old_group[paired:])
            changes.extend(Change("added", None, item) for item in new_group[paired:])
        elif opcode == "delete":
            changes.extend(Change("removed", item, None) for item in old_group)
        elif opcode == "insert":
            changes.extend(Change("added", None, item) for item in new_group)
        if len(changes) >= MAX_DIFF_ENTRIES:
            return changes[:MAX_DIFF_ENTRIES]
    return changes


def _mention_changes(old_text: str, new_text: str, pattern: re.Pattern[str]) -> list[tuple[str, str]]:
    old_values = pattern.findall(old_text)
    new_values = pattern.findall(new_text)
    old_remaining = Counter(_normalized(value) for value in old_values)
    new_remaining = Counter(_normalized(value) for value in new_values)
    matched = old_remaining & new_remaining
    old_remaining.subtract(matched)
    new_remaining.subtract(matched)
    removed: list[str] = []
    added: list[str] = []
    for value in old_values:
        normalized = _normalized(value)
        if old_remaining[normalized] > 0:
            removed.append(value)
            old_remaining[normalized] -= 1
    for value in new_values:
        normalized = _normalized(value)
        if new_remaining[normalized] > 0:
            added.append(value)
            new_remaining[normalized] -= 1
    pairs: list[tuple[str, str]] = []
    paired = min(len(removed), len(added))
    pairs.extend(zip(removed[:paired], added[:paired]))
    pairs.extend((value, "—") for value in removed[paired:])
    pairs.extend(("—", value) for value in added[paired:])
    return pairs


def _report(old: ParsedDocument, new: ParsedDocument) -> tuple[str, dict[str, Any]]:
    changes = _changes(old.blocks, new.blocks)
    changed = sum(item.kind == "changed" for item in changes)
    added = sum(item.kind == "added" for item in changes)
    removed = sum(item.kind == "removed" for item in changes)
    old_text = "\n".join(item.old.text for item in changes if item.old)
    new_text = "\n".join(item.new.text for item in changes if item.new)
    amount_changes = _mention_changes(old_text, new_text, _AMOUNT_RE)
    deadline_changes = _mention_changes(old_text, new_text, _DATE_RE)
    deadline_changes.extend(_mention_changes(old_text, new_text, _DURATION_RE))

    lines = [f"Hujjatlar farqi: {old.filename} → {new.filename}",
             f"O‘zgargan band: {changed} · qo‘shilgan: {added} · o‘chirilgan: {removed}"]
    if amount_changes:
        lines.extend(["", "Summa o‘zgarishlari:"])
        lines.extend(f"• {before} → {after}" for before, after in amount_changes[:30])
    if deadline_changes:
        lines.extend(["", "Sana yoki muddat o‘zgarishlari:"])
        lines.extend(f"• {before} → {after}" for before, after in deadline_changes[:30])
    if not changes:
        lines.extend(["", "Ikki hujjatning matni bir xil."])
    else:
        lines.extend(["", "Batafsil farqlar:"])
        for change in changes:
            if change.kind == "changed" and change.old and change.new:
                lines.append(f"\n[O‘ZGARDI] {change.old.location} → {change.new.location}")
                lines.append("Eski: " + change.old.text)
                lines.append("Yangi: " + change.new.text)
            elif change.kind == "added" and change.new:
                lines.append(f"\n[QO‘SHILDI] {change.new.location}")
                lines.append(change.new.text)
            elif change.kind == "removed" and change.old:
                lines.append(f"\n[O‘CHIRILDI] {change.old.location}")
                lines.append(change.old.text)
    if len(changes) >= MAX_DIFF_ENTRIES:
        lines.append(f"\nKo‘rsatish chegarasi: dastlabki {MAX_DIFF_ENTRIES} farq olindi.")
    text = "\n".join(lines)
    if len(text) > MAX_REPORT_CHARS:
        text = text[:MAX_REPORT_CHARS - 100] + "\n\nHisobot xavfsiz uzunlik chegarasida qisqartirildi."
    return text, {"changed": changed, "added": added, "removed": removed,
                  "amount_changes": amount_changes, "deadline_changes": deadline_changes}


async def compare_document_archive(data: bytes) -> dict[str, Any]:
    """Compare the first (old) and second (new) document inside a bounded ZIP archive."""
    files = _safe_archive_documents(data)
    old = await _parse_comparison_document(*files[0])
    new = await _parse_comparison_document(*files[1])
    report, stats = _report(old, new)
    safe_old = PurePosixPath(old.filename).stem[:45]
    safe_new = PurePosixPath(new.filename).stem[:45]
    filename = f"farq_{safe_old}_{safe_new}.txt"
    return {"old_filename": old.filename, "new_filename": new.filename,
            "report_filename": filename, "report": report, **stats}


__all__ = ["DocumentCompareError", "compare_document_archive"]
