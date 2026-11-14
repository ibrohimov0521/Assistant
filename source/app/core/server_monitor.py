from __future__ import annotations

import re
from typing import Any

import httpx


class ServerMonitorError(RuntimeError):
    """Safe, user-facing failure from the private read-only monitor."""


class ServerMonitor:
    def __init__(self, token: str | None, *, base_url: str = "http://server-agent:8090",
                 transport: httpx.AsyncBaseTransport | None = None):
        self.token = token
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"} if token else {},
            timeout=httpx.Timeout(15.0, connect=4.0), follow_redirects=False,
            trust_env=False, transport=transport,
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def health_status(self) -> dict[str, Any]:
        return await self._get("/health")

    async def _get(self, path: str, *, params: dict[str, str] | None = None) -> dict[str, Any]:
        if not self.token:
            raise ServerMonitorError("Ichki server monitori tokeni sozlanmagan.")
        try:
            response = await self.client.get(path, params=params)
            if len(response.content) > 2_000_000:
                raise ServerMonitorError("Server monitori javobi belgilangan hajmdan katta.")
            response.raise_for_status()
            body = response.json()
        except ServerMonitorError:
            raise
        except httpx.HTTPStatusError as exc:
            raise ServerMonitorError(f"Server monitori HTTP {exc.response.status_code} xatosini qaytardi.") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ServerMonitorError("Server monitoriga ulanib bo‘lmadi yoki javobini o‘qib bo‘lmadi.") from exc
        if not isinstance(body, dict):
            raise ServerMonitorError("Server monitori javobi noto‘g‘ri formatda.")
        return body

    async def host_status(self) -> dict[str, Any]:
        return await self._get("/v1/host")

    async def large_directories(self, *, limit: int = 20) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ServerMonitorError("Natija soni 1–50 oralig‘ida bo‘lishi kerak.")
        return await self._get("/v1/storage/large", params={"limit": str(limit)})

    async def docker_cleanup_candidates(self, *, limit: int = 50) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ServerMonitorError("Docker nomzodlari soni 1–1000 oralig‘ida bo‘lishi kerak.")
        return await self._get("/v1/docker/cleanup-candidates", params={"limit": str(limit)})

    async def incident_evidence(self, *, service: str = "bot", minutes: int = 60) -> dict[str, Any]:
        status = await self.host_status()
        logs = await self.logs(service=service, minutes=minutes, level="errors")
        containers = ((status.get("services") or {}).get("containers") or [])
        matches = [item for item in containers if isinstance(item, dict)
                   and str(item.get("service") or "").casefold() == service.casefold()]
        return {
            "measured_at": status.get("measured_at"),
            "host": {key: status.get(key) for key in ("cpu", "memory", "storage", "load_average_1_5_15")},
            "docker": status.get("docker"),
            "service": service,
            "containers": matches[:10],
            "logs": logs,
            "deploy_history_available": False,
        }

    async def logs(self, *, service: str, minutes: int = 60, level: str = "all") -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", service):
            raise ServerMonitorError("Xizmat nomi noto‘g‘ri.")
        if type(minutes) is not int or not 1 <= minutes <= 1440:
            raise ServerMonitorError("Log oralig‘i 1–1440 daqiqa bo‘lishi kerak.")
        if level not in {"all", "errors", "warnings"}:
            raise ServerMonitorError("Log darajasi all, errors yoki warnings bo‘lishi kerak.")
        return await self._get("/v1/logs", params={
            "service": service, "minutes": str(minutes), "level": level,
        })
