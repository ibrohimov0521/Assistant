from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from app.core.commands import CommandError, parse_reminder


@dataclass(frozen=True)
class VoiceReminder:
    due_at: datetime
    body: str


@dataclass(frozen=True)
class VoiceMessage:
    alias: str
    body: str


def parse_voice_intent(text: str, now: datetime, timezone: ZoneInfo) -> VoiceReminder | VoiceMessage | None:
    normalized = " ".join(text.strip().split())
    reminder = re.fullmatch(
        r"(?i)(bugun|ertaga|\d{4}-\d{2}-\d{2})\s+(?:soat\s+)?(\d{1,2}:\d{2})(?:\s+da)?\s+(.+?)\s+(?:ni\s+)?eslat(?:ib\s+qo['’]?y)?",
        normalized)
    if reminder:
        try:
            draft = parse_reminder(f"/eslat {reminder.group(1)} {reminder.group(2)} | {reminder.group(3)}", now, timezone)
        except CommandError:
            return None
        return VoiceReminder(draft.due_at, draft.body)
    message = re.fullmatch(r"(?i)([a-z0-9_]+)ga\s+(.+?)\s+yubor(?:ib\s+qo['’]?y)?", normalized)
    if message and len(message.group(2)) <= 2000:
        return VoiceMessage(message.group(1).lower(), message.group(2))
    return None
