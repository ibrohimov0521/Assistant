from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo


class CommandError(ValueError):
    pass


@dataclass(frozen=True)
class ReminderDraft:
    due_at: datetime
    body: str


@dataclass(frozen=True)
class MessageDraft:
    alias: str
    body: str


def parse_reminder(command_text: str, now: datetime, timezone: ZoneInfo) -> ReminderDraft:
    raw = command_text.strip()
    if raw.startswith("/eslat"):
        raw = raw[len("/eslat"):].strip()
    if "|" not in raw:
        raise CommandError("Format: /eslat bugun 18:00 | Eslatma matni yoki /eslat ertaga 09:00 | Eslatma matni")
    when, body = raw.split("|", 1)
    body = body.strip()
    if not body:
        raise CommandError("Eslatma matni bo‘sh bo‘lmasin.")
    pieces = when.strip().split()
    if len(pieces) != 2 or len(body) > 1000:
        raise CommandError("Sana va vaqtni `bugun 13:40`, `ertaga 09:00` yoki `YYYY-MM-DD 09:00` ko‘rinishida kiriting.")
    date_part, time_part = pieces
    try:
        parsed_time = time.fromisoformat(time_part)
    except ValueError as exc:
        raise CommandError("Vaqt HH:MM shaklida bo‘lishi kerak.") from exc
    if parsed_time.tzinfo is not None or parsed_time.second or parsed_time.microsecond:
        raise CommandError("Vaqt HH:MM shaklida bo‘lishi kerak.")
    local_now = now.astimezone(timezone)
    if date_part.lower() == "bugun":
        day = local_now.date()
    elif date_part.lower() == "ertaga":
        day = local_now.date() + timedelta(days=1)
    else:
        try:
            day = date.fromisoformat(date_part)
        except ValueError as exc:
            raise CommandError("Sana `bugun`, `ertaga` yoki YYYY-MM-DD bo‘lishi kerak.") from exc
    local_due = datetime.combine(day, parsed_time, tzinfo=timezone)
    if local_due <= local_now:
        raise CommandError("Eslatma kelajakdagi vaqtga qo‘yilishi kerak.")
    return ReminderDraft(local_due.astimezone(UTC), body)


def parse_message(command_text: str) -> MessageDraft:
    raw = command_text
    if raw.startswith("/xabar"):
        raw = raw[len("/xabar"):].lstrip()
    if "|" not in raw:
        raise CommandError("Format: /xabar alias | yuboriladigan matn")
    alias, body = raw.split("|", 1)
    alias = alias.strip().lower().lstrip("@").replace(" ", "")
    # Remove only the delimiter-adjacent separator. Preserve message lines and punctuation.
    body = body.lstrip(" ")
    if not alias or not body:
        raise CommandError("Oluvchi alias’i va xabar matnini kiriting.")
    if len(body) > 2000:
        raise CommandError("Xabar 2 000 belgidan oshmasin.")
    return MessageDraft(alias, body)

