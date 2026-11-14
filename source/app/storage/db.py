from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import time
import unicodedata
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from app.core.recurrence import next_occurrence
from app.storage.security import Vault


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("datetime timezone bilan bo‘lishi kerak")
    return value.astimezone(UTC).isoformat(timespec="seconds")


# A deliberately small, conservative set for conversation recall. These are
# search aliases only; they never rewrite the archived user text or model input.
_HISTORY_SUFFIXES = tuple(sorted({
    "larimizdan", "larimizga", "larimizda", "larimizni", "larimizning",
    "laringizdan", "laringizga", "laringizda", "laringizni", "laringizning",
    "larimiz", "laringiz", "larining", "laridan", "lariga", "larida", "larini",
    "larning", "lardan", "larga", "larda", "larni", "lar",
    "imizdan", "imizga", "imizda", "imizni", "imizning", "imiz",
    "ingizdan", "ingizga", "ingizda", "ingizni", "ingizning", "ingiz",
    "ishimizdan", "ishimizga", "ishimizda", "ishimizni", "ishimiz",
    "ishingizdan", "ishingizga", "ishingizda", "ishingizni", "ishingiz",
    "ishlaridan", "ishlariga", "ishlarida", "ishlarini", "ishlarning", "ishlar",
    "ishidan", "ishiga", "ishida", "ishini", "ishdan", "ishga", "ishda",
    "ishim", "ishing", "ishi", "ishni", "ishni", "ishni", "ish",
    "gacha", "dagi", "ning", "dan", "ga", "ka", "qa", "ni", "da",
    "im", "ing", "si", "i",
    # Frequent finite verb endings used in informal recollections.
    "ganman", "gandim", "ganing", "ganda", "gan", "dim", "ding", "di",
}, key=len, reverse=True))


_HISTORY_CYRILLIC_TO_LATIN = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo",
    "ж": "j", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "x", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "'", "ь": "", "э": "e", "ю": "yu", "я": "ya", "ў": "o'", "қ": "q",
    "ғ": "g'", "ҳ": "h",
})
_HISTORY_LATIN_TO_CYRILLIC = str.maketrans({
    "a": "а", "b": "б", "v": "в", "g": "г", "d": "д", "e": "е", "j": "ж",
    "z": "з", "i": "и", "y": "й", "k": "к", "l": "л", "m": "м", "n": "н",
    "o": "о", "p": "п", "r": "р", "s": "с", "t": "т", "u": "у", "f": "ф",
    "x": "х", "q": "қ", "h": "ҳ", "'": "ъ",
})


def _history_cyrillic_alias(value: str) -> str:
    """Return a search-only Uzbek Cyrillic alias for a Latin token."""
    for latin, cyrillic in (("shch", "щ"), ("o'", "ў"), ("g'", "ғ"),
                            ("ch", "ч"), ("sh", "ш"), ("yo", "ё"),
                            ("yu", "ю"), ("ya", "я"), ("ts", "ц")):
        value = value.replace(latin, cyrillic)
    return value.translate(_HISTORY_LATIN_TO_CYRILLIC)


class Database:
    def __init__(self, path: Path | str, vault: Vault):
        self.path = str(path)
        self.vault = vault
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.migrate()

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA foreign_keys=ON")
        if self.path != ":memory:":
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
        return db

    def migrate(self) -> None:
        if self.path == ":memory:":
            self._migrate_locked()
            return
        lock_path = Path(self.path + ".migration.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a+b") as lock_file:
            try:
                os.chmod(lock_path, 0o600)
            except OSError:
                pass
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                self._migrate_locked()
            finally:
                lock_file.seek(0)
                if os.name == "nt":
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _migrate_locked(self) -> None:
        with contextlib.closing(self.connect()) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)""")
            row = db.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()
            version = int(row["version"] or 0)
            if version < 1:
                db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS bot_updates (
                    update_id INTEGER PRIMARY KEY, processed_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS poll_state (
                    id INTEGER PRIMARY KEY CHECK(id=1), next_offset INTEGER NOT NULL);
                INSERT OR IGNORE INTO poll_state(id,next_offset) VALUES(1,0);
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY, value_enc BLOB NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS consents (
                    owner_key TEXT NOT NULL, kind TEXT NOT NULL, granted INTEGER NOT NULL,
                    updated_at TEXT NOT NULL, PRIMARY KEY(owner_key, kind));
                CREATE TABLE IF NOT EXISTS reminders (
                    id TEXT PRIMARY KEY, owner_key TEXT NOT NULL, due_at TEXT NOT NULL,
                    payload_enc BLOB NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','sending','sent','failed','unknown','cancelled')),
                    created_at TEXT NOT NULL, sent_at TEXT);
                CREATE INDEX IF NOT EXISTS reminders_due ON reminders(status, due_at);
                CREATE TABLE IF NOT EXISTS reminder_drafts (
                    id TEXT PRIMARY KEY, approval_hash TEXT NOT NULL UNIQUE, owner_key TEXT NOT NULL,
                    chat_enc BLOB NOT NULL, preview_message_id INTEGER, payload_enc BLOB NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','approved','cancelled','expired')),
                    expires_at TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS speech_jobs (
                    id TEXT PRIMARY KEY, update_id INTEGER NOT NULL UNIQUE, owner_key TEXT NOT NULL,
                    payload_enc BLOB NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','processing','done','failed','cancelled')),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS speech_queue ON speech_jobs(status, created_at);
                CREATE TABLE IF NOT EXISTS profile_actions (
                    id TEXT PRIMARY KEY, approval_hash TEXT NOT NULL UNIQUE, owner_key TEXT NOT NULL,
                    preview_message_id INTEGER, payload_enc BLOB NOT NULL, random_id INTEGER NOT NULL UNIQUE,
                    source TEXT NOT NULL DEFAULT 'text' CHECK(source IN ('text','voice')),
                    status TEXT NOT NULL CHECK(status IN ('pending','queued','leased','dispatching','sent','failed','unknown','cancelled','expired')),
                    expires_at TEXT NOT NULL, lease_hash TEXT, lease_until TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, result_category TEXT);
                CREATE INDEX IF NOT EXISTS actions_claim ON profile_actions(status, expires_at, lease_until);
                CREATE TABLE IF NOT EXISTS outbox (
                    id TEXT PRIMARY KEY, dedupe_key TEXT NOT NULL UNIQUE, owner_key TEXT NOT NULL,
                    chat_enc BLOB NOT NULL, payload_enc BLOB NOT NULL, due_at TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','sending','sent','failed','unknown')),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, sent_at TEXT, error_category TEXT,
                    reminder_id TEXT, attempt_count INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS outbox_due ON outbox(status, due_at);
                CREATE TABLE IF NOT EXISTS news_seen (
                    entry_key TEXT PRIMARY KEY, seen_at TEXT NOT NULL);
                INSERT INTO schema_migrations(version, applied_at) VALUES(1, strftime('%Y-%m-%dT%H:%M:%SZ','now'));
                COMMIT;
            """)
                version = 1
            if version < 2:
                db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS assistant_history (
                    update_id INTEGER PRIMARY KEY, owner_key TEXT NOT NULL,
                    payload_enc BLOB NOT NULL, created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS assistant_history_owner ON assistant_history(owner_key, update_id);
                CREATE TABLE IF NOT EXISTS assistant_actions (
                    update_id INTEGER PRIMARY KEY, owner_key TEXT NOT NULL, action TEXT NOT NULL,
                    result_enc BLOB NOT NULL, created_at TEXT NOT NULL);
                INSERT INTO schema_migrations(version, applied_at) VALUES(2, strftime('%Y-%m-%dT%H:%M:%SZ','now'));
                COMMIT;
            """)
                version = 2
            if version < 3:
                db.executescript("""
                BEGIN IMMEDIATE;
                ALTER TABLE profile_actions ADD COLUMN schedule_id TEXT;
                CREATE UNIQUE INDEX profile_actions_schedule ON profile_actions(schedule_id) WHERE schedule_id IS NOT NULL;
                CREATE TABLE IF NOT EXISTS scheduled_profile_messages (
                    id TEXT PRIMARY KEY, owner_key TEXT NOT NULL, source_key TEXT NOT NULL,
                    approval_hash TEXT NOT NULL UNIQUE, preview_message_id INTEGER,
                    payload_enc BLOB NOT NULL, random_id INTEGER NOT NULL UNIQUE,
                    due_at TEXT NOT NULL, approval_expires_at TEXT NOT NULL, deliver_until TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','scheduled','queued','leased','dispatching','sent','failed','unknown','cancelled','expired')),
                    linked_action_id TEXT UNIQUE, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    result_category TEXT, UNIQUE(owner_key, source_key));
                CREATE INDEX scheduled_profile_due ON scheduled_profile_messages(status,due_at);
                CREATE TABLE IF NOT EXISTS assistant_knowledge (
                    id TEXT PRIMARY KEY, owner_key TEXT NOT NULL, identity_hash TEXT NOT NULL,
                    payload_enc BLOB NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('public','personal')),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    review_after TEXT, UNIQUE(owner_key, identity_hash));
                CREATE INDEX assistant_knowledge_owner_time ON assistant_knowledge(owner_key,updated_at DESC);
                INSERT INTO schema_migrations(version, applied_at) VALUES(3, strftime('%Y-%m-%dT%H:%M:%SZ','now'));
                COMMIT;
            """)
            if version < 4:
                db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS assistant_history_terms (
                    update_id INTEGER NOT NULL REFERENCES assistant_history(update_id) ON DELETE CASCADE,
                    owner_key TEXT NOT NULL, term_hash TEXT NOT NULL,
                    PRIMARY KEY(update_id,term_hash));
                CREATE INDEX IF NOT EXISTS assistant_history_term_lookup
                    ON assistant_history_terms(owner_key,term_hash,update_id);
                CREATE TABLE IF NOT EXISTS chat_aliases (
                    owner_key TEXT NOT NULL, alias_hash TEXT NOT NULL, payload_enc BLOB NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(owner_key,alias_hash));
                CREATE TABLE IF NOT EXISTS assistant_tasks (
                    id TEXT PRIMARY KEY, owner_key TEXT NOT NULL, source_key TEXT NOT NULL,
                    payload_enc BLOB NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('open','needs_confirmation','scheduled','running','succeeded','failed','cancelled','unknown')),
                    due_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(owner_key,source_key));
                CREATE INDEX IF NOT EXISTS assistant_tasks_owner_status_due
                    ON assistant_tasks(owner_key,status,due_at,updated_at DESC);
                CREATE TABLE IF NOT EXISTS assistant_action_log (
                    id TEXT PRIMARY KEY, owner_key TEXT NOT NULL, update_id INTEGER,
                    action TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT,
                    payload_enc BLOB NOT NULL, created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS assistant_action_log_owner_time
                    ON assistant_action_log(owner_key,created_at DESC);
                INSERT INTO schema_migrations(version, applied_at) VALUES(4, strftime('%Y-%m-%dT%H:%M:%SZ','now'));
                COMMIT;
            """)
                version = 4

            if version < 5:
                db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS recurring_reminders (
                    id TEXT PRIMARY KEY, owner_key TEXT NOT NULL, update_id INTEGER NOT NULL,
                    payload_enc BLOB NOT NULL, next_due_at TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
                    created_at TEXT NOT NULL, UNIQUE(owner_key,update_id));
                CREATE INDEX IF NOT EXISTS recurring_reminders_due
                    ON recurring_reminders(owner_key,active,next_due_at);
                CREATE TABLE IF NOT EXISTS recurring_reminder_runs (
                    rule_id TEXT NOT NULL REFERENCES recurring_reminders(id) ON DELETE CASCADE,
                    due_at TEXT NOT NULL, reminder_id TEXT NOT NULL UNIQUE REFERENCES reminders(id) ON DELETE CASCADE,
                    PRIMARY KEY(rule_id,due_at));
                INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                    VALUES(5, strftime('%Y-%m-%dT%H:%M:%SZ','now'));
                COMMIT;
                """)

            if version < 6:
                db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS personal_records (
                    owner_key TEXT NOT NULL, kind TEXT NOT NULL, group_key TEXT NOT NULL,
                    item_key TEXT NOT NULL, payload_enc BLOB NOT NULL, local_day TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(owner_key,kind,group_key,item_key));
                CREATE INDEX IF NOT EXISTS personal_records_day
                    ON personal_records(owner_key,kind,local_day);
                CREATE TABLE IF NOT EXISTS model_usage (
                    owner_key TEXT NOT NULL, local_day TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
                    requests INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(owner_key,local_day));
                INSERT OR IGNORE INTO schema_migrations(version,applied_at)
                    VALUES(6,strftime('%Y-%m-%dT%H:%M:%SZ','now'));
                COMMIT;
                """)

            if version < 7:
                db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS reply_drafts (
                    id TEXT PRIMARY KEY, owner_key TEXT NOT NULL, payload_enc BLOB NOT NULL,
                    preview_message_id INTEGER, edit_prompt_message_id INTEGER,
                    status TEXT NOT NULL CHECK(status IN ('pending','editing','selected','cancelled','expired')),
                    expires_at TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE INDEX reply_drafts_owner_status ON reply_drafts(owner_key,status,expires_at);
                CREATE INDEX reply_drafts_edit_prompt ON reply_drafts(owner_key,edit_prompt_message_id,status);
                INSERT INTO schema_migrations(version,applied_at)
                    VALUES(7,strftime('%Y-%m-%dT%H:%M:%SZ','now'));
                COMMIT;
                """)

            if version < 8:
                db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS knowledge_documents (
                    id TEXT PRIMARY KEY, owner_key TEXT NOT NULL, filename_hash TEXT NOT NULL,
                    current_version INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(owner_key, filename_hash));
                CREATE INDEX IF NOT EXISTS knowledge_documents_owner_time
                    ON knowledge_documents(owner_key, updated_at DESC);
                CREATE TABLE IF NOT EXISTS knowledge_document_versions (
                    file_id TEXT NOT NULL REFERENCES knowledge_documents(id) ON DELETE CASCADE,
                    version INTEGER NOT NULL, owner_key TEXT NOT NULL, content_hash TEXT NOT NULL,
                    upload_update_id INTEGER NOT NULL, payload_enc BLOB NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(file_id, version), UNIQUE(owner_key, upload_update_id));
                CREATE TABLE IF NOT EXISTS knowledge_document_chunks (
                    id TEXT PRIMARY KEY, owner_key TEXT NOT NULL, file_id TEXT NOT NULL, version INTEGER NOT NULL,
                    chunk_index INTEGER NOT NULL, payload_enc BLOB NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(file_id, version, chunk_index),
                    FOREIGN KEY(file_id, version) REFERENCES knowledge_document_versions(file_id, version)
                        ON DELETE CASCADE);
                CREATE INDEX IF NOT EXISTS knowledge_document_chunks_owner_file
                    ON knowledge_document_chunks(owner_key, file_id, version, chunk_index);
                CREATE TABLE IF NOT EXISTS knowledge_document_terms (
                    chunk_id TEXT NOT NULL REFERENCES knowledge_document_chunks(id) ON DELETE CASCADE,
                    owner_key TEXT NOT NULL, term_hash TEXT NOT NULL,
                    PRIMARY KEY(chunk_id, term_hash));
                CREATE INDEX IF NOT EXISTS knowledge_document_terms_lookup
                    ON knowledge_document_terms(owner_key, term_hash, chunk_id);
                INSERT INTO schema_migrations(version,applied_at)
                    VALUES(8,strftime('%Y-%m-%dT%H:%M:%SZ','now'));
                COMMIT;
                """)
                version = 8

            if version < 9:
                db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS group_welcome_rules (
                    owner_key TEXT NOT NULL, chat_key TEXT NOT NULL,
                    payload_enc BLOB NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(owner_key,chat_key));
                CREATE TABLE IF NOT EXISTS group_welcome_events (
                    owner_key TEXT NOT NULL, event_key TEXT NOT NULL, payload_enc BLOB NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','sent','failed','unknown')),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(owner_key,event_key));
                CREATE INDEX IF NOT EXISTS group_welcome_events_time
                    ON group_welcome_events(owner_key,created_at);
                INSERT INTO schema_migrations(version,applied_at)
                    VALUES(9,strftime('%Y-%m-%dT%H:%M:%SZ','now'));
                COMMIT;
                """)
                version = 9

            if version < 10:
                db.executescript("""
                BEGIN IMMEDIATE;
                ALTER TABLE model_usage ADD COLUMN estimated_cost_micro_usd INTEGER NOT NULL DEFAULT 0;
                ALTER TABLE model_usage ADD COLUMN cost_alert_sent INTEGER NOT NULL DEFAULT 0;
                INSERT INTO schema_migrations(version,applied_at)
                    VALUES(10,strftime('%Y-%m-%dT%H:%M:%SZ','now'));
                COMMIT;
                """)
                version = 10

            if version < 11:
                db.executescript("""
                CREATE TABLE IF NOT EXISTS outbox (
                    id TEXT PRIMARY KEY, dedupe_key TEXT NOT NULL UNIQUE, owner_key TEXT NOT NULL,
                    chat_enc BLOB NOT NULL, payload_enc BLOB NOT NULL, due_at TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','sending','sent','failed','unknown','cancelled')),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, sent_at TEXT, error_category TEXT,
                    reminder_id TEXT, attempt_count INTEGER NOT NULL DEFAULT 0);
                BEGIN IMMEDIATE;
                DROP INDEX IF EXISTS outbox_due;
                ALTER TABLE outbox RENAME TO outbox_before_cancel_support;
                CREATE TABLE outbox (
                    id TEXT PRIMARY KEY, dedupe_key TEXT NOT NULL UNIQUE, owner_key TEXT NOT NULL,
                    chat_enc BLOB NOT NULL, payload_enc BLOB NOT NULL, due_at TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','sending','sent','failed','unknown','cancelled')),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, sent_at TEXT, error_category TEXT,
                    reminder_id TEXT, attempt_count INTEGER NOT NULL DEFAULT 0);
                INSERT INTO outbox(id,dedupe_key,owner_key,chat_enc,payload_enc,due_at,status,created_at,updated_at,
                                   sent_at,error_category,reminder_id,attempt_count)
                    SELECT id,dedupe_key,owner_key,chat_enc,payload_enc,due_at,status,created_at,updated_at,
                           sent_at,error_category,reminder_id,attempt_count FROM outbox_before_cancel_support;
                DROP TABLE outbox_before_cancel_support;
                CREATE INDEX IF NOT EXISTS outbox_due ON outbox(status,due_at);
                INSERT INTO schema_migrations(version,applied_at)
                    VALUES(11,strftime('%Y-%m-%dT%H:%M:%SZ','now'));
                COMMIT;
                """)
                version = 11

            if version < 12:
                db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS telegram_business_connections (
                    owner_key TEXT NOT NULL, connection_key TEXT NOT NULL, payload_enc BLOB NOT NULL,
                    active INTEGER NOT NULL CHECK(active IN (0,1)), updated_at TEXT NOT NULL,
                    PRIMARY KEY(owner_key,connection_key));
                CREATE TABLE IF NOT EXISTS telegram_business_drafts (
                    id TEXT PRIMARY KEY, owner_key TEXT NOT NULL, connection_key TEXT NOT NULL,
                    update_id INTEGER NOT NULL, payload_enc BLOB NOT NULL, preview_message_id INTEGER,
                    status TEXT NOT NULL CHECK(status IN ('pending','sending','sent','failed','unknown','cancelled','expired')),
                    expires_at TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(owner_key,update_id));
                CREATE INDEX IF NOT EXISTS telegram_business_drafts_owner_status
                    ON telegram_business_drafts(owner_key,status,expires_at);
                CREATE INDEX IF NOT EXISTS telegram_business_drafts_connection
                    ON telegram_business_drafts(owner_key,connection_key,status);
                INSERT INTO schema_migrations(version,applied_at)
                    VALUES(12,strftime('%Y-%m-%dT%H:%M:%SZ','now'));
                COMMIT;
                """)
                version = 12

            if version < 13:
                db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS telegram_call_attempts (
                    id TEXT PRIMARY KEY, owner_key TEXT NOT NULL, attempt_key TEXT NOT NULL,
                    payload_enc BLOB NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('needs_confirmation','calling','answered',
                        'rejected','no_answer','failed','unknown','cancelled','expired')),
                    voice_draft_enc BLOB, expires_at TEXT NOT NULL, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, UNIQUE(owner_key,attempt_key));
                CREATE INDEX IF NOT EXISTS telegram_call_attempts_owner_status
                    ON telegram_call_attempts(owner_key,status,expires_at);
                INSERT INTO schema_migrations(version,applied_at)
                    VALUES(13,strftime('%Y-%m-%dT%H:%M:%SZ','now'));
                COMMIT;
                """)
                version = 13

            if version < 14:
                db.executescript("""
                BEGIN IMMEDIATE;
                ALTER TABLE profile_actions ADD COLUMN call_fallback_for TEXT;
                CREATE UNIQUE INDEX profile_actions_call_fallback_active
                    ON profile_actions(call_fallback_for)
                    WHERE call_fallback_for IS NOT NULL
                      AND status IN ('pending','queued','leased','dispatching','sent','unknown');
                INSERT INTO schema_migrations(version,applied_at)
                    VALUES(14,strftime('%Y-%m-%dT%H:%M:%SZ','now'));
                COMMIT;
                """)
                version = 14

            if version < 15:
                db.executescript("""
                BEGIN IMMEDIATE;
                ALTER TABLE profile_actions ADD COLUMN call_attempt_id TEXT;
                CREATE UNIQUE INDEX profile_actions_call_attempt_active
                    ON profile_actions(call_attempt_id)
                    WHERE call_attempt_id IS NOT NULL
                      AND status IN ('pending','queued','leased','dispatching','sent','unknown');
                INSERT INTO schema_migrations(version,applied_at)
                    VALUES(15,strftime('%Y-%m-%dT%H:%M:%SZ','now'));
                COMMIT;
                """)
                version = 15

            if version < 16:
                db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS integration_action_drafts (
                    id TEXT PRIMARY KEY, approval_hash TEXT NOT NULL UNIQUE,
                    owner_key TEXT NOT NULL, source_update_id INTEGER NOT NULL,
                    preview_message_id INTEGER, payload_enc BLOB NOT NULL, result_enc BLOB,
                    status TEXT NOT NULL CHECK(status IN ('pending','executing','succeeded','failed','unknown','cancelled','expired')),
                    expires_at TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(owner_key,source_update_id));
                CREATE INDEX IF NOT EXISTS integration_action_owner_status
                    ON integration_action_drafts(owner_key,status,expires_at);
                INSERT INTO schema_migrations(version,applied_at)
                    VALUES(16,strftime('%Y-%m-%dT%H:%M:%SZ','now'));
                COMMIT;
                """)
                version = 16

            if version < 17:
                # Add suffix-normalized HMAC terms to existing encrypted turns.
                # INSERT OR IGNORE makes the backfill restart-safe if the process
                # exits mid-migration; the version is recorded only after it ends.
                history_table = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='assistant_history'"
                ).fetchone()
                if history_table:
                    cursor = db.execute(
                        "SELECT update_id,owner_key,payload_enc FROM assistant_history ORDER BY update_id"
                    )
                    while rows := cursor.fetchmany(250):
                        index_rows: list[tuple[int, str, str]] = []
                        for row in rows:
                            payload = self.vault.open_json(row["payload_enc"])
                            if not isinstance(payload, dict):
                                continue
                            text = " ".join(str(payload.get(part) or "")
                                            for part in ("user", "assistant"))
                            index_rows.extend(
                                (int(row["update_id"]), str(row["owner_key"]),
                                 self.vault.index("assistant-history-term", term))
                                for term in self._history_terms(text)
                            )
                        if index_rows:
                            db.executemany(
                                "INSERT OR IGNORE INTO assistant_history_terms"
                                "(update_id,owner_key,term_hash) VALUES(?,?,?)", index_rows
                            )
                db.execute(
                    "INSERT INTO schema_migrations(version,applied_at) "
                    "VALUES(17,strftime('%Y-%m-%dT%H:%M:%SZ','now'))"
                )
                version = 17

            if version >= 4:
                history_table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='assistant_history'").fetchone()
                rows = (db.execute("SELECT h.update_id,h.owner_key,h.payload_enc FROM assistant_history h "
                                   "WHERE NOT EXISTS (SELECT 1 FROM assistant_history_terms t WHERE t.update_id=h.update_id)").fetchall()
                        if history_table else [])
                for row in rows:
                    payload = self.vault.open_json(row["payload_enc"])
                    if isinstance(payload, dict):
                        terms = self._history_terms(" ".join(str(payload.get(part) or "")
                                                               for part in ("user", "assistant")))
                        db.executemany("INSERT OR IGNORE INTO assistant_history_terms(update_id,owner_key,term_hash) VALUES(?,?,?)",
                                       [(row["update_id"], row["owner_key"], self.vault.index("assistant-history-term", term))
                                        for term in terms])

    @staticmethod
    def owner_key(owner_id: int, vault: Vault) -> str:
        return vault.index("owner", owner_id)

    def _new_outbox(self, db: sqlite3.Connection, *, dedupe_key: str, owner_id: int,
                    chat_id: int, text: str, due_at: datetime) -> None:
        db.execute("""INSERT OR IGNORE INTO outbox
            (id,dedupe_key,owner_key,chat_enc,payload_enc,due_at,status,created_at,updated_at)
            VALUES(?,?,?,?,?,?,'pending',?,?)""",
            (str(uuid.uuid4()), dedupe_key, self.owner_key(owner_id, self.vault),
             self.vault.seal(str(chat_id)), self.vault.seal(text), iso(due_at), iso(utc_now()), iso(utc_now())))

    def enqueue_message(self, *, dedupe_key: str, owner_id: int, chat_id: int,
                        text: str, due_at: datetime | None = None) -> None:
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            self._new_outbox(db, dedupe_key=dedupe_key, owner_id=owner_id, chat_id=chat_id,
                             text=text, due_at=due_at or utc_now())
            db.commit()

    def set_group_welcome_rule(self, owner_id: int, chat_id: int, chat_name: str,
                               body: str | None, *, enabled: bool) -> dict[str, Any]:
        if chat_id == 0 or len(chat_name.strip()) > 160 or (enabled and (
                not body or not body.strip() or len(body) > 1000)):
            raise ValueError("Guruh kutib olish qoidasi noto‘g‘ri.")
        owner_key = self.owner_key(owner_id, self.vault)
        chat_key = self.vault.index("group-welcome-chat", chat_id)
        now = iso(utc_now())
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload_enc FROM group_welcome_rules WHERE owner_key=? AND chat_key=?",
                             (owner_key, chat_key)).fetchone()
            current = self.vault.open_json(row["payload_enc"]) if row else {}
            if enabled:
                payload = {"chat_id": int(chat_id), "chat_name": chat_name.strip()[:160],
                           "body": body.strip(), "enabled": True}
            elif row:
                payload = {**current, "enabled": False}
            else:
                db.rollback()
                return {"enabled": False, "exists": False}
            db.execute("""INSERT INTO group_welcome_rules(owner_key,chat_key,payload_enc,updated_at)
                VALUES(?,?,?,?) ON CONFLICT(owner_key,chat_key) DO UPDATE SET
                payload_enc=excluded.payload_enc,updated_at=excluded.updated_at""",
                (owner_key, chat_key, self.vault.seal_json(payload), now))
            db.commit()
        return {"exists": True, "enabled": bool(payload["enabled"]), "chat_name": payload.get("chat_name", chat_name),
                "body": payload.get("body", "")}

    def list_group_welcome_rules(self, owner_id: int) -> list[dict[str, Any]]:
        owner_key = self.owner_key(owner_id, self.vault)
        with contextlib.closing(self.connect()) as db:
            rows = db.execute("SELECT payload_enc,updated_at FROM group_welcome_rules WHERE owner_key=? "
                              "ORDER BY updated_at DESC", (owner_key,)).fetchall()
        return [{**self.vault.open_json(row["payload_enc"]), "updated_at": row["updated_at"]}
                for row in rows]

    def claim_group_welcome_event(self, owner_id: int, chat_id: int, event_key: str,
                                  user_id: int, user_name: str) -> dict[str, Any] | None:
        owner_key = self.owner_key(owner_id, self.vault)
        chat_key = self.vault.index("group-welcome-chat", chat_id)
        event_hash = self.vault.index("group-welcome-event", event_key)
        now = iso(utc_now())
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            rule = db.execute("SELECT payload_enc FROM group_welcome_rules WHERE owner_key=? AND chat_key=?",
                              (owner_key, chat_key)).fetchone()
            payload = self.vault.open_json(rule["payload_enc"]) if rule else {}
            if not payload.get("enabled") or not payload.get("body"):
                db.rollback()
                return None
            inserted = db.execute("""INSERT OR IGNORE INTO group_welcome_events
                (owner_key,event_key,payload_enc,status,created_at,updated_at)
                VALUES(?,?,?,'pending',?,?)""",
                (owner_key, event_hash,
                 self.vault.seal_json({"chat_id": chat_id, "user_id": user_id,
                                       "user_name": user_name[:100]}), now, now)).rowcount
            if not inserted:
                db.rollback()
                return None
            db.execute("DELETE FROM group_welcome_events WHERE owner_key=? AND created_at<?",
                       (owner_key, iso(utc_now() - timedelta(days=90))))
            db.commit()
        return payload

    def finish_group_welcome_event(self, owner_id: int, event_key: str, status: str) -> bool:
        if status not in {"sent", "failed", "unknown"}:
            raise ValueError("Noto‘g‘ri kutib olish natijasi.")
        owner_key = self.owner_key(owner_id, self.vault)
        event_hash = self.vault.index("group-welcome-event", event_key)
        with contextlib.closing(self.connect()) as db:
            cur = db.execute("UPDATE group_welcome_events SET status=?,updated_at=? "
                             "WHERE owner_key=? AND event_key=? AND status='pending'",
                             (status, iso(utc_now()), owner_key, event_hash))
            return cur.rowcount > 0

    def has_outbox_dedupe(self, dedupe_key: str) -> bool:
        with contextlib.closing(self.connect()) as db:
            return db.execute("SELECT 1 FROM outbox WHERE dedupe_key=? LIMIT 1",
                              (dedupe_key,)).fetchone() is not None

    def list_pending_outbox(self, owner_id: int, *, limit: int = 20) -> list[dict[str, Any]]:
        with contextlib.closing(self.connect()) as db:
            rows = db.execute("SELECT id,chat_enc,payload_enc,due_at,created_at,attempt_count FROM outbox "
                              "WHERE owner_key=? AND status='pending' ORDER BY due_at,created_at LIMIT ?",
                              (self.owner_key(owner_id, self.vault), max(1, min(limit, 30)))).fetchall()
        return [{"id": row["id"], "chat_id": int(self.vault.open_text(row["chat_enc"])),
                 "text": self.vault.open_text(row["payload_enc"]), "due_at": row["due_at"],
                 "created_at": row["created_at"], "attempt_count": int(row["attempt_count"])}
                for row in rows]

    def cancel_pending_outbox(self, owner_id: int, item_prefix: str) -> str:
        prefix = str(item_prefix or "").strip().lower()
        if len(prefix) < 8 or len(prefix) > 32 or any(char not in "0123456789abcdef" for char in prefix):
            return "invalid"
        owner_key = self.owner_key(owner_id, self.vault)
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT id,reminder_id FROM outbox WHERE owner_key=? AND status='pending' "
                              "AND id LIKE ? ORDER BY id LIMIT 2", (owner_key, prefix + "%")).fetchall()
            if not rows:
                db.rollback()
                return "not_found"
            if len(rows) > 1:
                db.rollback()
                return "ambiguous"
            row = rows[0]
            changed = db.execute("UPDATE outbox SET status='cancelled',error_category='owner_cancelled',updated_at=? "
                                 "WHERE id=? AND owner_key=? AND status='pending'",
                                 (iso(utc_now()), row["id"], owner_key)).rowcount
            if not changed:
                db.rollback()
                return "not_found"
            if row["reminder_id"]:
                db.execute("UPDATE reminders SET status='cancelled' WHERE id=? AND owner_key=? AND status='pending'",
                           (row["reminder_id"], owner_key))
            db.commit()
        return "cancelled"

    def create_reminder(self, *, update_id: int, owner_id: int, chat_id: int,
                        due_at: datetime, body: str) -> tuple[str, bool]:
        reminder_id = uuid.uuid4().hex[:10]
        owner_key = self.owner_key(owner_id, self.vault)
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            inserted = db.execute("INSERT OR IGNORE INTO bot_updates VALUES(?,?)", (update_id, iso(utc_now()))).rowcount
            if not inserted:
                db.rollback()
                return "", False
            db.execute("INSERT INTO reminders VALUES(?,?,?,?,?,?,NULL)",
                       (reminder_id, owner_key, iso(due_at), self.vault.seal(body), "pending", iso(utc_now())))
            self._new_outbox(db, dedupe_key=f"update:{update_id}:reply", owner_id=owner_id,
                             chat_id=chat_id,
                             text=f"Eslatma qo‘yildi.\nSana: {due_at.isoformat()}\nMatn: {body}\nID: {reminder_id}",
                             due_at=utc_now())
            db.commit()
        return reminder_id, True

    def create_reminder_draft(self, owner_id: int, chat_id: int, due_at: datetime, body: str,
                              ttl_seconds: int = 300) -> tuple[str, str] | None:
        draft_id, token = uuid.uuid4().hex, secrets.token_urlsafe(10)
        now = utc_now()
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            consent = db.execute("SELECT granted FROM consents WHERE owner_key=? AND kind='voice'",
                                 (self.owner_key(owner_id, self.vault),)).fetchone()
            if not consent or not consent["granted"]:
                db.rollback()
                return None
            db.execute("INSERT INTO reminder_drafts(id,approval_hash,owner_key,chat_enc,payload_enc,status,expires_at,created_at) "
                       "VALUES(?,?,?,?,?,'pending',?,?)",
                       (draft_id, self.vault.index("reminder-approval", token),
                        self.owner_key(owner_id, self.vault), self.vault.seal(str(chat_id)),
                        self.vault.seal_json({"due_at": iso(due_at), "body": body}),
                        iso(now + timedelta(seconds=ttl_seconds)), iso(now)))
            db.commit()
        return draft_id, token

    def set_reminder_draft_preview(self, draft_id: str, message_id: int) -> None:
        with contextlib.closing(self.connect()) as db:
            db.execute("UPDATE reminder_drafts SET preview_message_id=? WHERE id=? AND status='pending'",
                       (message_id, draft_id))

    def resolve_reminder_draft(self, token: str, owner_id: int, chat_id: int,
                               preview_message_id: int, *, approve: bool) -> str:
        now = utc_now()
        token_hash = self.vault.index("reminder-approval", token)
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM reminder_drafts WHERE approval_hash=? AND status='pending'", (token_hash,)).fetchone()
            if not row:
                db.rollback()
                return "unavailable"
            if (row["owner_key"] != self.owner_key(owner_id, self.vault)
                    or int(self.vault.open_text(row["chat_enc"])) != chat_id
                    or row["preview_message_id"] != preview_message_id):
                db.rollback()
                return "forbidden"
            if row["expires_at"] <= iso(now):
                db.execute("UPDATE reminder_drafts SET status='expired' WHERE id=?", (row["id"],))
                db.commit()
                return "expired"
            if not approve:
                db.execute("UPDATE reminder_drafts SET status='cancelled' WHERE id=?", (row["id"],))
                db.commit()
                return "cancelled"
            payload = self.vault.open_json(row["payload_enc"])
            reminder_id = uuid.uuid4().hex[:10]
            due = datetime.fromisoformat(payload["due_at"])
            db.execute("INSERT INTO reminders VALUES(?,?,?,?,?,?,NULL)",
                       (reminder_id, row["owner_key"], iso(due), self.vault.seal(payload["body"]), "pending", iso(now)))
            self._new_outbox(db, dedupe_key=f"voice-reminder:{row['id']}", owner_id=owner_id,
                             chat_id=chat_id,
                             text=f"Ovozli eslatma saqlandi.\nSana: {due.isoformat()}\nMatn: {payload['body']}\nID: {reminder_id}",
                             due_at=now)
            db.execute("UPDATE reminder_drafts SET status='approved' WHERE id=?", (row["id"],))
            db.commit()
        return "approved"

    def create_outbox_for_update(self, *, update_id: int, owner_id: int, chat_id: int, text: str) -> bool:
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            inserted = db.execute("INSERT OR IGNORE INTO bot_updates VALUES(?,?)", (update_id, iso(utc_now()))).rowcount
            if not inserted:
                db.rollback()
                return False
            self._new_outbox(db, dedupe_key=f"update:{update_id}:reply", owner_id=owner_id,
                             chat_id=chat_id, text=text, due_at=utc_now())
            db.commit()
        return True

    def mark_update(self, update_id: int) -> bool:
        with contextlib.closing(self.connect()) as db:
            cur = db.execute("INSERT OR IGNORE INTO bot_updates VALUES(?,?)", (update_id, iso(utc_now())))
            return cur.rowcount == 1

    def is_update_processed(self, update_id: int) -> bool:
        with contextlib.closing(self.connect()) as db:
            return db.execute("SELECT 1 FROM bot_updates WHERE update_id=?", (update_id,)).fetchone() is not None

    def commit_update_offset(self, update_id: int) -> None:
        """Persist the next Bot API offset only after handler state is durable."""
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO bot_updates VALUES(?,?)", (update_id, iso(utc_now())))
            db.execute("UPDATE poll_state SET next_offset=MAX(next_offset,?) WHERE id=1", (update_id + 1,))
            db.commit()

    def poll_offset(self) -> int:
        with contextlib.closing(self.connect()) as db:
            row = db.execute("SELECT next_offset FROM poll_state WHERE id=1").fetchone()
        return int(row["next_offset"]) if row else 0

    def get_reminders(self, owner_id: int, limit: int = 30) -> list[dict[str, Any]]:
        owner_key = self.owner_key(owner_id, self.vault)
        with contextlib.closing(self.connect()) as db:
            rows = db.execute("SELECT * FROM reminders WHERE owner_key=? AND status='pending' ORDER BY due_at LIMIT ?",
                              (owner_key, limit)).fetchall()
        return [{"id": r["id"], "due_at": r["due_at"], "body": self.vault.open_text(r["payload_enc"])} for r in rows]

    def find_assistant_reminders(self, owner_id: int, *, query: str | None = None,
                                 reminder_id: str | None = None) -> list[dict[str, Any]]:
        owner_key = self.owner_key(owner_id, self.vault)
        with contextlib.closing(self.connect()) as db:
            rows = db.execute("SELECT id,due_at,payload_enc,status FROM reminders WHERE owner_key=? "
                              "AND status IN ('pending','sending','sent','failed','unknown') "
                              "ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END,due_at DESC LIMIT 100",
                              (owner_key,)).fetchall()
        text_query = (query or "").casefold().strip()
        id_query = (reminder_id or "").casefold().strip()
        results = []
        for row in rows:
            body = self.vault.open_text(row["payload_enc"])
            if id_query and not str(row["id"]).casefold().startswith(id_query):
                continue
            if text_query and text_query not in body.casefold():
                continue
            results.append({"id": row["id"], "due_at": row["due_at"], "body": body,
                            "status": row["status"]})
        return results[:30]

    def cancel_reminder(self, owner_id: int, reminder_id: str) -> bool:
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            cancelled = self._cancel_reminder(db, self.owner_key(owner_id, self.vault), reminder_id)
            db.commit()
            return cancelled

    @staticmethod
    def _cancel_reminder(db: sqlite3.Connection, owner_key: str, reminder_id: str) -> bool:
        cur = db.execute("UPDATE reminders SET status='cancelled' WHERE id=? AND owner_key=? AND status='pending'",
                         (reminder_id, owner_key))
        if cur.rowcount:
            db.execute("DELETE FROM outbox WHERE reminder_id=? AND owner_key=? AND status='pending'",
                       (reminder_id, owner_key))
        return cur.rowcount == 1

    def reschedule_reminder(self, owner_id: int, reminder_id: str, due_at: datetime) -> bool:
        if due_at <= utc_now():
            raise ValueError("Yangi vaqt kelajakda bo‘lishi kerak.")
        owner_key = self.owner_key(owner_id, self.vault)
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("UPDATE reminders SET due_at=? WHERE id=? AND owner_key=? AND status='pending'",
                                 (iso(due_at), reminder_id, owner_key)).rowcount
            if changed:
                db.execute("DELETE FROM outbox WHERE reminder_id=? AND owner_key=? AND status='pending'",
                           (reminder_id, owner_key))
            db.commit()
        return changed == 1

    def reschedule_assistant_reminder(self, *, update_id: int, owner_id: int,
                                      reminder_id: str, due_at: datetime) -> dict[str, Any]:
        if due_at.tzinfo is None or due_at <= utc_now():
            raise ValueError("Yangi vaqt kelajakda va vaqt mintaqasi bilan bo‘lishi kerak.")
        owner_key = self.owner_key(owner_id, self.vault)
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT result_enc FROM assistant_actions WHERE update_id=?",
                                  (update_id,)).fetchone()
            if previous:
                result = self.vault.open_json(previous["result_enc"])
                db.commit()
                return result if isinstance(result, dict) else {"saved": False, "id": reminder_id}
            row = db.execute("SELECT due_at,status FROM reminders WHERE id=? AND owner_key=?",
                             (reminder_id, owner_key)).fetchone()
            saved = bool(row and row["status"] == "pending")
            if saved:
                db.execute("UPDATE reminders SET due_at=? WHERE id=? AND owner_key=? AND status='pending'",
                           (iso(due_at), reminder_id, owner_key))
                db.execute("DELETE FROM outbox WHERE reminder_id=? AND owner_key=? AND status='pending'",
                           (reminder_id, owner_key))
            result = {"saved": saved, "id": reminder_id, "due_at": iso(due_at) if saved else None}
            now = iso(utc_now())
            db.execute("INSERT INTO assistant_actions VALUES(?,?,?,?,?)",
                       (update_id, owner_key, "reschedule_reminder", self.vault.seal_json(result), now))
            if saved:
                db.execute("INSERT INTO assistant_action_log(id,owner_key,update_id,action,entity_type,entity_id,payload_enc,created_at) "
                           "VALUES(?,?,?,?,?,?,?,?)",
                           (uuid.uuid4().hex, owner_key, update_id, "reminder_rescheduled", "reminder",
                            reminder_id, self.vault.seal_json({"due_at": iso(due_at)}), now))
            db.commit()
        return result

    def complete_assistant_reminder(self, *, update_id: int, owner_id: int,
                                    reminder_id: str) -> dict[str, Any]:
        owner_key = self.owner_key(owner_id, self.vault)
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT result_enc FROM assistant_actions WHERE update_id=?",
                                  (update_id,)).fetchone()
            if previous:
                result = self.vault.open_json(previous["result_enc"])
                db.commit()
                return result if isinstance(result, dict) else {"completed": False, "id": reminder_id}
            row = db.execute("SELECT status FROM reminders WHERE id=? AND owner_key=?",
                             (reminder_id, owner_key)).fetchone()
            if not row:
                db.rollback()
                return {"completed": False, "id": reminder_id, "status": "not_found"}
            status = str(row["status"])
            if status == "pending":
                self._cancel_reminder(db, owner_key, reminder_id)
            runs = db.execute("SELECT rule_id FROM recurring_reminder_runs WHERE reminder_id=?",
                              (reminder_id,)).fetchall()
            stopped = False
            for run in runs:
                rule_id = run["rule_id"]
                stopped = bool(db.execute("UPDATE recurring_reminders SET active=0 WHERE id=? AND owner_key=? AND active=1",
                                          (rule_id, owner_key)).rowcount) or stopped
                pending_ids = db.execute("SELECT r.id FROM recurring_reminder_runs rr JOIN reminders r ON r.id=rr.reminder_id "
                                         "WHERE rr.rule_id=? AND r.owner_key=? AND r.status='pending'",
                                         (rule_id, owner_key)).fetchall()
                for pending in pending_ids:
                    self._cancel_reminder(db, owner_key, pending["id"])
            result = {"completed": True, "id": reminder_id, "status": status,
                      "recurring_stopped": stopped}
            now = iso(utc_now())
            db.execute("INSERT INTO assistant_actions VALUES(?,?,?,?,?)",
                       (update_id, owner_key, "complete_reminder", self.vault.seal_json(result), now))
            db.execute("INSERT INTO assistant_action_log(id,owner_key,update_id,action,entity_type,entity_id,payload_enc,created_at) "
                       "VALUES(?,?,?,?,?,?,?,?)",
                       (uuid.uuid4().hex, owner_key, update_id, "reminder_completed", "reminder",
                        reminder_id, self.vault.seal_json({"previous_status": status,
                                                          "recurring_stopped": stopped}), now))
            db.commit()
        return result

    def create_recurring_reminder(self, *, update_id: int, owner_id: int, body: str,
                                  first: datetime, timezone_name: str, frequency: str,
                                  weekday: int | None = None, interval_minutes: int | None = None,
                                  now: datetime | None = None) -> dict[str, Any]:
        now = now or utc_now()
        if not body.strip() or len(body) > 500:
            raise ValueError("Eslatma matni bo‘sh yoki juda uzun.")
        if first.tzinfo is None or not now < first <= now + timedelta(days=366):
            raise ValueError("Birinchi eslatma vaqti kelajakdagi bir yil ichida bo‘lishi kerak.")
        first = first.astimezone(ZoneInfo(timezone_name))
        next_due = next_occurrence(first, frequency, now, weekday=weekday, interval_minutes=interval_minutes)
        owner_key = self.owner_key(owner_id, self.vault)
        payload = {"body": body.strip(), "first": first.isoformat(), "timezone": timezone_name,
                   "frequency": frequency, "weekday": weekday, "interval_minutes": interval_minutes}
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""INSERT OR IGNORE INTO recurring_reminders
                (id,owner_key,update_id,payload_enc,next_due_at,created_at) VALUES(?,?,?,?,?,?)""",
                       (uuid.uuid4().hex[:12], owner_key, update_id, self.vault.seal_json(payload),
                        iso(next_due), iso(now)))
            row = db.execute("SELECT * FROM recurring_reminders WHERE owner_key=? AND update_id=?",
                             (owner_key, update_id)).fetchone()
            db.commit()
        return self._recurring_reminder_result(row)

    def _recurring_reminder_result(self, row: sqlite3.Row) -> dict[str, Any]:
        return {**self.vault.open_json(row["payload_enc"]), "id": row["id"],
                "next_due_at": row["next_due_at"], "active": bool(row["active"]), "created_at": row["created_at"]}

    def list_recurring_reminders(self, owner_id: int) -> list[dict[str, Any]]:
        with contextlib.closing(self.connect()) as db:
            rows = db.execute("SELECT * FROM recurring_reminders WHERE owner_key=? AND active=1 "
                              "ORDER BY next_due_at,id LIMIT 30", (self.owner_key(owner_id, self.vault),)).fetchall()
        return [self._recurring_reminder_result(row) for row in rows]

    def cancel_recurring_reminder(self, owner_id: int, rule_id: str) -> bool:
        owner_key = self.owner_key(owner_id, self.vault)
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("UPDATE recurring_reminders SET active=0 WHERE id=? AND owner_key=? AND active=1",
                                 (rule_id, owner_key)).rowcount
            if changed:
                rows = db.execute("""SELECT r.id FROM recurring_reminder_runs run
                    JOIN reminders r ON r.id=run.reminder_id
                    WHERE run.rule_id=? AND r.owner_key=? AND r.status='pending'""", (rule_id, owner_key))
                for row in rows.fetchall():
                    self._cancel_reminder(db, owner_key, row["id"])
            db.commit()
        return changed == 1

    def _materialize_recurring_reminders(self, db: sqlite3.Connection, owner_key: str, now: datetime) -> None:
        rules = db.execute("""SELECT * FROM recurring_reminders WHERE owner_key=? AND active=1 AND next_due_at<=?
                             ORDER BY next_due_at,id LIMIT 100""", (owner_key, iso(now))).fetchall()
        for row in rules:
            rule = self.vault.open_json(row["payload_enc"])
            due = datetime.fromisoformat(row["next_due_at"])
            first = datetime.fromisoformat(rule["first"]).astimezone(ZoneInfo(rule["timezone"]))
            # Skip missed runs after a long outage rather than sending a backlog.
            if now - due <= timedelta(minutes=5):
                reminder_id = uuid.uuid4().hex[:10]
                db.execute("INSERT INTO reminders VALUES(?,?,?,?,?,?,NULL)",
                           (reminder_id, owner_key, row["next_due_at"], self.vault.seal(rule["body"]), "pending", iso(now)))
                db.execute("INSERT INTO recurring_reminder_runs VALUES(?,?,?)",
                           (row["id"], row["next_due_at"], reminder_id))
            next_due = next_occurrence(first, rule["frequency"], now,
                                       weekday=rule["weekday"], interval_minutes=rule["interval_minutes"])
            db.execute("UPDATE recurring_reminders SET next_due_at=? WHERE id=?", (iso(next_due), row["id"]))

    def due_reminders(self, now: datetime | None = None) -> list[dict[str, Any]]:
        now = now or utc_now()
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT * FROM reminders WHERE status='pending' AND due_at<=? ORDER BY due_at LIMIT 100",
                              (iso(now),)).fetchall()
            result = []
            for row in rows:
                db.execute("UPDATE reminders SET status='sending' WHERE id=? AND status='pending'", (row["id"],))
                result.append({"id": row["id"], "owner_key": row["owner_key"], "due_at": row["due_at"],
                               "body": self.vault.open_text(row["payload_enc"])})
            db.commit()
        return result

    def queue_due_reminders(self, owner_id: int, chat_id: int, now: datetime | None = None) -> int:
        now = now or utc_now()
        owner_key = self.owner_key(owner_id, self.vault)
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            self._materialize_recurring_reminders(db, owner_key, now)
            rows = db.execute("""SELECT r.* FROM reminders r
                WHERE r.owner_key=? AND r.status='pending' AND r.due_at<=?
                AND NOT EXISTS (SELECT 1 FROM outbox o WHERE o.dedupe_key='reminder:' || r.id)
                ORDER BY r.due_at LIMIT 100""", (owner_key, iso(now))).fetchall()
            for row in rows:
                body = self.vault.open_text(row["payload_enc"])
                due = datetime.fromisoformat(row["due_at"])
                late = due < now - timedelta(minutes=1)
                text = ("Kechikkan eslatma:\n" if late else "Eslatma:\n") + body
                self._new_outbox(db, dedupe_key=f"reminder:{row['id']}", owner_id=owner_id,
                                 chat_id=chat_id, text=text, due_at=now)
                db.execute("UPDATE outbox SET reminder_id=? WHERE dedupe_key=?", (row["id"], f"reminder:{row['id']}"))
            db.commit()
        return len(rows)

    def resolve_reminder_owner(self, owner_key: str) -> int | None:
        # Owner's chat is configured independently; this method intentionally returns no plaintext id.
        return None

    def mark_reminder(self, reminder_id: str, status: str) -> None:
        if status not in {"sent", "failed", "unknown"}:
            raise ValueError("Noto‘g‘ri eslatma holati")
        with contextlib.closing(self.connect()) as db:
            db.execute("UPDATE reminders SET status=?, sent_at=? WHERE id=? AND status='sending'",
                       (status, iso(utc_now()) if status == "sent" else None, reminder_id))

    def due_outbox(self, now: datetime | None = None, limit: int = 50,
                   max_per_hour: int = 30) -> list[dict[str, Any]]:
        now = now or utc_now()
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            sent = db.execute("SELECT COUNT(*) AS n FROM outbox WHERE status='sent' AND sent_at>=?",
                              (iso(now - timedelta(hours=1)),)).fetchone()["n"]
            limit = min(limit, max(0, max_per_hour - int(sent)))
            if limit == 0:
                db.commit()
                return []
            rows = db.execute("""SELECT o.* FROM outbox o WHERE o.status='pending' AND o.due_at<=?
                AND (o.reminder_id IS NULL OR EXISTS (SELECT 1 FROM reminders r
                    WHERE r.id=o.reminder_id AND r.owner_key=o.owner_key AND r.status='pending'))
                ORDER BY o.due_at LIMIT ?""", (iso(now), limit)).fetchall()
            result = []
            for row in rows:
                db.execute("UPDATE outbox SET status='sending', updated_at=? WHERE id=? AND status='pending'",
                           (iso(utc_now()), row["id"]))
                if row["reminder_id"]:
                    db.execute("UPDATE reminders SET status='sending' WHERE id=? AND owner_key=? AND status='pending'",
                               (row["reminder_id"], row["owner_key"]))
                result.append({"id": row["id"], "chat_id": int(self.vault.open_text(row["chat_enc"])),
                               "text": self.vault.open_text(row["payload_enc"]),
                               "attempt_count": row["attempt_count"], "reminder_id": row["reminder_id"]})
            db.commit()
        return result

    def mark_outbox(self, item_id: str, status: str, error_category: str | None = None) -> None:
        if status not in {"sent", "failed", "unknown"}:
            raise ValueError("Noto‘g‘ri outbox holati")
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT reminder_id FROM outbox WHERE id=? AND status='sending'", (item_id,)).fetchone()
            if not row:
                db.rollback()
                return
            db.execute("UPDATE outbox SET status=?, updated_at=?, sent_at=?, error_category=? WHERE id=? AND status='sending'",
                       (status, iso(utc_now()), iso(utc_now()) if status == "sent" else None, error_category, item_id))
            if row["reminder_id"]:
                db.execute("UPDATE reminders SET status=?,sent_at=? WHERE id=? AND status='sending'",
                           (status, iso(utc_now()) if status == "sent" else None, row["reminder_id"]))
            db.commit()

    def retry_outbox(self, item_id: str, delay_seconds: int, error_category: str) -> bool:
        delay_seconds = max(1, min(delay_seconds, 300))
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT reminder_id,attempt_count FROM outbox WHERE id=? AND status='sending'", (item_id,)).fetchone()
            if not row or row["attempt_count"] >= 2:
                db.rollback()
                return False
            due = iso(utc_now() + timedelta(seconds=delay_seconds))
            db.execute("UPDATE outbox SET status='pending',due_at=?,attempt_count=attempt_count+1,updated_at=?,error_category=? WHERE id=?",
                       (due, iso(utc_now()), error_category, item_id))
            if row["reminder_id"]:
                db.execute("UPDATE reminders SET status='pending' WHERE id=? AND status='sending'", (row["reminder_id"],))
            db.commit()
        return True

    def create_action(self, *, owner_id: int, recipient: dict[str, Any], body: str,
                      update_id: int | None = None, preview_message_id: int | None = None,
                      ttl_seconds: int = 300, source: str = "text") -> tuple[str, str] | None:
        parameters = {"chat_id": int(recipient["user_id"]),
                      "chat_name": str(recipient.get("display_name") or recipient.get("alias") or "kontakt"),
                      "username": str(recipient.get("username") or ""), "body": body}
        return self.create_profile_operation_action(owner_id=owner_id, operation="send_message",
                                                    parameters=parameters, update_id=update_id,
                                                    preview_message_id=preview_message_id,
                                                    ttl_seconds=ttl_seconds, source=source,
                                                    recipient=recipient, body=body)

    def create_profile_operation_action(self, *, owner_id: int, operation: str,
                                        parameters: dict[str, Any], update_id: int | None = None,
                                        preview_message_id: int | None = None, ttl_seconds: int = 300,
                                        source: str = "text", recipient: dict[str, Any] | None = None,
                                        body: str | None = None,
                                        call_fallback_for: str | None = None) -> tuple[str, str] | None:
        allowed = {"send_message", "send_voice", "reply_message", "edit_message", "delete_message", "forward_message",
                   "send_media", "send_location", "send_contact", "send_poll", "archive_chat",
                   "unarchive_chat", "mute_chat", "unmute_chat", "mark_read", "pin_message", "unpin_message", "leave_chat",
                   "call_user",
                   "delete_group_message", "mute_group_member", "ban_group_member",
                   "set_group_welcome_rule", "disable_group_welcome_rule"}
        if operation not in allowed:
            raise ValueError("Noto‘g‘ri profil amali")
        if call_fallback_for is not None:
            call_fallback_for = str(call_fallback_for).strip()
            if operation != "send_voice" or not call_fallback_for or len(call_fallback_for) > 64:
                raise ValueError("Qo‘ng‘iroq ovozli qoralamasi bog‘lanishi noto‘g‘ri")
        call_attempt_id = str((parameters or {}).get("call_attempt_id") or "").strip() or None
        if (operation == "call_user") != bool(call_attempt_id):
            raise ValueError("Qo‘ng‘iroq topshirig‘i ID si noto‘g‘ri")
        action_id = uuid.uuid4().hex
        approval_token = secrets.token_urlsafe(12)
        random_id = secrets.randbelow((1 << 63) - 1) + 1
        now = utc_now()
        payload = {"owner_id": owner_id, "operation": operation, "params": parameters}
        if recipient is not None:
            payload["recipient"] = recipient
        if body is not None:
            payload["body"] = body
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            if update_id is not None:
                inserted = db.execute("INSERT OR IGNORE INTO bot_updates VALUES(?,?)",
                                      (update_id, iso(now))).rowcount
                if not inserted:
                    db.rollback()
                    return None
            if source not in {"text", "voice", "media"}:
                db.rollback()
                raise ValueError("Noto‘g‘ri profil amal manbasi")
            if source == "voice":
                consent = db.execute("SELECT granted FROM consents WHERE owner_key=? AND kind='voice'",
                                     (self.owner_key(owner_id, self.vault),)).fetchone()
                if not consent or not consent["granted"]:
                    db.rollback()
                    return None
            inserted = db.execute("""INSERT OR IGNORE INTO profile_actions
                (id,approval_hash,owner_key,preview_message_id,payload_enc,random_id,source,status,expires_at,created_at,updated_at,call_fallback_for,call_attempt_id)
                VALUES(?,?,?,?,?,?,?, 'pending',?,?,?,?,?)""",
                (action_id, self.vault.index("approval", approval_token), self.owner_key(owner_id, self.vault),
                 preview_message_id, self.vault.seal_json(payload), random_id, source,
                 iso(now + timedelta(seconds=ttl_seconds)), iso(now), iso(now),
                 call_fallback_for, call_attempt_id)).rowcount
            if not inserted:
                db.rollback()
                return None
            db.commit()
        return action_id, approval_token

    def _erase_profile_media(self, db: sqlite3.Connection, action_id: str) -> None:
        row = db.execute("SELECT payload_enc FROM profile_actions WHERE id=?", (action_id,)).fetchone()
        if not row:
            return
        payload = self.vault.open_json(row["payload_enc"])
        params = payload.get("params") or {}
        if "media_content_b64" not in params:
            return
        params.pop("media_content_b64", None)
        payload["params"] = params
        db.execute("UPDATE profile_actions SET payload_enc=? WHERE id=?",
                   (self.vault.seal_json(payload), action_id))

    def set_preview_message(self, action_id: str, message_id: int) -> None:
        with contextlib.closing(self.connect()) as db:
            db.execute("UPDATE profile_actions SET preview_message_id=?, updated_at=? WHERE id=? AND status='pending'",
                       (message_id, iso(utc_now()), action_id))

    def create_integration_action_draft(self, *, owner_id: int, update_id: int,
                                        operation: str, parameters: dict[str, Any],
                                        preview: str, ttl_seconds: int = 300
                                        ) -> tuple[str, str] | None:
        operation = str(operation).strip()[:80]
        preview = str(preview).strip()[:1800]
        if not operation or not preview or len(parameters) > 40:
            raise ValueError("Tashqi amal qoralamasi noto‘g‘ri")
        action_id, token = uuid.uuid4().hex, secrets.token_urlsafe(14)
        now = utc_now()
        owner_key = self.owner_key(owner_id, self.vault)
        payload = {"operation": operation, "parameters": parameters, "preview": preview}
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            inserted = db.execute("""INSERT OR IGNORE INTO integration_action_drafts
                (id,approval_hash,owner_key,source_update_id,preview_message_id,payload_enc,status,
                 expires_at,created_at,updated_at)
                VALUES(?,?,?,?,NULL,?,'pending',?,?,?)""",
                (action_id, self.vault.index("integration-action-approval", token), owner_key,
                 int(update_id), self.vault.seal_json(payload),
                 iso(now + timedelta(seconds=max(30, min(600, ttl_seconds)))), iso(now), iso(now))).rowcount
            if not inserted:
                db.rollback()
                return None
            db.commit()
        return action_id, token

    def set_integration_action_preview(self, action_id: str, message_id: int) -> bool:
        with contextlib.closing(self.connect()) as db:
            cur = db.execute("UPDATE integration_action_drafts SET preview_message_id=?,updated_at=? "
                             "WHERE id=? AND status='pending'",
                             (int(message_id), iso(utc_now()), action_id))
            return cur.rowcount == 1

    def discard_integration_action_draft(self, action_id: str) -> bool:
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT owner_key,payload_enc FROM integration_action_drafts "
                             "WHERE id=? AND status='pending'", (action_id,)).fetchone()
            if not row:
                db.rollback()
                return False
            payload = self.vault.open_json(row["payload_enc"])
            changed = db.execute("UPDATE integration_action_drafts SET status='cancelled',updated_at=? "
                "WHERE id=? AND status='pending'", (iso(utc_now()), action_id)).rowcount
            if changed:
                self._insert_action_log(db, owner_key=str(row["owner_key"]),
                    action="integration_action_preview_failed", entity_type="integration_action",
                    entity_id=action_id, payload={"operation": str(payload.get("operation") or "unknown"),
                                                  "status": "cancelled"})
            db.commit()
            return changed == 1

    def inspect_integration_action(self, token: str, owner_id: int,
                                   chat_id: int, message_id: int) -> dict[str, Any]:
        token_hash = self.vault.index("integration-action-approval", token)
        now = utc_now()
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM integration_action_drafts WHERE approval_hash=?",
                             (token_hash,)).fetchone()
            if not row:
                db.rollback()
                return {"status": "unavailable"}
            if (row["owner_key"] != self.owner_key(owner_id, self.vault)
                    or row["preview_message_id"] != int(message_id) or int(chat_id) != int(owner_id)):
                db.rollback()
                return {"status": "forbidden"}
            if row["status"] == "pending" and row["expires_at"] <= iso(now):
                db.execute("UPDATE integration_action_drafts SET status='expired',updated_at=? WHERE id=? AND status='pending'",
                           (iso(now), row["id"]))
                db.commit()
                return {"status": "expired"}
            payload = self.vault.open_json(row["payload_enc"])
            db.commit()
            return {"status": str(row["status"]), "id": str(row["id"]),
                    "operation": payload.get("operation"), "parameters": payload.get("parameters") or {},
                    "preview": payload.get("preview") or ""}

    def approve_integration_action(self, token: str, owner_id: int,
                                   chat_id: int, message_id: int) -> dict[str, Any]:
        token_hash = self.vault.index("integration-action-approval", token)
        now = utc_now()
        owner_key = self.owner_key(owner_id, self.vault)
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM integration_action_drafts WHERE approval_hash=? AND status='pending'",
                             (token_hash,)).fetchone()
            if not row:
                db.rollback()
                return {"status": "unavailable"}
            if (row["owner_key"] != owner_key or row["preview_message_id"] != int(message_id)
                    or int(chat_id) != int(owner_id)):
                db.rollback()
                return {"status": "forbidden"}
            if row["expires_at"] <= iso(now):
                db.execute("UPDATE integration_action_drafts SET status='expired',updated_at=? WHERE id=? AND status='pending'",
                           (iso(now), row["id"]))
                db.commit()
                return {"status": "expired"}
            changed = db.execute("UPDATE integration_action_drafts SET status='executing',updated_at=? "
                "WHERE id=? AND owner_key=? AND status='pending'",
                (iso(now), row["id"], owner_key)).rowcount
            if changed != 1:
                db.rollback()
                return {"status": "unavailable"}
            payload = self.vault.open_json(row["payload_enc"])
            self._insert_action_log(db, owner_key=owner_key,
                action="integration_action_approved", entity_type="integration_action",
                entity_id=str(row["id"]), payload={"operation": str(payload.get("operation") or "unknown"),
                                                    "status": "executing"})
            db.commit()
            return {"status": "executing", "id": str(row["id"]),
                    "operation": payload.get("operation"), "parameters": payload.get("parameters") or {},
                    "preview": payload.get("preview") or ""}

    def cancel_integration_action(self, token: str, owner_id: int,
                                  chat_id: int, message_id: int) -> str:
        pending = self.inspect_integration_action(token, owner_id, chat_id, message_id)
        if pending.get("status") != "pending":
            return str(pending.get("status") or "unavailable")
        token_hash = self.vault.index("integration-action-approval", token)
        owner_key = self.owner_key(owner_id, self.vault)
        with contextlib.closing(self.connect()) as db:
            cur = db.execute("UPDATE integration_action_drafts SET status='cancelled',updated_at=? "
                "WHERE approval_hash=? AND owner_key=? AND preview_message_id=? AND status='pending'",
                (iso(utc_now()), token_hash, owner_key, int(message_id)))
            if cur.rowcount:
                row = db.execute("SELECT id,payload_enc FROM integration_action_drafts WHERE approval_hash=?",
                                 (token_hash,)).fetchone()
                if row:
                    payload = self.vault.open_json(row["payload_enc"])
                    self._insert_action_log(db, owner_key=owner_key,
                        action="integration_action_cancelled", entity_type="integration_action",
                        entity_id=str(row["id"]), payload={"operation": str(payload.get("operation") or "unknown"),
                                                            "status": "cancelled"})
            return "cancelled" if cur.rowcount else "unavailable"

    def finish_integration_action(self, action_id: str, status: str,
                                  result: dict[str, Any] | None = None) -> bool:
        if status not in {"succeeded", "failed", "unknown"}:
            raise ValueError("Integratsiya amalining holati noto‘g‘ri")
        now = utc_now()
        safe_result = self.vault.seal_json(result or {})
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT owner_key,payload_enc FROM integration_action_drafts "
                             "WHERE id=? AND status='executing'", (action_id,)).fetchone()
            if not row:
                db.rollback()
                return False
            payload = self.vault.open_json(row["payload_enc"])
            changed = db.execute("UPDATE integration_action_drafts SET status=?,result_enc=?,updated_at=? "
                "WHERE id=? AND status='executing'",
                (status, safe_result, iso(now), action_id)).rowcount
            if changed:
                self._insert_action_log(db, owner_key=str(row["owner_key"]),
                    action="integration_action_finished", entity_type="integration_action",
                    entity_id=action_id, payload={"operation": str(payload.get("operation") or "unknown"),
                                                  "status": status,
                                                  "result": result or {}})
            db.commit()
            return changed == 1

    def approve_action(self, token: str, owner_id: int, chat_id: int, message_id: int,
                       max_per_hour: int = 5) -> str:
        token_hash = self.vault.index("approval", token)
        now = iso(utc_now())
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM profile_actions WHERE approval_hash=? AND status='pending'", (token_hash,)).fetchone()
            if not row:
                db.rollback()
                return "unavailable"
            if row["owner_key"] != self.owner_key(owner_id, self.vault) or row["preview_message_id"] != message_id:
                db.rollback()
                return "forbidden"
            if row["expires_at"] < now:
                self._erase_profile_media(db, row["id"])
                db.execute("UPDATE profile_actions SET status='expired', updated_at=? WHERE id=?", (now, row["id"]))
                db.commit()
                return "expired"
            count = db.execute("SELECT COUNT(*) AS n FROM profile_actions WHERE owner_key=? AND created_at>=? "
                               "AND status IN ('queued','leased','dispatching','sent','failed','unknown')",
                               (row["owner_key"], iso(utc_now() - timedelta(hours=1)))).fetchone()["n"]
            if int(count) >= max_per_hour:
                db.rollback()
                return "rate_limited"
            payload = self.vault.open_json(row["payload_enc"])
            params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
            db.execute("UPDATE profile_actions SET status='queued', updated_at=? WHERE id=? AND status='pending'", (now, row["id"]))
            self._insert_action_log(db, owner_key=row["owner_key"], action="profile_action_approved",
                entity_type="profile_action", entity_id=row["id"], payload={
                    "operation": str(payload.get("operation") or "unknown"),
                    "chat_name": str(params.get("chat_name") or "")[:160], "status": "queued"})
            db.commit()
        return "queued"

    def cancel_action(self, token: str, owner_id: int, message_id: int) -> str:
        token_hash = self.vault.index("approval", token)
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            cur = db.execute("UPDATE profile_actions SET status='cancelled',updated_at=? WHERE approval_hash=? AND owner_key=? AND preview_message_id=? AND status='pending'",
                             (iso(utc_now()), token_hash, self.owner_key(owner_id, self.vault), message_id))
            if cur.rowcount:
                row = db.execute("SELECT id,owner_key,payload_enc FROM profile_actions WHERE approval_hash=?", (token_hash,)).fetchone()
                if row:
                    self._erase_profile_media(db, row["id"])
                    payload = self.vault.open_json(row["payload_enc"])
                    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
                    self._insert_action_log(db, owner_key=row["owner_key"], action="profile_action_cancelled",
                        entity_type="profile_action", entity_id=row["id"], payload={
                            "operation": str(payload.get("operation") or "unknown"),
                            "chat_name": str(params.get("chat_name") or "")[:160], "status": "cancelled"})
            db.commit()
            return "cancelled" if cur.rowcount else "unavailable"

    def take_profile_action_for_edit(self, token: str, owner_id: int,
                                     chat_id: int, message_id: int) -> dict[str, Any]:
        """Cancel an editable pending message action before asking for replacement text."""
        token_hash = self.vault.index("approval", token)
        now = utc_now()
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM profile_actions WHERE approval_hash=? AND status='pending'",
                             (token_hash,)).fetchone()
            if not row:
                db.rollback()
                return {"status": "unavailable"}
            if (row["owner_key"] != self.owner_key(owner_id, self.vault)
                    or row["preview_message_id"] != message_id or chat_id != owner_id):
                db.rollback()
                return {"status": "forbidden"}
            if row["expires_at"] <= iso(now):
                self._erase_profile_media(db, row["id"])
                db.execute("UPDATE profile_actions SET status='expired',updated_at=? WHERE id=? AND status='pending'",
                           (iso(now), row["id"]))
                db.commit()
                return {"status": "expired"}
            payload = self.vault.open_json(row["payload_enc"])
            operation = str(payload.get("operation") or "")
            params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
            if operation not in {"send_message", "send_voice", "reply_message", "edit_message"} or not str(params.get("body") or "").strip():
                db.rollback()
                return {"status": "not_editable"}
            db.execute("UPDATE profile_actions SET status='cancelled',updated_at=? WHERE id=? AND status='pending'",
                       (iso(now), row["id"]))
            self._insert_action_log(db, owner_key=row["owner_key"], action="profile_action_cancelled_for_edit",
                entity_type="profile_action", entity_id=row["id"], payload={
                    "operation": operation, "chat_name": str(params.get("chat_name") or "")[:160],
                    "status": "cancelled"})
            db.commit()
            return {"status": "cancelled", "operation": operation,
                    "params": params, "source": str(row["source"])}

    def inspect_pending_profile_action(self, token: str, owner_id: int,
                                       chat_id: int, message_id: int) -> dict[str, Any]:
        """Read a confirmation draft for an owner-only dry run without queueing it."""
        token_hash = self.vault.index("approval", token)
        now = utc_now()
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM profile_actions WHERE approval_hash=? AND status='pending'",
                             (token_hash,)).fetchone()
            if not row:
                db.rollback()
                return {"status": "unavailable"}
            if (row["owner_key"] != self.owner_key(owner_id, self.vault)
                    or row["preview_message_id"] != message_id or chat_id != owner_id):
                db.rollback()
                return {"status": "forbidden"}
            if row["expires_at"] <= iso(now):
                self._erase_profile_media(db, row["id"])
                db.execute("UPDATE profile_actions SET status='expired',updated_at=? WHERE id=? AND status='pending'",
                           (iso(now), row["id"]))
                db.commit()
                return {"status": "expired"}
            payload = self.vault.open_json(row["payload_enc"])
            db.commit()
            return {"status": "pending", "id": str(row["id"]),
                    "operation": str(payload.get("operation") or ""),
                    "params": payload.get("params") if isinstance(payload.get("params"), dict) else {}}

    def create_reply_draft(self, *, owner_id: int, operation: str, parameters: dict[str, Any],
                           variants: list[dict[str, str]], source: str, ttl_seconds: int = 300) -> str:
        if operation not in {"reply_message", "send_voice"} or source not in {"text", "voice"}:
            raise ValueError("Noto‘g‘ri javob qoralamasi")
        clean_variants = [{"style": str(item["style"])[:20], "body": str(item["body"])[:1800]}
                          for item in variants[:4]
                          if isinstance(item, dict) and str(item.get("body") or "").strip()]
        if len(clean_variants) != 4:
            raise ValueError("Javob qoralamasi uchun to‘rtta variant kerak")
        draft_id = uuid.uuid4().hex
        now = utc_now()
        payload = {"operation": operation, "params": parameters, "variants": clean_variants,
                   "source": source, "selected_index": None, "selected_body": None}
        with contextlib.closing(self.connect()) as db:
            db.execute("INSERT INTO reply_drafts(id,owner_key,payload_enc,status,expires_at,created_at,updated_at) "
                       "VALUES(?,?,?,'pending',?,?,?)",
                       (draft_id, self.owner_key(owner_id, self.vault), self.vault.seal_json(payload),
                        iso(now + timedelta(seconds=ttl_seconds)), iso(now), iso(now)))
        return draft_id

    def set_reply_draft_preview(self, owner_id: int, draft_id: str, message_id: int) -> bool:
        with contextlib.closing(self.connect()) as db:
            cur = db.execute("UPDATE reply_drafts SET preview_message_id=?,updated_at=? "
                             "WHERE id=? AND owner_key=? AND status='pending'",
                             (message_id, iso(utc_now()), draft_id, self.owner_key(owner_id, self.vault)))
            return cur.rowcount == 1

    def select_reply_draft(self, owner_id: int, draft_id: str, message_id: int,
                           variant_index: int) -> dict[str, Any] | None:
        now = iso(utc_now())
        key = self.owner_key(owner_id, self.vault)
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM reply_drafts WHERE id=? AND owner_key=? AND status='pending'",
                             (draft_id, key)).fetchone()
            if not row or row["preview_message_id"] != message_id:
                db.rollback()
                return None
            if row["expires_at"] <= now:
                db.execute("UPDATE reply_drafts SET status='expired',updated_at=? WHERE id=?", (now, draft_id))
                db.commit()
                return None
            payload = self.vault.open_json(row["payload_enc"])
            variants = payload.get("variants") or []
            if not isinstance(variant_index, int) or variant_index < 0 or variant_index >= len(variants):
                db.rollback()
                return None
            body = str(variants[variant_index].get("body") or "").strip()
            payload["selected_index"] = variant_index
            payload["selected_body"] = body
            params = dict(payload.get("params") or {})
            params["body"] = body
            payload["params"] = params
            db.execute("UPDATE reply_drafts SET payload_enc=?,status='selected',updated_at=? WHERE id=?",
                       (self.vault.seal_json(payload), now, draft_id))
            db.commit()
            payload["draft_id"] = draft_id
            return payload

    def start_reply_draft_edit(self, owner_id: int, draft_id: str, variant_index: int,
                               prompt_message_id: int) -> bool:
        now = iso(utc_now())
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload_enc FROM reply_drafts WHERE id=? AND owner_key=? "
                             "AND status='selected' AND expires_at>?",
                             (draft_id, self.owner_key(owner_id, self.vault), now)).fetchone()
            if not row:
                db.rollback()
                return False
            payload = self.vault.open_json(row["payload_enc"])
            if payload.get("selected_index") != variant_index:
                db.rollback()
                return False
            db.execute("UPDATE reply_drafts SET payload_enc=?,edit_prompt_message_id=?,preview_message_id=NULL,"
                       "status='editing',updated_at=? WHERE id=? AND status='selected'",
                       (self.vault.seal_json(payload), prompt_message_id, now, draft_id))
            db.commit()
            return True

    def reply_draft_for_edit_prompt(self, owner_id: int, prompt_message_id: int) -> dict[str, Any] | None:
        now = iso(utc_now())
        with contextlib.closing(self.connect()) as db:
            row = db.execute("SELECT * FROM reply_drafts WHERE owner_key=? AND edit_prompt_message_id=? "
                             "AND status='editing' AND expires_at>?",
                             (self.owner_key(owner_id, self.vault), prompt_message_id, now)).fetchone()
            if not row:
                return None
            payload = self.vault.open_json(row["payload_enc"])
            payload["draft_id"] = row["id"]
            return payload

    def finish_reply_draft_edit(self, owner_id: int, prompt_message_id: int, body: str) -> dict[str, Any] | None:
        body = body.strip()
        if not body or len(body) > 2000:
            return None
        now = iso(utc_now())
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id,payload_enc FROM reply_drafts WHERE owner_key=? AND edit_prompt_message_id=? "
                             "AND status='editing' AND expires_at>?",
                             (self.owner_key(owner_id, self.vault), prompt_message_id, now)).fetchone()
            if not row:
                db.rollback()
                return None
            payload = self.vault.open_json(row["payload_enc"])
            payload["selected_body"] = body
            params = dict(payload.get("params") or {})
            params["body"] = body
            payload["params"] = params
            db.execute("UPDATE reply_drafts SET payload_enc=?,status='selected',edit_prompt_message_id=NULL,"
                       "updated_at=? WHERE id=? AND status='editing'",
                       (self.vault.seal_json(payload), now, row["id"]))
            db.commit()
            payload["draft_id"] = row["id"]
            return payload

    def cancel_reply_draft(self, owner_id: int, draft_id: str, message_id: int) -> bool:
        with contextlib.closing(self.connect()) as db:
            cur = db.execute("UPDATE reply_drafts SET status='cancelled',updated_at=? WHERE id=? AND owner_key=? "
                             "AND preview_message_id=? AND status='pending'",
                             (iso(utc_now()), draft_id, self.owner_key(owner_id, self.vault), message_id))
            return cur.rowcount == 1

    def claim_action(self, lease_seconds: int = 30) -> dict[str, Any] | None:
        now = utc_now()
        lease_token = secrets.token_urlsafe(24)
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            expired_leases = db.execute("SELECT schedule_id FROM profile_actions WHERE status='leased' "
                                         "AND lease_until<=? AND expires_at>? AND schedule_id IS NOT NULL",
                                         (iso(now), iso(now))).fetchall()
            db.execute("UPDATE profile_actions SET status='queued',lease_hash=NULL,lease_until=NULL,updated_at=? "
                       "WHERE status='leased' AND lease_until<=? AND expires_at>?",
                       (iso(now), iso(now), iso(now)))
            for expired_lease in expired_leases:
                db.execute("UPDATE scheduled_profile_messages SET status='queued',updated_at=? "
                           "WHERE id=? AND status='leased'", (iso(now), expired_lease["schedule_id"]))
            expired_actions = db.execute("SELECT id,schedule_id FROM profile_actions "
                                         "WHERE status IN ('pending','queued','leased') AND expires_at<=?",
                                         (iso(now),)).fetchall()
            for expired in expired_actions:
                self._erase_profile_media(db, expired["id"])
                if expired["schedule_id"]:
                    db.execute("UPDATE scheduled_profile_messages SET status='expired',updated_at=? "
                               "WHERE id=? AND status IN ('queued','leased')",
                               (iso(now), expired["schedule_id"]))
            db.execute("UPDATE profile_actions SET status='expired',updated_at=? "
                       "WHERE status IN ('pending','queued','leased') AND expires_at<=?",
                       (iso(now), iso(now)))
            row = db.execute("SELECT * FROM profile_actions WHERE status='queued' AND expires_at>? ORDER BY created_at LIMIT 1",
                             (iso(now),)).fetchone()
            if not row:
                db.commit()
                return None
            db.execute("UPDATE profile_actions SET status='leased',lease_hash=?,lease_until=?,updated_at=? WHERE id=? AND status='queued'",
                       (self.vault.index("lease", lease_token), iso(now + timedelta(seconds=lease_seconds)), iso(now), row["id"]))
            if row["schedule_id"]:
                db.execute("UPDATE scheduled_profile_messages SET status='leased',updated_at=? "
                           "WHERE id=? AND status='queued'", (iso(now), row["schedule_id"]))
            payload = self.vault.open_json(row["payload_enc"])
            db.commit()
        return {"job_id": row["id"], "lease_token": lease_token,
                "expires_at": row["expires_at"], **payload, "random_id": row["random_id"]}

    def mark_dispatching(self, job_id: str, lease_token: str) -> bool:
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT schedule_id FROM profile_actions WHERE id=?", (job_id,)).fetchone()
            cur = db.execute("UPDATE profile_actions SET status='dispatching',updated_at=? WHERE id=? AND status='leased' AND lease_hash=? AND lease_until>? AND expires_at>?",
                             (iso(utc_now()), job_id, self.vault.index("lease", lease_token), iso(utc_now()), iso(utc_now())))
            if cur.rowcount == 1 and row and row["schedule_id"]:
                db.execute("UPDATE scheduled_profile_messages SET status='dispatching',updated_at=? "
                           "WHERE id=? AND linked_action_id=?",
                           (iso(utc_now()), row["schedule_id"], job_id))
            db.commit()
            return cur.rowcount == 1

    def finish_action(self, job_id: str, lease_token: str, status: str, category: str | None = None,
                      chat_id: int | None = None) -> bool:
        if status not in {"sent", "failed", "unknown"}:
            raise ValueError("Noto‘g‘ri amal holati")
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM profile_actions WHERE id=?", (job_id,)).fetchone()
            lease_hash = self.vault.index("lease", lease_token)
            if not row or row["lease_hash"] != lease_hash:
                db.rollback()
                return False
            if row["status"] == "dispatching":
                db.execute("UPDATE profile_actions SET status=?,result_category=?,updated_at=? WHERE id=?",
                           (status, category, iso(utc_now()), job_id))
            elif row["status"] == "leased" and status == "failed":
                # Local synthesis or validation failed before any Telegram dispatch.
                db.execute("UPDATE profile_actions SET status='failed',result_category=?,updated_at=? WHERE id=?",
                           (category, iso(utc_now()), job_id))
            elif row["status"] != status:
                db.rollback()
                return False
            payload = self.vault.open_json(row["payload_enc"])
            operation = payload.get("operation", "send_message")
            params = payload.get("params") or {}
            if operation == "call_user":
                attempt_id = str(params.get("call_attempt_id") or "")
                outcomes = {"call_answered": "answered", "call_rejected": "rejected",
                            "call_no_answer": "no_answer", "call_failed": "failed",
                            "call_unknown": "unknown", "call_claim_failed": "failed",
                            "voice_unavailable": "failed", "voice_format_invalid": "failed"}
                call_outcome = outcomes.get(str(category or ""))
                if not attempt_id or not call_outcome:
                    db.rollback()
                    return False
                attempt = db.execute("SELECT payload_enc,status,expires_at FROM telegram_call_attempts "
                    "WHERE id=? AND owner_key=?", (attempt_id, row["owner_key"])).fetchone()
                if not attempt:
                    db.rollback()
                    return False
                if attempt["status"] in {"needs_confirmation", "calling"}:
                    attempt_payload = self.vault.open_json(attempt["payload_enc"])
                    voice_draft = None
                    if call_outcome == "no_answer" and isinstance(attempt_payload, dict):
                        voice_draft = {"format": "voice",
                            "recipient_id": int(attempt_payload["recipient_id"]),
                            "recipient_name": str(attempt_payload["recipient_name"]),
                            "body": str(attempt_payload["body"]), "status": "draft", "auto_send": False}
                    db.execute("UPDATE telegram_call_attempts SET status=?,voice_draft_enc=?,updated_at=?,expires_at=? "
                        "WHERE id=? AND owner_key=? AND status IN ('needs_confirmation','calling')",
                        (call_outcome, self.vault.seal_json(voice_draft) if voice_draft else None,
                         iso(utc_now()), iso(utc_now() + timedelta(days=7)) if call_outcome == "no_answer"
                         else attempt["expires_at"], attempt_id, row["owner_key"]))
                elif attempt["status"] != call_outcome and not (
                        call_outcome == "failed" and attempt["status"] in {"cancelled", "expired", "failed"}):
                    db.rollback()
                    return False
            if operation == "send_voice" and row["call_fallback_for"]:
                self.finish_call_voice_fallback(db, attempt_id=str(row["call_fallback_for"]), status=status)
            recipient = (payload.get("recipient") or {}).get("display_name") or params.get("chat_name") or "chat"
            labels = {"send_message": "Xabar yuborildi", "send_voice": "Ovozli xabar yuborildi",
                      "call_user": "Telegram qo‘ng‘irog‘i",
                      "reply_message": "Javob yuborildi", "edit_message": "Xabar tahrirlandi",
                      "delete_message": "Xabar o‘chirildi", "forward_message": "Xabar forward qilindi",
                      "delete_group_message": "Guruh xabari o‘chirildi",
                      "mute_group_member": "Guruh a’zosi vaqtincha cheklab qo‘yildi",
                      "ban_group_member": "Guruh a’zosi bloklandi",
                      "set_group_welcome_rule": "Guruh uchun kutib olish qoidasi yoqildi",
                      "disable_group_welcome_rule": "Guruh kutib olish qoidasi o‘chirildi",
                      "send_media": "Media fayl yuborildi", "send_location": "Joylashuv yuborildi",
                      "send_contact": "Kontakt yuborildi", "send_poll": "So‘rovnoma yuborildi",
                      "archive_chat": "Chat arxivlandi", "unarchive_chat": "Chat arxivdan chiqarildi",
                      "mute_chat": "Chat ovozsiz qilindi", "unmute_chat": "Chat ovozi yoqildi",
                      "mark_read": "Chat o‘qilgan deb belgilandi", "pin_message": "Xabar mahkamlandi",
                      "unpin_message": "Xabar mahkamdan olindi", "leave_chat": "Chatdan chiqildi"}
            messages = {
                "sent": (f"{recipient} qo‘ng‘iroqni qabul qildi; Saida ovozli xabarni aytdi."
                         if operation == "call_user" and category == "call_answered" else
                         f"{recipient}: {labels.get(operation, 'Amal bajarildi')}.")
                        if operation == "call_user" else f"{labels.get(operation, 'Amal bajarildi')}: {recipient}.",
                "failed": (f"{recipient} qo‘ng‘iroqni rad etdi; xabar aytilmadi."
                           if category == "call_rejected" else
                           f"{recipient} javob bermadi. Ovozli qoralama saqlandi; tekshirib yuborish uchun /qo_ngiroq_qoralamalar buyrug‘idan foydalaning."
                           if category == "call_no_answer" else
                           f"{recipient} ga qo‘ng‘iroq qilib bo‘lmadi; xabar yuborilmadi."
                           if operation == "call_user" else
                           f"{recipient} chatida media yuborish huquqi yo‘q yoki Telegram ruxsat bermadi."
                           if category == "media_forbidden" else
                           f"{recipient} guruhida moderatsiya huquqi yo‘q yoki u o‘zgargan; amal bajarilmadi."
                           if category == "moderation_forbidden" else
                           "Tanlangan xabar muallifi o‘zgargan yoki himoyalangan; moderatsiya bajarilmadi."
                           if category in {"moderation_target_protected", "moderation_target_changed",
                                           "moderation_target_unknown"} else
                           f"{recipient} guruhida o‘chiriladigan kutib olish qoidasi topilmadi."
                           if category == "welcome_rule_missing" else
                           "Kutib olish qoidasini saqlash so‘rovi yaroqsiz; hech qanday qoida o‘zgartirilmadi."
                           if category == "welcome_rule_rejected" else
                           f"Fayl hajmi yoki formati Telegram chekloviga mos kelmadi ({recipient})."
                           if category in {"file_too_large", "invalid_media_type", "invalid_media_payload"} else
                           f"{recipient} uchun amal bajarilmadi ({category or 'telegram_xatosi'})."),
                "unknown": (f"{recipient} bilan qo‘ng‘iroq natijasi noma’lum. Xavfsizlik uchun qayta qo‘ng‘iroq qilinmadi."
                            if operation == "call_user" else
                            "Telegram amalining natijasi aniq emas. Xavfsizlik uchun takroran bajarilmadi."),
            }
            owner_id = int(payload["owner_id"])
            has_audit = db.execute("SELECT 1 FROM assistant_action_log WHERE owner_key=? AND action=? "
                                   "AND entity_type='profile_action' AND entity_id=? LIMIT 1",
                                   (row["owner_key"], "profile_action_result", job_id)).fetchone()
            if not has_audit:
                self._insert_action_log(db, owner_key=row["owner_key"], action="profile_action_result",
                    entity_type="profile_action", entity_id=job_id, payload={
                        "operation": str(operation), "chat_name": str(recipient)[:160],
                        "status": status, "result_category": str(category or "")[:80]})
            self._new_outbox(db, dedupe_key=f"profile-result:{job_id}", owner_id=owner_id,
                             chat_id=chat_id if chat_id is not None else owner_id,
                             text=messages[status], due_at=utc_now())
            if row["schedule_id"]:
                db.execute("UPDATE scheduled_profile_messages SET status=?,result_category=?,updated_at=? "
                           "WHERE id=? AND linked_action_id=?",
                           (status, category, iso(utc_now()), row["schedule_id"], job_id))
            self._erase_profile_media(db, job_id)
            db.commit()
            return True

    def get_action_for_result(self, job_id: str) -> dict[str, Any] | None:
        with contextlib.closing(self.connect()) as db:
            row = db.execute("SELECT payload_enc,status FROM profile_actions WHERE id=?", (job_id,)).fetchone()
        if not row:
            return None
        payload = self.vault.open_json(row["payload_enc"])
        return {**payload, "status": row["status"]}

    def has_consent(self, owner_id: int, kind: str) -> bool:
        with contextlib.closing(self.connect()) as db:
            row = db.execute("SELECT granted FROM consents WHERE owner_key=? AND kind=?",
                             (self.owner_key(owner_id, self.vault), kind)).fetchone()
        return bool(row and row["granted"])

    def set_consent(self, owner_id: int, kind: str, granted: bool) -> None:
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO consents(owner_key,kind,granted,updated_at) VALUES(?,?,?,?) "
                       "ON CONFLICT(owner_key,kind) DO UPDATE SET granted=excluded.granted,updated_at=excluded.updated_at",
                       (self.owner_key(owner_id, self.vault), kind, int(granted), iso(utc_now())))
            if kind == "voice" and not granted:
                db.execute("UPDATE speech_jobs SET status='cancelled',payload_enc=?,updated_at=? "
                           "WHERE owner_key=? AND status IN ('pending','processing')",
                           (self.vault.seal_json({}), iso(utc_now()), self.owner_key(owner_id, self.vault)))
                db.execute("UPDATE reminder_drafts SET status='cancelled' WHERE owner_key=? AND status='pending'",
                           (self.owner_key(owner_id, self.vault),))
                db.execute("UPDATE profile_actions SET status='cancelled',updated_at=? WHERE owner_key=? AND source='voice' AND status IN ('pending','queued','leased')",
                           (iso(utc_now()), self.owner_key(owner_id, self.vault)))
                schedules = db.execute("SELECT id,payload_enc FROM scheduled_profile_messages WHERE owner_key=? "
                                       "AND status IN ('pending','scheduled','queued','leased')",
                                       (self.owner_key(owner_id, self.vault),)).fetchall()
                for scheduled in schedules:
                    scheduled_payload = self.vault.open_json(scheduled["payload_enc"])
                    if scheduled_payload.get("source") == "voice":
                        db.execute("UPDATE scheduled_profile_messages SET status='cancelled',updated_at=? WHERE id=?",
                                   (iso(utc_now()), scheduled["id"]))
            if kind == "telegram_business" and not granted:
                db.execute("UPDATE telegram_business_drafts SET status='cancelled',payload_enc=?,updated_at=? "
                           "WHERE owner_key=? AND status='pending'",
                           (self.vault.seal_json({}), iso(utc_now()), self.owner_key(owner_id, self.vault)))
            db.commit()

    def enqueue_speech_job(self, *, update_id: int, owner_id: int, chat_id: int,
                           file_id: str, duration: int, file_size: int) -> bool:
        owner_key = self.owner_key(owner_id, self.vault)
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            pending = db.execute("SELECT COUNT(*) AS n FROM speech_jobs WHERE status IN ('pending','processing') AND owner_key=?",
                                 (owner_key,)).fetchone()["n"]
            consent = db.execute("SELECT granted FROM consents WHERE owner_key=? AND kind='voice'",
                                 (owner_key,)).fetchone()
            if pending >= 3 or not consent or not consent["granted"]:
                db.rollback()
                return False
            inserted = db.execute("INSERT OR IGNORE INTO bot_updates VALUES(?,?)", (update_id, iso(utc_now()))).rowcount
            if not inserted:
                db.rollback()
                return False
            job_id = uuid.uuid4().hex
            db.execute("INSERT INTO speech_jobs VALUES(?,?,?,?, 'pending',?,?)",
                       (job_id, update_id, owner_key, self.vault.seal_json({"file_id": file_id,
                        "duration": duration, "file_size": file_size}), iso(utc_now()), iso(utc_now())))
            db.commit()
        return True

    def claim_speech_job(self) -> dict[str, Any] | None:
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM speech_jobs WHERE status='pending' ORDER BY created_at LIMIT 1").fetchone()
            if not row:
                db.commit()
                return None
            consent = db.execute("SELECT granted FROM consents WHERE owner_key=? AND kind='voice'", (row["owner_key"],)).fetchone()
            if not consent or not consent["granted"]:
                db.execute("UPDATE speech_jobs SET status='cancelled',payload_enc=?,updated_at=? WHERE id=?",
                           (self.vault.seal_json({}), iso(utc_now()), row["id"]))
                db.commit()
                return None
            db.execute("UPDATE speech_jobs SET status='processing',updated_at=? WHERE id=?", (iso(utc_now()), row["id"]))
            payload = self.vault.open_json(row["payload_enc"])
            db.commit()
        return {**payload, "id": row["id"], "update_id": row["update_id"]}

    def save_speech_checkpoint(self, job_id: str, index: int,
                               checkpoint: dict[str, Any]) -> bool:
        if type(index) is not int or not 0 <= index < 20 or not isinstance(checkpoint, dict):
            return False
        try:
            start, end = float(checkpoint.get("start")), float(checkpoint.get("end"))
        except (TypeError, ValueError):
            return False
        result = checkpoint.get("result")
        if (not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start
                or end - start > 31 or not isinstance(result, dict)):
            return False
        text = " ".join(str(result.get("text") or "").split())[:3000]
        raw_confidence = result.get("confidence")
        try:
            confidence = float(raw_confidence)
        except (TypeError, ValueError):
            confidence = None
        if confidence is not None and (not math.isfinite(confidence) or not 0 <= confidence <= 1):
            confidence = None
        normalized_result: dict[str, Any] = {"text": text, "confidence": confidence}
        segments = []
        for item in result.get("segments", [])[:50] if isinstance(result.get("segments"), list) else []:
            if not isinstance(item, dict):
                continue
            try:
                seg_start, seg_end = float(item.get("start")), float(item.get("end"))
            except (TypeError, ValueError):
                continue
            seg_text = " ".join(str(item.get("text") or "").split())[:1200]
            if (seg_text and math.isfinite(seg_start) and math.isfinite(seg_end)
                    and 0 <= seg_start <= seg_end <= end - start + 1):
                segments.append({"start": seg_start, "end": seg_end, "text": seg_text})
        if segments:
            normalized_result["segments"] = segments
        word_scores = []
        for item in result.get("word_confidences", [])[:400] if isinstance(result.get("word_confidences"), list) else []:
            if not isinstance(item, dict):
                continue
            word = " ".join(str(item.get("word") or "").split())[:80]
            try:
                score = float(item.get("confidence"))
                word_start, word_end = float(item.get("start")), float(item.get("end"))
            except (TypeError, ValueError):
                continue
            if (word and math.isfinite(score) and 0 <= score <= 1
                    and math.isfinite(word_start) and math.isfinite(word_end)
                    and 0 <= word_start <= word_end <= end - start + 1):
                word_scores.append({"word": word, "confidence": score,
                                    "start": word_start, "end": word_end})
        if word_scores:
            normalized_result["word_confidences"] = word_scores
        stored_checkpoint = {"start": start, "end": end, "result": normalized_result}
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload_enc FROM speech_jobs WHERE id=? AND status='processing'",
                             (job_id,)).fetchone()
            if not row:
                db.rollback()
                return False
            payload = self.vault.open_json(row["payload_enc"])
            checkpoints = payload.get("checkpoints")
            if not isinstance(checkpoints, dict):
                checkpoints = {}
            checkpoints[str(index)] = stored_checkpoint
            payload["checkpoints"] = checkpoints
            encoded = self.vault.seal_json(payload)
            if len(encoded) > 256_000:
                db.rollback()
                return False
            db.execute("UPDATE speech_jobs SET payload_enc=?,updated_at=? WHERE id=? AND status='processing'",
                       (encoded, iso(utc_now()), job_id))
            db.commit()
        return True

    def finish_speech_job(self, job_id: str, *, failed: bool = False) -> None:
        with contextlib.closing(self.connect()) as db:
            db.execute("UPDATE speech_jobs SET status=?,payload_enc=?,updated_at=? WHERE id=? AND status='processing'",
                       ("failed" if failed else "done", self.vault.seal_json({}), iso(utc_now()), job_id))

    def retry_speech_job(self, job_id: str, *, max_retries: int = 1) -> bool:
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload_enc FROM speech_jobs WHERE id=? AND status='processing'",
                             (job_id,)).fetchone()
            if not row:
                db.rollback()
                return False
            payload = self.vault.open_json(row["payload_enc"])
            retries = int(payload.get("retries") or 0)
            if retries >= max_retries:
                db.execute("UPDATE speech_jobs SET status='failed',payload_enc=?,updated_at=? WHERE id=?",
                           (self.vault.seal_json({}), iso(utc_now()), job_id))
                db.commit()
                return False
            payload["retries"] = retries + 1
            db.execute("UPDATE speech_jobs SET status='pending',payload_enc=?,updated_at=? WHERE id=?",
                       (self.vault.seal_json(payload), iso(utc_now()), job_id))
            db.commit()
            return True

    def set_report_time(self, owner_id: int, value: str | None) -> None:
        key = f"{self.owner_key(owner_id, self.vault)}:report_time"
        with contextlib.closing(self.connect()) as db:
            if value is None:
                db.execute("DELETE FROM settings WHERE key=?", (key,))
            else:
                db.execute("INSERT INTO settings(key,value_enc,updated_at) VALUES(?,?,?) "
                           "ON CONFLICT(key) DO UPDATE SET value_enc=excluded.value_enc,updated_at=excluded.updated_at",
                           (key, self.vault.seal(value), iso(utc_now())))

    def get_report_time(self, owner_id: int) -> str | None:
        key = f"{self.owner_key(owner_id, self.vault)}:report_time"
        with contextlib.closing(self.connect()) as db:
            row = db.execute("SELECT value_enc FROM settings WHERE key=?", (key,)).fetchone()
        return self.vault.open_text(row["value_enc"]) if row else None

    def set_delivery_format(self, owner_id: int, target: str, value: str) -> None:
        allowed = {"owner_reply": {"text", "voice", "both"},
                   "profile_message": {"text", "voice"}}
        if target not in allowed or value not in allowed[target]:
            raise ValueError("Noto‘g‘ri yuborish formati")
        key = f"{self.owner_key(owner_id, self.vault)}:delivery:{target}"
        with contextlib.closing(self.connect()) as db:
            db.execute("INSERT INTO settings(key,value_enc,updated_at) VALUES(?,?,?) "
                       "ON CONFLICT(key) DO UPDATE SET value_enc=excluded.value_enc,updated_at=excluded.updated_at",
                       (key, self.vault.seal(value), iso(utc_now())))

    def get_delivery_format(self, owner_id: int, target: str) -> str:
        defaults = {"owner_reply": "both", "profile_message": "text"}
        if target not in defaults:
            raise ValueError("Noto‘g‘ri yuborish sozlamasi")
        key = f"{self.owner_key(owner_id, self.vault)}:delivery:{target}"
        with contextlib.closing(self.connect()) as db:
            row = db.execute("SELECT value_enc FROM settings WHERE key=?", (key,)).fetchone()
        return self.vault.open_text(row["value_enc"]) if row else defaults[target]

    def get_archive_retention_days(self, owner_id: int) -> int:
        # Conversation history is permanent by default and cannot expire in the
        # background. The owner can still explicitly export or delete it.
        return 0

    def set_archive_retention_days(self, owner_id: int, days: int) -> None:
        if days != 0:
            raise ValueError("Suhbat tarixi avtomatik o‘chirilmaydi; faqat egasi aniq so‘rasa o‘chiriladi")
        key = f"{self.owner_key(owner_id, self.vault)}:archive_retention_days"
        now = utc_now()
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO settings(key,value_enc,updated_at) VALUES(?,?,?) "
                       "ON CONFLICT(key) DO UPDATE SET value_enc=excluded.value_enc,updated_at=excluded.updated_at",
                       (key, self.vault.seal(str(days)), iso(now)))
            if days:
                db.execute("DELETE FROM assistant_history WHERE owner_key=? AND created_at<?",
                           (self.owner_key(owner_id, self.vault), iso(now - timedelta(days=days))))
            db.commit()

    @staticmethod
    def _knowledge_terms(value: str) -> set[str]:
        normalized = unicodedata.normalize("NFKC", value).casefold()
        normalized = normalized.translate(str.maketrans({char: "'" for char in "‘’ʻʼ`"}))
        normalized = re.sub(r"[^\w]+", " ", normalized)
        stop_words = {"bilan", "uchun", "qanday", "qaysi", "nima", "qayerda", "qayerdan",
                      "kerak", "olish", "haqida", "ayt", "menga", "shu", "bor", "bu", "the",
                      "and", "how", "what", "from", "with", "about"}
        return {word for word in normalized.split() if len(word) > 2 and word not in stop_words}

    def find_knowledge(self, owner_id: int, query: str, *, now: datetime | None = None,
                       limit: int = 2, kind: str | None = None) -> list[dict[str, Any]]:
        owner_key = self.owner_key(owner_id, self.vault)
        terms = self._knowledge_terms(query)
        if not terms:
            return []
        now = now or utc_now()
        with contextlib.closing(self.connect()) as db:
            if kind not in {None, "public", "personal"}:
                raise ValueError("Noto‘g‘ri bilim turi")
            rows = db.execute("SELECT id,payload_enc,review_after,updated_at,kind FROM assistant_knowledge "
                              "WHERE owner_key=? AND (? IS NULL OR kind=?) ORDER BY updated_at DESC LIMIT 300",
                              (owner_key, kind, kind)).fetchall()
        matches: list[tuple[float, str, dict[str, Any]]] = []
        for row in rows:
            payload = self.vault.open_json(row["payload_enc"])
            if not isinstance(payload, dict):
                continue
            item_terms = self._knowledge_terms(" ".join(str(payload.get(key) or "")
                                                        for key in ("topic", "summary", "keywords")))
            overlap = terms.intersection(item_terms)
            score = len(overlap) / len(terms)
            if overlap and score >= 0.5:
                item = {**payload, "id": row["id"], "kind": row["kind"],
                        "updated_at": row["updated_at"],
                        "stale": bool(row["review_after"] and row["review_after"] <= iso(now))}
                matches.append((score, str(row["updated_at"]), item))
        matches.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
        return [item for _, _, item in matches[:max(1, min(limit, 3))]]

    def save_knowledge(self, *, owner_id: int, identity: str, kind: str,
                       payload: dict[str, Any], review_after: datetime | None = None,
                       now: datetime | None = None) -> str:
        if kind not in {"public", "personal"}:
            raise ValueError("Noto‘g‘ri bilim turi")
        now = now or utc_now()
        payload = dict(payload)
        payload.setdefault("source_type", "unknown")
        payload.setdefault("source_time", iso(now))
        required = {"topic", "summary"}
        if not required.issubset(payload) or len(str(payload["topic"])) > 240 or len(str(payload["summary"])) > 2400:
            raise ValueError("Xotira yozuvi bo‘sh yoki juda uzun")
        owner_key = self.owner_key(owner_id, self.vault)
        identity_hash = self.vault.index("knowledge", identity.strip().casefold())
        item_id = uuid.uuid4().hex[:16]
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id FROM assistant_knowledge WHERE owner_key=? AND identity_hash=?",
                             (owner_key, identity_hash)).fetchone()
            if row:
                item_id = str(row["id"])
            db.execute("INSERT INTO assistant_knowledge(id,owner_key,identity_hash,payload_enc,kind,created_at,updated_at,review_after) "
                       "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(owner_key,identity_hash) DO UPDATE SET "
                       "payload_enc=excluded.payload_enc,kind=excluded.kind,updated_at=excluded.updated_at,review_after=excluded.review_after",
                       (item_id, owner_key, identity_hash, self.vault.seal_json(payload), kind,
                        iso(now), iso(now), iso(review_after) if review_after else None))
            if kind == "public":
                old = db.execute("SELECT id FROM assistant_knowledge WHERE owner_key=? AND kind='public' "
                                 "ORDER BY updated_at DESC LIMIT -1 OFFSET 300", (owner_key,)).fetchall()
                db.executemany("DELETE FROM assistant_knowledge WHERE id=?", [(r["id"],) for r in old])
            db.commit()
        return item_id

    def delete_knowledge(self, owner_id: int, item_id: str) -> bool:
        with contextlib.closing(self.connect()) as db:
            cur = db.execute("DELETE FROM assistant_knowledge WHERE id=? AND owner_key=?",
                             (item_id, self.owner_key(owner_id, self.vault)))
            return cur.rowcount == 1

    def delete_knowledge_for_owner(self, owner_id: int) -> int:
        with contextlib.closing(self.connect()) as db:
            cur = db.execute("DELETE FROM assistant_knowledge WHERE owner_key=?",
                             (self.owner_key(owner_id, self.vault),))
            return cur.rowcount

    def list_knowledge(self, owner_id: int, *, kind: str | None = None,
                       limit: int = 20) -> list[dict[str, Any]]:
        if kind not in {None, "public", "personal"}:
            raise ValueError("Noto‘g‘ri bilim turi")
        with contextlib.closing(self.connect()) as db:
            rows = db.execute("SELECT id,payload_enc,review_after,updated_at,kind FROM assistant_knowledge "
                              "WHERE owner_key=? AND (? IS NULL OR kind=?) "
                              "ORDER BY updated_at DESC LIMIT ?",
                              (self.owner_key(owner_id, self.vault), kind, kind,
                               max(1, min(limit, 30)))).fetchall()
        return [{**self.vault.open_json(row["payload_enc"]), "id": row["id"],
                 "kind": row["kind"], "updated_at": row["updated_at"],
                 "stale": bool(row["review_after"] and row["review_after"] <= iso(utc_now()))}
                for row in rows]

    def add_knowledge_document(self, *, owner_id: int, filename: str, media_type: str,
                               content_sha256: str, upload_update_id: int,
                               chunks: list[dict[str, str]], now: datetime | None = None) -> dict[str, Any]:
        filename = " ".join(str(filename).replace("\\", "/").split("/")[-1].split())[:160]
        media_type = str(media_type)[:40]
        if (not filename or len(content_sha256) != 64 or
                any(char not in "0123456789abcdef" for char in content_sha256.lower())):
            raise ValueError("Hujjat nomi yoki mazmun belgisi noto‘g‘ri.")
        if not 1 <= len(chunks) <= 200:
            raise ValueError("Hujjat bo‘sh yoki indekslash chegarasidan katta.")
        normalized_chunks: list[tuple[str, str]] = []
        total_chars = 0
        for chunk in chunks:
            location = " ".join(str(chunk.get("location") or "").split())[:120]
            content = " ".join(str(chunk.get("text") or "").split())
            if not content or len(content) > 1800:
                raise ValueError("Hujjat parchasi bo‘sh yoki belgilangan chegaradan uzun.")
            total_chars += len(content)
            if total_chars > 360_000:
                raise ValueError("Hujjat matni indekslash chegarasidan katta.")
            normalized_chunks.append((location or "Bo‘lim ko‘rsatilmagan", content))
        now = now or utc_now()
        owner_key = self.owner_key(owner_id, self.vault)
        name_key = unicodedata.normalize("NFKC", filename).casefold()
        name_hash = self.vault.index("knowledge-document-name-v1", name_key)
        content_hash = self.vault.index("knowledge-document-content-v1", content_sha256.lower())
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            replay = db.execute("SELECT payload_enc FROM knowledge_document_versions "
                                "WHERE owner_key=? AND upload_update_id=?",
                                (owner_key, upload_update_id)).fetchone()
            if replay:
                saved = self.vault.open_json(replay["payload_enc"])
                db.commit()
                return {**saved, "duplicate": True}
            document = db.execute("SELECT id,current_version FROM knowledge_documents "
                                  "WHERE owner_key=? AND filename_hash=?", (owner_key, name_hash)).fetchone()
            if document:
                file_id = str(document["id"])
                same_content = db.execute("SELECT version,payload_enc FROM knowledge_document_versions "
                                          "WHERE file_id=? AND content_hash=? ORDER BY version DESC LIMIT 1",
                                          (file_id, content_hash)).fetchone()
                if same_content:
                    version = int(same_content["version"])
                    db.execute("UPDATE knowledge_documents SET current_version=?,updated_at=? WHERE id=?",
                               (version, iso(now), file_id))
                    saved = self.vault.open_json(same_content["payload_enc"])
                    db.commit()
                    return {**saved, "duplicate": True}
                version = int(document["current_version"]) + 1
            else:
                count = int(db.execute("SELECT COUNT(*) FROM knowledge_documents WHERE owner_key=?",
                                       (owner_key,)).fetchone()[0])
                if count >= 50:
                    db.rollback()
                    raise ValueError("Bilim bazasida ko‘pi bilan 50 ta hujjat saqlanadi.")
                file_id, version = uuid.uuid4().hex[:16], 1
                db.execute("INSERT INTO knowledge_documents(id,owner_key,filename_hash,current_version,created_at,updated_at) "
                           "VALUES(?,?,?,?,?,?)", (file_id, owner_key, name_hash, version, iso(now), iso(now)))
            metadata = {"file_id": file_id, "filename": filename, "media_type": media_type,
                        "version": version, "chunks": len(normalized_chunks),
                        "uploaded_at": iso(now), "source_type": "document_upload",
                        "source_time": iso(now), "source_update_id": int(upload_update_id)}
            db.execute("INSERT INTO knowledge_document_versions(file_id,version,owner_key,content_hash,"
                       "upload_update_id,payload_enc,created_at) VALUES(?,?,?,?,?,?,?)",
                       (file_id, version, owner_key, content_hash, upload_update_id,
                        self.vault.seal_json(metadata), iso(now)))
            db.execute("UPDATE knowledge_documents SET current_version=?,updated_at=? WHERE id=?",
                       (version, iso(now), file_id))
            for index, (location, content) in enumerate(normalized_chunks):
                chunk_id = uuid.uuid4().hex[:16]
                db.execute("INSERT INTO knowledge_document_chunks(id,owner_key,file_id,version,chunk_index,"
                           "payload_enc,created_at) VALUES(?,?,?,?,?,?,?)",
                           (chunk_id, owner_key, file_id, version, index,
                            self.vault.seal_json({"location": location, "text": content}), iso(now)))
                terms = self._knowledge_terms(filename + " " + content)
                db.executemany("INSERT INTO knowledge_document_terms(chunk_id,owner_key,term_hash) VALUES(?,?,?)",
                               [(chunk_id, owner_key, self.vault.index("knowledge-document-term-v1", term))
                                for term in sorted(terms)])
            db.execute("DELETE FROM knowledge_document_versions WHERE file_id=? AND version<=?",
                       (file_id, version - 20))
            db.commit()
        return {**metadata, "duplicate": False}

    def list_knowledge_documents(self, owner_id: int) -> list[dict[str, Any]]:
        owner_key = self.owner_key(owner_id, self.vault)
        with contextlib.closing(self.connect()) as db:
            rows = db.execute("SELECT v.payload_enc FROM knowledge_documents d "
                              "JOIN knowledge_document_versions v ON v.file_id=d.id "
                              "AND v.version=d.current_version WHERE d.owner_key=? "
                              "ORDER BY d.updated_at DESC,d.id", (owner_key,)).fetchall()
        return [self.vault.open_json(row["payload_enc"]) for row in rows]

    def search_knowledge_documents(self, owner_id: int, query: str, *, limit: int = 4) -> list[dict[str, Any]]:
        terms = sorted(self._knowledge_terms(query))[:32]
        if not terms:
            return []
        owner_key = self.owner_key(owner_id, self.vault)
        hashes = [self.vault.index("knowledge-document-term-v1", term) for term in terms]
        placeholders = ",".join("?" for _ in hashes)
        with contextlib.closing(self.connect()) as db:
            rows = db.execute(
                "SELECT c.id,c.payload_enc,c.file_id,c.version,c.chunk_index,COUNT(*) AS hits "
                "FROM knowledge_document_chunks c "
                "JOIN knowledge_documents d ON d.id=c.file_id AND d.current_version=c.version "
                "JOIN knowledge_document_terms t ON t.chunk_id=c.id "
                f"WHERE c.owner_key=? AND t.owner_key=? AND t.term_hash IN ({placeholders}) "
                "GROUP BY c.id ORDER BY hits DESC,c.created_at DESC,c.chunk_index LIMIT 80",
                (owner_key, owner_key, *hashes)).fetchall()
            files = {str(item["file_id"]): item for item in self.list_knowledge_documents(owner_id)}
        minimum_hits = 2 if len(terms) >= 3 else 1
        results: list[dict[str, Any]] = []
        for row in rows:
            hits = int(row["hits"])
            if hits < minimum_hits or hits / len(terms) < 0.18:
                continue
            metadata = files.get(str(row["file_id"]))
            if not metadata:
                continue
            chunk = self.vault.open_json(row["payload_enc"])
            results.append({**metadata, "location": chunk.get("location", ""),
                            "text": chunk.get("text", ""), "score": hits / len(terms),
                            "chunk_index": int(row["chunk_index"])})
            if len(results) >= max(1, min(limit, 6)):
                break
        return results

    def delete_knowledge_document(self, owner_id: int, file_id: str) -> bool:
        if not re.fullmatch(r"[a-f0-9]{16}", str(file_id)):
            return False
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            deleted = db.execute("DELETE FROM knowledge_documents WHERE id=? AND owner_key=?",
                                 (file_id, self.owner_key(owner_id, self.vault))).rowcount == 1
            db.commit()
        return deleted

    def create_scheduled_profile_message(self, *, owner_id: int, source_key: str,
                                         operation: str, parameters: dict[str, Any],
                                         body: str, message_format: str,
                                         due_at: datetime, timezone_name: str,
                                         ttl_seconds: int = 300, grace_minutes: int = 10,
                                         source: str = "text",
                                         weather_query: dict[str, Any] | None = None,
                                         reschedule_id: str | None = None,
                                         now: datetime | None = None) -> tuple[str, str] | None:
        if operation not in {"send_message", "send_voice"} or message_format not in {"text", "voice"}:
            raise ValueError("Noto‘g‘ri rejalangan xabar")
        if source not in {"text", "voice"} or not body.strip() or len(body) > 2000:
            raise ValueError("Yuborish matni yoki manbasi noto‘g‘ri")
        due_at = due_at.astimezone(UTC)
        now = now or utc_now()
        if due_at <= now + timedelta(minutes=1) or due_at > now + timedelta(days=365):
            raise ValueError("Yuborish vaqti kelajakdagi 1 daqiqadan 365 kungacha bo‘lishi kerak")
        if grace_minutes < 1 or grace_minutes > 30:
            raise ValueError("Noto‘g‘ri yetkazish muddati")
        owner_key = self.owner_key(owner_id, self.vault)
        schedule_id = uuid.uuid4().hex[:16]
        approval_token = secrets.token_urlsafe(16)
        payload = {"owner_id": owner_id, "operation": operation,
                   "params": {**parameters, "body": body.strip(), "format": message_format},
                   "body": body.strip(), "format": message_format,
                   "timezone": timezone_name, "source": source}
        if weather_query is not None:
            city = " ".join(str(weather_query.get("city") or "").split())[:100]
            target_date = str(weather_query.get("target_date") or "")
            hour = weather_query.get("hour")
            if (not city or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", target_date)
                    or (hour is not None and not re.fullmatch(r"(?:[01]\d|2[0-3]):00", str(hour)))):
                raise ValueError("Rejalangan ob-havo so‘rovi noto‘g‘ri.")
            payload["weather_query"] = {"city": city, "target_date": target_date,
                                         "hour": str(hour) if hour is not None else None}
        if reschedule_id:
            if not re.fullmatch(r"[a-f0-9]{16}", reschedule_id):
                raise ValueError("Ko‘chiriladigan reja ID si noto‘g‘ri.")
            payload["rescheduled_from_id"] = reschedule_id
        approval_expires_at = min(due_at, now + timedelta(seconds=ttl_seconds))
        deliver_until = due_at + timedelta(minutes=grace_minutes)
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            if reschedule_id:
                previous = db.execute("SELECT status,due_at FROM scheduled_profile_messages "
                                      "WHERE id=? AND owner_key=?",
                                      (reschedule_id, owner_key)).fetchone()
                if (not previous or previous["status"] not in {"pending", "scheduled"}
                        or previous["due_at"] <= iso(now + timedelta(seconds=ttl_seconds))):
                    db.rollback()
                    raise ValueError("Eski reja ko‘chirish uchun yaroqli emas yoki tasdiq oynasidan oldin yuborilishi mumkin.")
            if source == "voice":
                consent = db.execute("SELECT granted FROM consents WHERE owner_key=? AND kind='voice'",
                                     (owner_key,)).fetchone()
                if not consent or not consent["granted"]:
                    db.rollback()
                    return None
            inserted = db.execute("""INSERT OR IGNORE INTO scheduled_profile_messages
                (id,owner_key,source_key,approval_hash,payload_enc,random_id,due_at,approval_expires_at,
                 deliver_until,status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,'pending',?,?)""",
                (schedule_id, owner_key, source_key, self.vault.index("schedule-approval", approval_token),
                 self.vault.seal_json(payload), secrets.randbelow((1 << 63) - 1) + 1,
                 iso(due_at), iso(approval_expires_at), iso(deliver_until), iso(now), iso(now))).rowcount
            if inserted:
                db.commit()
                return schedule_id, approval_token
            existing = db.execute("SELECT id,payload_enc,due_at,approval_expires_at,status FROM scheduled_profile_messages "
                                  "WHERE owner_key=? AND source_key=?", (owner_key, source_key)).fetchone()
            if (existing and existing["status"] == "pending"
                    and existing["due_at"] == iso(due_at)
                    and existing["approval_expires_at"] > iso(now)
                    and self.vault.open_json(existing["payload_enc"]) == payload):
                db.execute("UPDATE scheduled_profile_messages SET approval_hash=?,updated_at=? "
                           "WHERE id=? AND status='pending'",
                           (self.vault.index("schedule-approval", approval_token), iso(now), existing["id"]))
                db.commit()
                return str(existing["id"]), approval_token
            db.rollback()
            return None

    def set_scheduled_preview(self, schedule_id: str, message_id: int) -> None:
        with contextlib.closing(self.connect()) as db:
            db.execute("UPDATE scheduled_profile_messages SET preview_message_id=?,updated_at=? "
                       "WHERE id=? AND status='pending'",
                       (message_id, iso(utc_now()), schedule_id))

    def inspect_pending_scheduled_profile_message(self, token: str, owner_id: int,
                                                 message_id: int, *, now: datetime | None = None) -> dict[str, Any]:
        now = now or utc_now()
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM scheduled_profile_messages WHERE approval_hash=? AND status='pending'",
                             (self.vault.index("schedule-approval", token),)).fetchone()
            if not row:
                db.rollback()
                return {"status": "unavailable"}
            if (row["owner_key"] != self.owner_key(owner_id, self.vault)
                    or row["preview_message_id"] != message_id):
                db.rollback()
                return {"status": "forbidden"}
            if row["approval_expires_at"] <= iso(now):
                db.execute("UPDATE scheduled_profile_messages SET status='expired',updated_at=? "
                           "WHERE id=? AND status='pending'", (iso(now), row["id"]))
                db.commit()
                return {"status": "expired"}
            payload = self.vault.open_json(row["payload_enc"])
            db.commit()
            return {"status": "pending", "id": str(row["id"]),
                    "params": payload.get("params") if isinstance(payload.get("params"), dict) else {},
                    "format": str(payload.get("format") or "text")}

    def approve_scheduled_profile_message(self, token: str, owner_id: int,
                                          message_id: int, *, now: datetime | None = None) -> str:
        now = now or utc_now()
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM scheduled_profile_messages WHERE approval_hash=? AND status='pending'",
                             (self.vault.index("schedule-approval", token),)).fetchone()
            if not row:
                db.rollback()
                return "unavailable"
            if row["owner_key"] != self.owner_key(owner_id, self.vault) or row["preview_message_id"] != message_id:
                db.rollback()
                return "forbidden"
            if row["approval_expires_at"] <= iso(now):
                db.execute("UPDATE scheduled_profile_messages SET status='expired',updated_at=? WHERE id=?",
                           (iso(now), row["id"]))
                db.commit()
                return "expired"
            payload = self.vault.open_json(row["payload_enc"])
            reschedule_id = str(payload.get("rescheduled_from_id") or "")
            if reschedule_id:
                previous = db.execute("SELECT status,due_at FROM scheduled_profile_messages "
                                      "WHERE id=? AND owner_key=?",
                                      (reschedule_id, row["owner_key"])).fetchone()
                if (not previous or previous["status"] not in {"pending", "scheduled"}
                        or previous["due_at"] <= iso(now + timedelta(minutes=1))):
                    db.execute("UPDATE scheduled_profile_messages SET status='cancelled',updated_at=? "
                               "WHERE id=? AND status='pending'", (iso(now), row["id"]))
                    db.commit()
                    return "reschedule_stale"
                changed = db.execute("UPDATE scheduled_profile_messages SET status='cancelled',updated_at=? "
                                     "WHERE id=? AND owner_key=? AND status IN ('pending','scheduled') "
                                     "AND due_at>?",
                                     (iso(now), reschedule_id, row["owner_key"],
                                      iso(now + timedelta(minutes=1)))).rowcount
                if changed != 1:
                    db.execute("UPDATE scheduled_profile_messages SET status='cancelled',updated_at=? "
                               "WHERE id=? AND status='pending'", (iso(now), row["id"]))
                    db.commit()
                    return "reschedule_stale"
            db.execute("UPDATE scheduled_profile_messages SET status='scheduled',updated_at=? "
                       "WHERE id=? AND status='pending'", (iso(now), row["id"]))
            db.commit()
        return "scheduled"

    def cancel_scheduled_profile_preview(self, token: str, owner_id: int,
                                         message_id: int) -> str:
        now = iso(utc_now())
        owner_key = self.owner_key(owner_id, self.vault)
        token_hash = self.vault.index("schedule-approval", token)
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id,payload_enc FROM scheduled_profile_messages "
                             "WHERE approval_hash=? AND owner_key=? AND preview_message_id=? AND status='pending'",
                             (token_hash, owner_key, message_id)).fetchone()
            if not row:
                db.rollback()
                return "unavailable"
            payload = self.vault.open_json(row["payload_enc"])
            db.execute("UPDATE scheduled_profile_messages SET status='cancelled',updated_at=? "
                       "WHERE id=? AND status='pending'", (now, row["id"]))
            db.commit()
            return "replacement_cancelled" if payload.get("rescheduled_from_id") else "cancelled"

    def list_scheduled_profile_messages(self, owner_id: int, *, limit: int = 20) -> list[dict[str, Any]]:
        with contextlib.closing(self.connect()) as db:
            rows = db.execute("SELECT * FROM scheduled_profile_messages WHERE owner_key=? AND status IN "
                              "('pending','scheduled','queued','leased','dispatching','sent','failed','unknown') "
                              "ORDER BY due_at DESC LIMIT ?",
                              (self.owner_key(owner_id, self.vault), max(1, min(limit, 30)))).fetchall()
        messages = []
        for row in rows:
            payload = self.vault.open_json(row["payload_enc"])
            params = payload.get("params") or {}
            messages.append({"id": row["id"], "chat_name": params.get("chat_name", "chat"),
                             "topic_name": params.get("topic_name"),
                             "body": payload.get("body", ""), "format": payload.get("format", "text"),
                             "weather_query": payload.get("weather_query"),
                             "replaces_schedule_id": payload.get("rescheduled_from_id"),
                             "timezone": payload.get("timezone", "Asia/Tashkent"),
                             "due_at": row["due_at"], "status": row["status"],
                             "result_category": row["result_category"]})
        return messages

    def due_scheduled_weather_messages(self, owner_id: int, *,
                                       now: datetime | None = None,
                                       grace_minutes: int = 10) -> list[dict[str, Any]]:
        now = now or utc_now()
        current = iso(now)
        cutoff = iso(now - timedelta(minutes=grace_minutes))
        owner_key = self.owner_key(owner_id, self.vault)
        with contextlib.closing(self.connect()) as db:
            rows = db.execute(
                "SELECT id,payload_enc FROM scheduled_profile_messages WHERE owner_key=? "
                "AND status='scheduled' AND due_at<=? AND due_at>=? ORDER BY due_at LIMIT 25",
                (owner_key, current, cutoff)).fetchall()
        due: list[dict[str, Any]] = []
        for row in rows:
            payload = self.vault.open_json(row["payload_enc"])
            query = payload.get("weather_query")
            if isinstance(query, dict) and not payload.get("weather_refreshed_at"):
                due.append({"id": row["id"], "weather_query": query})
        return due

    def update_scheduled_weather_message(self, owner_id: int, schedule_id: str,
                                         body: str, *, now: datetime | None = None) -> bool:
        body = " ".join(str(body or "").split())
        if not body or len(body) > 2000:
            return False
        now = now or utc_now()
        owner_key = self.owner_key(owner_id, self.vault)
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload_enc FROM scheduled_profile_messages "
                             "WHERE id=? AND owner_key=? AND status='scheduled'",
                             (schedule_id, owner_key)).fetchone()
            if not row:
                db.rollback()
                return False
            payload = self.vault.open_json(row["payload_enc"])
            if not isinstance(payload.get("weather_query"), dict) or payload.get("weather_refreshed_at"):
                db.rollback()
                return False
            payload["body"] = body
            payload["params"]["body"] = body
            payload["weather_refreshed_at"] = iso(now)
            db.execute("UPDATE scheduled_profile_messages SET payload_enc=?,updated_at=? "
                       "WHERE id=? AND owner_key=? AND status='scheduled'",
                       (self.vault.seal_json(payload), iso(now), schedule_id, owner_key))
            db.commit()
            return True

    def fail_scheduled_weather_message(self, owner_id: int, schedule_id: str, *,
                                       now: datetime | None = None) -> bool:
        now = now or utc_now()
        current = iso(now)
        owner_key = self.owner_key(owner_id, self.vault)
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload_enc FROM scheduled_profile_messages "
                             "WHERE id=? AND owner_key=? AND status='scheduled'",
                             (schedule_id, owner_key)).fetchone()
            if not row:
                db.rollback()
                return False
            payload = self.vault.open_json(row["payload_enc"])
            if not isinstance(payload.get("weather_query"), dict) or payload.get("weather_refreshed_at"):
                db.rollback()
                return False
            db.execute("UPDATE scheduled_profile_messages SET status='failed',updated_at=?,"
                       "result_category='weather_forecast_unavailable' WHERE id=? AND owner_key=? AND status='scheduled'",
                       (current, schedule_id, owner_key))
            self._new_outbox(db, dedupe_key=f"scheduled-weather-failed:{schedule_id}",
                             owner_id=owner_id, chat_id=owner_id,
                             text="Rejalangan ob-havo xabari yuborilmadi: yuborish vaqtida yangi prognoz olinmadi. "
                                  "Eski prognozni yubormadim.", due_at=now)
            db.commit()
            return True

    def cancel_scheduled_profile_message(self, owner_id: int, schedule_id: str) -> str:
        now = iso(utc_now())
        owner_key = self.owner_key(owner_id, self.vault)
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT linked_action_id,status FROM scheduled_profile_messages "
                             "WHERE id=? AND owner_key=?", (schedule_id, owner_key)).fetchone()
            if not row:
                db.rollback()
                return "not_found"
            if row["status"] in {"pending", "scheduled"}:
                db.execute("UPDATE scheduled_profile_messages SET status='cancelled',updated_at=? WHERE id=?",
                           (now, schedule_id))
                db.commit()
                return "cancelled"
            if row["status"] in {"queued", "leased"} and row["linked_action_id"]:
                action = db.execute("SELECT status FROM profile_actions WHERE id=?",
                                    (row["linked_action_id"],)).fetchone()
                if action and action["status"] in {"queued", "leased"}:
                    db.execute("UPDATE profile_actions SET status='cancelled',updated_at=? WHERE id=? "
                               "AND status IN ('queued','leased')", (now, row["linked_action_id"]))
                    db.execute("UPDATE scheduled_profile_messages SET status='cancelled',updated_at=? WHERE id=?",
                               (now, schedule_id))
                    db.commit()
                    return "cancelled"
            db.rollback()
            return "already_sending"

    def queue_due_profile_messages(self, *, max_per_hour: int,
                                   now: datetime | None = None,
                                   grace_minutes: int = 10) -> int:
        now = now or utc_now()
        current = iso(now)
        cutoff = iso(now - timedelta(minutes=grace_minutes))
        queued = 0
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE scheduled_profile_messages SET status='expired',updated_at=? "
                       "WHERE status='pending' AND approval_expires_at<=?", (current, current))
            expired = db.execute("SELECT id,owner_key,payload_enc FROM scheduled_profile_messages "
                                 "WHERE status='scheduled' AND due_at<?", (cutoff,)).fetchall()
            for row in expired:
                payload = self.vault.open_json(row["payload_enc"])
                owner_id = int(payload["owner_id"])
                db.execute("UPDATE scheduled_profile_messages SET status='expired',updated_at=?,"
                           "result_category='delivery_window_elapsed' WHERE id=? AND status='scheduled'",
                           (current, row["id"]))
                self._new_outbox(db, dedupe_key=f"scheduled-expired:{row['id']}", owner_id=owner_id,
                                 chat_id=owner_id, text="Rejalangan xabar 10 daqiqalik yuborish muddatidan kechikdi.",
                                 due_at=now)
            due = db.execute("SELECT * FROM scheduled_profile_messages WHERE status='scheduled' "
                             "AND due_at<=? AND due_at>=? ORDER BY due_at LIMIT 50",
                             (current, cutoff)).fetchall()
            for row in due:
                owner = db.execute("SELECT payload_enc FROM profile_actions WHERE owner_key=? AND created_at>=? "
                                    "AND status IN ('queued','leased','dispatching','sent','failed','unknown')",
                                    (row["owner_key"], iso(now - timedelta(hours=1)))).fetchall()
                if max_per_hour <= 0 or len(owner) >= max_per_hour:
                    continue
                payload = self.vault.open_json(row["payload_enc"])
                if isinstance(payload.get("weather_query"), dict) and not payload.get("weather_refreshed_at"):
                    continue
                if payload.get("format") == "voice":
                    payload["operation"] = "send_voice"
                action_id = uuid.uuid4().hex
                approval_hash = self.vault.index("approval", secrets.token_urlsafe(24))
                db.execute("""INSERT OR IGNORE INTO profile_actions
                    (id,approval_hash,owner_key,preview_message_id,payload_enc,random_id,source,status,
                     expires_at,lease_hash,lease_until,created_at,updated_at,result_category,schedule_id)
                    VALUES(?,?,?,?,?,?,?,'queued',?,NULL,NULL,?,?,NULL,?)""",
                    (action_id, approval_hash, row["owner_key"], row["preview_message_id"],
                     self.vault.seal_json(payload), row["random_id"],
                     "voice" if payload.get("source") == "voice" else "text",
                     row["deliver_until"], current, current, row["id"]))
                if db.execute("SELECT changes()").fetchone()[0] != 1:
                    continue
                db.execute("UPDATE scheduled_profile_messages SET status='queued',linked_action_id=?,updated_at=? "
                           "WHERE id=? AND status='scheduled'", (action_id, current, row["id"]))
                queued += 1
            db.commit()
        return queued

    def voice_job_payload(self, job_id: str, lease_token: str) -> dict[str, Any] | None:
        now = iso(utc_now())
        with contextlib.closing(self.connect()) as db:
            row = db.execute("SELECT payload_enc FROM profile_actions WHERE id=? AND status='leased' "
                             "AND lease_hash=? AND lease_until>? AND expires_at>?",
                             (job_id, self.vault.index("lease", lease_token), now, now)).fetchone()
        if not row:
            return None
        payload = self.vault.open_json(row["payload_enc"])
        params = payload.get("params") or {}
        operation = str(payload.get("operation") or "")
        if not (operation == "send_voice" and params.get("format") == "voice") and not (
                operation == "call_user" and params.get("format") == "voice"):
            return None
        body = str(payload.get("body") or params.get("body") or "").strip()
        return {"body": body[:2000]} if body else None

    def call_job_payload(self, job_id: str, lease_token: str) -> dict[str, Any] | None:
        now = iso(utc_now())
        with contextlib.closing(self.connect()) as db:
            row = db.execute("SELECT payload_enc FROM profile_actions WHERE id=? AND status='dispatching' "
                "AND lease_hash=? AND lease_until>? AND expires_at>?",
                (job_id, self.vault.index("lease", lease_token), now, now)).fetchone()
        if not row:
            return None
        payload = self.vault.open_json(row["payload_enc"])
        if payload.get("operation") != "call_user":
            return None
        params = payload.get("params") or {}
        try:
            recipient_id = int(params.get("chat_id"))
        except (TypeError, ValueError):
            return None
        attempt_id = str(params.get("call_attempt_id") or "")
        if recipient_id <= 0 or not attempt_id or params.get("format") != "voice":
            return None
        return {"attempt_id": attempt_id, "recipient_id": recipient_id}

    def finish_call_voice_fallback(self, db: sqlite3.Connection, *, attempt_id: str,
                                   status: str) -> None:
        if status not in {"sent", "unknown"}:
            return
        row = db.execute("SELECT voice_draft_enc FROM telegram_call_attempts WHERE id=?", (attempt_id,)).fetchone()
        if not row or not row["voice_draft_enc"]:
            return
        draft = self.vault.open_json(row["voice_draft_enc"])
        if not isinstance(draft, dict):
            return
        draft["status"] = "sent" if status == "sent" else "unknown"
        db.execute("UPDATE telegram_call_attempts SET voice_draft_enc=?,updated_at=? WHERE id=?",
                   (self.vault.seal_json(draft), iso(utc_now()), attempt_id))

    def get_assistant_history(self, owner_id: int, limit: int = 8, *,
                              exclude_update_id: int | None = None) -> list[dict[str, str]]:
        owner_key = self.owner_key(owner_id, self.vault)
        with contextlib.closing(self.connect()) as db:
            clauses = ["owner_key=?"]
            params: list[Any] = [owner_key]
            if exclude_update_id is not None:
                clauses.append("update_id<>?")
                params.append(exclude_update_id)
            params.append(max(1, min(limit, 12)))
            rows = db.execute("SELECT payload_enc FROM assistant_history WHERE " + " AND ".join(clauses) +
                              " ORDER BY created_at DESC, update_id DESC LIMIT ?", params).fetchall()
        history: list[dict[str, str]] = []
        for row in reversed(rows):
            payload = self.vault.open_json(row["payload_enc"])
            if isinstance(payload, dict):
                user_text = str(payload.get("user") or "")[:1600]
                answer = str(payload.get("assistant") or "")[:2000]
                if user_text:
                    history.append({"role": "user", "content": user_text})
                if answer:
                    history.append({"role": "assistant", "content": answer})
        return history

    @staticmethod
    def _history_token_groups(value: str) -> list[set[str]]:
        normalized = unicodedata.normalize("NFKC", value).casefold()
        normalized = normalized.translate(str.maketrans({char: "'" for char in "‘’ʻʼ`"}))
        normalized = re.sub(r"[^\w@.-]+", " ", normalized)
        stop_words = {"bilan", "uchun", "qanday", "qaysi", "nima", "qayerda", "qayerdan",
                      "kerak", "olish", "haqida", "ayt", "aytdim", "aytgan", "menga", "shu",
                      "bor", "bu", "the", "and", "how", "what", "from", "with", "about",
                      "oldingi", "oldin", "suhbat", "esingdami", "chat", "telegram",
                      "men", "sen", "siz", "biz", "ular", "qayer"}
        groups: list[set[str]] = []
        for raw_word in normalized.split():
            surface = raw_word.strip(".-_")
            word = surface.translate(_HISTORY_CYRILLIC_TO_LATIN)
            if len(word) <= 2 or word in stop_words:
                continue
            forms = {surface, word}
            stem = word
            for _ in range(3):
                suffix = next((candidate for candidate in _HISTORY_SUFFIXES
                               if stem.endswith(candidate) and len(stem) - len(candidate) >= 3), None)
                if suffix is None:
                    break
                stem = stem[:-len(suffix)].rstrip(".-_")
                if len(stem) < 3:
                    break
                if stem not in stop_words:
                    forms.add(stem)
            forms.update(_history_cyrillic_alias(form) for form in tuple(forms)
                         if not re.search(r"[\u0400-\u04ff]", form))
            # Repeated forms and inflected versions of the same term count as
            # one query concept, including Latin/Cyrillic aliases, so they cannot
            # satisfy the multi-term threshold as separate words.
            overlaps = [group for group in groups if group.intersection(forms)]
            if overlaps:
                merged = forms.union(*overlaps)
                groups = [group for group in groups if group not in overlaps]
                groups.append(merged)
            else:
                groups.append(forms)
        return groups

    @classmethod
    def _history_terms(cls, value: str) -> set[str]:
        return set().union(*cls._history_token_groups(value)) if value.strip() else set()

    def search_assistant_history(self, owner_id: int, query: str, *, since: str | None = None,
                                 until: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
        groups = self._history_token_groups(query)
        if not groups:
            return []
        # Keep SQLite work bounded even when a caller passes a long transcript.
        groups = groups[:40]
        terms = set().union(*groups)
        hashes = [self.vault.index("assistant-history-term", term) for term in sorted(terms)]
        placeholders = ",".join("?" for _ in hashes)
        clauses = ["h.owner_key=?", f"t.term_hash IN ({placeholders})"]
        where_params: list[Any] = [self.owner_key(owner_id, self.vault), *hashes]
        if since:
            clauses.append("h.created_at>=?")
            where_params.append(since)
        if until:
            clauses.append("h.created_at<?")
            where_params.append(until)
        group_expressions: list[str] = []
        group_params: list[str] = []
        for group in groups:
            group_hashes = [self.vault.index("assistant-history-term", term) for term in sorted(group)]
            group_placeholders = ",".join("?" for _ in group_hashes)
            group_expressions.append(
                f"MAX(CASE WHEN t.term_hash IN ({group_placeholders}) THEN 1 ELSE 0 END)"
            )
            group_params.extend(group_hashes)
        params = [*group_params, *where_params,
                  2 if len(groups) > 4 else 1, max(1, min(limit, 10)) * 12]
        with contextlib.closing(self.connect()) as db:
            rows = db.execute(
                "SELECT h.update_id,h.payload_enc,h.created_at,"
                f"({' + '.join(group_expressions)}) AS matched "
                "FROM assistant_history h JOIN assistant_history_terms t ON t.update_id=h.update_id "
                f"WHERE {' AND '.join(clauses)} GROUP BY h.update_id "
                "HAVING matched>=? ORDER BY matched DESC,h.created_at DESC LIMIT ?", params
            ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            payload = self.vault.open_json(row["payload_enc"])
            if not isinstance(payload, dict):
                continue
            results.append({"update_id": int(row["update_id"]), "created_at": str(row["created_at"]),
                            "user": str(payload.get("user") or ""),
                            "assistant": str(payload.get("assistant") or ""),
                            "matched_terms": int(row["matched"])})
        return results[:max(1, min(limit, 10))]

    def append_assistant_turn(self, update_id: int, owner_id: int, user_text: str, answer: str) -> None:
        owner_key = self.owner_key(owner_id, self.vault)
        # Keep the full useful Telegram turn in the encrypted archive. Model
        # context is still bounded separately in get_assistant_history().
        user_text, answer = user_text[:10000], answer[:10000]
        payload = self.vault.seal_json({"user": user_text, "assistant": answer})
        now = utc_now()
        created_at = now.isoformat(timespec="microseconds")
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT owner_key FROM assistant_history WHERE update_id=?",
                                  (update_id,)).fetchone()
            if existing and existing["owner_key"] != owner_key:
                db.rollback()
                return
            if existing:
                db.execute("UPDATE assistant_history SET payload_enc=? WHERE update_id=? AND owner_key=?",
                           (payload, update_id, owner_key))
                db.execute("DELETE FROM assistant_history_terms WHERE update_id=? AND owner_key=?",
                           (update_id, owner_key))
            else:
                db.execute("INSERT INTO assistant_history(update_id,owner_key,payload_enc,created_at) "
                           "VALUES(?,?,?,?)", (update_id, owner_key, payload, created_at))
            terms = self._history_terms(user_text + " " + answer)
            db.executemany("INSERT OR IGNORE INTO assistant_history_terms(update_id,owner_key,term_hash) VALUES(?,?,?)",
                           [(update_id, owner_key, self.vault.index("assistant-history-term", term)) for term in terms])
            db.commit()

    def append_realtime_voice_history(self, owner_id: int, session_id: str,
                                      turns: list[dict[str, str]]) -> int:
        """Store an explicitly consented live-voice transcript in the encrypted assistant archive."""
        owner_key = self.owner_key(owner_id, self.vault)
        normalized: list[tuple[str, str]] = []
        pending_user: str | None = None
        for item in turns[:16]:
            role = item.get("role")
            text = " ".join(str(item.get("text") or "").split())[:1800]
            if not text:
                continue
            if role == "user":
                if pending_user:
                    normalized.append((pending_user, ""))
                pending_user = text
            elif role == "assistant":
                normalized.append((pending_user or "", text))
                pending_user = None
        if pending_user:
            normalized.append((pending_user, ""))
        if not normalized:
            return 0

        digest = hashlib.sha256((owner_key + "\0" + session_id).encode("utf-8")).digest()
        base_id = -1 - (int.from_bytes(digest[:7], "big") % (1 << 56)) * 32
        created_at = utc_now().isoformat(timespec="microseconds")
        inserted = 0
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            consent = db.execute("SELECT granted FROM consents WHERE owner_key=? AND kind=?",
                                 (owner_key, "realtime_voice_archive")).fetchone()
            if not consent or not consent["granted"]:
                db.rollback()
                return 0
            for index, (user_text, answer) in enumerate(normalized[:16]):
                update_id = base_id + index
                existing = db.execute("SELECT owner_key FROM assistant_history WHERE update_id=?",
                                      (update_id,)).fetchone()
                if existing:
                    if existing["owner_key"] != owner_key:
                        db.rollback()
                        return 0
                    continue
                payload = self.vault.seal_json({"user": user_text, "assistant": answer})
                db.execute("INSERT INTO assistant_history(update_id,owner_key,payload_enc,created_at) "
                           "VALUES(?,?,?,?)", (update_id, owner_key, payload, created_at))
                terms = self._history_terms(user_text + " " + answer)
                db.executemany(
                    "INSERT OR IGNORE INTO assistant_history_terms(update_id,owner_key,term_hash) VALUES(?,?,?)",
                    [(update_id, owner_key, self.vault.index("assistant-history-term", term))
                     for term in terms],
                )
                inserted += 1
            db.commit()
        return inserted

    def _insert_action_log(self, db: sqlite3.Connection, *, owner_key: str, action: str,
                           entity_type: str, entity_id: str | None, payload: dict[str, Any],
                           update_id: int | None = None, now: datetime | None = None) -> None:
        db.execute("INSERT INTO assistant_action_log(id,owner_key,update_id,action,entity_type,entity_id,payload_enc,created_at) "
                   "VALUES(?,?,?,?,?,?,?,?)",
                   (uuid.uuid4().hex, owner_key, update_id, action[:80], entity_type[:40],
                    (entity_id or "")[:80] or None, self.vault.seal_json(payload), iso(now or utc_now())))

    def record_assistant_action(self, *, owner_id: int, action: str, entity_type: str,
                                entity_id: str | None, payload: dict[str, Any],
                                update_id: int | None = None, now: datetime | None = None) -> None:
        with contextlib.closing(self.connect()) as db:
            self._insert_action_log(db, owner_key=self.owner_key(owner_id, self.vault), action=action,
                                    entity_type=entity_type, entity_id=entity_id, payload=payload,
                                    update_id=update_id, now=now)

    def list_assistant_action_log(self, owner_id: int, *, limit: int = 15) -> list[dict[str, Any]]:
        with contextlib.closing(self.connect()) as db:
            rows = db.execute("SELECT action,entity_type,entity_id,payload_enc,created_at FROM assistant_action_log "
                              "WHERE owner_key=? ORDER BY created_at DESC,rowid DESC LIMIT ?",
                              (self.owner_key(owner_id, self.vault), max(1, min(limit, 30)))).fetchall()
        return [{"action": row["action"], "entity_type": row["entity_type"],
                 "entity_id": row["entity_id"], "details": self.vault.open_json(row["payload_enc"]),
                 "created_at": row["created_at"]} for row in rows]

    @staticmethod
    def _alias_key(alias: str) -> str:
        return " ".join(unicodedata.normalize("NFKC", alias).casefold().split())

    def save_chat_alias(self, *, owner_id: int, alias: str, chat_id: int, chat_name: str,
                        username: str | None = None, chat_type: str | None = None,
                        update_id: int | None = None, now: datetime | None = None) -> dict[str, Any]:
        alias = " ".join(alias.split())[:80]
        if not alias or not chat_id or not chat_name:
            raise ValueError("Taxallus va Telegram chat ma’lumoti zarur")
        owner_key = self.owner_key(owner_id, self.vault)
        alias_hash = self.vault.index("chat-alias", self._alias_key(alias))
        now = now or utc_now()
        payload = {"alias": alias, "chat_id": int(chat_id), "chat_name": chat_name[:160],
                   "username": username[:80] if username else None,
                   "chat_type": chat_type[:40] if chat_type else None}
        with contextlib.closing(self.connect()) as db:
            row = db.execute("SELECT created_at FROM chat_aliases WHERE owner_key=? AND alias_hash=?",
                             (owner_key, alias_hash)).fetchone()
            created_at = str(row["created_at"]) if row else iso(now)
            db.execute("INSERT INTO chat_aliases(owner_key,alias_hash,payload_enc,created_at,updated_at) VALUES(?,?,?,?,?) "
                       "ON CONFLICT(owner_key,alias_hash) DO UPDATE SET payload_enc=excluded.payload_enc,updated_at=excluded.updated_at",
                       (owner_key, alias_hash, self.vault.seal_json(payload), created_at, iso(now)))
            db.execute("INSERT INTO assistant_action_log(id,owner_key,update_id,action,entity_type,entity_id,payload_enc,created_at) "
                       "VALUES(?,?,?,?,?,?,?,?)",
                       (uuid.uuid4().hex, owner_key, update_id, "chat_alias_saved", "chat_alias", alias_hash[:16],
                        self.vault.seal_json({"alias": alias, "chat_name": chat_name[:160],
                                              "username": username[:80] if username else None}), iso(now)))
        return payload

    def get_chat_alias(self, owner_id: int, alias: str) -> dict[str, Any] | None:
        alias_hash = self.vault.index("chat-alias", self._alias_key(alias))
        with contextlib.closing(self.connect()) as db:
            row = db.execute("SELECT payload_enc FROM chat_aliases WHERE owner_key=? AND alias_hash=?",
                             (self.owner_key(owner_id, self.vault), alias_hash)).fetchone()
        value = self.vault.open_json(row["payload_enc"]) if row else None
        return value if isinstance(value, dict) else None

    def list_chat_aliases(self, owner_id: int) -> list[dict[str, Any]]:
        with contextlib.closing(self.connect()) as db:
            rows = db.execute("SELECT payload_enc FROM chat_aliases WHERE owner_key=? ORDER BY updated_at DESC",
                              (self.owner_key(owner_id, self.vault),)).fetchall()
        return [self.vault.open_json(row["payload_enc"]) for row in rows]

    def delete_chat_alias(self, owner_id: int, alias: str, *, update_id: int | None = None) -> bool:
        alias_hash = self.vault.index("chat-alias", self._alias_key(alias))
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload_enc FROM chat_aliases WHERE owner_key=? AND alias_hash=?",
                             (self.owner_key(owner_id, self.vault), alias_hash)).fetchone()
            if not row:
                db.rollback()
                return False
            payload = self.vault.open_json(row["payload_enc"])
            db.execute("DELETE FROM chat_aliases WHERE owner_key=? AND alias_hash=?",
                       (self.owner_key(owner_id, self.vault), alias_hash))
            db.execute("INSERT INTO assistant_action_log(id,owner_key,update_id,action,entity_type,entity_id,payload_enc,created_at) "
                       "VALUES(?,?,?,?,?,?,?,?)",
                       (uuid.uuid4().hex, self.owner_key(owner_id, self.vault), update_id,
                        "chat_alias_deleted", "chat_alias", alias_hash[:16],
                        self.vault.seal_json({"alias": payload.get("alias"), "chat_name": payload.get("chat_name")}),
                        iso(utc_now())))
            db.commit()
            return True

    def create_assistant_task(self, *, update_id: int, owner_id: int, title: str,
                              project: str | None = None, due_at: datetime | None = None,
                              priority: str = "normal", assignee: str | None = None,
                              source: str = "telegram",
                              now: datetime | None = None) -> dict[str, Any]:
        return self.create_assistant_tasks(
            update_id=update_id, owner_id=owner_id, source=source, now=now,
            tasks=[{"title": title, "project": project, "due_at": due_at, "priority": priority,
                    "assignee": assignee}],
        )[0]

    def create_assistant_tasks(self, *, update_id: int, owner_id: int, tasks: list[dict[str, Any]],
                               source: str = "telegram", now: datetime | None = None,
                               status: str = "open") -> list[dict[str, Any]]:
        if not 1 <= len(tasks) <= 8:
            raise ValueError("Bir so‘rovda 1 dan 8 tagacha vazifa saqlash mumkin.")
        if status not in {"open", "needs_confirmation"}:
            raise ValueError("Yangi vazifa holati noto‘g‘ri.")
        entries = []
        for index, task in enumerate(tasks):
            title = " ".join(task["title"].split())[:240]
            priority = task["priority"]
            if not title or priority not in {"low", "normal", "high"}:
                raise ValueError("Vazifa nomi yoki ustuvorligi noto‘g‘ri")
            due_at = task.get("due_at")
            source_message_ids = task.get("source_message_ids") or []
            if not isinstance(source_message_ids, list) or any(type(value) is not int or value <= 0
                                                                for value in source_message_ids[:10]):
                raise ValueError("Manba xabar IDlari noto‘g‘ri.")
            custom_key = str(task.get("source_key") or "").strip()[:240]
            entries.append(({"title": title, "project": (task.get("project") or "").strip()[:100] or None,
                             "priority": priority, "source": source[:80], "result": None,
                             "assignee": (str(task.get("assignee") or "").strip()[:100] or None),
                             "source_chat": (str(task.get("source_chat") or "").strip()[:160] or None),
                             "source_message_ids": [int(value) for value in source_message_ids[:10]],
                             "source_location": (" ".join(str(task.get("source_location") or "").split())[:120] or None),
                             "source_evidence": (" ".join(str(task.get("source_evidence") or "").split())[:500] or None),
                             "due_text": (" ".join(str(task.get("due_text") or "").split())[:120] or None),
                             "document_kind": (str(task.get("document_kind") or "")[:30] or None),
                             "reminder_eligible": task.get("reminder_eligible") is True},
                            iso(due_at) if due_at else None,
                            custom_key or (f"telegram:{update_id}" if index == 0 else f"telegram:{update_id}:{index}")))
        owner_key = self.owner_key(owner_id, self.vault)
        timestamp = iso(now or utc_now())
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            created = []
            for payload, due_value, key in entries:
                previous = db.execute("SELECT id,payload_enc,status,due_at,created_at,updated_at FROM assistant_tasks "
                                      "WHERE owner_key=? AND source_key=?", (owner_key, key)).fetchone()
                if previous:
                    if status == "needs_confirmation" and previous["status"] == "cancelled":
                        db.execute("UPDATE assistant_tasks SET payload_enc=?,status='needs_confirmation',due_at=?,updated_at=? "
                                   "WHERE id=? AND owner_key=?",
                                   (self.vault.seal_json(payload), due_value, timestamp, previous["id"], owner_key))
                        created.append({**payload, "id": previous["id"], "status": "needs_confirmation",
                                        "due_at": due_value, "created_at": previous["created_at"],
                                        "updated_at": timestamp, "created": True})
                    else:
                        saved = self.vault.open_json(previous["payload_enc"])
                        created.append({**saved, "id": previous["id"], "status": previous["status"],
                                        "due_at": previous["due_at"], "created_at": previous["created_at"],
                                        "updated_at": previous["updated_at"], "created": False})
                    continue
                task_id = uuid.uuid4().hex[:12]
                db.execute("INSERT INTO assistant_tasks(id,owner_key,source_key,payload_enc,status,due_at,created_at,updated_at) "
                           "VALUES(?,?,?,?,?,?,?,?)",
                           (task_id, owner_key, key, self.vault.seal_json(payload), status, due_value, timestamp, timestamp))
                db.execute("INSERT INTO assistant_action_log(id,owner_key,update_id,action,entity_type,entity_id,payload_enc,created_at) "
                           "VALUES(?,?,?,?,?,?,?,?)",
                           (uuid.uuid4().hex, owner_key, update_id,
                            "task_draft_created" if status == "needs_confirmation" else "task_created",
                            "task", task_id,
                            self.vault.seal_json({"title": payload["title"], "status": status}), timestamp))
                created.append({**payload, "id": task_id, "status": status, "due_at": due_value,
                                "created_at": timestamp, "updated_at": timestamp, "created": True})
            db.commit()
        return created

    def list_assistant_tasks(self, owner_id: int, *, status: str | None = None,
                             query: str | None = None, since: str | None = None,
                             until: str | None = None, limit: int = 20,
                             task_id: str | None = None, journal_only: bool = False) -> list[dict[str, Any]]:
        valid_statuses = {"open", "needs_confirmation", "scheduled", "running", "succeeded", "failed", "cancelled", "unknown"}
        if status not in {None, "all", "active", *valid_statuses}:
            raise ValueError("Noto‘g‘ri vazifa holati")
        owner_key = self.owner_key(owner_id, self.vault)
        sources = ["""SELECT id,payload_enc,status,due_at,created_at,updated_at,
                            'task' AS kind,NULL AS result_category FROM assistant_tasks WHERE owner_key=?"""]
        params: list[Any] = [owner_key]
        if not journal_only:
            sources.extend(["""SELECT r.id,r.payload_enc,
                CASE WHEN r.status='cancelled' AND EXISTS (SELECT 1 FROM assistant_action_log l
                    WHERE l.owner_key=r.owner_key AND l.entity_type='reminder' AND l.entity_id=r.id
                    AND l.action='reminder_completed') THEN 'succeeded'
                    WHEN r.status='pending' THEN 'scheduled' WHEN r.status='sending' THEN 'running'
                    WHEN r.status='sent' THEN 'succeeded' ELSE r.status END,
                r.due_at,r.created_at,COALESCE(o.updated_at,r.sent_at,r.created_at),'reminder',
                CASE WHEN EXISTS (SELECT 1 FROM assistant_action_log l WHERE l.owner_key=r.owner_key
                    AND l.entity_type='reminder' AND l.entity_id=r.id AND l.action='reminder_completed')
                    THEN 'Bajarildi' ELSE o.error_category END
                FROM reminders r LEFT JOIN outbox o ON o.dedupe_key='reminder:' || r.id AND o.owner_key=r.owner_key
                WHERE r.owner_key=?""", """SELECT id,payload_enc,
                CASE status WHEN 'pending' THEN
                    CASE WHEN approval_expires_at<=? THEN 'cancelled' ELSE 'needs_confirmation' END
                    WHEN 'queued' THEN 'scheduled' WHEN 'leased' THEN 'running'
                    WHEN 'dispatching' THEN 'running' WHEN 'sent' THEN 'succeeded'
                    WHEN 'expired' THEN 'cancelled' ELSE status END,
                due_at,created_at,updated_at,'scheduled_message',
                CASE WHEN status='expired' OR (status='pending' AND approval_expires_at<=?)
                    THEN 'expired' ELSE result_category END
                FROM scheduled_profile_messages WHERE owner_key=?"""])
            now = iso(utc_now())
            params.extend((owner_key, now, now, owner_key))
        clauses = []
        if status == "active":
            clauses.append("status IN ('open','needs_confirmation','scheduled','running')")
        elif status and status != "all":
            clauses.append("status=?")
            params.append(status)
        if task_id:
            clauses.append("substr(id,1,length(?))=?")
            params.extend((task_id, task_id))
        if since:
            clauses.append("due_at>=?")
            params.append(since)
        if until:
            clauses.append("due_at<?")
            params.append(until)
        results = []
        query_key = query.casefold().strip() if query else ""
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with contextlib.closing(self.connect()) as db:
            rows = db.execute("SELECT * FROM (" + " UNION ALL ".join(sources) + ")" + where
                              + " ORDER BY COALESCE(due_at,updated_at),created_at DESC,id", params)
            for row in rows:
                if row["kind"] == "reminder":
                    payload = {"title": self.vault.open_text(row["payload_enc"]), "source": "reminder"}
                else:
                    payload = self.vault.open_json(row["payload_enc"])
                    if row["kind"] == "scheduled_message":
                        chat_name = (payload.get("params") or {}).get("chat_name") or "Telegram"
                        payload = {"title": f"{chat_name}ga xabar: {payload.get('body', '')}",
                                   "source": "scheduled_message"}
                if row["kind"] != "task":
                    payload["result"] = row["result_category"] or (
                        "Yuborildi" if row["status"] == "succeeded" else None)
                if query_key and query_key not in " ".join(
                        str(payload.get(k) or "") for k in ("title", "project", "assignee")).casefold():
                    continue
                results.append({**payload, "id": row["id"], "status": row["status"], "due_at": row["due_at"],
                                "kind": row["kind"], "created_at": row["created_at"], "updated_at": row["updated_at"]})
                if len(results) >= max(1, min(limit, 30)):
                    break
        return results

    def set_panel_task_input(self, owner_id: int, prompt_message_id: int, *,
                             action: str, task_id: str | None = None,
                             expires_at: datetime) -> None:
        if type(prompt_message_id) is not int or prompt_message_id <= 0:
            raise ValueError("Panel xabar ID si noto‘g‘ri")
        if action not in {"create", "edit"} or (action == "edit") != bool(task_id):
            raise ValueError("Panel vazifa amali noto‘g‘ri")
        payload = {"prompt_message_id": prompt_message_id, "action": action,
                   "task_id": task_id, "expires_at": iso(expires_at)}
        key = f"{self.owner_key(owner_id, self.vault)}:panel_task_input"
        with contextlib.closing(self.connect()) as db:
            db.execute("INSERT INTO settings(key,value_enc,updated_at) VALUES(?,?,?) "
                       "ON CONFLICT(key) DO UPDATE SET value_enc=excluded.value_enc,updated_at=excluded.updated_at",
                       (key, self.vault.seal_json(payload), iso(utc_now())))

    def get_panel_task_input(self, owner_id: int, prompt_message_id: int) -> dict[str, Any] | None:
        key = f"{self.owner_key(owner_id, self.vault)}:panel_task_input"
        with contextlib.closing(self.connect()) as db:
            row = db.execute("SELECT value_enc FROM settings WHERE key=?", (key,)).fetchone()
            if not row:
                return None
            payload = self.vault.open_json(row["value_enc"])
            expires_at = datetime.fromisoformat(str(payload.get("expires_at") or ""))
            if expires_at <= utc_now():
                db.execute("DELETE FROM settings WHERE key=?", (key,))
                return None
        if payload.get("prompt_message_id") != prompt_message_id:
            return None
        return payload

    def clear_panel_task_input(self, owner_id: int, prompt_message_id: int) -> bool:
        key = f"{self.owner_key(owner_id, self.vault)}:panel_task_input"
        with contextlib.closing(self.connect()) as db:
            row = db.execute("SELECT value_enc FROM settings WHERE key=?", (key,)).fetchone()
            if not row:
                return False
            payload = self.vault.open_json(row["value_enc"])
            if payload.get("prompt_message_id") != prompt_message_id:
                return False
            return db.execute("DELETE FROM settings WHERE key=?", (key,)).rowcount > 0

    def update_assistant_task(self, *, owner_id: int, task_id: str,
                              status: str | None = None, result: str | None = None,
                              assignee: str | None = None, title: str | None = None,
                              due_at: datetime | None = None, update_due: bool = False,
                              update_id: int | None = None) -> dict[str, Any] | None:
        valid_statuses = {"open", "needs_confirmation", "scheduled", "running", "succeeded", "failed", "cancelled", "unknown"}
        if status is not None and status not in valid_statuses:
            raise ValueError("Noto‘g‘ri vazifa holati")
        normalized_title = " ".join(title.split())[:240] if title is not None else None
        if title is not None and not normalized_title:
            raise ValueError("Vazifa nomi bo‘sh bo‘lmasligi kerak")
        due_value = iso(due_at) if update_due and due_at is not None else None
        owner_key = self.owner_key(owner_id, self.vault)
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload_enc,status,due_at,created_at FROM assistant_tasks WHERE id=? AND owner_key=?",
                             (task_id, owner_key)).fetchone()
            if not row:
                db.rollback()
                return None
            next_status = status if status is not None else row["status"]
            if row["status"] in {"succeeded", "cancelled"} and next_status != row["status"]:
                db.rollback()
                raise ValueError("Yakunlangan vazifa holatini qayta ochib bo‘lmaydi")
            if row["status"] == "unknown" and next_status != "unknown":
                db.rollback()
                raise ValueError("Natijasi noma’lum vazifani tekshirmasdan o‘zgartirib bo‘lmaydi")
            if (title is not None or update_due) and row["status"] != "open":
                db.rollback()
                raise ValueError("Faqat ochiq vazifani tahrirlash mumkin")
            payload = self.vault.open_json(row["payload_enc"])
            if result is not None:
                payload["result"] = result[:1000]
            if assignee is not None:
                payload["assignee"] = " ".join(assignee.split())[:100] or None
            if normalized_title is not None:
                payload["title"] = normalized_title
            if update_due:
                due_value = iso(due_at) if due_at is not None else None
            else:
                due_value = row["due_at"]
            now = iso(utc_now())
            db.execute("UPDATE assistant_tasks SET payload_enc=?,status=?,due_at=?,updated_at=? WHERE id=? AND owner_key=?",
                       (self.vault.seal_json(payload), next_status, due_value, now, task_id, owner_key))
            action = "task_assigned" if assignee is not None else (
                "task_edited" if title is not None or update_due else (
                    "task_status_changed" if status is not None else "task_updated"))
            db.execute("INSERT INTO assistant_action_log(id,owner_key,update_id,action,entity_type,entity_id,payload_enc,created_at) "
                       "VALUES(?,?,?,?,?,?,?,?)",
                       (uuid.uuid4().hex, owner_key, update_id, action, "task", task_id,
                        self.vault.seal_json({"title": payload.get("title"), "status": next_status,
                                              "assignee": payload.get("assignee") if assignee is not None else None,
                                              "due_at": due_value,
                                              "result": (result or "")[:200]}), now))
            db.commit()
        return {**payload, "id": task_id, "status": next_status, "due_at": due_value,
                "created_at": row["created_at"], "updated_at": now,
                "assignee_updated": assignee is not None, "status_updated": status is not None,
                "title_updated": title is not None, "due_updated": update_due}

    def resolve_assistant_task_draft(self, *, owner_id: int, task_id: str, approve: bool,
                                     update_id: int | None = None) -> dict[str, Any] | None:
        owner_key = self.owner_key(owner_id, self.vault)
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload_enc,status,due_at,created_at FROM assistant_tasks "
                             "WHERE id=? AND owner_key=?", (task_id, owner_key)).fetchone()
            if not row:
                db.rollback()
                return None
            payload = self.vault.open_json(row["payload_enc"])
            if row["status"] != "needs_confirmation":
                db.rollback()
                return {**payload, "id": task_id, "status": row["status"], "due_at": row["due_at"],
                        "created_at": row["created_at"], "changed": False}
            status = "open" if approve else "cancelled"
            now = iso(utc_now())
            db.execute("UPDATE assistant_tasks SET status=?,updated_at=? WHERE id=? AND owner_key=? "
                       "AND status='needs_confirmation'", (status, now, task_id, owner_key))
            db.execute("INSERT INTO assistant_action_log(id,owner_key,update_id,action,entity_type,entity_id,payload_enc,created_at) "
                       "VALUES(?,?,?,?,?,?,?,?)",
                       (uuid.uuid4().hex, owner_key, update_id,
                        "task_draft_approved" if approve else "task_draft_cancelled", "task", task_id,
                        self.vault.seal_json({"title": payload.get("title"), "status": status}), now))
            db.commit()
        return {**payload, "id": task_id, "status": status, "due_at": row["due_at"],
                "created_at": row["created_at"], "updated_at": now, "changed": True}

    def resolve_document_task_reminder_draft(self, *, owner_id: int, task_id: str,
                                             update_id: int | None = None) -> dict[str, Any] | None:
        owner_key = self.owner_key(owner_id, self.vault)
        now = utc_now()
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload_enc,status,due_at,created_at FROM assistant_tasks "
                             "WHERE id=? AND owner_key=?", (task_id, owner_key)).fetchone()
            if not row:
                db.rollback()
                return None
            payload = self.vault.open_json(row["payload_enc"])
            previous_reminder = str(payload.get("converted_to_reminder") or "")
            if row["status"] != "needs_confirmation":
                db.commit()
                return {**payload, "id": task_id,
                        "status": "reminder_created" if previous_reminder else row["status"],
                        "reminder_id": previous_reminder or None, "changed": False}
            if (payload.get("source") != "document_followup" or payload.get("reminder_eligible") is not True
                    or not row["due_at"]):
                db.rollback()
                return {**payload, "id": task_id, "status": "invalid", "changed": False}
            try:
                due = datetime.fromisoformat(row["due_at"])
            except ValueError:
                db.rollback()
                return {**payload, "id": task_id, "status": "invalid", "changed": False}
            if due.tzinfo is None or due <= now:
                db.rollback()
                return {**payload, "id": task_id, "status": "expired", "changed": False}
            reminder_id = uuid.uuid4().hex[:10]
            db.execute("INSERT INTO reminders VALUES(?,?,?,?,?,?,NULL)",
                       (reminder_id, owner_key, row["due_at"], self.vault.seal(payload["title"]),
                        "pending", iso(now)))
            payload["converted_to_reminder"] = reminder_id
            db.execute("UPDATE assistant_tasks SET payload_enc=?,status='cancelled',updated_at=? "
                       "WHERE id=? AND owner_key=? AND status='needs_confirmation'",
                       (self.vault.seal_json(payload), iso(now), task_id, owner_key))
            log_payload = {"title": payload["title"], "reminder_id": reminder_id,
                           "due_at": row["due_at"], "source_filename": payload.get("source_chat"),
                           "source_location": payload.get("source_location"),
                           "source_evidence": payload.get("source_evidence")}
            db.execute("INSERT INTO assistant_action_log(id,owner_key,update_id,action,entity_type,entity_id,payload_enc,created_at) "
                       "VALUES(?,?,?,?,?,?,?,?)",
                       (uuid.uuid4().hex, owner_key, update_id, "document_task_converted_to_reminder",
                        "reminder", reminder_id, self.vault.seal_json(log_payload), iso(now)))
            db.commit()
        return {**payload, "id": task_id, "status": "reminder_created", "reminder_id": reminder_id,
                "due_at": row["due_at"], "created_at": row["created_at"], "updated_at": iso(now),
                "changed": True}

    def assistant_action_result(self, update_id: int) -> tuple[str, dict[str, Any]] | None:
        with contextlib.closing(self.connect()) as db:
            row = db.execute("SELECT action,result_enc FROM assistant_actions WHERE update_id=?", (update_id,)).fetchone()
        if not row:
            return None
        payload = self.vault.open_json(row["result_enc"])
        return str(row["action"]), payload if isinstance(payload, dict) else {}

    def create_assistant_reminder(self, *, update_id: int, owner_id: int, due_at: datetime,
                                  body: str) -> dict[str, Any]:
        owner_key = self.owner_key(owner_id, self.vault)
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT action,result_enc FROM assistant_actions WHERE update_id=?",
                                  (update_id,)).fetchone()
            if previous:
                result = self.vault.open_json(previous["result_enc"])
                db.commit()
                return result if isinstance(result, dict) else {"created": False}
            reminder_id = uuid.uuid4().hex[:10]
            now = utc_now()
            db.execute("INSERT INTO reminders VALUES(?,?,?,?,?,?,NULL)",
                       (reminder_id, owner_key, iso(due_at), self.vault.seal(body), "pending", iso(now)))
            result = {"id": reminder_id, "due_at": iso(due_at), "body": body}
            db.execute("INSERT INTO assistant_actions VALUES(?,?,?,?,?)",
                       (update_id, owner_key, "create_reminder", self.vault.seal_json(result), iso(now)))
            db.commit()
        return result

    def cancel_assistant_reminder(self, *, update_id: int, owner_id: int, reminder_id: str) -> dict[str, Any]:
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT result_enc FROM assistant_actions WHERE update_id=?", (update_id,)).fetchone()
            if previous:
                result = self.vault.open_json(previous["result_enc"])
                db.commit()
                return result if isinstance(result, dict) else {"cancelled": False}
            cancelled = self._cancel_reminder(db, self.owner_key(owner_id, self.vault), reminder_id)
            result = {"cancelled": cancelled, "id": reminder_id}
            db.execute("INSERT INTO assistant_actions VALUES(?,?,?,?,?)",
                       (update_id, self.owner_key(owner_id, self.vault), "cancel_reminder",
                        self.vault.seal_json(result), iso(utc_now())))
            db.commit()
        return result

    def set_assistant_report_time(self, *, update_id: int, owner_id: int, value: str | None) -> dict[str, Any]:
        key = f"{self.owner_key(owner_id, self.vault)}:report_time"
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT result_enc FROM assistant_actions WHERE update_id=?", (update_id,)).fetchone()
            if previous:
                result = self.vault.open_json(previous["result_enc"])
                db.commit()
                return result if isinstance(result, dict) else {"time": None}
            if value is None:
                db.execute("DELETE FROM settings WHERE key=?", (key,))
            else:
                db.execute("INSERT INTO settings(key,value_enc,updated_at) VALUES(?,?,?) "
                           "ON CONFLICT(key) DO UPDATE SET value_enc=excluded.value_enc,updated_at=excluded.updated_at",
                           (key, self.vault.seal(value), iso(utc_now())))
            result = {"time": value}
            db.execute("INSERT INTO assistant_actions VALUES(?,?,?,?,?)",
                       (update_id, self.owner_key(owner_id, self.vault), "set_report_time",
                        self.vault.seal_json(result), iso(utc_now())))
            db.commit()
        return result

    def queue_daily_report(self, owner_id: int, chat_id: int, local_day: str,
                           now_local_time: str, text: str) -> bool:
        owner_key = self.owner_key(owner_id, self.vault)
        report_time = self.get_report_time(owner_id)
        if not report_time or report_time > now_local_time:
            return False
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            key = f"{owner_key}:report:{local_day}"
            exists = db.execute("SELECT 1 FROM outbox WHERE dedupe_key=?", (key,)).fetchone()
            if exists:
                db.rollback()
                return False
            self._new_outbox(db, dedupe_key=key, owner_id=owner_id, chat_id=chat_id,
                             text=text, due_at=utc_now())
            db.commit()
        return True

    def daily_report_queued(self, owner_id: int, local_day: str) -> bool:
        key = f"{self.owner_key(owner_id, self.vault)}:report:{local_day}"
        with contextlib.closing(self.connect()) as db:
            return db.execute("SELECT 1 FROM outbox WHERE dedupe_key=?", (key,)).fetchone() is not None

    def export_owner_data(self, owner_id: int, *, since: str | None = None,
                          until: str | None = None) -> dict[str, Any]:
        owner_key = self.owner_key(owner_id, self.vault)
        def normalize_bound(value: str | None) -> str | None:
            if value is None:
                return None
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("Eksport sanasi vaqt zonasi bilan ISO ko‘rinishida bo‘lishi kerak.") from exc
            if parsed.tzinfo is None:
                raise ValueError("Eksport sanasida vaqt zonasi bo‘lishi kerak.")
            return iso(parsed)

        since = normalize_bound(since)
        until = normalize_bound(until)
        if since and until and since >= until:
            raise ValueError("Eksport oralig‘i bo‘sh yoki teskari.")

        def time_filter(column: str) -> tuple[str, list[str]]:
            clause = ""
            values: list[str] = []
            if since:
                clause += f" AND {column}>=?"
                values.append(since)
            if until:
                clause += f" AND {column}<?"
                values.append(until)
            return clause, values

        def owner_rows(table: str, columns: str) -> list[sqlite3.Row]:
            date_filter, bounds = time_filter("created_at")
            with contextlib.closing(self.connect()) as db:
                return db.execute(f"SELECT {columns} FROM {table} WHERE owner_key=?{date_filter} ORDER BY created_at",
                                  (owner_key, *bounds)).fetchall()

        conversations = []
        for row in owner_rows("assistant_history", "update_id,payload_enc,created_at"):
            payload = self.vault.open_json(row["payload_enc"])
            conversations.append({"update_id": row["update_id"], "created_at": row["created_at"], **payload})

        tasks = []
        for row in owner_rows("assistant_tasks", "id,payload_enc,status,due_at,created_at,updated_at"):
            payload = self.vault.open_json(row["payload_enc"])
            tasks.append({"id": row["id"], "status": row["status"], "due_at": row["due_at"],
                          "created_at": row["created_at"], "updated_at": row["updated_at"], **payload})
        recurring_reminders = [self._recurring_reminder_result(row)
                               for row in owner_rows("recurring_reminders", "*")]
        personal_records = [{"kind": row["kind"], "day": row["local_day"], "created_at": row["created_at"],
                             "updated_at": row["updated_at"], **self.vault.open_json(row["payload_enc"])}
                            for row in owner_rows("personal_records", "*")]

        aliases = []
        for row in owner_rows("chat_aliases", "payload_enc,created_at,updated_at"):
            aliases.append({**self.vault.open_json(row["payload_enc"]), "created_at": row["created_at"],
                            "updated_at": row["updated_at"]})

        knowledge = []
        for row in owner_rows("assistant_knowledge", "id,payload_enc,kind,created_at,updated_at,review_after"):
            knowledge.append({**self.vault.open_json(row["payload_enc"]), "id": row["id"], "kind": row["kind"],
                              "created_at": row["created_at"], "updated_at": row["updated_at"],
                              "review_after": row["review_after"]})

        knowledge_documents = []
        document_filter, document_bounds = time_filter("v.created_at")
        with contextlib.closing(self.connect()) as db:
            document_rows = db.execute(
                "SELECT d.id,v.payload_enc FROM knowledge_documents d "
                "JOIN knowledge_document_versions v ON v.file_id=d.id AND v.version=d.current_version "
                f"WHERE d.owner_key=?{document_filter} ORDER BY v.created_at,d.id",
                (owner_key, *document_bounds)).fetchall()
            for document_row in document_rows:
                metadata = self.vault.open_json(document_row["payload_enc"])
                chunks = db.execute("SELECT payload_enc FROM knowledge_document_chunks "
                                    "WHERE file_id=? AND version=? ORDER BY chunk_index",
                                    (document_row["id"], metadata["version"])).fetchall()
                knowledge_documents.append({**metadata,
                    "content": [self.vault.open_json(chunk["payload_enc"]) for chunk in chunks]})

        reminders = []
        for row in owner_rows("reminders", "id,due_at,payload_enc,status,created_at,sent_at"):
            reminders.append({"id": row["id"], "due_at": row["due_at"], "text": self.vault.open_text(row["payload_enc"]),
                              "status": row["status"], "created_at": row["created_at"], "sent_at": row["sent_at"]})

        scheduled = []
        for row in owner_rows("scheduled_profile_messages", "id,payload_enc,due_at,status,created_at,updated_at,result_category"):
            scheduled.append({"id": row["id"], "task": self.vault.open_json(row["payload_enc"]),
                              "due_at": row["due_at"], "status": row["status"],
                              "created_at": row["created_at"], "updated_at": row["updated_at"],
                              "result_category": row["result_category"]})

        reminder_drafts = []
        for row in owner_rows("reminder_drafts", "id,chat_enc,payload_enc,status,expires_at,created_at"):
            reminder_drafts.append({"id": row["id"], "chat_id": self.vault.open_text(row["chat_enc"]),
                                    "reminder": self.vault.open_json(row["payload_enc"]),
                                    "status": row["status"], "expires_at": row["expires_at"],
                                    "created_at": row["created_at"]})

        profile_actions = []
        for row in owner_rows("profile_actions", "id,payload_enc,status,created_at,updated_at,result_category"):
            profile_actions.append({"id": row["id"], "action": self.vault.open_json(row["payload_enc"]),
                                    "status": row["status"], "created_at": row["created_at"],
                                    "updated_at": row["updated_at"], "result_category": row["result_category"]})

        reply_drafts = []
        for row in owner_rows("reply_drafts", "id,payload_enc,status,expires_at,created_at,updated_at"):
            reply_drafts.append({"id": row["id"], "draft": self.vault.open_json(row["payload_enc"]),
                                 "status": row["status"], "expires_at": row["expires_at"],
                                 "created_at": row["created_at"], "updated_at": row["updated_at"]})

        telegram_business_drafts = []
        for row in owner_rows("telegram_business_drafts", "id,payload_enc,status,expires_at,created_at,updated_at"):
            telegram_business_drafts.append({"id": row["id"], "draft": self.vault.open_json(row["payload_enc"]),
                "status": row["status"], "expires_at": row["expires_at"],
                "created_at": row["created_at"], "updated_at": row["updated_at"]})

        telegram_call_attempts = []
        for row in owner_rows("telegram_call_attempts",
                              "id,payload_enc,voice_draft_enc,status,expires_at,created_at,updated_at"):
            telegram_call_attempts.append({"id": row["id"],
                "request": self.vault.open_json(row["payload_enc"]),
                "voice_draft": self.vault.open_json(row["voice_draft_enc"])
                    if row["voice_draft_enc"] else None,
                "status": row["status"], "expires_at": row["expires_at"],
                "created_at": row["created_at"], "updated_at": row["updated_at"]})

        outbox = []
        for row in owner_rows("outbox", "id,chat_enc,payload_enc,due_at,status,created_at,updated_at,sent_at,error_category"):
            outbox.append({"id": row["id"], "chat_id": self.vault.open_text(row["chat_enc"]),
                           "text": self.vault.open_text(row["payload_enc"]), "due_at": row["due_at"],
                           "status": row["status"], "created_at": row["created_at"],
                           "updated_at": row["updated_at"], "sent_at": row["sent_at"],
                           "error_category": row["error_category"]})

        action_log = []
        for row in owner_rows("assistant_action_log", "action,entity_type,entity_id,payload_enc,created_at"):
            action_log.append({"action": row["action"], "entity_type": row["entity_type"],
                               "entity_id": row["entity_id"], "details": self.vault.open_json(row["payload_enc"]),
                               "created_at": row["created_at"]})

        tool_actions = []
        for row in owner_rows("assistant_actions", "update_id,action,result_enc,created_at"):
            tool_actions.append({"update_id": row["update_id"], "action": row["action"],
                                 "result": self.vault.open_json(row["result_enc"]),
                                 "created_at": row["created_at"]})

        settings, consents, welcome_rules, welcome_events = {}, {}, [], []
        updated_filter, updated_bounds = time_filter("updated_at")
        event_filter, event_bounds = time_filter("created_at")
        with contextlib.closing(self.connect()) as db:
            for row in db.execute(f"SELECT key,value_enc,updated_at FROM settings WHERE key LIKE ?{updated_filter} ORDER BY updated_at,key",
                                  (f"{owner_key}:%", *updated_bounds)).fetchall():
                settings[str(row["key"]).split(":", 1)[-1]] = {"value": self.vault.open_text(row["value_enc"]),
                                                                "updated_at": row["updated_at"]}
            for row in db.execute(f"SELECT kind,granted,updated_at FROM consents WHERE owner_key=?{updated_filter} ORDER BY updated_at,kind",
                                  (owner_key, *updated_bounds)).fetchall():
                consents[row["kind"]] = {"granted": bool(row["granted"]), "updated_at": row["updated_at"]}
            for row in db.execute(f"SELECT payload_enc,updated_at FROM group_welcome_rules "
                                  f"WHERE owner_key=?{updated_filter} ORDER BY updated_at", (owner_key, *updated_bounds)).fetchall():
                welcome_rules.append({**self.vault.open_json(row["payload_enc"]),
                                      "updated_at": row["updated_at"]})
            for row in db.execute(f"SELECT event_key,payload_enc,status,created_at,updated_at "
                                  f"FROM group_welcome_events WHERE owner_key=?{event_filter} ORDER BY created_at",
                                  (owner_key, *event_bounds)).fetchall():
                welcome_events.append({"event": self.vault.open_json(row["payload_enc"]),
                    "status": row["status"], "created_at": row["created_at"],
                    "updated_at": row["updated_at"]})

        return {"exported_at": iso(utc_now()), "owner_id": owner_id,
                "date_range_utc": {"since": since, "until_exclusive": until},
                "conversations": conversations, "tasks": tasks, "chat_aliases": aliases,
                "recurring_reminders": recurring_reminders,
                "personal_records": personal_records,
                "knowledge": knowledge, "knowledge_documents": knowledge_documents,
                "reminders": reminders, "scheduled_profile_messages": scheduled,
                "reminder_drafts": reminder_drafts, "profile_actions": profile_actions,
                "reply_drafts": reply_drafts, "telegram_business_drafts": telegram_business_drafts,
                "telegram_call_attempts": telegram_call_attempts,
                "outbox": outbox,
                "action_log": action_log, "assistant_action_results": tool_actions,
                "group_welcome_rules": welcome_rules, "group_welcome_events": welcome_events,
                "settings": settings, "consents": consents}

    def recover_interrupted(self) -> None:
        """Never replay operations whose external result might already have happened."""
        now = iso(utc_now())
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            # Older versions marked a reminder as sending before claiming its outbox row.
            db.execute("""UPDATE reminders AS r SET status='pending' WHERE r.status='sending'
                AND EXISTS (SELECT 1 FROM outbox o WHERE o.reminder_id=r.id AND o.owner_key=r.owner_key
                            AND o.status='pending')""")
            db.execute("UPDATE reminders SET status='unknown' WHERE status='sending'")
            db.execute("UPDATE outbox SET status='unknown',updated_at=?,error_category='interrupted' WHERE status='sending'", (now,))
            rows = db.execute("SELECT id,payload_enc FROM profile_actions WHERE status='dispatching'").fetchall()
            for row in rows:
                payload = self.vault.open_json(row["payload_enc"])
                db.execute("UPDATE profile_actions SET status='unknown',updated_at=?,result_category='interrupted' WHERE id=?", (now, row["id"]))
                self._erase_profile_media(db, row["id"])
                schedule = db.execute("SELECT schedule_id FROM profile_actions WHERE id=?", (row["id"],)).fetchone()
                if schedule and schedule["schedule_id"]:
                    db.execute("UPDATE scheduled_profile_messages SET status='unknown',result_category='interrupted',updated_at=? WHERE id=?",
                               (now, schedule["schedule_id"]))
                self._new_outbox(db, dedupe_key=f"profile-result:{row['id']}",
                                 owner_id=int(payload["owner_id"]), chat_id=int(payload["owner_id"]),
                                 text="Telegram xabar natijasini tasdiqlamadi. Xavfsizlik uchun avtomatik qayta yuborilmadi.",
                                 due_at=utc_now())
            expired_leases = db.execute("SELECT schedule_id FROM profile_actions WHERE status='leased' AND lease_until<=? AND expires_at>? AND schedule_id IS NOT NULL",
                                        (now, now)).fetchall()
            db.execute("UPDATE profile_actions SET status='queued',lease_hash=NULL,lease_until=NULL,updated_at=? WHERE status='leased' AND lease_until<=? AND expires_at>?", (now, now, now))
            for expired_lease in expired_leases:
                db.execute("UPDATE scheduled_profile_messages SET status='queued',updated_at=? WHERE id=? AND status='leased'",
                           (now, expired_lease["schedule_id"]))
            expired_actions = db.execute("SELECT schedule_id FROM profile_actions WHERE status IN ('pending','queued','leased') AND expires_at<=? AND schedule_id IS NOT NULL", (now,)).fetchall()
            for expired in expired_actions:
                db.execute("UPDATE scheduled_profile_messages SET status='expired',updated_at=? WHERE id=? AND status IN ('queued','leased')",
                           (now, expired["schedule_id"]))
            db.execute("UPDATE profile_actions SET status='expired',updated_at=? WHERE status IN ('pending','queued','leased') AND expires_at<=?", (now, now))
            db.execute("UPDATE speech_jobs SET status='pending',updated_at=? WHERE status='processing'", (now,))
            db.execute("UPDATE reminder_drafts SET status='expired' WHERE status='pending' AND expires_at<=?", (now,))
            db.execute("UPDATE reply_drafts SET status='expired',updated_at=? "
                       "WHERE status IN ('pending','editing','selected') AND expires_at<=?", (now, now))
            expired_business = db.execute("SELECT id FROM telegram_business_drafts "
                "WHERE status='pending' AND expires_at<=?", (now,)).fetchall()
            for row in expired_business:
                db.execute("UPDATE telegram_business_drafts SET status='expired',payload_enc=?,updated_at=? "
                           "WHERE id=? AND status='pending'",
                           (self.vault.seal_json({}), now, row["id"]))
            db.execute("UPDATE telegram_business_drafts SET status='unknown',payload_enc=?,updated_at=? "
                       "WHERE status='sending'", (self.vault.seal_json({}), now))
            db.execute("UPDATE telegram_call_attempts SET status='unknown',updated_at=? "
                       "WHERE status='calling'", (now,))
            interrupted_integrations = db.execute(
                "SELECT id,owner_key,payload_enc FROM integration_action_drafts WHERE status='executing'").fetchall()
            for row in interrupted_integrations:
                payload = self.vault.open_json(row["payload_enc"])
                db.execute("UPDATE integration_action_drafts SET status='unknown',result_enc=?,updated_at=? "
                           "WHERE id=? AND status='executing'",
                           (self.vault.seal_json({"reason": "interrupted"}), now, row["id"]))
                self._insert_action_log(db, owner_key=str(row["owner_key"]),
                    action="integration_action_interrupted", entity_type="integration_action",
                    entity_id=str(row["id"]), payload={"operation": str(payload.get("operation") or "unknown"),
                                                        "status": "unknown"})
            db.execute("UPDATE integration_action_drafts SET status='expired',payload_enc=? "
                       "WHERE status='pending' AND expires_at<=?",
                       (self.vault.seal_json({}), now))
            db.execute("UPDATE telegram_call_attempts SET status='expired',payload_enc=?,voice_draft_enc=?,updated_at=? "
                       "WHERE status NOT IN ('expired','cancelled') AND expires_at<=?",
                       (self.vault.seal_json({}), self.vault.seal_json({}), now, now))
            db.commit()

    def delete_owner_data(self, owner_id: int) -> None:
        key = self.owner_key(owner_id, self.vault)
        with contextlib.closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM personal_records WHERE owner_key=?", (key,))
            db.execute("DELETE FROM model_usage WHERE owner_key=?", (key,))
            db.execute("DELETE FROM recurring_reminders WHERE owner_key=?", (key,))
            db.execute("DELETE FROM reminders WHERE owner_key=?", (key,))
            db.execute("DELETE FROM reminder_drafts WHERE owner_key=?", (key,))
            db.execute("DELETE FROM speech_jobs WHERE owner_key=?", (key,))
            db.execute("DELETE FROM profile_actions WHERE owner_key=?", (key,))
            db.execute("DELETE FROM reply_drafts WHERE owner_key=?", (key,))
            db.execute("DELETE FROM telegram_business_drafts WHERE owner_key=?", (key,))
            db.execute("DELETE FROM telegram_business_connections WHERE owner_key=?", (key,))
            db.execute("DELETE FROM telegram_call_attempts WHERE owner_key=?", (key,))
            db.execute("DELETE FROM integration_action_drafts WHERE owner_key=?", (key,))
            db.execute("DELETE FROM scheduled_profile_messages WHERE owner_key=?", (key,))
            db.execute("DELETE FROM assistant_knowledge WHERE owner_key=?", (key,))
            db.execute("DELETE FROM assistant_tasks WHERE owner_key=?", (key,))
            db.execute("DELETE FROM chat_aliases WHERE owner_key=?", (key,))
            db.execute("DELETE FROM assistant_action_log WHERE owner_key=?", (key,))
            db.execute("DELETE FROM outbox WHERE owner_key=?", (key,))
            db.execute("DELETE FROM assistant_history WHERE owner_key=?", (key,))
            db.execute("DELETE FROM assistant_actions WHERE owner_key=?", (key,))
            db.execute("DELETE FROM group_welcome_rules WHERE owner_key=?", (key,))
            db.execute("DELETE FROM group_welcome_events WHERE owner_key=?", (key,))
            db.execute("DELETE FROM consents WHERE owner_key=?", (key,))
            db.execute("DELETE FROM settings WHERE key LIKE ?", (f"{key}:%",))
            db.execute("DELETE FROM knowledge_documents WHERE owner_key=?", (key,))
            db.commit()

