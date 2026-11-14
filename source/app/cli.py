from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from app.core.commands import parse_message, parse_reminder
from app.core.config import Settings
from app.core.sources import NewsItem
from app.executor.worker import login_and_store
from app.storage.db import Database
from app.storage.security import Vault


def _vault() -> Vault:
    key = Settings.from_env().data_key
    if not key:
        raise SystemExit("DATA_KEY_FILE yoki DATA_KEY kerak")
    return Vault(key)


def demo() -> None:
    with tempfile.TemporaryDirectory(prefix="assistant-demo-") as tmp:
        vault = Vault(Vault.generate_key())
        db = Database(Path(tmp) / "demo.sqlite3", vault)
        now = datetime(2026, 11, 8, 8, 0, tzinfo=UTC)
        local = datetime(2026, 11, 8, 13, 0, tzinfo=Settings.from_env(demo=True).timezone)
        draft = parse_reminder("/eslat ertaga 09:00 | Shartnomani tekshir", local, Settings.from_env(demo=True).timezone)
        reminder_id, _ = db.create_reminder(update_id=1, owner_id=123, chat_id=123,
                                            due_at=draft.due_at, body=draft.body)
        print(f"1) Demo eslatma: {draft.due_at.isoformat()} — {draft.body} (ID {reminder_id})")
        fake_news = NewsItem("Toshkentda ob-havo haqida", "Demo manba", "2026-11-08",
                             "https://example.org/news/1", "Mahalliy")
        print(f"2) Demo hisobot: {fake_news.title} · {fake_news.source} · {fake_news.link}")
        action = db.create_action(owner_id=123, recipient={"alias": "akmal", "display_name": "Akmal",
                                                            "username": "akmal_demo", "user_id": 456},
                                  body="Soat 10 da yetib boraman.", update_id=2)
        assert action is not None
        action_id, approval = action
        db.set_preview_message(action_id, 99)
        preview_result = db.approve_action(approval, 123, 123, 99)
        job = db.claim_action()
        assert preview_result == "queued" and job is not None
        assert db.mark_dispatching(job["job_id"], job["lease_token"])
        assert db.finish_action(job["job_id"], job["lease_token"], "sent", "fake")
        print(f"3) Demo profil amali: preview → approve → fake sent ({job['body']})")
        assert now.year == 2026


def backup(destination: Path) -> None:
    settings = Settings.from_env()
    if not settings.database_path.exists():
        raise SystemExit("Ma’lumotlar bazasi topilmadi")
    db = sqlite3.connect(str(settings.database_path))
    try:
        with tempfile.TemporaryDirectory(prefix="assistant-backup-") as tmp:
            snapshot = Path(tmp) / "snapshot.sqlite3"
            target = sqlite3.connect(str(snapshot))
            try:
                db.backup(target)
            finally:
                target.close()
            encrypted = _vault().seal(snapshot.read_bytes())
    finally:
        db.close()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(encrypted)
    try:
        os.chmod(destination, 0o600)
    except OSError:
        pass
    print(f"Shifrlangan izchil backup yaratildi: {destination}")


def restore(source: Path, target_path: Path) -> None:
    if target_path.exists():
        raise SystemExit("Restore maqsad fayli allaqachon bor; mavjud bazani ustiga yozmayman")
    raw = _vault().open(source.read_bytes())
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="assistant-restore-", dir=target_path.parent) as tmp:
        candidate = Path(tmp) / "restore.sqlite3"
        candidate.write_bytes(raw)
        con = sqlite3.connect(str(candidate))
        try:
            check = con.execute("PRAGMA integrity_check").fetchone()[0]
            if check != "ok":
                raise SystemExit(f"Restore bazasi tekshiruvdan o‘tmadi: {check}")
        finally:
            con.close()
        Database(candidate, _vault()).migrate()
        os.replace(candidate, target_path)
    try:
        os.chmod(target_path, 0o600)
    except OSError:
        pass
    print(f"Backup test bazasiga tiklandi: {target_path}")


def encrypt_contacts(source: Path, destination: Path) -> None:
    contacts = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(contacts, list):
        raise SystemExit("Kontaktlar JSON ro‘yxati bo‘lishi kerak")
    aliases: set[str] = set()
    for contact in contacts:
        alias = str(contact.get("alias", "")).strip().lower()
        username = str(contact.get("username", "")).strip().lstrip("@")
        if not alias or alias in aliases or not username or int(contact.get("user_id", 0)) <= 0:
            raise SystemExit("Har kontaktga takrorlanmagan alias, username va musbat user_id kerak")
        aliases.add(alias)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(_vault().seal(json.dumps(contacts, ensure_ascii=False).encode("utf-8")))
    try:
        os.chmod(destination, 0o600)
    except OSError:
        pass
    print(f"Kontaktlar shifrlanib saqlandi: {destination}")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Shaxsiy Telegram yordamchisi operator CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("demo")
    backup_parser = sub.add_parser("backup")
    backup_parser.add_argument("destination", type=Path)
    restore_parser = sub.add_parser("restore")
    restore_parser.add_argument("source", type=Path)
    restore_parser.add_argument("target", type=Path)
    contacts_parser = sub.add_parser("contacts-encrypt")
    contacts_parser.add_argument("source", type=Path)
    contacts_parser.add_argument("destination", type=Path)
    transcribe_parser = sub.add_parser("transcribe")
    transcribe_parser.add_argument("audio", type=Path)
    sub.add_parser("profile-login")
    sub.add_parser("generate-key")
    args = parser.parse_args()
    if args.command == "demo":
        demo()
    elif args.command == "backup":
        backup(args.destination)
    elif args.command == "restore":
        restore(args.source, args.target)
    elif args.command == "contacts-encrypt":
        encrypt_contacts(args.source, args.destination)
    elif args.command == "profile-login":
        asyncio.run(login_and_store(Settings.from_env()))
    elif args.command == "generate-key":
        print(Vault.generate_key())
    elif args.command == "transcribe":
        from app.speech.service import transcribe_file
        print(transcribe_file(args.audio, Settings.from_env()))


if __name__ == "__main__":
    main()

