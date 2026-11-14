from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from typing import Any, Callable, Mapping
from urllib.parse import quote

from aiogram import Bot
from aiogram.types import BufferedInputFile

from app.core.config import Settings
from app.speech.uzbek_text import prepare_uzbek_speech

LOG = logging.getLogger(__name__)


class OpenAITTS:
    """Generate short voice replies with OpenAI Realtime and encode Telegram Opus audio."""

    def __init__(self, settings: Settings, *,
                 websocket_connector: Callable[..., Any] | None = None,
                 encoder: Callable[[bytes], bytes] | None = None) -> None:
        self.settings = settings
        self._websocket_connector = websocket_connector
        self._encoder = encoder

    async def synthesize_ogg(self, text: str, *,
                             pronunciations: Mapping[str, str] | None = None,
                             speed: str = "normal") -> bytes:
        if not self.settings.openai_api_key:
            raise RuntimeError("openai_key_missing")
        cleaned = prepare_uzbek_speech(text, pronunciations)
        if not cleaned:
            raise ValueError("empty_tts_text")
        if len(cleaned) > 4000:
            cleaned = cleaned[:4000].rsplit(" ", 1)[0].rstrip(" ,:;—-")
        if speed not in {"slow", "normal", "fast"}:
            speed = "normal"
        connector = self._websocket_connector
        if connector is None:
            from websockets.asyncio.client import connect
            connector = connect
        model = quote(self.settings.openai_tts_model.strip(), safe="")
        url = f"wss://api.openai.com/v1/realtime?model={model}"
        headers = {"Authorization": f"Bearer {self.settings.openai_api_key}"}
        pcm = bytearray()
        session_configured = False
        response_requested = False
        try:
            async with asyncio.timeout(60):
                async with connector(url, additional_headers=headers, open_timeout=15,
                                     close_timeout=5, max_size=2 * 1024 * 1024,
                                     max_queue=8) as websocket:
                    async for raw_event in websocket:
                        event = json.loads(raw_event)
                        if not isinstance(event, dict):
                            continue
                        event_type = event.get("type")
                        if event_type == "error":
                            raise RuntimeError("openai_realtime_tts_error")
                        if event_type == "session.created" and not session_configured:
                            await websocket.send(json.dumps({
                                "type": "session.update",
                                "session": {
                                    "type": "realtime",
                                    "output_modalities": ["audio"],
                                    "instructions": (
                                        "Speak the supplied Uzbek text exactly as written. Do not answer it, "
                                        "translate it, summarize it, or add any words. Use a calm, clear "
                                        "assistant voice and pronounce Uzbek names carefully."
                                    ),
                                    "audio": {"output": {
                                        "format": {"type": "audio/pcm", "rate": 24000},
                                        "voice": self.settings.openai_tts_voice,
                                        "speed": {"slow": 0.92, "normal": 1.0, "fast": 1.08}[speed],
                                    }},
                                },
                            }, ensure_ascii=False))
                            session_configured = True
                            continue
                        if event_type == "session.updated" and session_configured and not response_requested:
                            await websocket.send(json.dumps({
                                "type": "response.create",
                                "response": {
                                    "output_modalities": ["audio"],
                                    "conversation": "none",
                                    "max_output_tokens": 2048,
                                    "input": [{
                                        "type": "message",
                                        "role": "user",
                                        "content": [{"type": "input_text", "text": cleaned}],
                                    }],
                                },
                            }, ensure_ascii=False))
                            response_requested = True
                            continue
                        if event_type == "response.output_audio.delta":
                            chunk = base64.b64decode(str(event.get("delta") or ""), validate=True)
                            pcm.extend(chunk)
                            if len(pcm) > 20 * 1024 * 1024:
                                raise RuntimeError("tts_audio_size_invalid")
                            continue
                        if event_type == "response.done":
                            response = event.get("response")
                            if not isinstance(response, dict) or response.get("status") != "completed":
                                raise RuntimeError("openai_realtime_tts_incomplete")
                            break
        except asyncio.TimeoutError as exc:
            raise RuntimeError("openai_realtime_tts_timeout") from exc
        if not response_requested or not pcm:
            raise RuntimeError("openai_realtime_tts_empty")
        if self._encoder:
            audio = self._encoder(bytes(pcm))
        else:
            audio = await asyncio.to_thread(_encode_ogg_opus, bytes(pcm))
        if not audio or len(audio) > 20 * 1024 * 1024 or not audio.startswith(b"OggS"):
            raise RuntimeError("tts_audio_size_invalid")
        return audio

    async def close(self) -> None:
        return None


def _encode_ogg_opus(pcm: bytes) -> bytes:
    import subprocess

    process = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "s16le",
         "-ar", "24000", "-ac", "1", "-i", "pipe:0", "-c:a", "libopus",
         "-application", "voip", "-b:a", "32k", "-f", "ogg", "pipe:1"],
        input=pcm, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=30, check=False,
    )
    if process.returncode or not process.stdout.startswith(b"OggS"):
        raise RuntimeError("tts_audio_encoding_failed")
    return process.stdout


def _voice_excerpt(text: str, length: str, *, text_also_sent: bool) -> str:
    limits = {"short": (3, 320), "normal": (8, 900), "detailed": (20, 1600)}
    max_sentences, max_chars = limits.get(length, limits["normal"])
    source = " ".join(text.split())
    sentences = re.split(r"(?<=[.!?])\s+", source)
    if len(source) <= max_chars and len(sentences) <= max_sentences:
        return source
    kept: list[str] = []
    for sentence in sentences:
        if not sentence:
            continue
        candidate = " ".join([*kept, sentence])
        if len(kept) >= max_sentences or len(candidate) > max_chars:
            break
        kept.append(sentence)
    if not kept:
        clipped = source[:max_chars].rsplit(" ", 1)[0].rstrip(" ,:;—-")
        kept = [clipped + ("…" if clipped else "")]
    excerpt = " ".join(kept)
    if text_also_sent and len(excerpt) < len(source):
        excerpt += " Batafsil javobni matnda ko‘rishingiz mumkin."
    return excerpt


async def send_assistant_message(bot: Bot, chat_id: int, text: str, tts: OpenAITTS | None,
                                *, reply_markup=None, mode: str = "both",
                                pronunciations: Mapping[str, str] | None = None,
                                speed: str = "normal", voice_length: str = "normal") -> None:
    if mode not in {"text", "voice", "both"}:
        mode = "both"
    sent = None
    if mode in {"text", "both"}:
        sent = await bot.send_message(chat_id, text[:3900], disable_web_page_preview=True,
                                      reply_markup=reply_markup)
    if mode == "text":
        return
    if not tts:
        if sent is None:
            await bot.send_message(chat_id, text[:3700] + "\n\nOvozli javob xizmati hozircha mavjud emas.",
                                   disable_web_page_preview=True, reply_markup=reply_markup)
        return
    try:
        voice_text = _voice_excerpt(text, voice_length, text_also_sent=mode == "both")
        audio = await tts.synthesize_ogg(voice_text, pronunciations=pronunciations, speed=speed)
        kwargs = {"reply_to_message_id": sent.message_id} if sent else {}
        await bot.send_voice(chat_id, BufferedInputFile(audio, filename="javob.ogg"),
                             caption="Ovoz sun’iy intellekt tomonidan yaratildi.", **kwargs)
    except Exception as exc:
        LOG.warning("OpenAI voice reply failed category=%s", type(exc).__name__)
        if sent is None:
            await bot.send_message(chat_id, text[:3700] + "\n\nOvozli javobni tayyorlay olmadim.",
                                   disable_web_page_preview=True, reply_markup=reply_markup)
