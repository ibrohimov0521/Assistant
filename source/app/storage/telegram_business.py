from __future__ import annotations

import contextlib
import uuid
from datetime import timedelta
from typing import Any

from app.storage.db import Database, iso, utc_now


class TelegramBusinessStore:
    """Owner-scoped encrypted connection state and short-lived approval drafts."""

    def __init__(self, db: Database):
        self.db = db

    def _owner_key(self, owner_id: int) -> str:
        return self.db.owner_key(owner_id, self.db.vault)

    def _connection_key(self, connection_id: str) -> str:
        return self.db.vault.index("telegram-business-connection", connection_id)

    def save_connection(self, *, owner_id: int, connection_id: str, user_id: int,
                        is_enabled: bool, can_read_messages: bool, can_reply: bool) -> None:
        owner_key = self._owner_key(owner_id)
        connection_key = self._connection_key(connection_id)
        now = iso(utc_now())
        payload = {"connection_id": connection_id, "user_id": int(user_id),
                   "can_read_messages": bool(can_read_messages), "can_reply": bool(can_reply)}
        with contextlib.closing(self.db.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO telegram_business_connections(owner_key,connection_key,payload_enc,active,updated_at) "
                         "VALUES(?,?,?,?,?) ON CONFLICT(owner_key,connection_key) DO UPDATE SET "
                         "payload_enc=excluded.payload_enc,active=excluded.active,updated_at=excluded.updated_at",
                         (owner_key, connection_key, self.db.vault.seal_json(payload), int(is_enabled), now))
            if not is_enabled:
                conn.execute("UPDATE telegram_business_drafts SET status='cancelled',payload_enc=?,updated_at=? "
                             "WHERE owner_key=? AND connection_key=? AND status='pending'",
                             (self.db.vault.seal_json({}), now, owner_key, connection_key))
            conn.commit()

    def connection(self, owner_id: int, connection_id: str) -> dict[str, Any] | None:
        owner_key = self._owner_key(owner_id)
        connection_key = self._connection_key(connection_id)
        with contextlib.closing(self.db.connect()) as conn:
            row = conn.execute("SELECT payload_enc,active FROM telegram_business_connections "
                               "WHERE owner_key=? AND connection_key=?", (owner_key, connection_key)).fetchone()
        if not row or not row["active"]:
            return None
        payload = self.db.vault.open_json(row["payload_enc"])
        return payload if isinstance(payload, dict) else None

    def readiness(self, owner_id: int) -> str:
        owner_key = self._owner_key(owner_id)
        with contextlib.closing(self.db.connect()) as conn:
            rows = conn.execute("SELECT payload_enc FROM telegram_business_connections "
                                "WHERE owner_key=? AND active=1", (owner_key,)).fetchall()
        for row in rows:
            payload = self.db.vault.open_json(row["payload_enc"])
            if isinstance(payload, dict) and payload.get("can_read_messages") and payload.get("can_reply"):
                return "ok"
        return "waiting_for_rights" if rows else "disconnected"

    def create_draft(self, *, owner_id: int, update_id: int, connection_id: str,
                     chat_id: int, source_message_id: int, customer_text: str,
                     topic: str, priority: str, variants: list[dict[str, str]],
                     ttl_seconds: int = 300) -> str | None:
        cleaned = [{"style": str(item.get("style") or "Javob")[:20],
                    "body": str(item.get("body") or "").strip()[:700]}
                   for item in variants[:4] if isinstance(item, dict) and str(item.get("body") or "").strip()]
        if not 1 <= len(cleaned) <= 4 or not customer_text.strip() or len(customer_text) > 4000:
            raise ValueError("Business qoralama ma’lumoti noto‘g‘ri")
        now = utc_now()
        draft_id = uuid.uuid4().hex
        owner_key = self._owner_key(owner_id)
        connection_key = self._connection_key(connection_id)
        payload = {"connection_id": connection_id, "chat_id": int(chat_id),
                   "source_message_id": int(source_message_id), "customer_text": customer_text,
                   "topic": topic[:40], "priority": priority[:20], "variants": cleaned}
        with contextlib.closing(self.db.connect()) as conn:
            try:
                inserted = conn.execute("INSERT OR IGNORE INTO telegram_business_drafts "
                    "(id,owner_key,connection_key,update_id,payload_enc,status,expires_at,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,'pending',?,?,?)",
                    (draft_id, owner_key, connection_key, update_id, self.db.vault.seal_json(payload),
                     iso(now + timedelta(seconds=ttl_seconds)), iso(now), iso(now))).rowcount
            except Exception:
                raise
        return draft_id if inserted == 1 else None

    def set_preview(self, owner_id: int, draft_id: str, message_id: int) -> bool:
        with contextlib.closing(self.db.connect()) as conn:
            cur = conn.execute("UPDATE telegram_business_drafts SET preview_message_id=?,updated_at=? "
                "WHERE id=? AND owner_key=? AND status='pending'",
                (message_id, iso(utc_now()), draft_id, self._owner_key(owner_id)))
            return cur.rowcount == 1

    def claim_send(self, *, owner_id: int, draft_id: str, preview_message_id: int,
                   variant_index: int) -> dict[str, Any]:
        now = iso(utc_now())
        owner_key = self._owner_key(owner_id)
        with contextlib.closing(self.db.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM telegram_business_drafts WHERE id=? AND owner_key=? "
                "AND status='pending'", (draft_id, owner_key)).fetchone()
            if not row or row["preview_message_id"] != preview_message_id:
                conn.rollback()
                return {"status": "unavailable"}
            if row["expires_at"] <= now:
                conn.execute("UPDATE telegram_business_drafts SET status='expired',payload_enc=?,updated_at=? WHERE id=?",
                             (self.db.vault.seal_json({}), now, draft_id))
                conn.commit()
                return {"status": "expired"}
            consent = conn.execute("SELECT granted FROM consents WHERE owner_key=? AND kind='telegram_business'",
                                   (owner_key,)).fetchone()
            if not consent or not consent["granted"]:
                conn.execute("UPDATE telegram_business_drafts SET status='cancelled',payload_enc=?,updated_at=? WHERE id=?",
                             (self.db.vault.seal_json({}), now, draft_id))
                conn.commit()
                return {"status": "consent_revoked"}
            connection = conn.execute("SELECT payload_enc,active FROM telegram_business_connections "
                "WHERE owner_key=? AND connection_key=?", (owner_key, row["connection_key"])).fetchone()
            if not connection or not connection["active"]:
                conn.execute("UPDATE telegram_business_drafts SET status='cancelled',payload_enc=?,updated_at=? WHERE id=?",
                             (self.db.vault.seal_json({}), now, draft_id))
                conn.commit()
                return {"status": "disconnected"}
            business = self.db.vault.open_json(connection["payload_enc"])
            if (not isinstance(business, dict) or not business.get("can_reply")
                    or not business.get("can_read_messages")):
                conn.execute("UPDATE telegram_business_drafts SET status='cancelled',payload_enc=?,updated_at=? WHERE id=?",
                             (self.db.vault.seal_json({}), now, draft_id))
                conn.commit()
                return {"status": "rights_changed"}
            payload = self.db.vault.open_json(row["payload_enc"])
            variants = payload.get("variants") if isinstance(payload, dict) else None
            if (not isinstance(variants, list) or isinstance(variant_index, bool)
                    or not isinstance(variant_index, int) or not 0 <= variant_index < len(variants)):
                conn.rollback()
                return {"status": "unavailable"}
            payload["selected_body"] = str(variants[variant_index].get("body") or "")
            payload["selected_style"] = str(variants[variant_index].get("style") or "")
            payload["connection_user_id"] = int(business.get("user_id") or 0)
            conn.execute("UPDATE telegram_business_drafts SET payload_enc=?,status='sending',updated_at=? "
                         "WHERE id=? AND status='pending'",
                         (self.db.vault.seal_json(payload), now, draft_id))
            conn.commit()
        return {"status": "sending", **payload}

    def finish(self, *, owner_id: int, draft_id: str, status: str) -> bool:
        if status not in {"sent", "failed", "unknown"}:
            raise ValueError("Business qoralama yakuniy holati noto‘g‘ri")
        with contextlib.closing(self.db.connect()) as conn:
            cur = conn.execute("UPDATE telegram_business_drafts SET status=?,payload_enc=?,updated_at=? "
                "WHERE id=? AND owner_key=? AND status='sending'",
                (status, self.db.vault.seal_json({}), iso(utc_now()), draft_id, self._owner_key(owner_id)))
            return cur.rowcount == 1

    def cancel(self, *, owner_id: int, draft_id: str, preview_message_id: int) -> bool:
        with contextlib.closing(self.db.connect()) as conn:
            cur = conn.execute("UPDATE telegram_business_drafts SET status='cancelled',payload_enc=?,updated_at=? "
                "WHERE id=? AND owner_key=? AND preview_message_id=? AND status='pending'",
                (self.db.vault.seal_json({}), iso(utc_now()), draft_id, self._owner_key(owner_id), preview_message_id))
            return cur.rowcount == 1

    def cancel_for_source_message(self, *, owner_id: int, connection_id: str, chat_id: int,
                                  message_ids: set[int]) -> int:
        if not message_ids:
            return 0
        owner_key = self._owner_key(owner_id)
        connection_key = self._connection_key(connection_id)
        now = iso(utc_now())
        cancelled = 0
        with contextlib.closing(self.db.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("SELECT id,payload_enc FROM telegram_business_drafts "
                "WHERE owner_key=? AND connection_key=? AND status='pending'",
                (owner_key, connection_key)).fetchall()
            for row in rows:
                payload = self.db.vault.open_json(row["payload_enc"])
                if (isinstance(payload, dict) and int(payload.get("chat_id") or 0) == chat_id
                        and int(payload.get("source_message_id") or 0) in message_ids):
                    cancelled += conn.execute("UPDATE telegram_business_drafts SET status='cancelled',payload_enc=?,updated_at=? "
                        "WHERE id=? AND status='pending'",
                        (self.db.vault.seal_json({}), now, row["id"])).rowcount
            conn.commit()
        return cancelled

