from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import unicodedata
import zipfile
from datetime import date, datetime
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo


MAX_UPLOAD_BYTES = 15 * 1024 * 1024
MAX_WORKSPACE_BYTES = 5 * 1024 * 1024 * 1024
MAX_WORKSPACE_FILES = 5_000
MAX_ARCHIVE_FILES = 50
MAX_ARCHIVE_INPUT_BYTES = 50 * 1024 * 1024
MAX_ARCHIVE_OUTPUT_BYTES = 15 * 1024 * 1024


class WorkspaceError(ValueError):
    pass


class Workspace:
    """Owner-only file operations constrained to one configured workspace root."""

    def __init__(self, root: Path | str, timezone: ZoneInfo | None = None):
        requested = Path(root).expanduser()
        if requested.exists() and requested.is_symlink():
            raise WorkspaceError("Ish papkasi symbolic link bo‘lishi mumkin emas.")
        try:
            requested.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.root = requested.resolve(strict=True)
            if not self.root.is_dir():
                raise WorkspaceError("Ish papkasi katalog bo‘lishi kerak.")
            os.chmod(self.root, 0o700)
        except OSError as exc:
            raise WorkspaceError("Ajratilgan ish papkasini tayyorlay olmadim.") from exc
        self.timezone = timezone or ZoneInfo("Asia/Tashkent")

    @staticmethod
    def _clean_name(value: str, *, fallback: str = "fayl", max_length: int = 120) -> str:
        value = unicodedata.normalize("NFKC", str(value).replace("\\", "/").split("/")[-1])
        value = "".join(char for char in value if char.isprintable())
        value = re.sub(r"[^\w(). -]+", "_", value, flags=re.UNICODE).strip(" ._- ")
        return (value[:max_length] or fallback)

    @staticmethod
    def _relative_parts(value: str) -> tuple[str, ...]:
        raw = str(value or "")
        if not raw or "\x00" in raw or "\\" in raw:
            raise WorkspaceError("Fayl yo‘li ish papkasi ichidagi nisbiy manzil bo‘lishi kerak.")
        path = PurePosixPath(raw)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise WorkspaceError("Ish papkasidan tashqaridagi manzilga ruxsat yo‘q.")
        return path.parts

    def _path(self, relative_path: str, *, must_exist: bool = False,
              file_only: bool = False) -> Path:
        parts = self._relative_parts(relative_path)
        path = self.root.joinpath(*parts)
        current = self.root
        for index, part in enumerate(parts):
            current = current / part
            if current.is_symlink():
                raise WorkspaceError("Ish papkasidagi symbolic link orqali fayl ochilmaydi.")
            if index < len(parts) - 1 and current.exists() and not current.is_dir():
                raise WorkspaceError("Yo‘ldagi katalog bo‘lmagan band bor.")
        if path.resolve(strict=False) != self.root and not path.resolve(strict=False).is_relative_to(self.root):
            raise WorkspaceError("Manzil ruxsat etilgan ish papkasidan tashqariga chiqadi.")
        if must_exist and not path.exists():
            raise WorkspaceError("Ko‘rsatilgan fayl ish papkasida topilmadi.")
        if path.exists() and file_only and not path.is_file():
            raise WorkspaceError("Faqat oddiy fayl bilan ishlash mumkin.")
        return path

    def _walk(self):
        count = 0
        for current, directories, filenames in os.walk(self.root, topdown=True, followlinks=False):
            base = Path(current)
            directories[:] = [name for name in directories
                              if not (base / name).is_symlink()]
            for name in filenames:
                path = base / name
                if path.is_symlink() or not path.is_file():
                    continue
                count += 1
                if count > MAX_WORKSPACE_FILES:
                    raise WorkspaceError("Ish papkasida 5 000 dan ortiq fayl bor; qidiruvni toraytiring.")
                yield path

    def _usage(self) -> tuple[int, int]:
        total = 0
        count = 0
        for path in self._walk():
            try:
                total += path.stat().st_size
            except OSError:
                continue
            count += 1
        return total, count

    def clear(self) -> int:
        """Remove every entry in this workspace without following symbolic links."""
        removed = 0

        def remove_entry(path: Path) -> None:
            nonlocal removed
            try:
                if path.is_symlink() or not path.is_dir():
                    path.unlink()
                    removed += 1
                    return
                for child in path.iterdir():
                    remove_entry(child)
                path.rmdir()
            except OSError as exc:
                raise WorkspaceError("Ish papkasidagi barcha fayllarni o‘chira olmadim.") from exc

        try:
            for child in self.root.iterdir():
                remove_entry(child)
        except OSError as exc:
            raise WorkspaceError("Ish papkasidagi barcha fayllarni o‘chira olmadim.") from exc
        return removed

    def save_upload(self, filename: str, content: bytes, *, project: str | None = None) -> dict[str, Any]:
        if not content:
            raise WorkspaceError("Bo‘sh faylni ish papkasiga saqlab bo‘lmaydi.")
        if len(content) > MAX_UPLOAD_BYTES:
            raise WorkspaceError("Saqlanadigan fayl 15 MB dan katta.")
        if project and ("/" in project or "\\" in project or project.strip() in {".", ".."}):
            raise WorkspaceError("Loyiha nomi bitta xavfsiz papka nomi bo‘lishi kerak.")
        folder = self.root
        project_name = None
        if project and project.strip():
            project_name = self._clean_name(project, fallback="loyiha", max_length=80)
            folder = self.root / project_name
            if folder.is_symlink():
                raise WorkspaceError("Loyiha papkasi symbolic link bo‘lishi mumkin emas.")
            folder.mkdir(mode=0o700, exist_ok=True)
            os.chmod(folder, 0o700)
        used_bytes, used_files = self._usage()
        if used_files >= MAX_WORKSPACE_FILES or used_bytes + len(content) > MAX_WORKSPACE_BYTES:
            raise WorkspaceError("Ish papkasi hajm yoki fayl soni chegarasiga yetdi.")
        safe_filename = self._clean_name(filename)
        stem, suffix = Path(safe_filename).stem, Path(safe_filename).suffix[:16]
        target = folder / safe_filename
        suffix_number = 2
        while target.exists() or target.is_symlink():
            target = folder / f"{stem} ({suffix_number}){suffix}"
            suffix_number += 1
            if suffix_number > 10_000:
                raise WorkspaceError("Bir xil nomli fayllar juda ko‘p.")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(target, flags, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            raise WorkspaceError("Fayl shu paytda yaratildi; saqlashni qayta urinib ko‘ring.")
        except OSError as exc:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
            raise WorkspaceError("Faylni ish papkasiga xavfsiz saqlay olmadim.") from exc
        return {"file_path": target.relative_to(self.root).as_posix(),
                "filename": target.name, "project": project_name,
                "size_bytes": len(content), "renamed": target.name != safe_filename}

    def search(self, *, query: str | None = None, project: str | None = None,
               modified_after: str | None = None, modified_before: str | None = None,
               file_type: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        try:
            after = date.fromisoformat(modified_after) if modified_after else None
            before = date.fromisoformat(modified_before) if modified_before else None
        except ValueError as exc:
            raise WorkspaceError("Sana YYYY-MM-DD ko‘rinishida bo‘lishi kerak.") from exc
        if after and before and after > before:
            raise WorkspaceError("Boshlanish sanasi tugash sanasidan keyin bo‘lishi mumkin emas.")
        normalized_query = " ".join(str(query or "").casefold().split())
        normalized_project = self._clean_name(project, fallback="").casefold() if project else ""
        normalized_type = str(file_type or "").casefold().lstrip(".")
        if normalized_type and not re.fullmatch(r"[a-z0-9]{1,12}", normalized_type):
            raise WorkspaceError("Fayl turini kengaytma nomi bilan yozing, masalan pdf yoki xlsx.")
        results = []
        for path in self._walk():
            try:
                stat_result = path.stat(follow_symlinks=False)
            except OSError:
                continue
            relative = path.relative_to(self.root).as_posix()
            parts = PurePosixPath(relative).parts
            project_name = parts[0] if len(parts) > 1 else None
            if normalized_query and normalized_query not in path.name.casefold() and normalized_query not in relative.casefold():
                continue
            if normalized_project and (project_name or "").casefold() != normalized_project:
                continue
            if normalized_type and path.suffix.casefold().lstrip(".") != normalized_type:
                continue
            modified = datetime.fromtimestamp(stat_result.st_mtime, self.timezone)
            if after and modified.date() < after:
                continue
            if before and modified.date() > before:
                continue
            results.append({"file_path": relative, "filename": path.name, "project": project_name,
                            "modified_at": modified.isoformat(timespec="minutes"),
                            "size_bytes": stat_result.st_size,
                            "file_type": path.suffix.casefold().lstrip(".")})
        results.sort(key=lambda item: (item["modified_at"], item["file_path"]), reverse=True)
        return results[:max(1, min(int(limit), 50))]

    def prepare_move(self, file_path: str, *, target_folder: str | None = None) -> dict[str, Any]:
        source = self._path(file_path, must_exist=True, file_only=True)
        source_stat = source.stat(follow_symlinks=False)
        if not stat.S_ISREG(source_stat.st_mode):
            raise WorkspaceError("Faqat oddiy faylni ko‘chirish mumkin.")
        folder = self.root
        if target_folder and target_folder.strip():
            folder = self._path(target_folder.strip())
            if folder.exists() and not folder.is_dir():
                raise WorkspaceError("Yangi manzil katalog bo‘lishi kerak.")
        if folder.resolve(strict=False) == source.parent.resolve(strict=True):
            raise WorkspaceError("Fayl allaqachon shu papkada turibdi.")
        safe_parts = folder.relative_to(self.root).parts if folder != self.root else ()
        safe_folder = self.root.joinpath(*safe_parts)
        original_target = safe_folder / source.name
        target = original_target
        number = 2
        while target.exists() or target.is_symlink():
            target = safe_folder / f"{source.stem} ({number}){source.suffix}"
            number += 1
            if number > 10_000:
                raise WorkspaceError("Yangi papkada bir xil nomli fayllar juda ko‘p.")
        digest = self._hash_file(source)
        return {"source_path": source.relative_to(self.root).as_posix(),
                "target_path": target.relative_to(self.root).as_posix(),
                "source_sha256": digest, "source_size": source_stat.st_size,
                "collision_renamed": target.name != source.name}

    def complete_move(self, plan: dict[str, Any]) -> dict[str, Any]:
        source = self._path(str(plan.get("source_path") or ""), must_exist=True, file_only=True)
        target = self._path(str(plan.get("target_path") or ""))
        if source == target:
            raise WorkspaceError("Faylning manzili o‘zgarmaydi.")
        if target.exists() or target.is_symlink():
            raise WorkspaceError("Ko‘rsatilgan manzil endi band. Yangi ko‘chirish rejasini tayyorlang.")
        if self._hash_file(source) != str(plan.get("source_sha256") or ""):
            raise WorkspaceError("Fayl tasdiqdan keyin o‘zgargan. Yangi ko‘chirish rejasini tayyorlang.")
        parent = target.parent
        relative_parent = parent.relative_to(self.root).parts if parent != self.root else ()
        current = self.root
        for part in relative_parent:
            current = current / part
            if current.is_symlink():
                raise WorkspaceError("Yangi manzildagi symbolic link ruxsat etilmaydi.")
            current.mkdir(mode=0o700, exist_ok=True)
            if not current.is_dir():
                raise WorkspaceError("Yangi manzildagi katalog yaroqsiz.")
            os.chmod(current, 0o700)
        target = self._path(str(plan["target_path"]))
        if target.exists() or target.is_symlink():
            raise WorkspaceError("Yangi manzil band bo‘lib qoldi; fayl ustiga yozilmadi.")
        try:
            with source.open("rb") as source_stream, target.open("xb") as target_stream:
                shutil.copyfileobj(source_stream, target_stream, length=1024 * 1024)
                target_stream.flush()
                os.fsync(target_stream.fileno())
            os.chmod(target, 0o600)
            if self._hash_file(target) != str(plan["source_sha256"]):
                target.unlink(missing_ok=True)
                raise WorkspaceError("Nusxa yaxlitligi tekshiruvdan o‘tmadi; asl fayl saqlandi.")
            if self._hash_file(source) != str(plan["source_sha256"]):
                target.unlink(missing_ok=True)
                raise WorkspaceError("Asl fayl ko‘chirish paytida o‘zgardi; asl nusxa saqlandi.")
            source.unlink()
        except FileExistsError as exc:
            raise WorkspaceError("Yangi manzil band; fayl ustiga yozilmadi.") from exc
        except WorkspaceError:
            raise
        except OSError as exc:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
            raise WorkspaceError("Faylni ko‘chira olmadim; asl nusxaga tegmadim.") from exc
        return {"source_path": str(plan["source_path"]), "target_path": str(plan["target_path"]),
                "size_bytes": int(plan.get("source_size") or target.stat().st_size)}

    def create_archive(self, file_paths: list[str], *, archive_name: str | None = None) -> dict[str, Any]:
        if not isinstance(file_paths, list) or not 1 <= len(file_paths) <= MAX_ARCHIVE_FILES:
            raise WorkspaceError("ZIP uchun 1–50 ta aniq fayl tanlang.")
        if len(set(file_paths)) != len(file_paths):
            raise WorkspaceError("ZIP ro‘yxatida bir xil fayl takrorlangan.")
        selected: list[tuple[str, bytes]] = []
        selected_names: set[str] = set()
        total = 0
        for relative in file_paths:
            path = self._path(relative, must_exist=True, file_only=True)
            archive_path = path.relative_to(self.root).as_posix()
            if archive_path in selected_names:
                raise WorkspaceError("ZIP ro‘yxatida bir xil fayl yo‘li takrorlangan.")
            selected_names.add(archive_path)
            try:
                with path.open("rb") as stream:
                    content = stream.read(MAX_ARCHIVE_INPUT_BYTES - total + 1)
            except OSError as exc:
                raise WorkspaceError("Tanlangan faylni o‘qiy olmadim.") from exc
            total += len(content)
            if total > MAX_ARCHIVE_INPUT_BYTES:
                raise WorkspaceError("Tanlangan fayllar jami 50 MB dan katta.")
            selected.append((archive_path, content))
        name = self._clean_name(archive_name or "fayllar", fallback="fayllar", max_length=100)
        if not name.casefold().endswith(".zip"):
            name += ".zip"
        output = BytesIO()
        try:
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for relative, content in selected:
                    archive.writestr(relative, content)
        except (OSError, zipfile.LargeZipFile) as exc:
            raise WorkspaceError("Tanlangan fayllardan ZIP yarata olmadim.") from exc
        content = output.getvalue()
        if len(content) > MAX_ARCHIVE_OUTPUT_BYTES:
            raise WorkspaceError("Yaratilgan ZIP 15 MB dan katta; fayllar sonini kamaytiring.")
        return {"filename": name, "content": content,
                "files": [relative for relative, _ in selected],
                "size_bytes": len(content)}

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        try:
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as exc:
            raise WorkspaceError("Faylni tekshira olmadim.") from exc
        return digest.hexdigest()


__all__ = ["Workspace", "WorkspaceError", "MAX_UPLOAD_BYTES", "MAX_ARCHIVE_FILES"]
