from __future__ import annotations

import contextlib
import uuid
from datetime import timedelta
from typing import Any

from app.storage.db import Database, iso, utc_now


class TelegramCallStore:
    """Encrypted owner-scoped call requests and non-sending voice drafts."""

    def __init__(self, db: Database):
        self.db = db

    def _owner_key(self, owner_id: int) -> str:
        return self.db.owner_key(owner_id, self.db.vault)

    def prepare(self, *, owner_id: int, idempotency_key: str, recipient_id: int,
                recipient_name: str, body: str, ttl_seconds: int = 300) -> dict[str, Any]:
        key = idempotency_key.strip()
        name = " ".join(recipient_name.split())[:120]
        message = body.strip()
        if owner_id <= 0 or not key or len(key) > 120:
            raise ValueError("Qo‘ng‘iroq so‘rovining takrorlanmas kaliti noto‘g‘ri")
        if recipient_id <= 0 or not name or not message or len(message) > 2000:
            raise ValueError("Qo‘ng‘iroq oluvchisi yoki matni noto‘g‘ri")
        if not 30 <= ttl_seconds <= 3600:
            raise ValueError("Qo‘ng‘iroq qoralamasi muddati 30–3600 soniya bo‘lishi kerak")

        owner_key = self._owner_key(owner_id)
        attempt_key = self.db.vault.index("telegram-call-attempt", f"{owner_id}:{key}")
        attempt_id = uuid.uuid4().hex
        now = utc_now()
        payload = {"recipient_id": int(recipient_id), "recipient_name": name, "body": message}
        with contextlib.closing(self.db.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            inserted = conn.execute(
                "INSERT OR IGNORE INTO telegram_call_attempts "
                "(id,owner_key,attempt_key,payload_enc,status,expires_at,created_at,updated_at) "
                "VALUES(?,?,?,?,'needs_confirmation',?,?,?)",
                (attempt_id, owner_key, attempt_key, self.db.vault.seal_json(payload),
                 iso(now + timedelta(seconds=ttl_seconds)), iso(now), iso(now)),
            ).rowcount
            if inserted:
                conn.commit()
                return {"id": attempt_id, "status": "needs_confirmation", "duplicate": False,
                        **payload}
            row = conn.execute(
                "SELECT id,payload_enc,status FROM telegram_call_attempts "
                "WHERE owner_key=? AND attempt_key=?", (owner_key, attempt_key),
            ).fetchone()
            conn.commit()
        if not row:
            return {"status": "unavailable", "duplicate": True}
        saved = self.db.vault.open_json(row["payload_enc"])
        return {"id": row["id"], "status": str(row["status"]), "duplicate": True,
                **(saved if isinstance(saved, dict) else {})}

    def claim(self, *, owner_id: int, attempt_id: str,
              confirmed_recipient_id: int) -> dict[str, Any]:
        owner_key = self._owner_key(owner_id)
        now = iso(utc_now())
        with contextlib.closing(self.db.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM telegram_call_attempts WHERE id=? AND owner_key=? "
                "AND status='needs_confirmation'", (attempt_id, owner_key),
            ).fetchone()
            if not row:
                conn.rollback()
                return {"status": "unavailable"}
            if row["expires_at"] <= now:
                conn.execute("UPDATE telegram_call_attempts SET status='expired',payload_enc=?,updated_at=? "
                             "WHERE id=? AND status='needs_confirmation'",
                             (self.db.vault.seal_json({}), now, attempt_id))
                conn.commit()
                return {"status": "expired"}
            payload = self.db.vault.open_json(row["payload_enc"])
            if (not isinstance(payload, dict)
                    or int(payload.get("recipient_id") or 0) != confirmed_recipient_id):
                conn.rollback()
                return {"status": "recipient_confirmation_mismatch"}
            changed = conn.execute("UPDATE telegram_call_attempts SET status='calling',updated_at=? "
                "WHERE id=? AND owner_key=? AND status='needs_confirmation'",
                (now, attempt_id, owner_key)).rowcount
            if changed != 1:
                conn.rollback()
                return {"status": "unavailable"}
            conn.commit()
        return {"status": "calling", **payload}

    def finish(self, *, owner_id: int, attempt_id: str, outcome: str) -> dict[str, Any]:
        if outcome not in {"answered", "rejected", "no_answer", "failed", "unknown"}:
            raise ValueError("Qo‘ng‘iroq natijasi noto‘g‘ri")
        owner_key = self._owner_key(owner_id)
        now = iso(utc_now())
        with contextlib.closing(self.db.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT payload_enc,status,expires_at FROM telegram_call_attempts "
                "WHERE id=? AND owner_key=?", (attempt_id, owner_key)).fetchone()
            if not row or row["status"] != "calling":
                conn.rollback()
                return {"status": "unavailable"}
            payload = self.db.vault.open_json(row["payload_enc"])
            voice_draft = None
            if outcome == "no_answer" and isinstance(payload, dict):
                voice_draft = {"format": "voice", "recipient_id": int(payload["recipient_id"]),
                    "recipient_name": str(payload["recipient_name"]), "body": str(payload["body"]),
                    "status": "draft", "auto_send": False}
            changed = conn.execute("UPDATE telegram_call_attempts SET status=?,voice_draft_enc=?,updated_at=?,expires_at=? "
                "WHERE id=? AND owner_key=? AND status='calling'",
                (outcome, self.db.vault.seal_json(voice_draft) if voice_draft else None,
                 now, iso(utc_now() + timedelta(days=7)) if outcome == "no_answer" else
                 row["expires_at"], attempt_id, owner_key)).rowcount
            if changed != 1:
                conn.rollback()
                return {"status": "unavailable"}
            conn.commit()
        return {"status": outcome, "voice_draft": voice_draft}

    def cancel(self, *, owner_id: int, attempt_id: str) -> bool:
        owner_key = self._owner_key(owner_id)
        with contextlib.closing(self.db.connect()) as conn:
            changed = conn.execute("UPDATE telegram_call_attempts SET status='cancelled',payload_enc=?,updated_at=? "
                "WHERE id=? AND owner_key=? AND status='needs_confirmation'",
                (self.db.vault.seal_json({}), iso(utc_now()), attempt_id, owner_key)).rowcount
            return changed == 1

    def voice_draft(self, *, owner_id: int, attempt_id: str) -> dict[str, Any] | None:
        owner_key = self._owner_key(owner_id)
        with contextlib.closing(self.db.connect()) as conn:
            row = conn.execute("SELECT voice_draft_enc,status FROM telegram_call_attempts "
                "WHERE id=? AND owner_key=?", (attempt_id, owner_key)).fetchone()
        if not row or row["status"] != "no_answer" or not row["voice_draft_enc"]:
            return None
        value = self.db.vault.open_json(row["voice_draft_enc"])
        return value if isinstance(value, dict) else None

    def list_voice_drafts(self, *, owner_id: int, limit: int = 12) -> list[dict[str, Any]]:
        owner_key = self._owner_key(owner_id)
        now = iso(utc_now())
        with contextlib.closing(self.db.connect()) as conn:
            rows = conn.execute("SELECT id,voice_draft_enc FROM telegram_call_attempts "
                "WHERE owner_key=? AND status='no_answer' AND expires_at>? "
                "AND voice_draft_enc IS NOT NULL ORDER BY updated_at DESC LIMIT ?",
                (owner_key, now, max(1, min(int(limit), 30)))).fetchall()
        drafts = []
        for row in rows:
            draft = self.db.vault.open_json(row["voice_draft_enc"])
            if isinstance(draft, dict) and draft.get("status") == "draft" and draft.get("auto_send") is False:
                drafts.append({"id": str(row["id"]), **draft})
        return drafts

