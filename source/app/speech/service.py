from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path

import httpx

from app.core.config import Settings
from app.speech.openai_audio import OpenAITranscriber


def cleanup_abandoned_media(directory: Path) -> None:
    if not directory.exists() or directory.is_symlink():
        return
    resolved = directory.resolve()
    for child in directory.iterdir():
        if child.name.startswith("voice-") and child.is_dir() and not child.is_symlink():
            if child.resolve().parent == resolved:
                shutil.rmtree(child)


async def transcribe_telegram_voice(bot, voice, settings: Settings,
                                    transcriber: OpenAITranscriber) -> str:
    result = await transcribe_telegram_voice_detailed(bot, voice, settings, transcriber)
    return str(result.get("text") or "")


async def transcribe_telegram_voice_detailed(bot, voice, settings: Settings,
                                             transcriber: OpenAITranscriber) -> dict:
    if not settings.enable_stt or not settings.openai_api_key or not settings.bot_token:
        raise RuntimeError("stt_disabled")
    if voice.duration <= 0 or voice.duration > settings.stt_max_seconds:
        raise RuntimeError("audio_duration_limit")
    if voice.file_size is not None and voice.file_size > settings.stt_max_file_bytes:
        raise RuntimeError("audio_size_limit")
    file_info = await bot.get_file(voice.file_id)
    if not file_info.file_path:
        raise RuntimeError("telegram_file_unavailable")

    settings.media_temp_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(settings.media_temp_dir, 0o700)
    except OSError:
        pass
    with tempfile.TemporaryDirectory(prefix="voice-", dir=settings.media_temp_dir) as temp:
        directory = Path(temp)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
        source = directory / "voice.oga"
        file_url = bot.session.api.file_url(bot.token, file_info.file_path)
        total = 0
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=8.0),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            async with client.stream("GET", file_url) as response:
                response.raise_for_status()
                with source.open("wb") as output:
                    async for chunk in response.aiter_bytes(65536):
                        total += len(chunk)
                        if total > settings.stt_max_file_bytes:
                            raise RuntimeError("audio_size_limit")
                        output.write(chunk)
        if total <= 0:
            raise RuntimeError("invalid_audio")
        timeout = max(settings.stt_timeout_seconds,
                      min(900, int(voice.duration * 2.5)))
        return await transcriber.transcribe_file(source, timeout_seconds=timeout)


def transcribe_file(path: Path, settings: Settings) -> str:
    if not settings.openai_api_key or not settings.enable_stt:
        raise RuntimeError("OpenAI audio transcription requires ENABLE_STT=1 and OPENAI_API_KEY")

    async def run() -> str:
        transcriber = OpenAITranscriber(settings)
        try:
            result = await transcriber.transcribe_file(path)
            return str(result.get("text") or "")
        finally:
            await transcriber.close()

    return asyncio.run(run())
