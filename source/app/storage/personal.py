from __future__ import annotations

import calendar
import contextlib
import json
import re
import sqlite3
import unicodedata
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

from app.storage.db import Database, iso, utc_now


_PERMISSION_CAPABILITIES = {
    "account": {"read", "search", "download", "write", "send", "send_voice", "call", "archive", "mute",
                "pin", "delete", "moderate", "manage_members", "change_settings", "manage"},
    "chat": {"read", "search", "download", "send", "send_voice", "call", "archive", "mute",
             "pin", "delete", "moderate", "manage_members", "change_settings"},
    "project": {"read", "write", "delete", "share"},
    "integration": {"read", "write", "send", "delete", "manage"},
    "server": {"monitor", "logs", "restart", "deploy", "cleanup"},
}


class PersonalRecords:
    def __init__(self, db: Database, owner_id: int):
        self.db = db
        self.owner = db.owner_key(owner_id, db.vault)

    @staticmethod
    def _label(value: str, limit: int = 100) -> str:
        label = " ".join(value.split())
        if not 1 <= len(label) <= limit:
            raise ValueError(f"Matn 1–{limit} belgi bo‘lishi kerak.")
        return label

    def _key(self, value: str) -> str:
        normalized = unicodedata.normalize("NFKC", value).casefold()
        normalized = normalized.translate(str.maketrans("‘’ʻʼ`", "'''''"))
        return self.db.vault.index("personal-record", normalized)

    def _get(self, conn: sqlite3.Connection, kind: str, group: str, item: str) -> dict[str, Any] | None:
        row = conn.execute("SELECT payload_enc FROM personal_records "
                           "WHERE owner_key=? AND kind=? AND group_key=? AND item_key=?",
                           (self.owner, kind, self._key(group), self._key(item))).fetchone()
        return self.db.vault.open_json(row["payload_enc"]) if row else None

    def _put(self, conn: sqlite3.Connection, kind: str, group: str, item: str,
             payload: dict[str, Any], day: str | None = None) -> None:
        now = iso(utc_now())
        conn.execute("""INSERT INTO personal_records VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(owner_key,kind,group_key,item_key) DO UPDATE SET
                payload_enc=excluded.payload_enc,local_day=excluded.local_day,updated_at=excluded.updated_at""",
                     (self.owner, kind, self._key(group), self._key(item), self.db.vault.seal_json(payload),
                      day, now, now))

    def preferences(self) -> dict[str, str]:
        with contextlib.closing(self.db.connect()) as conn:
            return self._get(conn, "preferences", "", "profile") or {}

    def daily_report_sections(self) -> list[str]:
        defaults = ["calendar", "tasks", "reminders", "weather", "news", "usage"]
        with contextlib.closing(self.db.connect()) as conn:
            record = self._get(conn, "daily_report", "", "sections") or {}
        sections = record.get("sections")
        allowed = set(defaults)
        if not isinstance(sections, list) or not sections or any(item not in allowed for item in sections):
            return defaults
        return [item for item in defaults if item in sections]

    def set_daily_report_sections(self, sections: list[str]) -> list[str]:
        defaults = ["calendar", "tasks", "reminders", "weather", "news", "usage"]
        if (not isinstance(sections, list) or not 1 <= len(sections) <= len(defaults)
                or any(not isinstance(item, str) or item not in defaults for item in sections)
                or len(set(sections)) != len(sections)):
            raise ValueError("Hisobot bo‘limlarini 1–6 ta qilib, takrorlamasdan tanlang.")
        normalized = [item for item in defaults if item in sections]
        with contextlib.closing(self.db.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._put(conn, "daily_report", "", "sections", {"sections": normalized})
            conn.commit()
        return normalized

    def set_chat_format(self, chat_id: int, message_format: str) -> None:
        self.set_chat_profile(chat_id, message_format=message_format)

    def set_chat_profile(self, chat_id: int, *, message_format: str | None = None,
                         language: str | None = None, tone: str | None = None,
                         length: str | None = None, chat_name: str | None = None,
                         chat_reference: str | None = None) -> dict[str, Any]:
        if type(chat_id) is not int or chat_id == 0:
            raise ValueError("Telegram chat ID si noto‘g‘ri.")
        if message_format is not None and message_format not in {"text", "voice"}:
            raise ValueError("Chat formati matn yoki voice bo‘lishi kerak.")
        if language is not None and language not in {"auto", "uz", "ru", "en"}:
            raise ValueError("Chat tili auto, uz, ru yoki en bo‘lishi kerak.")
        if tone is not None and tone not in {"default", "formal", "friendly", "concise"}:
            raise ValueError("Chat ohangi default, formal, friendly yoki concise bo‘lishi kerak.")
        if length is not None and length not in {"short", "normal", "detailed"}:
            raise ValueError("Chat javobi short, normal yoki detailed bo‘lishi kerak.")
        updates = {"format": message_format, "language": language, "tone": tone, "length": length}
        if not any(value is not None for value in updates.values()):
            raise ValueError("Saqlanadigan chat formati, tili, ohangi yoki uzunligini ayting.")
        with contextlib.closing(self.db.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            profile = self._get(conn, "chat_format", "", str(chat_id)) or {}
            profile.update({key: value for key, value in updates.items() if value is not None})
            if chat_name:
                profile["chat_name"] = self._label(" ".join(str(chat_name).split()), 120)
            if chat_reference:
                profile["chat_reference"] = self._label(" ".join(str(chat_reference).split()), 120)
            profile["chat_id"] = chat_id
            self._put(conn, "chat_format", "", str(chat_id), profile)
            conn.commit()
        return profile

    def chat_profile(self, chat_id: int) -> dict[str, Any]:
        with contextlib.closing(self.db.connect()) as conn:
            profile = self._get(conn, "chat_format", "", str(chat_id)) or {}
        return profile

    def chat_profiles(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        with contextlib.closing(self.db.connect()) as conn:
            rows = conn.execute("SELECT payload_enc FROM personal_records WHERE owner_key=? AND kind='chat_format' "
                                "ORDER BY updated_at DESC LIMIT ?", (self.owner, limit)).fetchall()
        return [self.db.vault.open_json(row["payload_enc"]) for row in rows]

    def chat_format(self, chat_id: int) -> str | None:
        with contextlib.closing(self.db.connect()) as conn:
            record = self._get(conn, "chat_format", "", str(chat_id))
        return str(record["format"]) if record else None

    @staticmethod
    def _validate_permission(scope_type: str, scope_name: str, capability: str) -> tuple[str, str, str]:
        scope_type = str(scope_type or "").strip().lower()
        if scope_type not in _PERMISSION_CAPABILITIES:
            raise ValueError("Ruxsat turi account, chat, project, integration yoki server bo‘lishi kerak.")
        scope_name = "*" if scope_name is None or not str(scope_name).strip() else " ".join(str(scope_name).split())
        if not 1 <= len(scope_name) <= 120 or any(ord(char) < 32 for char in scope_name):
            raise ValueError("Ruxsat doirasi 1–120 belgidan iborat bo‘lishi kerak.")
        capability = str(capability or "").strip().lower()
        if capability not in _PERMISSION_CAPABILITIES[scope_type]:
            raise ValueError("Bu ruxsat turi uchun amal nomi noto‘g‘ri.")
        return scope_type, scope_name, capability

    def set_permission_profile(self, scope_type: str, scope_name: str, capability: str,
                               mode: str) -> bool:
        scope_type, scope_name, capability = self._validate_permission(scope_type, scope_name, capability)
        mode = str(mode or "").strip().lower()
        if mode not in {"allow", "deny", "inherit"}:
            raise ValueError("Ruxsat qiymati allow, deny yoki inherit bo‘lishi kerak.")
        item_key = f"{scope_name}|{capability}"
        with contextlib.closing(self.db.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if mode == "inherit":
                cursor = conn.execute("DELETE FROM personal_records WHERE owner_key=? AND kind=? "
                                      "AND group_key=? AND item_key=?",
                    (self.owner, "permission_profile", self._key(scope_type), self._key(item_key)))
                conn.commit()
                return cursor.rowcount > 0
            self._put(conn, "permission_profile", scope_type, item_key, {
                "scope_type": scope_type, "scope_name": scope_name,
                "capability": capability, "mode": mode,
                "updated_at": iso(utc_now()),
            })
            conn.commit()
        return True

    def permission_profile(self, scope_type: str, scope_name: str,
                           capability: str) -> dict[str, str] | None:
        scope_type, scope_name, capability = self._validate_permission(scope_type, scope_name, capability)
        names = [scope_name] if scope_name == "*" else [scope_name, "*"]
        with contextlib.closing(self.db.connect()) as conn:
            for name in names:
                item = self._get(conn, "permission_profile", scope_type, f"{name}|{capability}")
                if item and item.get("mode") in {"allow", "deny"}:
                    return {"scope_type": scope_type, "scope_name": str(item.get("scope_name") or name),
                            "capability": capability, "mode": str(item["mode"])}
        return None

    def permission_profiles(self, scope_type: str | None = None) -> list[dict[str, str]]:
        if scope_type is not None and scope_type not in _PERMISSION_CAPABILITIES:
            raise ValueError("Ruxsat turi account, chat, project, integration yoki server bo‘lishi kerak.")
        with contextlib.closing(self.db.connect()) as conn:
            rows = conn.execute("SELECT group_key,item_key,payload_enc FROM personal_records "
                                "WHERE owner_key=? AND kind='permission_profile'",
                                (self.owner,)).fetchall()
            items = [self.db.vault.open_json(row["payload_enc"]) for row in rows]
        result = [item for item in items if isinstance(item, dict)
                  and item.get("scope_type") in _PERMISSION_CAPABILITIES
                  and item.get("mode") in {"allow", "deny"}
                  and (scope_type is None or item.get("scope_type") == scope_type)]
        return sorted(result, key=lambda item: (item["scope_type"], item["scope_name"], item["capability"]))

    def project_note(self, project: str, action: str, note_key: str | None = None,
                     fact: str | None = None, update_id: int | None = None, *,
                     record_type: str = "note", participant: str | None = None,
                     url: str | None = None, document_id: str | None = None,
                     task_id: str | None = None, source_type: str = "bot_conversation",
                     source_chat: str | None = None, source_message_id: int | None = None,
                     source_location: str | None = None, query: str | None = None,
                     type_filter: str | None = None) -> list[dict[str, Any]]:
        project = self._label(project, 80)
        record_types = {"note", "decision", "participant", "link", "file", "next_step"}
        source_types = {"bot_conversation", "telegram_message", "document", "url", "owner_note"}
        if action not in {"save", "list", "search", "delete"}:
            raise ValueError("Loyiha xotirasi amali noto‘g‘ri.")
        if type_filter is not None and type_filter not in record_types:
            raise ValueError("Loyiha xotira turi tan olinmadi.")
        if action == "search" and not query:
            raise ValueError("Loyiha ichida qidirish uchun ibora kerak.")
        if query is not None:
            query = self._label(query, 120)
        project_id = self.db.vault.index("project-id-v1", f"{self.owner}:{self._key(project)}")[:16]
        project_key = self._key(project)
        if action == "save":
            note_key = self._label(note_key or "")
            fact = self._label(fact or "", 1200)
            if update_id is None or update_id <= 0:
                raise ValueError("Manba update ID si kerak.")
            if record_type not in record_types or source_type not in source_types:
                raise ValueError("Loyiha xotirasi turi yoki manba turi noto‘g‘ri.")
            participant = self._label(participant, 100) if participant else None
            source_chat = self._label(source_chat, 160) if source_chat else None
            source_location = self._label(source_location, 120) if source_location else None
            if participant and record_type != "participant":
                raise ValueError("Ishtirokchi nomi faqat participant yozuviga qo‘shiladi.")
            if record_type == "participant" and not participant:
                raise ValueError("Ishtirokchi yozuvi uchun aniq ism kerak.")
            if url is not None:
                url = self._label(url, 2048)
                parsed = urlsplit(url)
                if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                        or parsed.username or parsed.password):
                    raise ValueError("Loyiha havolasi HTTP(S) manzil bo‘lishi kerak.")
            if document_id is not None:
                document_id = str(document_id).lower().strip()
                if not re.fullmatch(r"[a-f0-9]{16}", document_id):
                    raise ValueError("Hujjat ID si noto‘g‘ri.")
            if task_id is not None:
                task_id = str(task_id).lower().strip()
                if not re.fullmatch(r"[a-f0-9]{12}", task_id):
                    raise ValueError("Vazifa ID si noto‘g‘ri.")
            if source_message_id is not None and source_message_id <= 0:
                raise ValueError("Manba xabar ID si musbat bo‘lishi kerak.")
            if source_type == "telegram_message" and (not source_chat or not source_message_id):
                raise ValueError("Telegram manbasi uchun chat nomi va xabar ID si kerak.")
            if source_type == "document" and not document_id:
                raise ValueError("Hujjat manbasi uchun mavjud hujjat ID si kerak.")
            if source_type == "url" and not url:
                raise ValueError("Havola manbasi uchun URL kerak.")
            if record_type == "link" and not url:
                raise ValueError("Havola yozuvi uchun aniq URL kerak.")
            if record_type == "file" and not document_id:
                raise ValueError("Fayl yozuvi uchun bilim bazasidagi hujjat ID si kerak.")

            document_metadata = None
            task_metadata = None
            with contextlib.closing(self.db.connect()) as conn:
                conn.execute("BEGIN IMMEDIATE")
                if document_id:
                    document = conn.execute("SELECT v.payload_enc FROM knowledge_documents d "
                        "JOIN knowledge_document_versions v ON v.file_id=d.id AND v.version=d.current_version "
                        "WHERE d.id=? AND d.owner_key=?", (document_id, self.owner)).fetchone()
                    if not document:
                        raise ValueError("Bu hujjat ID si egasining bilim bazasida topilmadi.")
                    document_metadata = self.db.vault.open_json(document["payload_enc"])
                if task_id:
                    task = conn.execute("SELECT payload_enc,status FROM assistant_tasks "
                        "WHERE id=? AND owner_key=?", (task_id, self.owner)).fetchone()
                    if not task:
                        raise ValueError("Bu vazifa ID si egasining vazifalar jurnalida topilmadi.")
                    task_metadata = {**self.db.vault.open_json(task["payload_enc"]), "status": task["status"]}

                item_key = self._key(note_key)
                previous = conn.execute("SELECT payload_enc FROM personal_records "
                    "WHERE owner_key=? AND kind='project_note' AND group_key=? AND item_key=?",
                    (self.owner, project_key, item_key)).fetchone()
                saved = self.db.vault.open_json(previous["payload_enc"]) if previous else None
                if saved and saved.get("source_update_id") == update_id:
                    conn.commit()
                    return [{**saved, "project_id": project_id,
                             "record_id": saved.get("record_id") or self.db.vault.index(
                                 "project-record-id-v1", f"{self.owner}:{project_key}:{item_key}")[:12]}]
                now = iso(utc_now())
                record_id = (saved or {}).get("record_id") or self.db.vault.index(
                    "project-record-id-v1", f"{self.owner}:{project_key}:{item_key}")[:12]
                payload = {"project": project, "project_id": project_id, "record_id": record_id,
                    "key": note_key, "fact": fact, "record_type": record_type,
                    "participant": participant, "url": url, "document_id": document_id,
                    "document_filename": document_metadata.get("filename") if document_metadata else None,
                    "document_version": document_metadata.get("version") if document_metadata else None,
                    "task_id": task_id, "task_title": task_metadata.get("title") if task_metadata else None,
                    "task_status": task_metadata.get("status") if task_metadata else None,
                    "source_type": source_type, "source_chat": source_chat,
                    "source_message_id": source_message_id, "source_update_id": int(update_id),
                    "source_location": source_location,
                    "created_at": (saved or {}).get("created_at", now), "updated_at": now}
                self._put(conn, "project_note", project, note_key, payload)
                conn.commit()
            return [payload]
        with contextlib.closing(self.db.connect()) as conn:
            if action == "delete":
                note_key = self._label(note_key or "")
                item_key = self._key(note_key)
                row = conn.execute("SELECT item_key,payload_enc FROM personal_records WHERE owner_key=? "
                    "AND kind='project_note' AND group_key=? AND item_key=?",
                    (self.owner, project_key, item_key)).fetchone()
                if not row:
                    candidates = conn.execute("SELECT item_key,payload_enc FROM personal_records WHERE owner_key=? "
                        "AND kind='project_note' AND group_key=?", (self.owner, project_key)).fetchall()
                    row = next((candidate for candidate in candidates
                        if (self.db.vault.open_json(candidate["payload_enc"]).get("record_id")
                            or self.db.vault.index("project-record-id-v1",
                                f"{self.owner}:{project_key}:{candidate['item_key']}")[:12]) == note_key), None)
                if not row:
                    return []
                saved = self.db.vault.open_json(row["payload_enc"])
                record_id = saved.get("record_id") or self.db.vault.index(
                    "project-record-id-v1", f"{self.owner}:{project_key}:{row['item_key']}")[:12]
                conn.execute("DELETE FROM personal_records WHERE owner_key=? AND kind='project_note' "
                             "AND group_key=? AND item_key=?", (self.owner, project_key, row["item_key"]))
                return [{"deleted": True, "record_id": record_id}]
            rows = conn.execute("SELECT item_key,payload_enc FROM personal_records WHERE owner_key=? "
                                "AND kind='project_note' AND group_key=? ORDER BY created_at DESC,item_key LIMIT 30",
                                (self.owner, project_key)).fetchall()
        results = []
        for row in rows:
            record = self.db.vault.open_json(row["payload_enc"])
            record.setdefault("project_id", project_id)
            record.setdefault("record_id", self.db.vault.index(
                "project-record-id-v1", f"{self.owner}:{project_key}:{row['item_key']}")[:12])
            record.setdefault("record_type", "note")
            record.setdefault("source_type", record.get("source", "bot_conversation"))
            record.setdefault("created_at", None)
            record.setdefault("updated_at", None)
            if type_filter and record.get("record_type") != type_filter:
                continue
            if action == "search" and query:
                haystack = " ".join(str(record.get(key) or "") for key in
                    ("key", "fact", "participant", "url", "document_filename", "task_title")).casefold()
                normalized_query = " ".join((query or "").casefold().split())
                if normalized_query not in haystack:
                    continue
            results.append(record)
        return results[:20]

    def set_preference(self, field: str, value: str | None) -> dict[str, str]:
        if field not in {"city", "language", "address", "length", "voice_speed", "voice_length"}:
            raise ValueError("Bunday afzallik mavjud emas.")
        if value is not None:
            value = self._label(value, 80 if field == "city" else 40)
            if field == "language" and value not in {"uz", "ru", "en"}:
                raise ValueError("Til uchun uz, ru yoki en ni tanlang.")
            if field == "length" and value not in {"short", "normal", "detailed"}:
                raise ValueError("Javob uzunligi short, normal yoki detailed bo‘lishi kerak.")
            if field == "voice_speed" and value not in {"slow", "normal", "fast"}:
                raise ValueError("Ovoz tezligi slow, normal yoki fast bo‘lishi kerak.")
            if field == "voice_length" and value not in {"short", "normal", "detailed"}:
                raise ValueError("Ovozli javob uzunligi short, normal yoki detailed bo‘lishi kerak.")
        with contextlib.closing(self.db.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            prefs = self._get(conn, "preferences", "", "profile") or {}
            if value is None:
                prefs.pop(field, None)
            else:
                prefs[field] = value
            self._put(conn, "preferences", "", "profile", prefs)
            conn.commit()
        return prefs

    def news_digest_settings(self) -> dict[str, str]:
        """Return this owner's encrypted RSS digest preferences."""
        with contextlib.closing(self.db.connect()) as conn:
            return self._get(conn, "news_digest", "", "settings") or {}

    def set_news_digest(self, *, frequency: str, time: str | None,
                        topic: str | None = None, region: str | None = None,
                        language: str | None = None, source: str | None = None) -> dict[str, str]:
        if frequency not in {"off", "daily", "weekly"}:
            raise ValueError("Yangiliklar jadvali off, daily yoki weekly bo‘lishi kerak.")
        if time is not None:
            time = self._label(time, 5)
            try:
                datetime.strptime(time, "%H:%M")
            except ValueError as exc:
                raise ValueError("Vaqt HH:MM, Toshkentning 24 soatlik formati bo‘lsin.") from exc
        filters = self.news_digest_settings()
        filters["frequency"] = frequency
        if time:
            filters["time"] = time
        for key, value in (("topic", topic), ("region", region),
                           ("language", language), ("source", source)):
            if value is not None:
                if value == "*":
                    filters.pop(key, None)
                else:
                    filters[key] = self._label(value, 80)
        if "language" in filters and filters["language"].casefold() not in {"uz", "ru", "en"}:
            raise ValueError("Yangilik tili uz, ru yoki en bo‘lishi kerak.")
        if frequency != "off" and not filters.get("time"):
            raise ValueError("Kunlik yoki haftalik yuborish vaqtini ham ayting.")
        with contextlib.closing(self.db.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._put(conn, "news_digest", "", "settings", filters)
            conn.commit()
        return filters

    def create_site_monitor(self, *, update_id: int, name: str, url: str,
                            interval_minutes: int = 60) -> dict[str, Any]:
        name = self._label(name, 80)
        url = self._label(url, 2048)
        parts = urlsplit(url)
        if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
                or parts.port not in (None, 443) or parts.fragment):
            raise ValueError("Kuzatuv manzili credential’siz, fragmentsiz HTTPS bo‘lishi kerak.")
        if type(interval_minutes) is not int or not 15 <= interval_minutes <= 1440:
            raise ValueError("Kuzatuv oralig‘i 15 daqiqadan 24 soatgacha bo‘lsin.")
        with contextlib.closing(self.db.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("SELECT payload_enc FROM personal_records WHERE owner_key=? AND kind='site_monitor'",
                                (self.owner,)).fetchall()
            monitors = [self.db.vault.open_json(row["payload_enc"]) for row in rows]
            previous = next((row for row in monitors if row.get("source_update_id") == update_id), None)
            if previous:
                conn.commit()
                return previous
            if len(monitors) >= 10:
                conn.rollback()
                raise ValueError("Ko‘pi bilan 10 ta sayt kuzatuvi saqlanadi.")
            if any(str(row.get("name", "")).casefold() == name.casefold() for row in monitors):
                conn.rollback()
                raise ValueError("Bu nomdagi kuzatuv bor; ro‘yxatni ko‘rib, mavjudini boshqaring.")
            monitor_id = uuid.uuid4().hex[:12]
            record: dict[str, Any] = {"id": monitor_id, "name": name, "url": url,
                                      "interval_minutes": interval_minutes, "active": True,
                                      "snapshot_text": "", "snapshot_sha256": "",
                                      "last_checked_at": None, "last_error": None,
                                      "is_up": None, "consecutive_failures": 0, "outage_started_at": None,
                                      "last_status_code": None, "response_time_ms": None,
                                      "certificate_expires_at": None, "certificate_error": None,
                                      "domain_expires_at": None, "domain_checked_at": None,
                                      "certificate_alert_keys": [], "domain_alert_keys": [],
                                      "structured_items": [], "structured_initialized": False,
                                      "source_update_id": update_id,
                                      "created_at": iso(utc_now())}
            self._put(conn, "site_monitor", monitor_id, "state", record)
            conn.commit()
        return record

    def site_monitors(self, *, active_only: bool = False) -> list[dict[str, Any]]:
        with contextlib.closing(self.db.connect()) as conn:
            rows = conn.execute("SELECT payload_enc FROM personal_records WHERE owner_key=? AND kind='site_monitor' "
                                "ORDER BY created_at,group_key", (self.owner,)).fetchall()
        items = [self.db.vault.open_json(row["payload_enc"]) for row in rows]
        return [item for item in items if item.get("active")] if active_only else items

    def update_site_monitor(self, monitor_id: str, *, snapshot_text: str,
                            snapshot_sha256: str, last_checked_at: str,
                            last_error: str | None = None,
                            updates: dict[str, Any] | None = None) -> dict[str, Any] | None:
        if not re.fullmatch(r"[a-f0-9]{12}", monitor_id):
            return None
        with contextlib.closing(self.db.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT payload_enc FROM personal_records WHERE owner_key=? AND kind='site_monitor' "
                               "AND group_key=? AND item_key=?",
                               (self.owner, self._key(monitor_id), self._key("state"))).fetchone()
            if not row:
                conn.rollback()
                return None
            record = self.db.vault.open_json(row["payload_enc"])
            record.update({"snapshot_text": snapshot_text[:50000], "snapshot_sha256": snapshot_sha256,
                           "last_checked_at": last_checked_at, "last_error": last_error})
            allowed_updates = {"is_up", "consecutive_failures", "outage_started_at", "last_status_code",
                               "response_time_ms", "certificate_expires_at", "certificate_error",
                               "domain_expires_at", "domain_checked_at", "certificate_alert_keys",
                               "domain_alert_keys", "structured_items", "structured_initialized"}
            if updates:
                record.update({key: value for key, value in updates.items() if key in allowed_updates})
            self._put(conn, "site_monitor", monitor_id, "state", record)
            conn.commit()
            return record

    def set_site_monitor_active(self, monitor_id: str, active: bool) -> dict[str, Any] | None:
        if not re.fullmatch(r"[a-f0-9]{12}", monitor_id):
            return None
        with contextlib.closing(self.db.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT payload_enc FROM personal_records WHERE owner_key=? AND kind='site_monitor' "
                               "AND group_key=? AND item_key=?",
                               (self.owner, self._key(monitor_id), self._key("state"))).fetchone()
            if not row:
                conn.rollback()
                return None
            record = self.db.vault.open_json(row["payload_enc"])
            record["active"] = bool(active)
            record["last_error"] = None
            self._put(conn, "site_monitor", monitor_id, "state", record)
            conn.commit()
            return record

    def delete_site_monitor(self, monitor_id: str) -> bool:
        if not re.fullmatch(r"[a-f0-9]{12}", monitor_id):
            return False
        with contextlib.closing(self.db.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            deleted = conn.execute("DELETE FROM personal_records WHERE owner_key=? AND kind='site_monitor' "
                                   "AND group_key=? AND item_key=?",
                                   (self.owner, self._key(monitor_id), self._key("state"))).rowcount == 1
            conn.commit()
            return deleted

    def pronunciation_lexicon(self, action: str, *, word: str | None = None,
                              spoken: str | None = None) -> dict[str, Any]:
        if action not in {"add", "list", "delete"}:
            raise ValueError("Talaffuz lug‘ati amali noto‘g‘ri.")
        if action == "list":
            with contextlib.closing(self.db.connect()) as conn:
                rows = conn.execute("SELECT payload_enc FROM personal_records WHERE owner_key=? "
                                    "AND kind='pronunciation' ORDER BY created_at,item_key LIMIT 60",
                                    (self.owner,)).fetchall()
            return {"entries": [self.db.vault.open_json(row["payload_enc"]) for row in rows]}
        word = self._label(word or "", 80)
        if action == "delete":
            with contextlib.closing(self.db.connect()) as conn:
                conn.execute("BEGIN IMMEDIATE")
                deleted = conn.execute("DELETE FROM personal_records WHERE owner_key=? AND kind='pronunciation' "
                                       "AND group_key=? AND item_key=?",
                                       (self.owner, self._key(""), self._key(word))).rowcount == 1
                conn.commit()
            return {"word": word, "deleted": deleted}
        spoken = self._label(spoken or "", 100)
        with contextlib.closing(self.db.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = self._get(conn, "pronunciation", "", word)
            count = conn.execute("SELECT COUNT(*) FROM personal_records WHERE owner_key=? AND kind='pronunciation'",
                                 (self.owner,)).fetchone()[0]
            if not existing and count >= 60:
                conn.rollback()
                raise ValueError("Talaffuz lug‘atida ko‘pi bilan 60 ta so‘z bo‘lishi mumkin.")
            self._put(conn, "pronunciation", "", word,
                      {"word": word, "spoken": spoken})
            conn.commit()
        return {"word": word, "spoken": spoken, "saved": True}

    def pronunciations(self) -> dict[str, str]:
        entries = self.pronunciation_lexicon("list")["entries"]
        return {str(item["word"]): str(item["spoken"]) for item in entries}

    def list_items(self, name: str | None) -> list[dict[str, Any]]:
        params = [self.owner]
        suffix = ""
        if name:
            suffix = " AND group_key=?"
            params.append(self._key(self._label(name)))
        with contextlib.closing(self.db.connect()) as conn:
            rows = conn.execute("SELECT payload_enc FROM personal_records WHERE owner_key=? AND kind='list_item'"
                                + suffix + " ORDER BY created_at,item_key LIMIT 40", params).fetchall()
        return [self.db.vault.open_json(row["payload_enc"]) for row in rows]

    def change_item(self, name: str | None, item: str, action: str,
                    replacement: str | None = None) -> dict[str, Any]:
        name = self._label(name) if name else None
        item = self._label(item)
        if action not in {"add", "complete", "edit", "remove"}:
            raise ValueError("Ro‘yxat amali noto‘g‘ri.")
        with contextlib.closing(self.db.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if name is None:
                rows = conn.execute("SELECT group_key,payload_enc FROM personal_records WHERE owner_key=? "
                                    "AND kind='list_item' AND item_key=?",
                                    (self.owner, self._key(item))).fetchall()
                matches = [self.db.vault.open_json(row["payload_enc"]) for row in rows]
                if len(matches) != 1:
                    raise ValueError("Bandni topish uchun ro‘yxat nomini ayting." if matches
                                     else "Bu band nomli ro‘yxatlarda topilmadi.")
                record = matches[0]
                name = record["list"]
            else:
                record = self._get(conn, "list_item", name, item)
            if not record and action != "add":
                raise ValueError("Bu band tanlangan ro‘yxatda topilmadi.")
            if action == "add":
                record = record or {"list": name, "item": item, "done": False}
                self._put(conn, "list_item", name, item, record)
            elif action == "complete":
                record["done"] = True
                self._put(conn, "list_item", name, item, record)
            elif action == "edit":
                new_item = self._label(replacement or "")
                if self._key(new_item) != self._key(item) and self._get(conn, "list_item", name, new_item):
                    raise ValueError("Bu nomdagi band avvaldan bor.")
                conn.execute("DELETE FROM personal_records WHERE owner_key=? AND kind='list_item' AND group_key=? AND item_key=?",
                             (self.owner, self._key(name), self._key(item)))
                record["item"] = new_item
                self._put(conn, "list_item", name, new_item, record)
            else:
                conn.execute("DELETE FROM personal_records WHERE owner_key=? AND kind='list_item' AND group_key=? AND item_key=?",
                             (self.owner, self._key(name), self._key(item)))
            conn.commit()
        return record

    def travel_plan(self, action: str, *, name: str | None = None, trip_id: str | None = None,
                    origin: str | None = None, destination: str | None = None,
                    depart_at: str | None = None, transport: str | None = None,
                    booking_info: str | None = None, checklist: list[str] | None = None,
                    item: str | None = None, query: str | None = None) -> dict[str, Any]:
        allowed = {"save", "list", "show", "delete", "add_item", "complete_item", "remove_item"}
        if action not in allowed:
            raise ValueError("Safar amali save, list, show, delete yoki checklist bandi bo‘lishi kerak.")
        if checklist is not None and (not isinstance(checklist, list) or len(checklist) > 30):
            raise ValueError("Safar tayyorgarlik ro‘yxatida 30 tagacha band bo‘lishi mumkin.")
        if action == "save":
            name = self._label(name or "", 100)
        if action in {"add_item", "complete_item", "remove_item"}:
            item = self._label(item or "", 120)
        if depart_at:
            try:
                parsed = datetime.fromisoformat(depart_at.replace("Z", "+00:00"))
            except ValueError:
                raise ValueError("Jo‘nash vaqti ISO formatda bo‘lishi kerak.") from None
            if parsed.tzinfo is None:
                raise ValueError("Jo‘nash vaqtida vaqt mintaqasi aniq bo‘lishi kerak.")
            depart_at = parsed.isoformat()
        if transport is not None:
            transport = self._label(transport, 40).upper()
            if transport not in {"DRIVE", "WALK", "BICYCLE", "TRANSIT", "TWO_WHEELER", "FLIGHT", "OTHER"}:
                raise ValueError("Transport DRIVE, WALK, BICYCLE, TRANSIT, TWO_WHEELER, FLIGHT yoki OTHER bo‘lishi kerak.")
        with contextlib.closing(self.db.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("SELECT item_key,payload_enc FROM personal_records WHERE owner_key=? AND kind='trip'",
                                (self.owner,)).fetchall()
            trips = [(row["item_key"], self.db.vault.open_json(row["payload_enc"])) for row in rows]
            if action == "list":
                needle = (query or "").strip().casefold()
                selected = [trip for _key, trip in trips if not needle or any(
                    needle in str(trip.get(field) or "").casefold()
                    for field in ("name", "origin", "destination", "booking_info"))]
                conn.commit()
                selected.sort(key=lambda trip: str(trip.get("depart_at") or ""))
                return {"trips": selected[:30]}
            matches = [(key, trip) for key, trip in trips if (
                (trip_id and str(trip.get("id") or "").casefold() == trip_id.strip().casefold()) or
                (name and str(trip.get("name") or "").casefold() == name.strip().casefold()))]
            if action == "save" and not matches:
                record = {"id": uuid.uuid4().hex[:12], "name": name, "origin": None,
                    "destination": None, "depart_at": None, "transport": None,
                    "booking_info": None, "checklist": [], "created_at": iso(utc_now())}
                key = self._key(name)
            elif len(matches) == 1:
                key, record = matches[0]
            elif not matches:
                conn.rollback()
                raise ValueError("Safar rejasi topilmadi; aniq nom yoki ID ni tekshiring.")
            else:
                conn.rollback()
                raise ValueError("Bir nechta safar mos keldi; ID sini ko‘rsating.")
            if action == "delete":
                cursor = conn.execute("DELETE FROM personal_records WHERE owner_key=? AND kind='trip' AND item_key=?",
                                      (self.owner, key))
                conn.commit()
                return {"id": record.get("id"), "name": record.get("name"), "deleted": cursor.rowcount == 1}
            if action == "save":
                for field, value, limit in (("origin", origin, 160), ("destination", destination, 160),
                                            ("booking_info", booking_info, 1500)):
                    if value is not None:
                        record[field] = self._label(value, limit)
                if depart_at is not None:
                    record["depart_at"] = depart_at
                if transport is not None:
                    record["transport"] = transport
                if checklist is not None:
                    cleaned = [self._label(value, 120) for value in checklist if isinstance(value, str)]
                    if len(cleaned) != len(checklist) or len(set(item.casefold() for item in cleaned)) != len(cleaned):
                        raise ValueError("Safar ro‘yxatida takror yoki noto‘g‘ri band bor.")
                    existing = {str(row.get("text", "")).casefold(): row for row in record.get("checklist", [])}
                    record["checklist"] = [existing.get(value.casefold(), {"text": value, "done": False})
                                            for value in cleaned]
                self._put(conn, "trip", "", str(record["id"]), record)
            elif action in {"add_item", "complete_item", "remove_item"}:
                checklist_rows = record.setdefault("checklist", [])
                found = next((row for row in checklist_rows if str(row.get("text", "")).casefold() == item.casefold()), None)
                if action == "add_item":
                    if found:
                        raise ValueError("Bu tayyorgarlik bandi safar rejasida bor.")
                    if len(checklist_rows) >= 30:
                        raise ValueError("Safar ro‘yxatida 30 ta band limiti to‘ldi.")
                    checklist_rows.append({"text": item, "done": False})
                elif not found:
                    raise ValueError("Safar rejasida bunday tayyorgarlik bandi yo‘q.")
                elif action == "complete_item":
                    found["done"] = True
                else:
                    checklist_rows.remove(found)
                self._put(conn, "trip", "", str(record["id"]), record)
            conn.commit()
            return {"trip": record}

    def habit(self, name: str, day: date, *, action: str, target: int | None = None) -> dict[str, Any]:
        name = self._label(name)
        if action not in {"create", "record", "status"} or (target is not None and not 1 <= target <= 7):
            raise ValueError("Odat rejasi haftasiga 1–7 marta bo‘lishi kerak.")
        added_today = False
        with contextlib.closing(self.db.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            record = self._get(conn, "habit", "", name)
            if action == "create":
                record = {"name": name, "weekly_target": target or 7}
                self._put(conn, "habit", "", name, record)
            elif not record:
                raise ValueError("Avval odatni nomi va haftalik rejasi bilan qo‘shing.")
            if action == "record" and not self._get(conn, "habit_check", name, day.isoformat()):
                self._put(conn, "habit_check", name, day.isoformat(), {"habit": record["name"]}, day.isoformat())
                added_today = True
            rows = conn.execute("SELECT local_day FROM personal_records WHERE owner_key=? AND kind='habit_check' "
                                "AND group_key=? AND local_day<=? ORDER BY local_day DESC",
                                (self.owner, self._key(name), day.isoformat())).fetchall()
            conn.commit()
        days = {date.fromisoformat(row["local_day"]) for row in rows}
        cursor = day if day in days else day - timedelta(days=1)
        streak = 0
        while cursor in days:
            streak += 1
            cursor -= timedelta(days=1)
        week_start = day - timedelta(days=day.weekday())
        weekly_counts: dict[date, int] = {}
        for logged_day in days:
            logged_week = logged_day - timedelta(days=logged_day.weekday())
            weekly_counts[logged_week] = weekly_counts.get(logged_week, 0) + 1
        week_count = weekly_counts.get(week_start, 0)
        streak_week = week_start if week_count >= record["weekly_target"] else week_start - timedelta(days=7)
        weekly_streak = 0
        while weekly_counts.get(streak_week, 0) >= record["weekly_target"]:
            weekly_streak += 1
            streak_week -= timedelta(days=7)
        goal_reached = added_today and week_count >= record["weekly_target"] and (
            week_count - 1 < record["weekly_target"])
        return {**record, "recorded_today": day in days, "week": week_count,
                "week_start": week_start.isoformat(), "streak": streak,
                "weekly_streak": weekly_streak, "goal_reached": goal_reached}

    def save_workflow_template(self, *, name: str, inputs: list[str], steps: list[dict[str, Any]],
                               approval_boundary: str, update_id: int) -> dict[str, Any]:
        name = self._label(name, 80)
        if approval_boundary not in {"read_only", "local_changes", "prepare_external"}:
            raise ValueError("Ish tartibi uchun tasdiq chegarasi noto‘g‘ri.")
        if not isinstance(inputs, list) or len(inputs) > 10:
            raise ValueError("Ish tartibida ko‘pi bilan 10 ta kirish parametri bo‘lishi mumkin.")
        normalized_inputs: list[str] = []
        for key in inputs:
            if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", key):
                raise ValueError("Kirish parametri kichik lotin harfi bilan boshlanib, harf, raqam yoki _ dan tuzilsin.")
            if key in normalized_inputs:
                raise ValueError("Kirish parametrlari takrorlanmasin.")
            normalized_inputs.append(key)
        if not isinstance(steps, list) or not 1 <= len(steps) <= 12:
            raise ValueError("Ish tartibida 1–12 ta qadam bo‘lishi kerak.")
        safe_steps: list[dict[str, Any]] = []
        marker_re = re.compile(r"\{\{([a-z][a-z0-9_]*)\}\}")
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                raise ValueError("Ish tartibidagi qadam tuzilishi noto‘g‘ri.")
            step_id = str(step.get("id") or f"step_{index + 1}")
            tool_name = str(step.get("tool") or "")
            arguments_json = str(step.get("arguments_json") or "")
            dependencies = step.get("depends_on", [])
            condition_json = step.get("condition_json")
            if (not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", step_id)
                    or not isinstance(dependencies, list)
                    or not all(isinstance(item, str) for item in dependencies)
                    or len(dependencies) != len(set(dependencies))
                    or condition_json is not None and not isinstance(condition_json, str)):
                raise ValueError("Ish tartibi qadamining ID, bog‘liqlik yoki sharti noto‘g‘ri.")
            if len(arguments_json) > 4000:
                raise ValueError("Qadam parametrlari juda uzun.")
            try:
                arguments = json.loads(arguments_json)
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError("Qadam parametrlari JSON obyekt bo‘lishi kerak.") from exc
            if not isinstance(arguments, dict):
                raise ValueError("Qadam parametrlari JSON obyekt bo‘lishi kerak.")
            markers = set(marker_re.findall(json.dumps(arguments, ensure_ascii=False)))
            unknown = markers - set(normalized_inputs)
            if unknown:
                raise ValueError("Qadamda e’lon qilinmagan parametr bor: " + ", ".join(sorted(unknown)))
            safe_steps.append({"id": step_id, "tool": tool_name,
                               "arguments_json": json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
                               "depends_on": dependencies, "condition_json": condition_json})
        with contextlib.closing(self.db.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = self._get(conn, "workflow_template", "", name) or {}
            if previous.get("last_update_id") == update_id:
                conn.commit()
                return {"name": name, "version": previous.get("current_version", 1), "created": False}
            versions = list(previous.get("versions") or [])
            version = int(previous.get("current_version") or 0) + 1
            snapshot = {"version": version, "inputs": normalized_inputs, "steps": safe_steps,
                        "approval_boundary": approval_boundary}
            versions.append(snapshot)
            versions = versions[-10:]
            payload = {"name": name, "current_version": version, "versions": versions,
                       "last_update_id": update_id}
            self._put(conn, "workflow_template", "", name, payload)
            conn.commit()
        return {"name": name, "version": version, "created": True}

    def workflow_templates(self) -> list[dict[str, Any]]:
        with contextlib.closing(self.db.connect()) as conn:
            rows = conn.execute("SELECT payload_enc FROM personal_records WHERE owner_key=? "
                                "AND kind='workflow_template' ORDER BY updated_at DESC,item_key LIMIT 30",
                                (self.owner,)).fetchall()
        return [self.db.vault.open_json(row["payload_enc"]) for row in rows]

    def workflow_template(self, name: str) -> dict[str, Any] | None:
        name = self._label(name, 80)
        with contextlib.closing(self.db.connect()) as conn:
            return self._get(conn, "workflow_template", "", name)

    def delete_workflow_template(self, name: str) -> bool:
        name = self._label(name, 80)
        with contextlib.closing(self.db.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            deleted = conn.execute("DELETE FROM personal_records WHERE owner_key=? AND kind='workflow_template' "
                                   "AND group_key=? AND item_key=?",
                                   (self.owner, self._key(""), self._key(name))).rowcount == 1
            conn.commit()
        return deleted

    def save_document_template(self, *, name: str, template_text: str, format: str,
                               document_type: str, title: str, style: str,
                               source: str, update_id: int) -> dict[str, Any]:
        name = self._label(name, 80)
        title = self._label(title, 180)
        template_text = str(template_text or "").strip()
        if not 1 <= len(template_text) <= 20_000:
            raise ValueError("Shablon matni 1–20 000 belgi bo‘lishi kerak.")
        fields = list(dict.fromkeys(re.findall(r"\[\[([a-z][a-z0-9_]{0,39})\]\]", template_text)))
        if not 1 <= len(fields) <= 20:
            raise ValueError("Shablonda 1–20 ta [[maydon_nomi]] ko‘rinishidagi to‘ldiriladigan maydon bo‘lishi kerak.")
        if format not in {"docx", "pdf", "pptx", "xlsx"}:
            raise ValueError("Shablon formati DOCX, PDF, PPTX yoki XLSX bo‘lishi kerak.")
        if document_type not in {"general", "letter", "proposal", "technical_spec",
                                 "meeting_minutes", "guide", "presentation", "table"}:
            raise ValueError("Shablon hujjat turi qo‘llab-quvvatlanmaydi.")
        if style not in {"formal", "simple", "compact"}:
            raise ValueError("Uslub formal, simple yoki compact bo‘lishi kerak.")
        if source not in {"uploaded_document", "latest_answer", "provided_text"}:
            raise ValueError("Shablon manbasi yaroqsiz.")
        with contextlib.closing(self.db.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = self._get(conn, "document_template", "", name) or {}
            if previous.get("last_update_id") == update_id:
                conn.commit()
                return {"name": name, "version": int(previous.get("current_version") or 1),
                        "created": False, "fields": list((previous.get("versions") or [{}])[-1].get("fields") or [])}
            version = int(previous.get("current_version") or 0) + 1
            snapshot = {"version": version, "template_text": template_text, "fields": fields,
                        "format": format, "document_type": document_type, "title": title,
                        "style": style, "source": source, "source_update_id": update_id,
                        "created_at": iso(utc_now())}
            versions = [*(previous.get("versions") or []), snapshot][-10:]
            payload = {"name": name, "current_version": version,
                       "versions": versions, "last_update_id": update_id}
            self._put(conn, "document_template", "", name, payload)
            conn.commit()
        return {"name": name, "version": version, "created": True, "fields": fields,
                "format": format, "style": style}

    def list_document_templates(self) -> list[dict[str, Any]]:
        with contextlib.closing(self.db.connect()) as conn:
            rows = conn.execute("SELECT payload_enc FROM personal_records WHERE owner_key=? "
                                "AND kind='document_template' ORDER BY updated_at DESC,item_key LIMIT 30",
                                (self.owner,)).fetchall()
        templates = []
        for row in rows:
            payload = self.db.vault.open_json(row["payload_enc"])
            current = next((item for item in reversed(payload.get("versions") or [])
                            if item.get("version") == payload.get("current_version")), {})
            templates.append({"name": payload.get("name"),
                              "current_version": payload.get("current_version"),
                              "versions": [int(item.get("version") or 0)
                                          for item in payload.get("versions") or []],
                              "format": current.get("format"), "style": current.get("style"),
                              "fields": list(current.get("fields") or [])})
        return templates

    def get_document_template(self, name: str, version: int | None = None) -> dict[str, Any] | None:
        name = self._label(name, 80)
        with contextlib.closing(self.db.connect()) as conn:
            payload = self._get(conn, "document_template", "", name)
        if not payload:
            return None
        requested = int(version or payload.get("current_version") or 1)
        return next((item for item in payload.get("versions") or []
                     if int(item.get("version") or 0) == requested), None)

    def delete_document_template(self, name: str) -> bool:
        name = self._label(name, 80)
        with contextlib.closing(self.db.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            deleted = conn.execute("DELETE FROM personal_records WHERE owner_key=? AND kind='document_template' "
                                   "AND group_key=? AND item_key=?",
                                   (self.owner, self._key(""), self._key(name))).rowcount == 1
            conn.commit()
        return deleted

    def begin_workflow_run(self, *, name: str, update_id: int, version: int,
                           inputs: dict[str, str] | None = None) -> dict[str, Any]:
        name = self._label(name, 80)
        with contextlib.closing(self.db.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM personal_records WHERE owner_key=? AND kind='workflow_run' AND updated_at<?",
                         (self.owner, iso(utc_now() - timedelta(days=30))))
            previous = self._get(conn, "workflow_run", name, str(update_id))
            if previous:
                conn.commit()
                return {"run": previous, "replay": True}
            record = {"name": name, "version": version, "update_id": update_id,
                      "format_version": 2, "inputs": dict(inputs or {}),
                      "status": "running", "current_step": None,
                      "completed_steps": [], "step_results": {}, "retry_count": 0,
                      "retryable": False, "failure_category": None,
                      "created_at": iso(utc_now())}
            self._put(conn, "workflow_run", name, str(update_id), record)
            conn.commit()
        return {"run": record, "replay": False}

    def update_workflow_run(self, *, name: str, update_id: int, status: str,
                            completed_steps: list[dict[str, Any]],
                            current_step: dict[str, Any] | None = None,
                            step_results: dict[str, dict[str, Any]] | None = None,
                            retry_count: int | None = None, retryable: bool = False,
                            failure_category: str | None = None) -> dict[str, Any]:
        if status not in {"running", "completed", "waiting", "blocked_permission",
                          "blocked_limit", "failed", "unknown"}:
            raise ValueError("Ish tartibi ijro holati noto‘g‘ri.")
        if retry_count is not None and (type(retry_count) is not int or retry_count < 0 or retry_count > 2):
            raise ValueError("Ish tartibi qayta urinish soni noto‘g‘ri.")
        name = self._label(name, 80)
        with contextlib.closing(self.db.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            record = self._get(conn, "workflow_run", name, str(update_id))
            if not record:
                conn.rollback()
                raise ValueError("Ish tartibi ijrosi topilmadi.")
            record["status"] = status
            record["completed_steps"] = completed_steps[-12:]
            record["current_step"] = current_step
            record["retryable"] = bool(retryable)
            record["failure_category"] = failure_category
            if retry_count is not None:
                record["retry_count"] = retry_count
            if step_results is not None:
                if len(json.dumps(step_results, ensure_ascii=False, default=str)) > 100_000:
                    conn.rollback()
                    raise ValueError("Ish tartibi natijalari davom ettirish uchun juda katta.")
                record["step_results"] = step_results
            record["updated_at"] = iso(utc_now())
            self._put(conn, "workflow_run", name, str(update_id), record)
            conn.commit()
        return record

    def workflow_run(self, *, name: str, update_id: int) -> dict[str, Any] | None:
        name = self._label(name, 80)
        if type(update_id) is not int or update_id <= 0:
            return None
        with contextlib.closing(self.db.connect()) as conn:
            return self._get(conn, "workflow_run", name, str(update_id))

    def workflow_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        limit = min(50, max(1, int(limit)))
        with contextlib.closing(self.db.connect()) as conn:
            rows = conn.execute("SELECT payload_enc,updated_at FROM personal_records "
                                "WHERE owner_key=? AND kind='workflow_run' "
                                "ORDER BY updated_at DESC LIMIT ?", (self.owner, limit)).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            record = self.db.vault.open_json(row["payload_enc"])
            completed = record.get("completed_steps") or []
            result.append({
                "name": record.get("name"), "version": record.get("version"),
                "update_id": record.get("update_id"), "status": record.get("status"),
                "updated_at": row["updated_at"], "created_at": record.get("created_at"),
                "current_step": ({"id": record["current_step"].get("id"),
                                  "tool": record["current_step"].get("tool")}
                                 if isinstance(record.get("current_step"), dict) else None),
                "completed_steps": [{"id": item.get("id"), "tool": item.get("tool"),
                                     "status": item.get("status")}
                                    for item in completed if isinstance(item, dict)],
                "failure_category": record.get("failure_category"),
                "retryable": bool(record.get("retryable")),
                "retry_count": int(record.get("retry_count") or 0),
            })
        return result

    @staticmethod
    def _money(amount: str, currency: str) -> tuple[int, int]:
        digits = {"UZS": 2, "USD": 2, "EUR": 2, "RUB": 2, "GBP": 2, "JPY": 0}.get(currency)
        if digits is None or not re.fullmatch(r"\d{1,12}(?:[.,]\d{1,3})?", amount):
            raise ValueError("Summani raqam bilan, valyutani UZS, USD, EUR, RUB, GBP yoki JPY bilan kiriting.")
        try:
            minor = Decimal(amount.replace(",", ".")) * 10 ** digits
            if minor != minor.to_integral_value() or minor <= 0:
                raise ValueError("Summa musbat va valyuta aniqligiga mos bo‘lishi kerak.")
            return int(minor), digits
        except InvalidOperation as exc:
            raise ValueError("Summa noto‘g‘ri.") from exc

    def add_transaction(self, *, update_id: int, day: date, amount: str, currency: str,
                        direction: str, category: str,
                        receipt_hash: str | None = None) -> dict[str, Any]:
        currency = currency.upper()
        minor, digits = self._money(amount, currency)
        if direction not in {"expense", "income"}:
            raise ValueError("Xarajat yoki daromad turini belgilang.")
        if receipt_hash is not None and not re.fullmatch(r"[a-f0-9]{64}", receipt_hash):
            raise ValueError("Chek identifikatori noto‘g‘ri.")
        record = {"currency": currency, "minor": minor, "digits": digits, "direction": direction,
                  "category": self._label(category), "day": day.isoformat()}
        with contextlib.closing(self.db.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if receipt_hash:
                prior_receipt = self._get(conn, "receipt_transaction", "", receipt_hash)
                if prior_receipt:
                    prior_transaction = self._get(conn, "transaction", "",
                                                  str(prior_receipt.get("update_id") or ""))
                    if prior_transaction:
                        conn.commit()
                        return {**prior_transaction, "created": False, "duplicate_receipt": True}
            saved = self._get(conn, "transaction", "", str(update_id))
            if not saved:
                self._put(conn, "transaction", "", str(update_id), record, day.isoformat())
                if receipt_hash:
                    self._put(conn, "receipt_transaction", "", receipt_hash,
                              {"update_id": update_id}, day.isoformat())
            conn.commit()
        return {**(saved or record), "created": not bool(saved), "duplicate_receipt": False}

    def set_budget(self, currency: str, amount: str) -> dict[str, Any]:
        currency = currency.upper()
        minor, digits = self._money(amount, currency)
        record = {"currency": currency, "minor": minor, "digits": digits}
        with contextlib.closing(self.db.connect()) as conn:
            self._put(conn, "budget", "", currency, record)
        return record

    def monthly_totals(self, day: date) -> list[dict[str, Any]]:
        start = day.replace(day=1)
        end = (start + timedelta(days=32)).replace(day=1)
        with contextlib.closing(self.db.connect()) as conn:
            rows = conn.execute("SELECT payload_enc FROM personal_records WHERE owner_key=? AND kind='transaction' "
                                "AND local_day>=? AND local_day<?", (self.owner, start.isoformat(), end.isoformat())).fetchall()
            totals: dict[str, dict[str, Any]] = {}
            for row in rows:
                record = self.db.vault.open_json(row["payload_enc"])
                currency = record["currency"]
                if currency not in totals:
                    budget = self._get(conn, "budget", "", currency)
                    totals[currency] = {"currency": currency, "digits": record["digits"], "expense": 0, "income": 0,
                                        "budget": budget["minor"] if budget else None}
                totals[currency][record["direction"]] += record["minor"]
        return [totals[currency] for currency in sorted(totals)]

    def usage(self, day: date) -> dict[str, int]:
        with contextlib.closing(self.db.connect()) as conn:
            row = conn.execute("SELECT requests,input_tokens,output_tokens,estimated_cost_micro_usd,cost_alert_sent "
                               "FROM model_usage WHERE owner_key=? AND local_day=?",
                               (self.owner, day.isoformat())).fetchone()
        return dict(row) if row else {"requests": 0, "input_tokens": 0, "output_tokens": 0,
                                     "estimated_cost_micro_usd": 0, "cost_alert_sent": 0}

    @staticmethod
    def _next_important_date(record: dict[str, Any], today: date) -> date:
        event_date = date.fromisoformat(str(record["date"]))
        if not record.get("annual"):
            return event_date
        try:
            candidate = date(today.year, event_date.month, event_date.day)
        except ValueError:
            # A February 29 anniversary is observed on February 28 in non-leap years.
            candidate = date(today.year, 2, 28)
        if candidate < today:
            try:
                candidate = date(today.year + 1, event_date.month, event_date.day)
            except ValueError:
                candidate = date(today.year + 1, 2, 28)
        return candidate

    @staticmethod
    def _subtract_calendar_months(value: date, months: int) -> date:
        """Subtract whole calendar months, clamping to the destination month end."""
        if months < 0:
            raise ValueError("Oylar manfiy bo‘lishi mumkin emas.")
        absolute_month = value.year * 12 + (value.month - 1) - months
        year, month_zero = divmod(absolute_month, 12)
        month = month_zero + 1
        last_day = calendar.monthrange(year, month)[1]
        return date(year, month, min(value.day, last_day))

    def create_important_date(self, *, update_id: int, title: str, event_date: str,
                              annual: bool, remind_days_before: int = 0,
                              remind_months_before: int = 0) -> dict[str, Any]:
        title = self._label(title, 120)
        try:
            parsed_date = date.fromisoformat(event_date)
        except (TypeError, ValueError) as exc:
            raise ValueError("Muhim sana YYYY-MM-DD formatida bo‘lishi kerak.") from exc
        if parsed_date < date(1900, 1, 1) or not 0 <= remind_days_before <= 30:
            raise ValueError("Sana 1900-yildan keyin va kunlik eslatma oralig‘i 0–30 kun bo‘lsin.")
        if type(remind_months_before) is not int or not 0 <= remind_months_before <= 24:
            raise ValueError("Kalendar oy bo‘yicha eslatma oralig‘i 0–24 oy bo‘lsin.")
        if remind_months_before and remind_days_before:
            raise ValueError("Eslatishni kun yoki kalendar oy bilan belgilang, ikkalasini birga emas.")
        now = utc_now()
        with contextlib.closing(self.db.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("SELECT item_key,payload_enc FROM personal_records WHERE owner_key=? "
                                "AND kind='important_date'", (self.owner,)).fetchall()
            for row in rows:
                prior = self.db.vault.open_json(row["payload_enc"])
                if prior.get("source_update_id") == update_id:
                    conn.commit()
                    return {**prior, "id": str(prior.get("id") or row["item_key"]), "created": False}
            active = [row for row in rows if self.db.vault.open_json(row["payload_enc"]).get("active")]
            if len(active) >= 100:
                conn.rollback()
                raise ValueError("Ko‘pi bilan 100 ta faol muhim sana saqlash mumkin.")
            item_id = uuid.uuid4().hex[:12]
            record = {"id": item_id, "title": title, "date": parsed_date.isoformat(),
                      "annual": bool(annual), "remind_days_before": remind_days_before,
                      "remind_months_before": remind_months_before,
                      "active": True, "created_at": iso(now), "source_update_id": update_id}
            self._put(conn, "important_date", "", item_id, record)
            conn.commit()
        return {**record, "created": True}

    def important_dates(self, today: date) -> list[dict[str, Any]]:
        with contextlib.closing(self.db.connect()) as conn:
            rows = conn.execute("SELECT item_key,payload_enc FROM personal_records WHERE owner_key=? "
                                "AND kind='important_date' ORDER BY created_at,item_key", (self.owner,)).fetchall()
        items = []
        for row in rows:
            record = self.db.vault.open_json(row["payload_enc"])
            if not record.get("active"):
                continue
            try:
                event_date = date.fromisoformat(record["date"])
                upcoming = self._next_important_date(record, today)
            except (KeyError, TypeError, ValueError):
                continue
            if not record.get("annual") and event_date < today:
                record["active"] = False
                self._save_important_date(record)
                continue
            items.append({**record, "id": str(record.get("id") or row["item_key"]),
                          "next_date": upcoming.isoformat()})
        return sorted(items, key=lambda item: (item["next_date"], item["id"]))[:100]

    def due_important_dates(self, today: date) -> list[dict[str, Any]]:
        due = []
        for record in self.important_dates(today):
            target = date.fromisoformat(record["next_date"])
            months_before = int(record.get("remind_months_before") or 0)
            if months_before:
                notify_date = self._subtract_calendar_months(target, months_before)
            else:
                notify_date = target - timedelta(days=int(record.get("remind_days_before") or 0))
            if notify_date <= today <= target:
                record["notify_date"] = notify_date.isoformat()
                due.append(record)
        return due

    def cancel_important_date(self, item_id: str) -> bool:
        item_id = item_id.strip().lower()
        with contextlib.closing(self.db.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            record = self._get(conn, "important_date", "", item_id)
            if not record or not record.get("active"):
                conn.rollback()
                return False
            record["active"] = False
            self._put(conn, "important_date", "", item_id, record)
            conn.commit()
        return True

    def _save_important_date(self, record: dict[str, Any]) -> None:
        with contextlib.closing(self.db.connect()) as conn:
            self._put(conn, "important_date", "", str(record["id"]), record)

    def create_weather_alert(self, *, update_id: int, city: str, threshold: int | float,
                             tomorrow: bool, interval_minutes: int, days: int,
                             metric: str = "precipitation_probability") -> dict[str, Any]:
        city = self._label(city, 80)
        if metric not in {"precipitation_probability", "temperature_above", "temperature_below", "wind_speed"}:
            raise ValueError("Ob-havo sharti yog‘ingarchilik, harorat yoki shamol bo‘lishi kerak.")
        try:
            parsed_threshold = Decimal(str(threshold))
        except (InvalidOperation, ValueError):
            raise ValueError("Ob-havo chegarasi son bo‘lishi kerak.") from None
        bounds = {
            "precipitation_probability": (Decimal("1"), Decimal("100")),
            "temperature_above": (Decimal("-80"), Decimal("60")),
            "temperature_below": (Decimal("-80"), Decimal("60")),
            "wind_speed": (Decimal("1"), Decimal("300")),
        }
        minimum, maximum = bounds[metric]
        if (not parsed_threshold.is_finite() or not minimum <= parsed_threshold <= maximum
                or not 15 <= interval_minutes <= 1440 or not 1 <= days <= 30):
            raise ValueError("Chegara tanlangan o‘lchovga mos bo‘lsin; interval 15–1440 daqiqa va muddat 1–30 kun bo‘lsin.")
        threshold_value: int | float = (int(parsed_threshold) if parsed_threshold == parsed_threshold.to_integral_value()
                                         else float(parsed_threshold))
        now = utc_now()
        with contextlib.closing(self.db.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("SELECT item_key,payload_enc FROM personal_records WHERE owner_key=? "
                                "AND kind='weather_alert'", (self.owner,)).fetchall()
            for row in rows:
                prior = self.db.vault.open_json(row["payload_enc"])
                if prior.get("source_update_id") == update_id:
                    conn.commit()
                    return {**prior, "id": str(prior.get("id") or row["item_key"]), "created": False}
            active = []
            for row in rows:
                prior = self.db.vault.open_json(row["payload_enc"])
                if not prior.get("active"):
                    continue
                try:
                    expires = datetime.fromisoformat(prior["expires_at"]).astimezone(UTC)
                except (KeyError, TypeError, ValueError):
                    continue
                if expires > now:
                    active.append(row)
            if len(active) >= 10:
                conn.rollback()
                raise ValueError("Bir vaqtda ko‘pi bilan 10 ta ob-havo kuzatuvi bo‘lishi mumkin.")
            alert_id = uuid.uuid4().hex[:12]
            record = {"id": alert_id, "city": city, "threshold": threshold_value, "metric": metric,
                      "tomorrow": bool(tomorrow),
                      "interval_minutes": interval_minutes, "active": True,
                      "next_check_at": iso(now), "expires_at": iso(now + timedelta(days=days)),
                      "last_notified_at": None, "source_update_id": update_id}
            self._put(conn, "weather_alert", "", alert_id, record)
            conn.commit()
        return {**record, "created": True}

    def weather_alerts(self, *, due_at: datetime | None = None) -> list[dict[str, Any]]:
        now = (due_at or utc_now()).astimezone(UTC)
        with contextlib.closing(self.db.connect()) as conn:
            rows = conn.execute("SELECT item_key,payload_enc FROM personal_records WHERE owner_key=? "
                                "AND kind='weather_alert' ORDER BY created_at,item_key", (self.owner,)).fetchall()
        items = []
        for row in rows:
            record = self.db.vault.open_json(row["payload_enc"])
            record.setdefault("id", row["item_key"])
            if record.get("active") and record.get("expires_at"):
                try:
                    expires = datetime.fromisoformat(record["expires_at"]).astimezone(UTC)
                    if expires <= now:
                        record["active"] = False
                        self._save_weather_alert(record)
                        continue
                    next_check = datetime.fromisoformat(record.get("next_check_at") or "").astimezone(UTC)
                    if due_at is None or next_check <= now:
                        items.append(record)
                except ValueError:
                    continue
        return items[:30]

    def cancel_weather_alert(self, alert_id: str) -> bool:
        alert_id = alert_id.strip().lower()
        with contextlib.closing(self.db.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            record = self._get(conn, "weather_alert", "", alert_id)
            if not record or not record.get("active"):
                conn.rollback()
                return False
            record["active"] = False
            self._put(conn, "weather_alert", "", alert_id, record)
            conn.commit()
        return True

    def advance_weather_alert(self, alert_id: str, *, next_check_at: datetime,
                              last_notified_at: datetime | None = None) -> None:
        with contextlib.closing(self.db.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            record = self._get(conn, "weather_alert", "", alert_id)
            if record and record.get("active"):
                record["next_check_at"] = iso(next_check_at)
                if last_notified_at:
                    record["last_notified_at"] = iso(last_notified_at)
                self._put(conn, "weather_alert", "", alert_id, record)
            conn.commit()

    def create_home_watch(self, *, update_id: int, entity_id: str, condition: str,
                          value: str | None = None, interval_minutes: int = 5,
                          days: int = 30) -> dict[str, Any]:
        entity_id = self._label(entity_id, 120).lower()
        if not re.fullmatch(r"[a-z0-9_]+\.[a-z0-9_]+", entity_id):
            raise ValueError("Home Assistant entity_id aniq sensor.nomi ko‘rinishida bo‘lishi kerak.")
        if condition not in {"change", "equals", "above", "below"}:
            raise ValueError("Sensor sharti change, equals, above yoki below bo‘lishi kerak.")
        if condition == "change":
            value = None
        else:
            value = self._label(value or "", 80)
            if condition in {"above", "below"}:
                try:
                    threshold = Decimal(value)
                except InvalidOperation:
                    raise ValueError("Sensor chegarasi son bo‘lishi kerak.") from None
                if not threshold.is_finite():
                    raise ValueError("Sensor chegarasi yaroqli son bo‘lishi kerak.")
        if not 1 <= interval_minutes <= 60 or not 1 <= days <= 30:
            raise ValueError("Sensor kuzatuvi har 1–60 daqiqada, 1–30 kun oralig‘ida ishlaydi.")
        now = utc_now()
        with contextlib.closing(self.db.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("SELECT item_key,payload_enc FROM personal_records WHERE owner_key=? "
                                "AND kind='home_watch'", (self.owner,)).fetchall()
            records = [self.db.vault.open_json(row["payload_enc"]) for row in rows]
            for record in records:
                if record.get("source_update_id") == update_id:
                    conn.commit()
                    return {**record, "created": False}
            active = [record for record in records if record.get("active") and
                      datetime.fromisoformat(record["expires_at"]).astimezone(UTC) > now]
            if any(record.get("entity_id") == entity_id and record.get("condition") == condition
                   and record.get("value") == value for record in active):
                conn.rollback()
                raise ValueError("Bu sensor sharti uchun faol kuzatuv allaqachon bor.")
            if len(active) >= 10:
                conn.rollback()
                raise ValueError("Bir vaqtda ko‘pi bilan 10 ta Home Assistant kuzatuvi bo‘lishi mumkin.")
            watch_id = uuid.uuid4().hex[:12]
            record = {"id": watch_id, "entity_id": entity_id, "condition": condition,
                "value": value, "interval_minutes": interval_minutes, "active": True,
                "next_check_at": iso(now), "expires_at": iso(now + timedelta(days=days)),
                "last_state": None, "last_condition_met": None, "source_update_id": update_id,
                "created_at": iso(now)}
            self._put(conn, "home_watch", "", watch_id, record)
            conn.commit()
        return {**record, "created": True}

    def home_watches(self, *, due_at: datetime | None = None) -> list[dict[str, Any]]:
        now = (due_at or utc_now()).astimezone(UTC)
        with contextlib.closing(self.db.connect()) as conn:
            rows = conn.execute("SELECT item_key,payload_enc FROM personal_records WHERE owner_key=? "
                                "AND kind='home_watch' ORDER BY created_at,item_key", (self.owner,)).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            record = self.db.vault.open_json(row["payload_enc"])
            expires = datetime.fromisoformat(record["expires_at"]).astimezone(UTC)
            if record.get("active") and expires <= now:
                record["active"] = False
                self._save_home_watch(record)
                continue
            next_check = datetime.fromisoformat(record["next_check_at"]).astimezone(UTC)
            if record.get("active") and (due_at is None or next_check <= now):
                result.append(record)
        return result[:10]

    def observe_home_watch(self, watch_id: str, state: str, *, observed_at: datetime) -> dict[str, Any] | None:
        watch_id = watch_id.strip().lower()
        state = self._label(state, 100)
        observed_at = observed_at.astimezone(UTC)
        with contextlib.closing(self.db.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            record = self._get(conn, "home_watch", "", watch_id)
            if not record or not record.get("active"):
                conn.rollback()
                return None
            if datetime.fromisoformat(record["expires_at"]).astimezone(UTC) <= observed_at:
                record["active"] = False
                self._put(conn, "home_watch", "", watch_id, record)
                conn.commit()
                return {"expired": True, "id": watch_id}
            condition = record["condition"]
            target_met: bool | None
            if condition == "change":
                target_met = None
            elif condition == "equals":
                target_met = state.casefold() == str(record.get("value") or "").casefold()
            else:
                try:
                    current = Decimal(state)
                    threshold = Decimal(str(record["value"]))
                    target_met = current > threshold if condition == "above" else current < threshold
                except (InvalidOperation, ValueError):
                    target_met = False
            previous = record.get("last_state")
            previous_met = record.get("last_condition_met")
            if previous is None:
                triggered = condition != "change" and target_met is True
            elif condition == "change":
                triggered = state != previous
            else:
                triggered = target_met is True and previous_met is False
            record["last_state"] = state
            record["last_condition_met"] = target_met
            record["last_checked_at"] = iso(observed_at)
            record["next_check_at"] = iso(observed_at + timedelta(minutes=int(record["interval_minutes"])))
            if triggered:
                record["last_triggered_at"] = iso(observed_at)
            self._put(conn, "home_watch", "", watch_id, record)
            conn.commit()
        return {"id": watch_id, "entity_id": record["entity_id"], "condition": condition,
            "value": record.get("value"), "previous_state": previous, "state": state,
            "triggered": triggered, "observed_at": iso(observed_at),
            "next_check_at": record["next_check_at"]}

    def cancel_home_watch(self, watch_id: str) -> bool:
        watch_id = watch_id.strip().lower()
        with contextlib.closing(self.db.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            record = self._get(conn, "home_watch", "", watch_id)
            if not record or not record.get("active"):
                conn.rollback()
                return False
            record["active"] = False
            self._put(conn, "home_watch", "", watch_id, record)
            conn.commit()
        return True

    def defer_home_watch(self, watch_id: str, next_check_at: datetime) -> None:
        with contextlib.closing(self.db.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            record = self._get(conn, "home_watch", "", watch_id.strip().lower())
            if record and record.get("active"):
                record["next_check_at"] = iso(next_check_at.astimezone(UTC))
                self._put(conn, "home_watch", "", str(record["id"]), record)
            conn.commit()

    def _save_home_watch(self, record: dict[str, Any]) -> None:
        with contextlib.closing(self.db.connect()) as conn:
            self._put(conn, "home_watch", "", str(record["id"]), record)

    def _save_weather_alert(self, record: dict[str, Any]) -> None:
        with contextlib.closing(self.db.connect()) as conn:
            self._put(conn, "weather_alert", "", str(record["id"]), record)

    def reserve_request(self, day: date, request_limit: int, token_limit: int,
                        daily_budget_micro_usd: int = 0) -> bool:
        budget = max(0, int(daily_budget_micro_usd))
        with contextlib.closing(self.db.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT OR IGNORE INTO model_usage(owner_key,local_day) VALUES(?,?)",
                         (self.owner, day.isoformat()))
            changed = conn.execute("""UPDATE model_usage SET requests=requests+1
                WHERE owner_key=? AND local_day=? AND requests<? AND input_tokens+output_tokens<?
                AND (?=0 OR estimated_cost_micro_usd<?)""",
                                   (self.owner, day.isoformat(), request_limit, token_limit,
                                    budget, budget)).rowcount
            conn.commit()
        return changed == 1

    def add_usage(self, day: date, input_tokens: int, output_tokens: int, *,
                  estimated_cost_micro_usd: int = 0, daily_budget_micro_usd: int = 0,
                  alert_percent: int = 80) -> bool:
        cost = max(0, int(estimated_cost_micro_usd))
        budget = max(0, int(daily_budget_micro_usd))
        alert_percent = min(100, max(1, int(alert_percent)))
        with contextlib.closing(self.db.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT OR IGNORE INTO model_usage(owner_key,local_day) VALUES(?,?)",
                         (self.owner, day.isoformat()))
            conn.execute("UPDATE model_usage SET input_tokens=input_tokens+?,output_tokens=output_tokens+?, "
                         "estimated_cost_micro_usd=estimated_cost_micro_usd+? "
                         "WHERE owner_key=? AND local_day=?",
                         (max(0, input_tokens), max(0, output_tokens), cost, self.owner, day.isoformat()))
            row = conn.execute("SELECT estimated_cost_micro_usd,cost_alert_sent FROM model_usage "
                               "WHERE owner_key=? AND local_day=?", (self.owner, day.isoformat())).fetchone()
            should_alert = bool(budget and cost and not row["cost_alert_sent"]
                                and row["estimated_cost_micro_usd"] * 100 >= budget * alert_percent)
            conn.commit()
        return should_alert

    def mark_usage_cost_alert_sent(self, day: date) -> bool:
        with contextlib.closing(self.db.connect()) as conn:
            changed = conn.execute("UPDATE model_usage SET cost_alert_sent=1 WHERE owner_key=? AND local_day=? "
                                   "AND cost_alert_sent=0", (self.owner, day.isoformat())).rowcount
        return changed == 1
