from __future__ import annotations

import asyncio
import hmac
import os
import re
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query

from app.core.config import read_secret

_DOCKER_SOCKET = "/var/run/docker.sock"
_DOCKER_API_PREFIX = "v1.41"
_DOCKER_PROXY_URL = "http://docker-proxy:8091"
_MAX_LOG_RESPONSE_BYTES = 512 * 1024
_MAX_LOG_LINES = 80
_MAX_LOG_CHARS = 24_000
_MAX_STORAGE_SCAN_ENTRIES = 50_000
_SECRET_VALUE = re.compile(
    r"(?i)([\"']?(?:bot[_-]?token|token|api[_-]?key|openai[_-]?key|password|secret|"
    r"authorization|cookie|session|code)[\"']?\s*[:=]\s*)"
    r"(?:\"(?:\\.|[^\"])*\"|'(?:\\.|[^'])*'|[^\s,;]+)"
)
_BOT_TOKEN = re.compile(r"(?<!\w)\d{6,12}:[A-Za-z0-9_-]{20,}(?!\w)")
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE = re.compile(r"(?<!\w)\+?\d[\d .()\-]{7,}\d(?!\w)")
_ERROR = re.compile(r"(?i)\b(error|exception|failed|failure|fatal|critical|traceback)\b")
_WARNING = re.compile(r"(?i)\b(warn(?:ing)?|deprecated|retrying)\b")
_VIRTUAL_INTERFACE = re.compile(r"^(?:lo|docker\d*|br-[0-9a-f]+|veth|virbr\d*|tun\d*|tap\d*|wg\d*)$")


class DockerAPIError(RuntimeError):
    """Sanitized failure communicating with the restricted Docker proxy."""


def _parse_cpu(proc_root: Path) -> tuple[int, int]:
    first = (proc_root / "stat").read_text(encoding="ascii").splitlines()[0].split()
    if not first or first[0] != "cpu" or len(first) < 5:
        raise ValueError("CPU counters unavailable")
    values = [int(value) for value in first[1:]]
    return sum(values), values[3] + (values[4] if len(values) > 4 else 0)


def _parse_meminfo(proc_root: Path) -> dict[str, int | float]:
    values: dict[str, int] = {}
    for line in (proc_root / "meminfo").read_text(encoding="ascii").splitlines():
        key, _, rest = line.partition(":")
        match = re.match(r"\s*(\d+)\s+kB", rest)
        if match:
            values[key] = int(match.group(1)) * 1024
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable")
    if available is None:
        available = sum(values.get(key, 0) for key in ("MemFree", "Buffers", "Cached"))
    if total <= 0:
        raise ValueError("Memory counters unavailable")
    used = max(0, total - available)
    return {"total_bytes": total, "available_bytes": available, "used_bytes": used,
            "used_percent": round(used * 100 / total, 1)}


def _parse_network(proc_root: Path) -> dict[str, tuple[int, int]]:
    result: dict[str, tuple[int, int]] = {}
    for line in (proc_root / "net" / "dev").read_text(encoding="ascii").splitlines()[2:]:
        interface, separator, counters = line.partition(":")
        name = interface.strip()
        if not separator or not name or _VIRTUAL_INTERFACE.match(name):
            continue
        fields = counters.split()
        if len(fields) < 9:
            continue
        try:
            result[name] = (int(fields[0]), int(fields[8]))
        except ValueError:
            continue
    return result


def read_host_metrics(*, proc_root: Path = Path("/host/proc"),
                      storage_path: Path = Path("/host/storage"),
                      host_root_path: Path = Path("/host/root"),
                      sample_seconds: float = 0.2) -> dict[str, Any]:
    """Read host counters from read-only procfs and storage mounts."""
    measured_at = datetime.now(UTC).isoformat(timespec="seconds")
    cpu_before = _parse_cpu(proc_root)
    network_before = _parse_network(proc_root)
    elapsed = max(0.05, min(float(sample_seconds), 0.5))
    time.sleep(elapsed)
    cpu_after = _parse_cpu(proc_root)
    network_after = _parse_network(proc_root)
    delta_total = max(0, cpu_after[0] - cpu_before[0])
    delta_idle = max(0, cpu_after[1] - cpu_before[1])
    cpu_percent = round(max(0, delta_total - delta_idle) * 100 / delta_total, 1) if delta_total else 0.0
    memory = _parse_meminfo(proc_root)
    try:
        load_values = [float(item) for item in (proc_root / "loadavg").read_text(encoding="ascii").split()[:3]]
    except (ValueError, OSError):
        load_values = []
    disk = shutil.disk_usage(storage_path)
    try:
        root_disk = shutil.disk_usage(host_root_path)
        root_filesystem = {"path": "/", "total_bytes": root_disk.total, "used_bytes": root_disk.used,
                           "free_bytes": root_disk.free,
                           "used_percent": round(root_disk.used * 100 / root_disk.total, 1)
                           if root_disk.total else 0.0}
    except OSError:
        root_filesystem = None
    interfaces = []
    for name, (received, sent) in network_after.items():
        old_received, old_sent = network_before.get(name, (received, sent))
        interfaces.append({
            "name": name,
            "received_bytes_per_second": round(max(0, received - old_received) / elapsed, 1),
            "sent_bytes_per_second": round(max(0, sent - old_sent) / elapsed, 1),
            "received_total_bytes": received, "sent_total_bytes": sent,
        })
    interfaces.sort(key=lambda item: item["received_bytes_per_second"] + item["sent_bytes_per_second"], reverse=True)
    cpu_lines = (proc_root / "stat").read_text(encoding="ascii").splitlines()
    return {
        "measured_at": measured_at,
        "cpu": {"used_percent": cpu_percent,
                "logical_cores": sum(1 for line in cpu_lines if re.match(r"^cpu\d+\s", line))},
        "memory": memory,
        "load_average_1_5_15": load_values,
        "storage": {"path": str(storage_path), "total_bytes": disk.total, "used_bytes": disk.used,
                    "free_bytes": disk.free,
                    "used_percent": round(disk.used * 100 / disk.total, 1) if disk.total else 0.0},
        "root_filesystem": root_filesystem,
        "network": interfaces[:5],
    }


def scan_storage_directories(*, storage_path: Path = Path("/host/storage"),
                             limit: int = 20, max_entries: int = _MAX_STORAGE_SCAN_ENTRIES,
                             max_depth: int = 8) -> dict[str, Any]:
    """Return bounded directory-size estimates without following symlinks or changing files."""
    if not 1 <= limit <= 50 or not 100 <= max_entries <= _MAX_STORAGE_SCAN_ENTRIES or not 1 <= max_depth <= 12:
        raise ValueError("Storage scan limits are outside the allowed range.")
    root = storage_path.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Storage path is not a directory.")
    stack: list[tuple[Path, int]] = [(root, 0)]
    totals: dict[Path, int] = {root: 0}
    scanned = 0
    inaccessible = 0
    truncated = False
    depth_limited = False
    while stack:
        directory, depth = stack.pop()
        try:
            entries = os.scandir(directory)
        except OSError:
            inaccessible += 1
            continue
        with entries:
            for entry in entries:
                if scanned >= max_entries:
                    truncated = True
                    break
                scanned += 1
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        child = Path(entry.path)
                        totals.setdefault(child, 0)
                        if depth < max_depth:
                            stack.append((child, depth + 1))
                        else:
                            depth_limited = True
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    size = max(0, entry.stat(follow_symlinks=False).st_size)
                    parent = Path(entry.path).parent
                    while parent == root or root in parent.parents:
                        totals[parent] = totals.get(parent, 0) + size
                        if parent == root:
                            break
                        parent = parent.parent
                except OSError:
                    inaccessible += 1
        if truncated:
            break
    rows = sorted(
        ((path.relative_to(root).as_posix() or ".", size) for path, size in totals.items() if path != root),
        key=lambda item: (-item[1], item[0].casefold()),
    )[:limit]
    return {
        "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "root": str(root), "scanned_entries": scanned,
        "max_entries": max_entries, "max_depth": max_depth,
        "truncated": truncated or depth_limited, "depth_limited": depth_limited,
        "inaccessible_entries": inaccessible,
        "directories": [{"path": path, "size_bytes": size} for path, size in rows],
        "read_only": True,
    }


def decode_docker_logs(payload: bytes) -> bytes:
    """Decode Docker's stdout/stderr multiplex stream; accept TTY raw output too."""
    if not payload:
        return b""
    chunks: list[bytes] = []
    offset = 0
    while offset < len(payload):
        if len(payload) - offset < 8:
            return payload
        header = payload[offset:offset + 8]
        if header[0] not in {1, 2} or header[1:4] != b"\x00\x00\x00":
            return payload
        end = offset + 8 + int.from_bytes(header[4:8], "big")
        if end > len(payload):
            return payload
        chunks.append(payload[offset + 8:end])
        offset = end
    return b"".join(chunks)


def redact_log_line(line: str) -> str:
    value = _SECRET_VALUE.sub(r"\1[REDACTED]", line)
    value = _BOT_TOKEN.sub("[REDACTED_TELEGRAM_TOKEN]", value)
    value = _BEARER.sub("Bearer [REDACTED]", value)
    value = _EMAIL.sub("[REDACTED_EMAIL]", value)
    return _PHONE.sub("[REDACTED_PHONE]", value)[:2000]


class DockerReadOnly:
    """Read-only view over the separately isolated Docker API proxy."""

    def __init__(self, *, allowed_projects: set[str] | None = None,
                 transport: httpx.AsyncBaseTransport | None = None,
                 proxy_url: str | None = None, proxy_token: str | None = None):
        self.allowed_projects = {item.strip() for item in (allowed_projects or set()) if item.strip()}
        resolved_proxy_url = (proxy_url or os.getenv("DOCKER_PROXY_URL") or _DOCKER_PROXY_URL).strip()
        self.client = httpx.AsyncClient(
            base_url=resolved_proxy_url.rstrip("/") + f"/{_DOCKER_API_PREFIX}/", transport=transport,
            timeout=httpx.Timeout(8.0, connect=2.0), follow_redirects=False, trust_env=False,
            headers={"Authorization": f"Bearer {proxy_token}"} if proxy_token else {},
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def _get_json(self, endpoint: str, *, params: dict[str, str] | None = None) -> Any:
        try:
            response = await self.client.get(endpoint, params=params)
            response.raise_for_status()
            if len(response.content) > 2_000_000:
                raise DockerAPIError("Docker javobi belgilangan hajmdan katta.")
            return response.json()
        except DockerAPIError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise DockerAPIError("Docker ma’lumotini olish muvaffaqiyatsiz bo‘ldi.") from exc

    async def containers(self) -> dict[str, Any]:
        raw = await self._get_json("containers/json", params={"all": "1"})
        if not isinstance(raw, list):
            raise DockerAPIError("Docker konteyner ro‘yxati noto‘g‘ri formatda.")
        items = []
        counts = {"running": 0, "exited": 0, "other": 0}
        for entry in raw[:1000]:
            if not isinstance(entry, dict):
                continue
            labels = entry.get("Labels") or {}
            labels = labels if isinstance(labels, dict) else {}
            state = str(entry.get("State") or "unknown").lower()[:30]
            counts["running" if state == "running" else "exited" if state == "exited" else "other"] += 1
            names = entry.get("Names") or []
            name = next((str(item).lstrip("/") for item in names if item), "unknown")[:160]
            status = str(entry.get("Status") or state)[:160]
            health = re.search(r"\((healthy|unhealthy|starting)\)", status, re.I)
            try:
                created = datetime.fromtimestamp(int(entry.get("Created") or 0), UTC).isoformat() if entry.get("Created") else None
            except (TypeError, ValueError, OSError):
                created = None
            items.append({
                "name": name,
                "service": str(labels.get("com.docker.compose.service") or "")[:80] or None,
                "project": str(labels.get("com.docker.compose.project") or "")[:100] or None,
                "image": str(entry.get("Image") or "")[:180], "state": state,
                "health": health.group(1).lower() if health else None,
                "status": status, "created_at": created,
            })
        return {"measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "total": len(raw), "truncated": len(raw) > len(items), "counts": counts,
                "containers": items}

    async def cleanup_candidates(self, *, limit: int = 50) -> dict[str, Any]:
        """List Docker resources with a confirmed zero-reference count; never mutate them."""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise DockerAPIError("Docker nomzodlari chegarasi 1–1000 oralig‘ida bo‘lishi kerak.")
        raw_images = await self._get_json("images/json", params={"all": "1"})
        raw_volumes = await self._get_json("volumes")
        if not isinstance(raw_images, list) or not isinstance(raw_volumes, dict):
            raise DockerAPIError("Docker image yoki volume javobi noto‘g‘ri formatda.")
        image_rows: list[dict[str, Any]] = []
        unknown_images = 0
        for item in raw_images[:1000]:
            if not isinstance(item, dict):
                continue
            references = item.get("container_references")
            if type(references) is not int:
                unknown_images += 1
                continue
            if references != 0:
                continue
            image_id = str(item.get("id") or "")[:96]
            if not re.fullmatch(r"sha256:[a-fA-F0-9]{64}", image_id):
                continue
            tags = item.get("repo_tags") or []
            tags = [str(tag)[:180] for tag in tags[:20] if isinstance(tag, str)] if isinstance(tags, list) else []
            size = item.get("size_bytes")
            image_rows.append({
                "id": image_id,
                "repo_tags": tags,
                "estimated_size_bytes": max(0, min(int(size), 2**63 - 1))
                if type(size) is int and size >= 0 else None,
                "reference_count": 0,
                "requires_owner_review": True,
            })

        volume_values = raw_volumes.get("Volumes")
        if not isinstance(volume_values, list):
            raise DockerAPIError("Docker volume ro‘yxati noto‘g‘ri formatda.")
        volume_rows: list[dict[str, Any]] = []
        unknown_volumes = 0
        for item in volume_values[:1000]:
            if not isinstance(item, dict):
                continue
            references = item.get("container_references")
            if type(references) is not int:
                unknown_volumes += 1
                continue
            if references != 0:
                continue
            name = str(item.get("name") or "")[:180]
            if (not name or len(name) > 96
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,179}", name)):
                continue
            size = item.get("size_bytes")
            volume_rows.append({
                "name": name,
                "driver": str(item.get("driver") or "")[:40],
                "project": str(item.get("project") or "")[:100] or None,
                "service": str(item.get("service") or "")[:80] or None,
                "estimated_size_bytes": max(0, min(int(size), 2**63 - 1))
                if type(size) is int and size >= 0 else None,
                "reference_count": 0,
                "requires_owner_review": True,
            })
        image_rows.sort(key=lambda row: (row["estimated_size_bytes"] is None,
                                         -(row["estimated_size_bytes"] or 0), row["id"]))
        volume_rows.sort(key=lambda row: (row["estimated_size_bytes"] is None,
                                          -(row["estimated_size_bytes"] or 0), row["name"]))
        return {
            "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "read_only": True,
            "image_size_estimates_may_share_layers": True,
            "images": image_rows[:limit],
            "volumes": volume_rows[:limit],
            "total_volume_candidates": len(volume_rows),
            "unknown_reference_counts": {"images": unknown_images, "volumes": unknown_volumes},
            "truncated": len(image_rows) > limit or len(volume_rows) > limit
                or len(raw_images) > 1000 or len(volume_values) > 1000,
            "deletion_performed": False,
        }

    async def delete_unused_volumes(self, names: list[str]) -> dict[str, Any]:
        if not isinstance(names, list) or not 1 <= len(names) <= 8:
            raise DockerAPIError("Faqat 1–8 ta aniq Docker volume nomini ko‘rsatish mumkin.")
        if any(not isinstance(name, str) or len(name) > 96
               or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,179}", name)
               for name in names) or len(set(names)) != len(names):
            raise DockerAPIError("Docker volume ro‘yxatida noto‘g‘ri yoki takror nom bor.")
        candidates = await self.cleanup_candidates(limit=1000)
        current = {str(row.get("name")): row for row in candidates.get("volumes", [])
                   if isinstance(row, dict) and row.get("reference_count") == 0}
        missing = sorted(set(names) - set(current))
        if missing:
            raise DockerAPIError("Tasdiqlangan volume endi nol havolali nomzodlar ro‘yxatida yo‘q.")
        removed: list[dict[str, Any]] = []
        for name in names:
            try:
                response = await self.client.delete(f"volumes/{name}")
                response.raise_for_status()
                item = response.json()
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                if code in {404, 409}:
                    return {"status": "partial" if removed else "failed", "deleted": removed,
                            "remaining": names[len(removed):], "failed_volume": name,
                            "error": "Volume topilmadi yoki Docker uni ishlatilayotgan deb rad etdi; qolganlari bajarilmadi."}
                return {"status": "unknown", "deleted": removed, "remaining": names[len(removed):],
                        "uncertain_volume": name,
                        "error": "Docker javobi noaniq; takroriy o‘chirish yuborilmadi."}
            except (httpx.HTTPError, ValueError):
                return {"status": "unknown", "deleted": removed, "remaining": names[len(removed):],
                        "uncertain_volume": name,
                        "error": "Docker javobi yo‘qoldi yoki noto‘g‘ri; takroriy o‘chirish yuborilmadi."}
            if not isinstance(item, dict) or item.get("deleted") is not True or item.get("name") != name:
                return {"status": "unknown", "deleted": removed, "remaining": names[len(removed):],
                        "uncertain_volume": name,
                        "error": "Docker volume o‘chganini tekshirib bo‘lmadi; takroriy o‘chirish yuborilmadi."}
            removed.append(item)
        return {"status": "succeeded", "deleted": removed, "remaining": []}

    async def logs(self, *, service: str, minutes: int, level: str) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", service):
            raise HTTPException(status_code=400, detail="Xizmat nomi noto‘g‘ri.")
        if not self.allowed_projects:
            raise HTTPException(status_code=503, detail="Log ko‘rish uchun ruxsatli loyiha sozlanmagan.")
        containers = await self._get_json("containers/json", params={"all": "1"})
        matches: list[tuple[str, str]] = []
        if isinstance(containers, list):
            for item in containers:
                if not isinstance(item, dict):
                    continue
                labels = item.get("Labels") or {}
                if (labels.get("com.docker.compose.project") in self.allowed_projects
                        and str(labels.get("com.docker.compose.service") or "").casefold() == service.casefold()):
                    matches.append((str(item.get("Id") or ""), str(labels.get("com.docker.compose.project"))))
        if len(matches) != 1:
            allowed = sorted({str(item.get("Labels", {}).get("com.docker.compose.service"))
                              for item in containers if isinstance(item, dict)
                              and item.get("Labels", {}).get("com.docker.compose.project") in self.allowed_projects
                              and item.get("Labels", {}).get("com.docker.compose.service")}) if isinstance(containers, list) else []
            message = ("Xizmat topilmadi. Ruxsatli xizmatlar: " + ", ".join(allowed[:20])
                       if allowed else "Ruxsatli loyiha xizmatlari topilmadi.")
            raise HTTPException(status_code=404, detail=message)
        container_id, project = matches[0]
        if not re.fullmatch(r"[a-fA-F0-9]{12,64}", container_id):
            raise DockerAPIError("Docker konteyner identifikatori noto‘g‘ri.")
        params = {"stdout": "1", "stderr": "1", "timestamps": "1",
                  "since": str(int(time.time()) - minutes * 60), "tail": "500"}
        try:
            async with self.client.stream("GET", f"containers/{container_id}/logs", params=params) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    remaining = _MAX_LOG_RESPONSE_BYTES - len(body)
                    if remaining <= 0:
                        break
                    body.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        break
        except httpx.HTTPError as exc:
            raise DockerAPIError("Docker loglarini olish muvaffaqiyatsiz bo‘ldi.") from exc
        raw_lines = decode_docker_logs(bytes(body)).decode("utf-8", errors="replace").splitlines()
        if level == "errors":
            raw_lines = [line for line in raw_lines if _ERROR.search(line)]
        elif level == "warnings":
            raw_lines = [line for line in raw_lines if _WARNING.search(line) or _ERROR.search(line)]
        safe_lines = [redact_log_line(line) for line in raw_lines[-_MAX_LOG_LINES:]]
        lines: list[str] = []
        remaining = _MAX_LOG_CHARS
        for line in safe_lines:
            if remaining <= 0:
                break
            line = line[:remaining]
            lines.append(line)
            remaining -= len(line)
        return {"service": service, "project": project, "window_minutes": minutes,
                "level": level, "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "line_count": len(lines), "truncated": len(raw_lines) > len(lines), "lines": lines}


def create_app(*, token: str | None = None, allowed_projects: set[str] | None = None,
               docker: DockerReadOnly | None = None,
               proc_root: Path = Path("/host/proc"),
               storage_path: Path = Path("/host/storage"),
               host_root_path: Path = Path("/host/root")) -> FastAPI:
    secret = token if token is not None else read_secret("INTERNAL_TOKEN")
    reader = docker or DockerReadOnly(allowed_projects=allowed_projects, proxy_token=secret)
    app = FastAPI(title="Read-only server monitor", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.docker_reader = reader

    async def authorize(authorization: str | None = Header(default=None)) -> None:
        supplied = authorization.removeprefix("Bearer ") if authorization and authorization.startswith("Bearer ") else ""
        if not secret or not supplied or not hmac.compare_digest(supplied, secret):
            raise HTTPException(status_code=401, detail="Ruxsat talab qilinadi.")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/host")
    async def host_status(_: None = Depends(authorize)) -> dict[str, Any]:
        try:
            metrics = await asyncio.to_thread(read_host_metrics, proc_root=proc_root, storage_path=storage_path,
                                              host_root_path=host_root_path)
            version = await reader._get_json("version")
            info = await reader._get_json("info")
            docker_info = {
                "version": str(version.get("Version") or "noma’lum")[:80],
                "api_version": str(version.get("ApiVersion") or "noma’lum")[:40],
                "containers": int(info.get("Containers") or 0),
                "containers_running": int(info.get("ContainersRunning") or 0),
                "containers_paused": int(info.get("ContainersPaused") or 0),
                "containers_stopped": int(info.get("ContainersStopped") or 0),
                "images": int(info.get("Images") or 0),
            }
            return {**metrics, "docker": docker_info, "services": await reader.containers()}
        except (OSError, ValueError, DockerAPIError) as exc:
            if isinstance(exc, DockerAPIError):
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            raise HTTPException(status_code=503, detail="Server resurs o‘lchovlarini olish muvaffaqiyatsiz bo‘ldi.") from exc

    @app.get("/v1/containers")
    async def containers(_: None = Depends(authorize)) -> dict[str, Any]:
        try:
            return await reader.containers()
        except DockerAPIError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/v1/storage/large")
    async def storage_large(limit: int = Query(default=20, ge=1, le=50),
                            _: None = Depends(authorize)) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(scan_storage_directories, storage_path=storage_path, limit=limit)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=503, detail="Disk papkalarini xavfsiz skanerlash muvaffaqiyatsiz bo‘ldi.") from exc

    @app.get("/v1/docker/cleanup-candidates")
    async def docker_cleanup_candidates(limit: int = Query(default=50, ge=1, le=1000),
                                       _: None = Depends(authorize)) -> dict[str, Any]:
        try:
            return await reader.cleanup_candidates(limit=limit)
        except DockerAPIError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/v1/docker/cleanup")
    async def docker_cleanup(body: dict[str, Any], _: None = Depends(authorize)) -> dict[str, Any]:
        try:
            return await reader.delete_unused_volumes(body.get("volume_names"))
        except DockerAPIError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/v1/logs")
    async def logs(service: str = Query(min_length=1, max_length=64),
                   minutes: int = Query(default=60, ge=1, le=1440),
                   level: str = Query(default="all", pattern="^(all|errors|warnings)$"),
                   _: None = Depends(authorize)) -> dict[str, Any]:
        try:
            return await reader.logs(service=service, minutes=minutes, level=level)
        except DockerAPIError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    return app


def main() -> None:
    token = read_secret("INTERNAL_TOKEN", required=True)
    projects = {item.strip() for item in os.getenv("SERVER_ALLOWED_PROJECTS", "").split(",") if item.strip()}
    uvicorn.run(create_app(token=token, allowed_projects=projects), host="0.0.0.0", port=8090,
                log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
