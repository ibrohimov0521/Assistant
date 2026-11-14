from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from app.storage.telegram_calls import TelegramCallStore


class CallOutcome(StrEnum):
    ANSWERED = "answered"
    REJECTED = "rejected"
    NO_ANSWER = "no_answer"
    FAILED = "failed"
    UNKNOWN = "unknown"


class CallAdapter(Protocol):
    async def call(self, *, recipient_id: int, body: str,
                   timeout_seconds: int) -> CallOutcome: ...


class TelegramCallFlow:
    """Approval-gated call orchestration; transports never send fallback messages."""

    def __init__(self, store: TelegramCallStore, *, owner_id: int,
                 adapter: CallAdapter | None, timeout_seconds: int = 30):
        if owner_id <= 0:
            raise ValueError("Qo‘ng‘iroq egasining ID si noto‘g‘ri")
        if not 5 <= timeout_seconds <= 120:
            raise ValueError("Qo‘ng‘iroq kutish vaqti 5–120 soniya bo‘lishi kerak")
        self.store = store
        self.owner_id = owner_id
        self.adapter = adapter
        self.timeout_seconds = timeout_seconds

    def prepare(self, *, owner_id: int, idempotency_key: str, recipient_id: int,
                recipient_name: str, body: str) -> dict[str, object]:
        if owner_id != self.owner_id:
            return {"status": "forbidden"}
        return self.store.prepare(owner_id=owner_id, idempotency_key=idempotency_key,
            recipient_id=recipient_id, recipient_name=recipient_name, body=body)

    async def confirm_and_call(self, *, owner_id: int, attempt_id: str,
                               confirmed_recipient_id: int) -> dict[str, object]:
        if owner_id != self.owner_id:
            return {"status": "forbidden"}
        claimed = self.store.claim(owner_id=owner_id, attempt_id=attempt_id,
                                  confirmed_recipient_id=confirmed_recipient_id)
        if claimed.get("status") != "calling":
            return claimed
        if self.adapter is None:
            return self.store.finish(owner_id=owner_id, attempt_id=attempt_id, outcome="failed")
        try:
            outcome = await self.adapter.call(recipient_id=int(claimed["recipient_id"]),
                body=str(claimed["body"]), timeout_seconds=self.timeout_seconds)
            if not isinstance(outcome, CallOutcome):
                outcome = CallOutcome(str(outcome))
        except Exception:
            # A lost transport result is not evidence that nobody answered. Never redial.
            outcome = CallOutcome.UNKNOWN
        return self.store.finish(owner_id=owner_id, attempt_id=attempt_id, outcome=outcome.value)
