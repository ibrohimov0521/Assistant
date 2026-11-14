from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, date, datetime
from typing import Any

import httpx


class GitHubReadError(ValueError):
    pass


_REPOSITORY_PART = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_CATEGORIES = {"issues", "pulls", "commits", "ci", "releases"}
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def _repository(value: object) -> tuple[str, str]:
    text = str(value or "").strip().strip("/")
    pieces = text.split("/")
    if (len(pieces) != 2 or any(not _REPOSITORY_PART.fullmatch(part) or part in {".", ".."}
                                for part in pieces)):
        raise GitHubReadError("Repository `owner/name` ko‘rinishida bo‘lishi kerak.")
    return pieces[0], pieces[1]


def _date_value(value: object, field: str) -> date | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise GitHubReadError(f"{field} sanasi YYYY-MM-DD formatida bo‘lishi kerak.") from exc


def _timestamp_day(value: object) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC).date()
    except (ValueError, OverflowError):
        return None


class GitHubReadOnly:
    """Bounded read-only summaries from the official GitHub REST API."""

    def __init__(self, token: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "personal-telegram-assistant-readonly",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._headers = headers
        self._transport = transport

    @staticmethod
    def _endpoint(category: str, since: date | None, until: date | None) -> tuple[str, dict[str, str]]:
        params = {"per_page": "100"}
        if category == "issues":
            params["state"] = "all"
            if since:
                params["since"] = f"{since.isoformat()}T00:00:00Z"
            return "issues", params
        if category == "pulls":
            params.update({"state": "all", "sort": "updated", "direction": "desc"})
            return "pulls", params
        if category == "commits":
            if since:
                params["since"] = f"{since.isoformat()}T00:00:00Z"
            if until:
                params["until"] = f"{until.isoformat()}T23:59:59Z"
            return "commits", params
        if category == "ci":
            if since and until:
                params["created"] = f"{since.isoformat()}..{until.isoformat()}"
            elif since:
                params["created"] = f">={since.isoformat()}"
            elif until:
                params["created"] = f"<={until.isoformat()}"
            return "actions/runs", params
        if category == "releases":
            return "releases", params
        raise GitHubReadError("Bo‘lim issues, pulls, commits, ci yoki releases bo‘lishi kerak.")

    async def _get(self, client: httpx.AsyncClient, repository: str, category: str,
                   since: date | None, until: date | None) -> list[dict[str, Any]]:
        endpoint, params = self._endpoint(category, since, until)
        url = f"https://api.github.com/repos/{repository}/{endpoint}"
        try:
            async with client.stream("GET", url, params=params) as response:
                if response.status_code in {401, 403}:
                    if response.status_code == 401:
                        raise GitHubReadError("GitHub ulanishi rad etildi; token yaroqsiz yoki kerakli faqat o‘qish huquqi yo‘q.")
                    remaining = response.headers.get("X-RateLimit-Remaining")
                    if remaining == "0" or response.headers.get("Retry-After"):
                        raise GitHubReadError("GitHub API so‘rov limiti tugagan; keyinroq urinib ko‘ring.")
                    raise GitHubReadError("GitHub bu repository uchun ruxsat bermadi; xususiy repo bo‘lsa faqat o‘qish tokenini sozlang.")
                if response.status_code == 404:
                    raise GitHubReadError("Repository topilmadi yoki ochiq emas; xususiy repo uchun faqat o‘qish tokeni kerak.")
                if response.status_code == 429:
                    raise GitHubReadError("GitHub API so‘rov limiti tugagan; keyinroq urinib ko‘ring.")
                if response.status_code >= 500:
                    raise GitHubReadError("GitHub vaqtincha javob bermayapti.")
                if response.status_code < 200 or response.status_code >= 300:
                    raise GitHubReadError(f"GitHub API HTTP {response.status_code} qaytardi.")
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > _MAX_RESPONSE_BYTES:
                        raise GitHubReadError("GitHub javobi 2 MB chegarasidan oshdi; so‘rovni toraytiring.")
        except GitHubReadError:
            raise
        except httpx.TimeoutException as exc:
            raise GitHubReadError("GitHub javobi vaqtida kelmadi.") from exc
        except httpx.RequestError as exc:
            raise GitHubReadError("GitHub bilan xavfsiz ulanish o‘rnata olmadim.") from exc
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise GitHubReadError("GitHub javobini o‘qib bo‘lmadi.") from exc
        rows = payload.get("workflow_runs") if category == "ci" and isinstance(payload, dict) else payload
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise GitHubReadError("GitHub kutilmagan ma’lumot qaytardi.")
        return rows

    @staticmethod
    def _entry(category: str, row: dict[str, Any]) -> dict[str, Any] | None:
        if category in {"issues", "pulls"}:
            if category == "issues" and "pull_request" in row:
                return None
            return {
                "number": row.get("number"), "title": str(row.get("title") or "")[:240],
                "state": row.get("state"), "author": (row.get("user") or {}).get("login"),
                "created_at": row.get("created_at"), "updated_at": row.get("updated_at"),
                "url": row.get("html_url"),
                "labels": [str(label.get("name") or "")[:60] for label in row.get("labels", [])[:10]
                           if isinstance(label, dict)],
                "merged": bool(row.get("merged_at")) if category == "pulls" else None,
                "base": (row.get("base") or {}).get("ref") if category == "pulls" else None,
                "head": (row.get("head") or {}).get("ref") if category == "pulls" else None,
            }
        if category == "commits":
            commit = row.get("commit") or {}
            author = row.get("author") or {}
            commit_author = commit.get("author") or {}
            message = str(commit.get("message") or "").splitlines()[0][:240]
            return {"sha": str(row.get("sha") or "")[:12], "title": message,
                    "author": author.get("login") or commit_author.get("name"),
                    "created_at": commit_author.get("date"), "url": row.get("html_url")}
        if category == "ci":
            return {"name": str(row.get("name") or row.get("display_title") or "Workflow")[:160],
                    "branch": str(row.get("head_branch") or "")[:100],
                    "status": row.get("status"), "conclusion": row.get("conclusion"),
                    "created_at": row.get("created_at"), "updated_at": row.get("updated_at"),
                    "url": row.get("html_url")}
        if category == "releases":
            return {"name": str(row.get("name") or row.get("tag_name") or "Release")[:180],
                    "tag": str(row.get("tag_name") or "")[:100],
                    "published_at": row.get("published_at"), "prerelease": bool(row.get("prerelease")),
                    "url": row.get("html_url")}
        return None

    async def _section(self, client: httpx.AsyncClient, repository: str, category: str,
                       since: date | None, until: date | None, limit: int) -> dict[str, Any]:
        rows = await self._get(client, repository, category, since, until)
        entries: list[dict[str, Any]] = []
        for row in rows:
            if category == "issues" and "pull_request" in row:
                continue
            entry = self._entry(category, row)
            if entry is None:
                continue
            timestamp_key = {"issues": "updated_at", "pulls": "updated_at", "commits": "created_at",
                             "ci": "created_at", "releases": "published_at"}[category]
            entry_day = _timestamp_day(entry.get(timestamp_key))
            if since and (entry_day is None or entry_day < since):
                continue
            if until and (entry_day is None or entry_day > until):
                continue
            entries.append(entry)
        return {"items": entries[:limit], "count": min(len(entries), limit),
                "truncated": len(entries) > limit or len(rows) >= 100,
                "date_field": {"issues": "updated_at", "pulls": "updated_at", "commits": "created_at",
                               "ci": "created_at", "releases": "published_at"}[category]}

    async def summary(self, repository: object, category: object = "overview", *,
                      since: object = None, until: object = None, limit: object = 5) -> dict[str, Any]:
        owner, name = _repository(repository)
        category_value = str(category or "overview").strip().lower()
        if category_value not in _CATEGORIES | {"overview"}:
            raise GitHubReadError("Bo‘lim overview, issues, pulls, commits, ci yoki releases bo‘lishi kerak.")
        since_day = _date_value(since, "Boshlanish")
        until_day = _date_value(until, "Tugash")
        if since_day and until_day and since_day > until_day:
            raise GitHubReadError("Boshlanish sanasi tugash sanasidan keyin bo‘la olmaydi.")
        try:
            limit_value = int(limit) if limit is not None else 5
        except (TypeError, ValueError) as exc:
            raise GitHubReadError("Natija soni 1–20 oralig‘ida bo‘lishi kerak.") from exc
        if not 1 <= limit_value <= 20:
            raise GitHubReadError("Natija soni 1–20 oralig‘ida bo‘lishi kerak.")
        repository_name = f"{owner}/{name}"
        async with httpx.AsyncClient(timeout=httpx.Timeout(12, connect=5), headers=self._headers,
                                     follow_redirects=False, trust_env=False,
                                     transport=self._transport) as client:
            if category_value == "overview":
                categories = sorted(_CATEGORIES)
                results = await asyncio.gather(*(
                    self._section(client, repository_name, item, since_day, until_day, limit_value)
                    for item in categories), return_exceptions=True)
                sections: dict[str, Any] = {}
                for item, result in zip(categories, results, strict=True):
                    if isinstance(result, GitHubReadError):
                        sections[item] = {"error": str(result)}
                    elif isinstance(result, Exception):
                        sections[item] = {"error": "GitHub ma’lumotini olishda xatolik yuz berdi."}
                    elif isinstance(result, BaseException):
                        raise result
                    else:
                        sections[item] = result
                return {"repository": repository_name, "category": "overview",
                        "period": {"since": since_day.isoformat() if since_day else None,
                                   "until": until_day.isoformat() if until_day else None},
                        "sections": sections}
            try:
                result = await self._section(client, repository_name, category_value,
                                             since_day, until_day, limit_value)
            except GitHubReadError as exc:
                return {"error": str(exc), "repository": repository_name,
                        "category": category_value}
        return {"repository": repository_name, "category": category_value,
                "period": {"since": since_day.isoformat() if since_day else None,
                           "until": until_day.isoformat() if until_day else None}, **result}


__all__ = ["GitHubReadError", "GitHubReadOnly"]
