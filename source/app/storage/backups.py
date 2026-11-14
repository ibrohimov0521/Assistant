from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.storage.db import Database
from app.storage.security import Vault


class BackupError(ValueError):
    pass


class LocalBackupManager:
    """Encrypted, app-local SQLite backups with isolated restore verification."""

    MAX_DATABASE_BYTES = 256 * 1024 * 1024
    MAX_BACKUPS = 20
    BACKUP_ID = re.compile(r"^\d{8}T\d{6}Z-[a-f0-9]{8}$")

    def __init__(self, database_path: Path | str, vault: Vault):
        if str(database_path) == ":memory:":
            raise BackupError("Xotiradagi vaqtinchalik bazani zaxiralab bo‘lmaydi.")
        self.database_path = Path(database_path)
        self.vault = vault
        self.directory = self.database_path.parent / "backups"

    def _ensure_directory(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.directory.is_symlink() or not self.directory.is_dir():
            raise BackupError("Zaxira papkasi xavfsiz oddiy papka emas.")
        try:
            os.chmod(self.directory, 0o700)
        except OSError:
            pass

    @staticmethod
    def _canonical(value: dict[str, Any]) -> bytes:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")).encode("utf-8")

    def _manifest_path(self, backup_id: str) -> Path:
        self._validate_id(backup_id)
        return self.directory / f"{backup_id}.json"

    def _backup_path(self, backup_id: str) -> Path:
        self._validate_id(backup_id)
        return self.directory / f"{backup_id}.sqlite3.enc"

    @classmethod
    def _validate_id(cls, backup_id: str) -> None:
        if not cls.BACKUP_ID.fullmatch(str(backup_id)):
            raise BackupError("Zaxira ID si noto‘g‘ri.")

    def _write_manifest(self, manifest: dict[str, Any]) -> None:
        unsigned = {key: value for key, value in manifest.items() if key != "manifest_mac"}
        signed = {**unsigned, "manifest_mac": self.vault.index(
            "assistant-local-backup-manifest", self._canonical(unsigned).decode("utf-8"))}
        target = self._manifest_path(str(signed["backup_id"]))
        temp = self.directory / f".{signed['backup_id']}.{os.urandom(4).hex()}.tmp"
        try:
            with temp.open("xb") as stream:
                stream.write(self._canonical(signed))
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.chmod(temp, 0o600)
            except OSError:
                pass
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)

    def _read_manifest(self, backup_id: str) -> dict[str, Any] | None:
        path = self._manifest_path(backup_id)
        if not path.is_file() or path.is_symlink():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict) or value.get("backup_id") != backup_id:
            return None
        mac = value.get("manifest_mac")
        unsigned = {key: item for key, item in value.items() if key != "manifest_mac"}
        expected = self.vault.index("assistant-local-backup-manifest",
                                    self._canonical(unsigned).decode("utf-8"))
        return value if isinstance(mac, str) and mac == expected else None

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def create(self) -> dict[str, Any]:
        self._ensure_directory()
        if not self.database_path.is_file():
            raise BackupError("Ma’lumotlar bazasi topilmadi.")
        existing = [path for path in self.directory.glob("*.sqlite3.enc")
                    if path.is_file() and not path.is_symlink()]
        if len(existing) >= self.MAX_BACKUPS:
            raise BackupError(f"Zaxira limiti ({self.MAX_BACKUPS}) to‘ldi; hech biri avtomatik o‘chirilmaydi.")

        created_at = datetime.now(UTC).replace(microsecond=0).isoformat()
        backup_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + f"-{os.urandom(4).hex()}"
        # Keep the plaintext SQLite snapshot on the OS temporary filesystem,
        # separate from the persistent encrypted-backup directory.
        with tempfile.TemporaryDirectory(prefix="assistant-backup-") as temp_dir:
            snapshot = Path(temp_dir) / "snapshot.sqlite3"
            source = sqlite3.connect(str(self.database_path), timeout=15)
            target = sqlite3.connect(str(snapshot), timeout=15)
            try:
                source.backup(target)
            finally:
                target.close()
                source.close()
            plain_size = snapshot.stat().st_size
            if plain_size > self.MAX_DATABASE_BYTES:
                raise BackupError("Ma’lumotlar bazasi 256 MiB chegaradan katta; zaxira yaratilmadi.")
            check_db = sqlite3.connect(str(snapshot))
            try:
                integrity = check_db.execute("PRAGMA integrity_check").fetchone()[0]
                schema = check_db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            except sqlite3.DatabaseError as exc:
                raise BackupError("Yangi zaxira ichidagi baza tuzilishini tekshirib bo‘lmadi.") from exc
            finally:
                check_db.close()
            if integrity != "ok":
                raise BackupError(f"Yangi zaxira yaxlitlik tekshiruvidan o‘tmadi: {integrity}")
            encrypted = self.vault.seal(snapshot.read_bytes())

        backup_path = self._backup_path(backup_id)
        tmp_path = self.directory / f".{backup_id}.{os.urandom(4).hex()}.tmp"
        try:
            with tmp_path.open("xb") as stream:
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.chmod(tmp_path, 0o600)
            except OSError:
                pass
            os.replace(tmp_path, backup_path)
        finally:
            tmp_path.unlink(missing_ok=True)

        manifest = {
            "backup_id": backup_id,
            "created_at": created_at,
            "file_name": backup_path.name,
            "file_size_bytes": backup_path.stat().st_size,
            "file_sha256": hashlib.sha256(encrypted).hexdigest(),
            "database_size_bytes": plain_size,
            "schema_version": int(schema or 0),
            "integrity_check": "ok",
            "restore_test": "not_run",
            "restore_test_at": None,
            "restore_test_integrity": None,
            "restore_test_schema_version": None,
        }
        self._write_manifest(manifest)
        return self._entry(backup_id, manifest)

    def list_backups(self) -> dict[str, Any]:
        self._ensure_directory()
        paths = sorted((path for path in self.directory.glob("*.sqlite3.enc")
                        if path.is_file() and not path.is_symlink()), reverse=True)
        entries = []
        for path in paths[: self.MAX_BACKUPS]:
            backup_id = path.name.removesuffix(".sqlite3.enc")
            if not self.BACKUP_ID.fullmatch(backup_id):
                continue
            entries.append(self._entry(backup_id, self._read_manifest(backup_id)))
        return {"count": len(paths), "backups": entries,
                "directory": str(self.directory), "retention_limit": self.MAX_BACKUPS}

    def _entry(self, backup_id: str, manifest: dict[str, Any] | None) -> dict[str, Any]:
        path = self._backup_path(backup_id)
        present = path.is_file() and not path.is_symlink()
        current_size = path.stat().st_size if present else None
        if not present:
            file_status = "missing"
        elif manifest is None:
            file_status = "metadata_missing_or_invalid"
        else:
            try:
                actual_hash = self._sha256_file(path)
            except OSError:
                actual_hash = ""
            file_status = "intact" if actual_hash == manifest.get("file_sha256") else "changed"
        return {
            "backup_id": backup_id,
            "created_at": manifest.get("created_at") if manifest else None,
            "location": str(path),
            "file_exists": present,
            "file_size_bytes": current_size,
            "file_status": file_status,
            "restore_test": manifest.get("restore_test", "unknown") if manifest else "unknown",
            "restore_test_at": manifest.get("restore_test_at") if manifest else None,
            "restore_test_integrity": manifest.get("restore_test_integrity") if manifest else None,
            "restore_test_schema_version": manifest.get("restore_test_schema_version") if manifest else None,
        }

    def verify_restore(self, backup_id: str) -> dict[str, Any]:
        self._ensure_directory()
        self._validate_id(backup_id)
        path = self._backup_path(backup_id)
        manifest = self._read_manifest(backup_id)
        result: dict[str, Any] = {
            "backup_id": backup_id,
            "file_exists": path.is_file() and not path.is_symlink(),
            "file_status": "missing",
            "restore_test": "failed",
            "restore_test_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "restore_test_integrity": None,
            "restore_test_schema_version": None,
        }
        if not result["file_exists"]:
            error = "Zaxira fayli topilmadi."
        elif manifest is None:
            result["file_status"] = "metadata_missing_or_invalid"
            error = "Zaxira metama’lumoti yo‘q yoki buzilgan; xavfsiz tiklash sinovi o‘tkazilmadi."
        else:
            try:
                encrypted = path.read_bytes()
                result["file_status"] = ("intact" if hashlib.sha256(encrypted).hexdigest()
                                         == manifest.get("file_sha256") else "changed")
                if result["file_status"] != "intact":
                    raise BackupError("Zaxira fayli yaratilgandan keyin o‘zgargan; tiklash sinovi to‘xtatildi.")
                raw = self.vault.open(encrypted)
                if len(raw) > self.MAX_DATABASE_BYTES:
                    raise BackupError("Ochilgan zaxira 256 MiB chegaradan katta.")
                with tempfile.TemporaryDirectory(prefix="assistant-restore-test-") as temp_dir:
                    candidate = Path(temp_dir) / "restore-test.sqlite3"
                    candidate.write_bytes(raw)
                    try:
                        os.chmod(candidate, 0o600)
                    except OSError:
                        pass
                    check_db = sqlite3.connect(str(candidate))
                    try:
                        integrity = check_db.execute("PRAGMA integrity_check").fetchone()[0]
                        if integrity != "ok":
                            raise BackupError(f"Ajratilgan tiklash bazasi yaxlit emas: {integrity}")
                    finally:
                        check_db.close()
                    # Migrate only the disposable candidate, never the live database.
                    Database(candidate, self.vault).migrate()
                    check_db = sqlite3.connect(str(candidate))
                    try:
                        schema = check_db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
                        integrity = check_db.execute("PRAGMA integrity_check").fetchone()[0]
                    finally:
                        check_db.close()
                    if integrity != "ok":
                        raise BackupError(f"Migratsiyadan keyingi test bazasi yaxlit emas: {integrity}")
                result.update(restore_test="passed", restore_test_integrity="ok",
                              restore_test_schema_version=int(schema or 0))
                error = None
            except Exception as exc:
                error = f"Tiklash sinovi o‘tmadi: {type(exc).__name__}."
        result["error"] = error
        if manifest is not None:
            manifest.update({key: result[key] for key in (
                "restore_test", "restore_test_at", "restore_test_integrity",
                "restore_test_schema_version")})
            self._write_manifest(manifest)
        return result


__all__ = ["BackupError", "LocalBackupManager"]
