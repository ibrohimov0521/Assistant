from __future__ import annotations

import hmac
import json
import os
import re
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import Response

from app.core.config import read_secret

_API_PREFIX = "/v1.41/"
_MAX_UPSTREAM_API = (1, 51)
_DOCKER_SOCKET = "/var/run/docker.sock"
_MAX_JSON_BYTES = 2_000_000
_MAX_LOG_BYTES = 512 * 1024
_MAX_PROJECTS = 100
_MAX_CONTAINERS = 1000
_HEX_ID = re.compile(r"^[a-fA-F0-9]{12,64}$")


class DockerSocketProxy:
    """Expose bounded Docker inventory/log reads and exact, unforced volume deletion."""

    def __init__(self, *, token: str, allowed_projects: set[str],
                 transport: httpx.AsyncBaseTransport | None = None):
        self.token = token
        self.allowed_projects = set(sorted({item.strip() for item in allowed_projects if item.strip()})[:_MAX_PROJECTS])
        uds_transport = transport or httpx.AsyncHTTPTransport(uds=_DOCKER_SOCKET)
        self.client = httpx.AsyncClient(
            base_url="http://docker/", transport=uds_transport,
            timeout=httpx.Timeout(8.0, connect=2.0), follow_redirects=False, trust_env=False,
        )
        self._upstream_api_prefix: str | None = None

    async def close(self) -> None:
        await self.client.aclose()

    async def _json(self, path: str, *, params: dict[str, str] | None = None) -> Any:
        try:
            if path == "version":
                request_path = "version"
            else:
                request_path = await self._versioned_path(path)
            response = await self.client.get(request_path, params=params)
            response.raise_for_status()
            if len(response.content) > _MAX_JSON_BYTES:
                raise HTTPException(status_code=502, detail="Docker javobi belgilangan hajmdan katta.")
            return response.json()
        except HTTPException:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(status_code=502, detail="Docker’dan o‘qish muvaffaqiyatsiz bo‘ldi.") from exc

    async def _versioned_path(self, path: str) -> str:
        if self._upstream_api_prefix is None:
            try:
                response = await self.client.get("version")
                response.raise_for_status()
                body = response.json()
                version = str(body.get("ApiVersion") or "") if isinstance(body, dict) else ""
                match = re.fullmatch(r"(\d{1,2})\.(\d{1,2})", version)
                if not match:
                    raise ValueError("Docker API versiyasi noto‘g‘ri.")
                server_version = (int(match.group(1)), int(match.group(2)))
                selected = min(server_version, _MAX_UPSTREAM_API)
                if selected < (1, 40):
                    raise ValueError("Docker API versiyasi qo‘llab-quvvatlanmaydi.")
                self._upstream_api_prefix = f"v{selected[0]}.{selected[1]}/"
            except (httpx.HTTPError, ValueError) as exc:
                raise HTTPException(status_code=502, detail="Docker API versiyasini aniqlab bo‘lmadi.") from exc
        return self._upstream_api_prefix + path

    async def version(self) -> dict[str, str]:
        raw = await self._json("version")
        if not isinstance(raw, dict):
            raise HTTPException(status_code=502, detail="Docker versiyasi javobi noto‘g‘ri.")
        return {key: str(raw.get(key) or "")[:80] for key in ("Version", "ApiVersion")}

    async def info(self) -> dict[str, int]:
        raw = await self._json("info")
        if not isinstance(raw, dict):
            raise HTTPException(status_code=502, detail="Docker holati javobi noto‘g‘ri.")
        values: dict[str, int] = {}
        for key in ("Containers", "ContainersRunning", "ContainersPaused", "ContainersStopped", "Images"):
            try:
                values[key] = max(0, min(1_000_000, int(raw.get(key) or 0)))
            except (TypeError, ValueError):
                values[key] = 0
        return values

    async def containers(self) -> list[dict[str, Any]]:
        raw = await self._json("containers/json", params={"all": "1"})
        if not isinstance(raw, list):
            raise HTTPException(status_code=502, detail="Docker konteyner javobi noto‘g‘ri.")
        items: list[dict[str, Any]] = []
        for entry in raw[:_MAX_CONTAINERS]:
            if not isinstance(entry, dict):
                continue
            labels = entry.get("Labels") or {}
            if not isinstance(labels, dict):
                continue
            safe_labels = {
                key: str(labels[key])[:120]
                for key in ("com.docker.compose.project", "com.docker.compose.service")
                if labels.get(key) is not None
            }
            items.append({key: entry.get(key) for key in ("Id", "Names", "Image", "State", "Status", "Created")})
            items[-1]["Labels"] = safe_labels
        return items

    async def images(self) -> list[dict[str, Any]]:
        raw = await self._json("images/json", params={"all": "1"})
        if not isinstance(raw, list):
            raise HTTPException(status_code=502, detail="Docker image javobi noto‘g‘ri.")
        items: list[dict[str, Any]] = []
        for entry in raw[:_MAX_CONTAINERS]:
            if not isinstance(entry, dict):
                continue
            image_id = str(entry.get("Id") or "")[:96]
            if not re.fullmatch(r"sha256:[a-fA-F0-9]{64}", image_id):
                continue
            tags = entry.get("RepoTags") or []
            tags = [str(tag)[:180] for tag in tags[:20] if isinstance(tag, str)] if isinstance(tags, list) else []
            size = entry.get("Size")
            containers = entry.get("Containers")
            items.append({
                "id": image_id,
                "repo_tags": tags,
                "size_bytes": max(0, min(int(size), 2**63 - 1)) if type(size) is int and size >= 0 else None,
                "container_references": max(0, min(int(containers), 1_000_000))
                if type(containers) is int and containers >= 0 else None,
            })
        return items

    async def volumes(self) -> list[dict[str, Any]]:
        raw = await self._json("volumes")
        entries = raw.get("Volumes") if isinstance(raw, dict) else None
        if not isinstance(entries, list):
            raise HTTPException(status_code=502, detail="Docker volume javobi noto‘g‘ri.")
        items: list[dict[str, Any]] = []
        for entry in entries[:_MAX_CONTAINERS]:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("Name") or "")[:180]
            if not name or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,179}", name):
                continue
            labels = entry.get("Labels") or {}
            labels = labels if isinstance(labels, dict) else {}
            usage = entry.get("UsageData") or {}
            usage = usage if isinstance(usage, dict) else {}
            size = usage.get("Size")
            references = usage.get("RefCount")
            items.append({
                "name": name,
                "driver": str(entry.get("Driver") or "")[:40],
                "created_at": str(entry.get("CreatedAt") or "")[:40] or None,
                "project": str(labels.get("com.docker.compose.project") or "")[:100] or None,
                "service": str(labels.get("com.docker.compose.service") or "")[:80] or None,
                "size_bytes": max(0, min(int(size), 2**63 - 1)) if type(size) is int and size >= 0 else None,
                "container_references": max(0, min(int(references), 1_000_000))
                if type(references) is int and references >= 0 else None,
            })
        return items

    async def delete_unused_volume(self, name: str) -> dict[str, Any]:
        if len(name) > 96 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,179}", name):
            raise HTTPException(status_code=400, detail="Docker volume nomi noto‘g‘ri.")
        current = await self.volumes()
        volume = next((item for item in current if item.get("name") == name), None)
        if not volume:
            raise HTTPException(status_code=404, detail="Docker volume topilmadi.")
        if type(volume.get("container_references")) is not int or volume["container_references"] != 0:
            raise HTTPException(status_code=409, detail="Docker volume ishlatilmoqda yoki foydalanish soni noma’lum.")
        try:
            path = await self._versioned_path(f"volumes/{name}")
            response = await self.client.delete(path)
            if response.status_code == 409:
                raise HTTPException(status_code=409, detail="Docker volume endi ishlatilmoqda; o‘chirmadim.")
            response.raise_for_status()
        except HTTPException:
            raise
        except httpx.HTTPStatusError as exc:
            raise HTTPException(status_code=502, detail="Docker volume o‘chirish javobi xato; natijani tekshiring.") from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="Docker volume o‘chirish natijasi noma’lum.") from exc
        try:
            remains = any(item.get("name") == name for item in await self.volumes())
        except (HTTPException, httpx.HTTPError):
            raise HTTPException(status_code=502, detail="Volume amalidan keyingi holatni tekshirib bo‘lmadi.") from None
        if remains:
            raise HTTPException(status_code=502, detail="Docker DELETE javobi keldi, ammo volume ro‘yxatda qolmoqda.")
        return {"name": name, "deleted": True, "estimated_size_bytes": volume.get("size_bytes"),
                "project": volume.get("project"), "service": volume.get("service")}

    async def logs(self, container_id: str, params: dict[str, str]) -> bytes:
        if not _HEX_ID.fullmatch(container_id):
            raise HTTPException(status_code=400, detail="Konteyner ID si noto‘g‘ri.")
        inventory = await self.containers()
        if not any(str(item.get("Id") or "").startswith(container_id)
                   and (item.get("Labels") or {}).get("com.docker.compose.project") in self.allowed_projects
                   for item in inventory):
            raise HTTPException(status_code=404, detail="Ruxsatli konteyner topilmadi.")
        safe: dict[str, str] = {}
        for key in ("stdout", "stderr", "timestamps"):
            value = params.get(key, "0")
            if value not in {"0", "1"}:
                raise HTTPException(status_code=400, detail="Log so‘rovi parametri noto‘g‘ri.")
            safe[key] = value
        since = params.get("since", "0")
        tail = params.get("tail", "500")
        if not since.isdigit() or len(since) > 12 or not tail.isdigit() or not 1 <= int(tail) <= 500:
            raise HTTPException(status_code=400, detail="Log davri yoki qator chegarasi noto‘g‘ri.")
        safe.update({"since": since, "tail": tail})
        try:
            path = await self._versioned_path(f"containers/{container_id}/logs")
            async with self.client.stream("GET", path, params=safe) as response:
                if response.status_code == 404:
                    raise HTTPException(status_code=404, detail="Konteyner logi topilmadi.")
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    remaining = _MAX_LOG_BYTES - len(body)
                    if remaining <= 0:
                        break
                    body.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        break
                return bytes(body)
        except HTTPException:
            raise
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="Docker logini o‘qish muvaffaqiyatsiz bo‘ldi.") from exc


def create_app(*, token: str, allowed_projects: set[str],
               docker: DockerSocketProxy | None = None) -> FastAPI:
    proxy = docker or DockerSocketProxy(token=token, allowed_projects=allowed_projects)
    app = FastAPI(title="Restricted Docker read proxy", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.docker_proxy = proxy

    async def authorize(authorization: str | None = Header(default=None)) -> None:
        supplied = authorization.removeprefix("Bearer ") if authorization and authorization.startswith("Bearer ") else ""
        if not proxy.token or not supplied or not hmac.compare_digest(supplied, proxy.token):
            raise HTTPException(status_code=401, detail="Ruxsat talab qilinadi.")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def route(path: str, request: Request, _: None = Header(default=None, include_in_schema=False)) -> Response:
        # Authenticate every non-health route before inspecting or relaying it.
        await authorize(request.headers.get("authorization"))
        full_path = "/" + path
        if not full_path.startswith(_API_PREFIX):
            raise HTTPException(status_code=404, detail="Docker yo‘li ruxsat etilmagan.")
        endpoint = full_path[len(_API_PREFIX):]
        if request.method == "DELETE":
            if request.query_params or await request.body():
                raise HTTPException(status_code=400, detail="Docker volume o‘chirishda qo‘shimcha parametr ruxsat etilmagan.")
            match = re.fullmatch(r"volumes/([A-Za-z0-9][A-Za-z0-9_.-]{0,179})", endpoint)
            if not match:
                raise HTTPException(status_code=405, detail="Faqat aniq volume nomini o‘chirishga ruxsat etilgan.")
            return Response(content=json.dumps(await proxy.delete_unused_volume(match.group(1))),
                            media_type="application/json")
        if request.method != "GET":
            raise HTTPException(status_code=405, detail="Faqat ruxsatli GET yoki exact volume DELETE yo‘llari qo‘llanadi.")
        if endpoint == "version":
            if request.query_params:
                raise HTTPException(status_code=400, detail="Docker so‘rovi parametri ruxsat etilmagan.")
            return Response(content=json.dumps(await proxy.version()), media_type="application/json")
        if endpoint == "info":
            if request.query_params:
                raise HTTPException(status_code=400, detail="Docker so‘rovi parametri ruxsat etilmagan.")
            return Response(content=json.dumps(await proxy.info()), media_type="application/json")
        if endpoint == "containers/json":
            if set(request.query_params.keys()) - {"all"} or request.query_params.get("all", "1") not in {"0", "1"}:
                raise HTTPException(status_code=400, detail="Docker so‘rovi parametri ruxsat etilmagan.")
            return Response(content=json.dumps(await proxy.containers()), media_type="application/json")
        if endpoint == "images/json":
            if set(request.query_params.keys()) - {"all"} or request.query_params.get("all", "1") not in {"0", "1"}:
                raise HTTPException(status_code=400, detail="Docker so‘rovi parametri ruxsat etilmagan.")
            return Response(content=json.dumps(await proxy.images()), media_type="application/json")
        if endpoint == "volumes":
            if request.query_params:
                raise HTTPException(status_code=400, detail="Docker so‘rovi parametri ruxsat etilmagan.")
            return Response(content=json.dumps({"Volumes": await proxy.volumes()}), media_type="application/json")
        match = re.fullmatch(r"containers/([a-fA-F0-9]{12,64})/logs", endpoint)
        if match:
            pairs = list(request.query_params.multi_items())
            params = dict(pairs)
            if len(params) != len(pairs) or set(params) - {"stdout", "stderr", "timestamps", "since", "tail"}:
                raise HTTPException(status_code=400, detail="Log so‘rovi parametri ruxsat etilmagan.")
            payload = await proxy.logs(match.group(1), params)
            return Response(content=payload, media_type="application/octet-stream")
        raise HTTPException(status_code=404, detail="Docker yo‘li ruxsat etilmagan.")

    return app


def main() -> None:
    token = read_secret("INTERNAL_TOKEN", required=True)
    projects = {item.strip() for item in os.getenv("DOCKER_ALLOWED_PROJECTS", "").split(",") if item.strip()}
    if not projects:
        raise RuntimeError("DOCKER_ALLOWED_PROJECTS kamida bitta loyiha bilan sozlanishi kerak")
    uvicorn.run(create_app(token=token, allowed_projects=projects), host="0.0.0.0", port=8091,
                log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
