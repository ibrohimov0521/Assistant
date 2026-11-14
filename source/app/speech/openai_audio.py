from __future__ import annotations

from pathlib import Path

import httpx

from app.core.config import Settings

_TRANSCRIPT_PROMPT = (
    "O‘zbekcha, ruscha yoki inglizcha nutqni aynan aytilganidek yoz. "
    "Ismlar, manzillar, inkorlar, chat nomlari, sana, vaqt va raqamlarni saqla. "
    "Tarjima qilma. O‘zbekcha gap lotin yozuvida bo‘lsa, lotinda yoz."
)
_CONTENT_TYPES = {
    ".oga": "audio/ogg",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".webm": "audio/webm",
    ".flac": "audio/flac",
}


class OpenAITranscriber:
    """Transcribe bounded audio files with the configured OpenAI API key."""

    def __init__(self, settings: Settings, *,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(90.0, connect=10.0),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    async def transcribe_file(self, path: Path, *, timeout_seconds: int | None = None) -> dict:
        if not self.settings.openai_api_key:
            raise RuntimeError("openai_key_missing")
        if not path.is_file() or path.is_symlink():
            raise RuntimeError("audio_file_unavailable")
        size = path.stat().st_size
        if size <= 0 or size > self.settings.stt_max_file_bytes:
            raise RuntimeError("audio_size_limit")
        content_type = _CONTENT_TYPES.get(path.suffix.casefold())
        if not content_type:
            raise RuntimeError("audio_format_unsupported")
        headers = {"Authorization": f"Bearer {self.settings.openai_api_key}"}
        data = {"model": "gpt-transcribe", "prompt": _TRANSCRIPT_PROMPT}
        request_options = {}
        if timeout_seconds is not None:
            request_options["timeout"] = httpx.Timeout(
                max(15.0, min(900.0, timeout_seconds)), connect=10.0,
            )
        with path.open("rb") as audio_file:
            response = await self.client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers=headers,
                data=data,
                files={"file": (path.name, audio_file, content_type)},
                **request_options,
            )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("OpenAI transcription response is invalid")
        text = " ".join(str(payload.get("text") or "").split())[:20_000]
        return {"text": text, "segments": [], "word_confidences": [], "confidence": None}

    async def close(self) -> None:
        await self.client.aclose()
