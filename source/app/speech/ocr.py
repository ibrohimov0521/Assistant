from __future__ import annotations

import asyncio
import csv
import io
from dataclasses import dataclass

import httpx

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_OCR_CHARS = 20_000
OCR_TIMEOUT_SECONDS = 25
OCR_LANGUAGES = "eng+rus+uzb+uzb_cyrl"


@dataclass(frozen=True)
class OCRResult:
    text: str
    mean_confidence: float | None


def image_format(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if data.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if data.startswith((b"II*\x00", b"MM\x00*")):
        return "TIFF"
    if data.startswith(b"BM"):
        return "BMP"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "GIF"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "WebP"
    return None


async def download_telegram_image(bot, file_id: str, file_size: int | None) -> bytes:
    if file_size is not None and file_size > MAX_IMAGE_BYTES:
        raise ValueError("image_size_limit")
    file_info = await bot.get_file(file_id)
    if not file_info.file_path:
        raise RuntimeError("telegram_file_unavailable")
    file_url = bot.session.api.file_url(bot.token, file_info.file_path)
    chunks = []
    total = 0
    async with httpx.AsyncClient(timeout=httpx.Timeout(15), follow_redirects=False,
                                 trust_env=False) as client:
        async with client.stream("GET", file_url) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes(65536):
                total += len(chunk)
                if total > MAX_IMAGE_BYTES:
                    raise ValueError("image_size_limit")
                chunks.append(chunk)
    if total == 0:
        raise RuntimeError("empty_image")
    return b"".join(chunks)


def _from_tsv(raw: str) -> OCRResult:
    grouped: dict[tuple[str, str, str, str], list[str]] = {}
    confidences: list[float] = []
    reader = csv.DictReader(io.StringIO(raw), delimiter="\t")
    for row in reader:
        if row.get("level") != "5":
            continue
        word = " ".join(str(row.get("text") or "").split())
        if not word:
            continue
        try:
            confidence = float(row.get("conf") or -1)
        except ValueError:
            confidence = -1
        if confidence >= 0:
            confidences.append(confidence)
        if 0 <= confidence < 55:
            word = f"[taxmin: {word}]"
        key = tuple(str(row.get(name) or "0") for name in
                    ("page_num", "block_num", "par_num", "line_num"))
        grouped.setdefault(key, []).append(word)
    text = "\n".join(" ".join(words) for _, words in sorted(grouped.items()))[:MAX_OCR_CHARS].strip()
    confidence = sum(confidences) / len(confidences) if confidences else None
    return OCRResult(text=text, mean_confidence=confidence)


async def recognize_image(data: bytes) -> OCRResult:
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("image_size_limit" if data else "empty_image")
    if image_format(data) is None:
        raise ValueError("unsupported_image_type")
    process = await asyncio.create_subprocess_exec(
        "tesseract", "stdin", "stdout", "-l", OCR_LANGUAGES, "--psm", "3", "tsv", "quiet",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL)
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(data), timeout=OCR_TIMEOUT_SECONDS)
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    if process.returncode != 0:
        raise RuntimeError("ocr_failed")
    return _from_tsv(stdout.decode("utf-8", errors="replace"))
