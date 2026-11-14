from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.core.telegram_calls import CallOutcome

LOG = logging.getLogger(__name__)
AudioProvider = Callable[[str], Awaitable[bytes]]


class PyTgCallsAdapter:
    """Deliver a locally synthesized OGG voice message through a user-profile call."""

    MAX_AUDIO_BYTES = 8_000_000

    def __init__(self, client, audio_provider: AudioProvider | None = None, *, app: Any | None = None):
        self.client = client
        self.audio_provider = audio_provider
        self.app = app
        self._call_config = None
        self._stream_ended = None
        self._call_busy = None
        self._call_declined = None
        self._call_discarded = None
        self._timed_out_answer = None
        self._started = False
        self._call_lock = asyncio.Lock()

    async def start(self) -> None:
        if not self._started:
            from pytgcalls import PyTgCalls
            from pytgcalls.exceptions import CallBusy, CallDeclined, CallDiscarded, TimedOutAnswer
            from pytgcalls.types import CallConfig, StreamEnded

            self.app = self.app or PyTgCalls(self.client)
            self._call_config = CallConfig
            self._stream_ended = StreamEnded
            self._call_busy = CallBusy
            self._call_declined = CallDeclined
            self._call_discarded = CallDiscarded
            self._timed_out_answer = TimedOutAnswer
            await self.app.start()
            self._started = True

    async def call(self, *, recipient_id: int, body: str,
                    timeout_seconds: int, audio_bytes: bytes | None = None) -> CallOutcome:
        if recipient_id <= 0 or not body.strip() or len(body) > 2000:
            return CallOutcome.FAILED
        if not 5 <= timeout_seconds <= 120:
            return CallOutcome.FAILED
        async with self._call_lock:
            if audio_bytes is None:
                if self.audio_provider is None:
                    return CallOutcome.FAILED
                try:
                    audio = await self.audio_provider(body)
                except Exception as exc:
                    LOG.warning("Call audio preparation failed category=%s", type(exc).__name__)
                    return CallOutcome.FAILED
            else:
                audio = audio_bytes
            if (not isinstance(audio, bytes) or not audio.startswith(b"OggS")
                    or len(audio) > self.MAX_AUDIO_BYTES):
                return CallOutcome.FAILED

            try:
                await self.start()
            except Exception as exc:
                LOG.warning("Telegram call engine could not start category=%s", type(exc).__name__)
                return CallOutcome.FAILED
            ended = asyncio.Event()

            async def on_update(_client, update) -> None:
                if (isinstance(update, self._stream_ended) and update.chat_id == recipient_id
                        and update.stream_type & self._stream_ended.Type.AUDIO):
                    ended.set()

            active = False
            handler_added = False
            try:
                with tempfile.TemporaryDirectory(prefix="assistant-call-") as temp_dir:
                    audio_path = Path(temp_dir) / "saida.ogg"
                    audio_path.write_bytes(audio)
                    self.app.add_handler(on_update)
                    handler_added = True
                    await self.app.play(recipient_id, audio_path,
                                        config=self._call_config(timeout=timeout_seconds))
                    active = True
                    playback_timeout = min(180, max(15, len(body) // 16 + 15))
                    try:
                        await asyncio.wait_for(ended.wait(), timeout=playback_timeout)
                    except asyncio.TimeoutError:
                        # The call connected, but audio completion could not be verified.
                        return CallOutcome.UNKNOWN
                    return CallOutcome.ANSWERED
            except self._timed_out_answer:
                return CallOutcome.NO_ANSWER
            except (self._call_busy, self._call_declined):
                return CallOutcome.REJECTED
            except self._call_discarded:
                return CallOutcome.UNKNOWN
            except Exception as exc:
                LOG.warning("Telegram call failed category=%s", type(exc).__name__)
                return CallOutcome.UNKNOWN
            finally:
                if handler_added:
                    self.app.remove_handler(on_update)
                if active:
                    try:
                        await self.app.leave_call(recipient_id)
                    except Exception as exc:
                        LOG.info("Telegram call cleanup ended category=%s", type(exc).__name__)

    async def stop(self) -> None:
        if self._started and self.app is not None:
            try:
                await self.app.stop()
                self._started = False
            except Exception as exc:
                LOG.info("Telegram call engine cleanup ended category=%s", type(exc).__name__)
                self._started = False
