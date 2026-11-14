from __future__ import annotations

import calendar
import contextlib
import hashlib
import json
import re
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app.storage.db import Database, iso, utc_now
from app.storage.personal import PersonalRecords


class BusinessRecords:
    """Encrypted, owner-scoped local records for CRM, content and subscriptions."""

    _CRM_STATUSES = {"lead", "active", "waiting", "closed"}
    _CONTENT_STATUSES = {"draft", "scheduled", "published", "cancelled"}
    _SUBSCRIPTION_STATUSES = {"active", "paused", "cancelled"}

    def __init__(self, db: Database, owner_id: int):
        self.db = db
        self.owner_id = owner_id
        self.records = PersonalRecords(db, owner_id)

    def _rows(self, kind: str) -> list[dict[str, Any]]:
        with contextlib.closing(self.db.connect()) as conn:
            rows = conn.execute("SELECT payload_enc FROM personal_records WHERE owner_key=? AND kind=? "
                                "ORDER BY updated_at DESC,item_key LIMIT 500",
                                (self.records.owner, kind)).fetchall()
        return [self.db.vault.open_json(row["payload_enc"]) for row in rows]

    def _get(self, kind: str, record_id: str) -> dict[str, Any] | None:
        with contextlib.closing(self.db.connect()) as conn:
            return self.records._get(conn, kind, "", record_id)

    def _save(self, kind: str, record_id: str, payload: dict[str, Any]) -> None:
        with contextlib.closing(self.db.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            self.records._put(conn, kind, "", record_id, payload)
            conn.commit()

    def _delete(self, kind: str, record_id: str) -> bool:
        with contextlib.closing(self.db.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            deleted = conn.execute("DELETE FROM personal_records WHERE owner_key=? AND kind=? "
                                   "AND group_key=? AND item_key=?",
                                   (self.records.owner, kind, self.records._key(""),
                                    self.records._key(record_id))).rowcount == 1
            conn.commit()
        return deleted

    @staticmethod
    def _record_id(value: str | None) -> str:
        record_id = str(value or "").strip().lower()
        if not re.fullmatch(r"[a-f0-9]{12}", record_id):
            raise ValueError("Yozuvning to‘liq 12 belgili ID sini kiriting.")
        return record_id

    @staticmethod
    def _optional_label(value: str | None, limit: int) -> str | None:
        text = " ".join(str(value or "").split())
        if len(text) > limit:
            raise ValueError(f"Matn {limit} belgidan oshmasin.")
        return text or None

    @staticmethod
    def _day(value: str | None) -> date | None:
        if not value:
            return None
        try:
            return date.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("Sana YYYY-MM-DD formatida bo‘lishi kerak.") from exc

    def manage_crm_contact(self, *, action: str, update_id: int, record_id: str | None = None,
                           name: str | None = None, organization: str | None = None,
                           email: str | None = None, phone: str | None = None,
                           need: str | None = None, last_conversation: str | None = None,
                           next_contact: str | None = None,
                           status: str | None = None, notes: str | None = None,
                           query: str | None = None) -> dict[str, Any]:
        kind = "crm_contact"
        if action not in {"create", "list", "get", "update", "delete"}:
            raise ValueError("Mijoz kartasi amali noto‘g‘ri.")
        if action == "list":
            needle = " ".join(str(query or "").casefold().split())
            rows = self._rows(kind)
            if needle:
                rows = [row for row in rows if needle in " ".join(
                    str(row.get(key) or "") for key in
                    ("name", "organization", "email", "phone", "need", "last_conversation", "status")).casefold()]
            return {"contacts": rows[:30]}
        if action in {"get", "update", "delete"}:
            if record_id:
                wanted = self._record_id(record_id)
                record = self._get(kind, wanted)
                matches = [record] if record else []
            elif action == "get" and name:
                folded = " ".join(name.casefold().split())
                matches = [row for row in self._rows(kind)
                           if " ".join(str(row.get("name") or "").casefold().split()) == folded]
                wanted = str(matches[0]["id"]) if len(matches) == 1 else ""
                record = matches[0] if len(matches) == 1 else None
            else:
                raise ValueError("Mijozni to‘liq ID yoki aniq ism bilan belgilang.")
            if action == "get":
                if len(matches) > 1:
                    return {"ambiguous": True, "contacts": [
                        {"id": row["id"], "name": row["name"], "organization": row.get("organization")}
                        for row in matches[:10]]}
                return {"contact": record} if record else {"found": False}
            if not record:
                return {"changed": False, "found": False}
            if action == "delete":
                changed = self._delete(kind, wanted)
                if changed:
                    self.db.record_assistant_action(owner_id=self.owner_id, action="crm_contact_deleted",
                        entity_type="crm_contact", entity_id=wanted, payload={}, update_id=update_id)
                return {"id": wanted, "deleted": changed}
            updates = {"name": name, "organization": organization, "email": email, "phone": phone,
                       "need": need, "last_conversation": last_conversation,
                       "next_contact": next_contact, "status": status, "notes": notes}
            if not any(value is not None for value in updates.values()):
                raise ValueError("O‘zgartiriladigan maydonni ayting.")
            if record.get("last_update_id") == update_id:
                return {"contact": record, "changed": False}
            parsed_next = self._day(next_contact) if next_contact is not None else None
            for key, value in updates.items():
                if value is None:
                    continue
                if key == "status" and value not in self._CRM_STATUSES:
                    raise ValueError("Mijoz holati lead, active, waiting yoki closed bo‘lsin.")
                if key == "email":
                    email_value = self._optional_label(value, 254)
                    if email_value and (not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email_value)
                                        or len(email_value) > 254):
                        raise ValueError("Email manzili noto‘g‘ri ko‘rinishda.")
                    record[key] = email_value
                elif key == "next_contact":
                    record[key] = parsed_next.isoformat() if parsed_next else None
                else:
                    record[key] = self._optional_label(value, 1200 if key in {"need", "notes"} else 160)
            record["last_update_id"] = update_id
            record["updated_at"] = iso(utc_now())
            self._save(kind, wanted, record)
            self.db.record_assistant_action(owner_id=self.owner_id, action="crm_contact_updated",
                entity_type="crm_contact", entity_id=wanted, payload={}, update_id=update_id)
            return {"contact": record, "changed": True}

        name_value = self.records._label(name or "", 120)
        status_value = status or "lead"
        if status_value not in self._CRM_STATUSES:
            raise ValueError("Mijoz holati lead, active, waiting yoki closed bo‘lsin.")
        email_value = self._optional_label(email, 254)
        if email_value and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email_value):
            raise ValueError("Email manzili noto‘g‘ri ko‘rinishda.")
        next_day = self._day(next_contact)
        rows = self._rows(kind)
        duplicate = next((row for row in rows if row.get("last_update_id") == update_id), None)
        if duplicate:
            return {"contact": duplicate, "created": False}
        duplicate = next((row for row in rows if row.get("name", "").casefold() == name_value.casefold()
                          and ((email_value and str(row.get("email") or "").casefold() == email_value.casefold())
                               or (phone and row.get("phone") == phone.strip()))), None)
        if duplicate:
            return {"contact": duplicate, "created": False}
        contact_id = uuid.uuid4().hex[:12]
        now = iso(utc_now())
        record = {"id": contact_id, "name": name_value,
                  "organization": self._optional_label(organization, 160),
                  "email": email_value,
                  "phone": self._optional_label(phone, 60),
                  "need": self._optional_label(need, 1200),
                "last_conversation": self._optional_label(last_conversation, 2000),
                  "next_contact": next_day.isoformat() if next_day else None,
                  "status": status_value, "notes": self._optional_label(notes, 1200),
                  "created_at": now, "updated_at": now, "last_update_id": update_id}
        self._save(kind, contact_id, record)
        self.db.record_assistant_action(owner_id=self.owner_id, action="crm_contact_created",
            entity_type="crm_contact", entity_id=contact_id, payload={}, update_id=update_id)
        return {"contact": record, "created": True}

    @staticmethod
    def _content_time(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("Kontent vaqti ISO sana-vaqtida bo‘lsin.") from exc
        if parsed.tzinfo is None:
            raise ValueError("Kontent vaqti Asia/Tashkent vaqt mintaqasi bilan kerak.")
        if parsed.utcoffset() != timedelta(hours=5):
            raise ValueError("Kontent vaqti Toshkentning UTC+05:00 vaqti bilan bo‘lsin.")
        return parsed

    @staticmethod
    def _content_digest(record: dict[str, Any]) -> str:
        fields = {key: record.get(key) for key in
                  ("platform", "title", "body", "headline_variants", "publish_at")}
        encoded = json.dumps(fields, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def manage_content_calendar(self, *, action: str, update_id: int,
                                record_id: str | None = None, platform: str | None = None,
                                title: str | None = None, body: str | None = None,
                                headline_variants: list[str] | None = None,
                                publish_at: str | None = None, status: str | None = None,
                                query: str | None = None) -> dict[str, Any]:
        kind = "content_calendar"
        allowed = {"create", "list", "update", "approve", "cancel", "mark_published",
                   "retry_publish"}
        if action not in allowed:
            raise ValueError("Kontent kalendari amali noto‘g‘ri.")
        if action == "list":
            rows = self._rows(kind)
            if status:
                rows = [row for row in rows if row.get("status") == status]
            if platform:
                rows = [row for row in rows if str(row.get("platform") or "").casefold() == platform.casefold()]
            if query:
                folded = " ".join(query.casefold().split())
                rows = [row for row in rows if folded in (row.get("title", "") + " " + row.get("body", "")).casefold()]
            for row in rows:
                row["approval_current"] = bool(row.get("approved_digest")
                                                and row.get("approved_digest") == self._content_digest(row))
                row["publishing_automatic"] = bool(row.get("status") == "scheduled"
                    and row.get("approval_current")
                    and row.get("publish_state", "pending") == "pending")
            return {"entries": rows[:30], "publishing_automatic": any(
                row.get("publishing_automatic") for row in rows)}

        if action == "create":
            title_value = self.records._label(title or "", 160)
            body_value = self._optional_label(body, 3000)
            variants = self._headline_variants(headline_variants)
            publish_time = self._content_time(publish_at)
            status_value = status or ("scheduled" if publish_time else "draft")
            if status_value not in {"draft", "scheduled"}:
                raise ValueError("Yangi kontent faqat draft yoki scheduled holatida yaratiladi.")
            if status_value == "scheduled" and not body_value:
                raise ValueError("Nashrga rejalangan post uchun matn kerak.")
            if status_value == "scheduled" and (not publish_time or publish_time.astimezone(UTC) <= utc_now()):
                raise ValueError("Rejalangan post uchun kelajakdagi aniq sana-vaqt kerak.")
            duplicate = next((row for row in self._rows(kind) if row.get("last_update_id") == update_id), None)
            if duplicate:
                return {"entry": duplicate, "created": False, "publishing_automatic": False}
            entry_id = uuid.uuid4().hex[:12]
            now = iso(utc_now())
            record = {"id": entry_id, "platform": self.records._label(platform or "Umumiy", 80),
                      "title": title_value, "body": body_value, "headline_variants": variants,
                      "version": 1, "approved_digest": None, "approved_at": None,
                      "publish_at": publish_time.isoformat() if publish_time else None,
                      "status": status_value, "publish_state": "pending",
                      "created_at": now, "updated_at": now,
                      "last_update_id": update_id}
            self._save(kind, entry_id, record)
            self.db.record_assistant_action(owner_id=self.owner_id, action="content_entry_created",
                entity_type="content_calendar", entity_id=entry_id, payload={"status": status_value},
                update_id=update_id)
            return {"entry": record, "created": True, "publishing_automatic": False}

        entry_id = self._record_id(record_id)
        record = self._get(kind, entry_id)
        if not record:
            return {"id": entry_id, "changed": False, "found": False, "publishing_automatic": False}
        if record.get("last_update_id") == update_id:
            return {"entry": record, "changed": False, "publishing_automatic": False}

        if action == "cancel":
            if record.get("status") in {"published", "cancelled"}:
                return {"entry": record, "changed": False, "publishing_automatic": False}
            if record.get("publish_state") in {"sending", "unknown", "published"}:
                raise ValueError("Nashr holati hal qilinmagan yoki allaqachon bajarilgan; avval platformadagi holatni tekshiring.")
            record["status"] = "cancelled"
            record["approved_digest"] = None
            record["approved_at"] = None
            record["publish_state"] = "cancelled"
        elif action == "approve":
            if record.get("status") != "scheduled" or not record.get("publish_at"):
                raise ValueError("Avval postni aniq Toshkent vaqtiga rejalashtiring, keyin tasdiqlang.")
            if not str(record.get("body") or "").strip():
                raise ValueError("Nashrga tasdiqlashdan oldin post matnini kiriting.")
            if record.get("publish_state") in {"sending", "unknown", "published"}:
                raise ValueError("Nashr boshlangan yoki natijasi noma’lum; takror nashrni oldini olish uchun tasdiqlash to‘xtatildi.")
            record["approved_digest"] = self._content_digest(record)
            record["approved_at"] = iso(utc_now())
            record["publish_state"] = "pending"
            record.pop("publish_error", None)
        elif action == "mark_published":
            if record.get("status") != "scheduled":
                raise ValueError("Faqat scheduled postni published deb belgilash mumkin.")
            if record.get("approved_digest") != self._content_digest(record):
                raise ValueError("Joriy post versiyasi tasdiqlanmagan; avval aynan shu versiyani tasdiqlang.")
            publish_time = self._content_time(str(record.get("publish_at") or ""))
            if publish_time and publish_time.astimezone(UTC) > utc_now():
                raise ValueError("Rejalangan vaqt hali kelmagan.")
            record["status"] = "published"
            record["published_at"] = iso(utc_now())
            record["publish_state"] = "published_owner_reported"
        elif action == "retry_publish":
            if record.get("status") != "scheduled" or record.get("publish_state") != "failed":
                raise ValueError("Faqat aniq muvaffaqiyatsiz tugagan rejalangan postni qayta urinish mumkin; noma’lum holat takrorlanmaydi.")
            if record.get("approved_digest") != self._content_digest(record):
                raise ValueError("Joriy post versiyasi tasdiqlanmagan; avval aynan shu matnni tasdiqlang.")
            publish_time = self._content_time(str(record.get("publish_at") or ""))
            if not publish_time or publish_time.astimezone(UTC) > utc_now():
                raise ValueError("Qayta nashr qilish uchun rejalangan vaqt kelgan bo‘lishi kerak.")
            record["publish_state"] = "pending"
            record.pop("publish_error", None)
        else:  # update
            if record.get("status") in {"published", "cancelled"}:
                raise ValueError("Yakunlangan yoki bekor qilingan postni o‘zgartirib bo‘lmaydi.")
            if record.get("publish_state") in {"sending", "unknown", "published"}:
                raise ValueError("Nashr boshlangan yoki natijasi noma’lum; avval platformadagi holatni tekshiring.")
            if not any(value is not None for value in
                       (platform, title, body, headline_variants, publish_at, status)):
                raise ValueError("O‘zgartiriladigan post maydonini ayting.")
            if platform is not None:
                record["platform"] = self.records._label(platform, 80)
            if title is not None:
                record["title"] = self.records._label(title, 160)
            if body is not None:
                record["body"] = self._optional_label(body, 3000)
            if headline_variants is not None:
                record["headline_variants"] = self._headline_variants(headline_variants)
            if publish_at is not None:
                parsed_time = self._content_time(publish_at)
                if parsed_time.astimezone(UTC) <= utc_now():
                    raise ValueError("Rejalangan post uchun kelajakdagi vaqt kerak.")
                record["publish_at"] = parsed_time.isoformat()
                record["status"] = "scheduled"
            if status is not None:
                if status not in {"draft", "scheduled"}:
                    raise ValueError("Postni faqat draft yoki scheduled qilib o‘zgartirish mumkin.")
                record["status"] = status
            if record.get("status") == "scheduled":
                scheduled_time = self._content_time(str(record.get("publish_at") or ""))
                if not scheduled_time or scheduled_time.astimezone(UTC) <= utc_now():
                    raise ValueError("Scheduled post uchun kelajakdagi Toshkent vaqti kerak.")
                if not str(record.get("body") or "").strip():
                    raise ValueError("Nashrga rejalangan post uchun matn kerak.")
            record["version"] = int(record.get("version") or 1) + 1
            record["approved_digest"] = None
            record["approved_at"] = None
            record["publish_state"] = "pending"
            record.pop("publish_error", None)

        record["last_update_id"] = update_id
        record["updated_at"] = iso(utc_now())
        self._save(kind, entry_id, record)
        self.db.record_assistant_action(owner_id=self.owner_id, action=f"content_entry_{action}",
            entity_type="content_calendar", entity_id=entry_id, payload={"status": record["status"]},
            update_id=update_id)
        return {"entry": record, "changed": True,
                "publishing_automatic": action in {"approve", "retry_publish"}}

    def claim_due_content_publishes(self, now: datetime, *, limit: int = 5) -> list[dict[str, Any]]:
        """Atomically lease approved due posts; a lease is never automatically replayed."""
        if now.tzinfo is None:
            raise ValueError("Kontent navbati uchun timezone’li vaqt kerak.")
        now = now.astimezone(UTC)
        owner = self.records.owner
        claimed: list[dict[str, Any]] = []
        with contextlib.closing(self.db.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("SELECT item_key,payload_enc FROM personal_records "
                "WHERE owner_key=? AND kind=? AND group_key=? ORDER BY updated_at,item_key LIMIT 500",
                (owner, "content_calendar", self.records._key(""))).fetchall()
            candidates = []
            for row in rows:
                record = self.db.vault.open_json(row["payload_enc"])
                if not isinstance(record, dict) or record.get("status") != "scheduled":
                    continue
                if record.get("publish_state", "pending") != "pending":
                    continue
                if record.get("approved_digest") != self._content_digest(record):
                    continue
                publish_time = self._content_time(str(record.get("publish_at") or ""))
                if not publish_time or publish_time.astimezone(UTC) > now:
                    continue
                candidates.append((publish_time.astimezone(UTC), str(record.get("id") or ""), record))
            for _, record_id, record in sorted(candidates, key=lambda item: (item[0], item[1]))[:max(1, min(limit, 10))]:
                attempt_id = uuid.uuid4().hex
                record["publish_state"] = "sending"
                record["publish_attempt_id"] = attempt_id
                record["publish_claimed_at"] = iso(now)
                record.pop("publish_error", None)
                self.records._put(conn, "content_calendar", "", record_id, record)
                claimed.append(record)
            conn.commit()
        for record in claimed:
            self.db.record_assistant_action(owner_id=self.owner_id, action="content_publish_claimed",
                entity_type="content_calendar", entity_id=str(record["id"]),
                payload={"platform": str(record.get("platform") or "")[:80], "state": "sending"})
        return claimed

    def finish_content_publish(self, record_id: str, attempt_id: str, state: str, *,
                               post_id: str | None = None, error: str | None = None,
                               now: datetime | None = None) -> bool:
        if state not in {"published", "failed", "unknown"}:
            raise ValueError("Nashr yakuniy holati noto‘g‘ri.")
        record_id = self._record_id(record_id)
        now = now or utc_now()
        with contextlib.closing(self.db.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            record = self.records._get(conn, "content_calendar", "", record_id)
            if (not record or record.get("status") != "scheduled"
                    or record.get("publish_state") != "sending"
                    or record.get("publish_attempt_id") != attempt_id):
                conn.rollback()
                return False
            record["publish_state"] = state
            record["publish_checked_at"] = iso(now)
            if state == "published":
                record["status"] = "published"
                record["published_at"] = iso(now)
                if post_id:
                    record["platform_post_id"] = str(post_id)[:200]
                record.pop("publish_error", None)
            elif error:
                record["publish_error"] = " ".join(str(error).split())[:400]
            self.records._put(conn, "content_calendar", "", record_id, record)
            conn.commit()
        self.db.record_assistant_action(owner_id=self.owner_id, action=f"content_publish_{state}",
            entity_type="content_calendar", entity_id=record_id,
            payload={"platform": str(record.get("platform") or "")[:80],
                     "state": state, "post_id": str(post_id or "")[:200]})
        return True

    def recover_stale_content_publishes(self, now: datetime, *,
                                        stale_after: timedelta = timedelta(minutes=3)) -> list[dict[str, Any]]:
        """Mark interrupted external writes unknown so a restart cannot duplicate a post."""
        if now.tzinfo is None:
            raise ValueError("Kontent navbati uchun timezone’li vaqt kerak.")
        now = now.astimezone(UTC)
        cutoff = iso(now - max(timedelta(minutes=2), stale_after))
        recovered: list[dict[str, Any]] = []
        with contextlib.closing(self.db.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("SELECT item_key,payload_enc FROM personal_records "
                "WHERE owner_key=? AND kind=? AND group_key=? ORDER BY updated_at LIMIT 500",
                (self.records.owner, "content_calendar", self.records._key(""))).fetchall()
            for row in rows:
                record = self.db.vault.open_json(row["payload_enc"])
                if (not isinstance(record, dict) or record.get("status") != "scheduled"
                        or record.get("publish_state") != "sending"
                        or str(record.get("publish_claimed_at") or "") > cutoff):
                    continue
                record["publish_state"] = "unknown"
                record["publish_checked_at"] = iso(now)
                record["publish_error"] = "Bot uzilib qoldi; platformadagi nashr holatini qo‘lda tekshirish kerak."
                self.records._put(conn, "content_calendar", "", str(record["id"]), record)
                recovered.append(record)
            conn.commit()
        for record in recovered:
            self.db.record_assistant_action(owner_id=self.owner_id, action="content_publish_unknown",
                entity_type="content_calendar", entity_id=str(record.get("id") or ""),
                payload={"platform": str(record.get("platform") or "")[:80], "state": "unknown"})
        return recovered

    @staticmethod
    def _headline_variants(values: list[str] | None) -> list[str]:
        if values is None:
            return []
        if not isinstance(values, list) or len(values) > 5:
            raise ValueError("Sarlavha variantlari 0–5 ta bo‘lishi kerak.")
        result = []
        for value in values:
            if not isinstance(value, str):
                raise ValueError("Har bir sarlavha matn bo‘lishi kerak.")
            clean = " ".join(value.split())
            if not clean or len(clean) > 160:
                raise ValueError("Sarlavha bo‘sh bo‘lmasin va 160 belgidan oshmasin.")
            result.append(clean)
        return result

    @staticmethod
    def _advance_subscription_day(current: date, period: str) -> date:
        if period == "yearly":
            year = current.year + 1
            day = min(current.day, calendar.monthrange(year, current.month)[1])
            return date(year, current.month, day)
        year, month = current.year, current.month + 1
        if month > 12:
            year, month = year + 1, 1
        if period == "month_end":
            return date(year, month, calendar.monthrange(year, month)[1])
        return date(year, month, min(current.day, calendar.monthrange(year, month)[1]))

    def manage_subscription(self, *, action: str, update_id: int, record_id: str | None = None,
                            name: str | None = None, amount: str | None = None,
                            currency: str | None = None, period: str | None = None,
                            next_due: str | None = None, remind_days: int | None = None,
                            query: str | None = None) -> dict[str, Any]:
        kind = "subscription"
        if action not in {"add", "list", "paid", "pause", "resume", "cancel"}:
            raise ValueError("Obuna amali noto‘g‘ri.")
        if action == "list":
            rows = self._rows(kind)
            if query:
                folded = " ".join(query.casefold().split())
                rows = [row for row in rows if folded in row.get("name", "").casefold()]
            return {"subscriptions": rows[:30], "external_payment_connected": False,
                    "exchange_rate_connected": False}
        if action == "add":
            name_value = self.records._label(name or "", 120)
            currency_value = str(currency or "").upper()
            minor, digits = self.records._money(str(amount or ""), currency_value)
            if period not in {"monthly", "month_end", "yearly"}:
                raise ValueError("Hisob davri monthly, month_end yoki yearly bo‘lsin.")
            due = self._day(next_due)
            if not due:
                raise ValueError("Keyingi to‘lov sanasini YYYY-MM-DD qilib kiriting.")
            lead = 3 if remind_days is None else remind_days
            if type(lead) is not int or not 0 <= lead <= 30:
                raise ValueError("Eslatma 0–30 kun oldin bo‘lishi kerak.")
            rows = self._rows(kind)
            duplicate = next((row for row in rows if row.get("last_update_id") == update_id), None)
            if duplicate:
                return {"subscription": duplicate, "created": False}
            now = iso(utc_now())
            sub_id = uuid.uuid4().hex[:12]
            record = {"id": sub_id, "name": name_value, "minor": minor, "digits": digits,
                      "currency": currency_value, "period": period, "next_due": due.isoformat(),
                      "remind_days": lead, "status": "active", "created_at": now,
                      "updated_at": now, "last_update_id": update_id}
            self._save(kind, sub_id, record)
            self.db.record_assistant_action(owner_id=self.owner_id, action="subscription_created",
                entity_type="subscription", entity_id=sub_id,
                payload={"currency": currency_value, "minor": minor}, update_id=update_id)
            return {"subscription": record, "created": True}

        sub_id = self._record_id(record_id)
        record = self._get(kind, sub_id)
        if not record:
            return {"id": sub_id, "changed": False, "found": False}
        if record.get("last_update_id") == update_id:
            return {"subscription": record, "changed": False}
        if action == "paid":
            if record.get("status") == "cancelled":
                raise ValueError("Bekor qilingan obuna uchun to‘lov davrini yangilab bo‘lmaydi.")
            due = date.fromisoformat(record["next_due"])
            record["next_due"] = self._advance_subscription_day(due, record["period"]).isoformat()
            record["status"] = "active"
            record["last_paid_at"] = iso(utc_now())
        else:
            state = {"pause": "paused", "resume": "active", "cancel": "cancelled"}[action]
            record["status"] = state
        record["last_update_id"] = update_id
        record["updated_at"] = iso(utc_now())
        self._save(kind, sub_id, record)
        self.db.record_assistant_action(owner_id=self.owner_id, action=f"subscription_{action}",
            entity_type="subscription", entity_id=sub_id, payload={"status": record["status"]},
            update_id=update_id)
        return {"subscription": record, "changed": True}

    def due_subscription_reminders(self, today: date) -> list[dict[str, Any]]:
        due: list[dict[str, Any]] = []
        for row in self._rows("subscription"):
            if row.get("status") != "active":
                continue
            due_day = date.fromisoformat(row["next_due"])
            if due_day - timedelta(days=int(row.get("remind_days") or 0)) <= today:
                due.append({**row, "days_remaining": (due_day - today).days})
        return due[:100]
