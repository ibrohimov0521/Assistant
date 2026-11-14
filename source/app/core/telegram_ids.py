from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator, Field


def _validate_telegram_chat_id(value: int) -> int:
    if value == 0:
        raise ValueError("Telegram chat ID zero is invalid")
    return value


TelegramChatId = Annotated[
    int,
    Field(strict=True, ge=-(2**63), le=(2**63) - 1),
    AfterValidator(_validate_telegram_chat_id),
]
