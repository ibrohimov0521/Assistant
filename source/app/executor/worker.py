from __future__ import annotations

import asyncio
import getpass
import logging
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from telethon import TelegramClient, errors, events, utils
from telethon.sessions import StringSession

from app.core.config import Settings
from app.core.telegram_calls import CallOutcome
from app.executor.management import ProfileManager
from app.executor.telegram_call_adapter import PyTgCallsAdapter
from app.storage.security import Vault

LOG = logging.getLogger(__name__)


def _read_encrypted_contacts(path: Path, vault: Vault) -> list[dict[str, Any]]:
    raw = path.read_bytes()
    contacts = vault.open_json(raw)
    if not isinstance(contacts, list):
        raise RuntimeError("Encrypted allowlist formati noto‘g‘ri")
    return contacts


class ProfileSender:
    def __init__(self, client: TelegramClient, expected_profile_id: int,
                 contacts: list[dict[str, Any]]):
        self.client = client
        self.expected_profile_id = expected_profile_id
        self.contacts = contacts
        self.manager = ProfileManager(client, expected_profile_id)

    async def start(self) -> None:
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise RuntimeError("session_revoked")
        me = await self.client.get_me()
        if not me or me.id != self.expected_profile_id:
            await self.client.disconnect()
            raise RuntimeError("wrong_profile")

    async def send(self, job: dict[str, Any]) -> str:
        if int(job["sender_profile_id"]) != self.expected_profile_id:
            return "wrong_profile"
        try:
            return await self.manager.execute(job)
        except errors.FloodWaitError:
            return "flood_wait"
        except (errors.UserIsBlockedError, errors.UserPrivacyRestrictedError):
            return "recipient_restricted"
        except (errors.AuthKeyUnregisteredError, errors.SessionRevokedError):
            return "session_revoked"
        except (errors.UsernameNotOccupiedError, errors.UsernameInvalidError):
            return "recipient_unavailable"
        except errors.RPCError:
            return "telegram_rpc_error"
        except (OSError, asyncio.TimeoutError, httpx.HTTPError):
            return "unknown"


async def _request(client: httpx.AsyncClient, method: str, url: str,
                   token: str, data: dict[str, Any] | None = None) -> httpx.Response:
    return await client.post(url, json=data, headers={"Authorization": f"Bearer {token}"})


def _group_welcome_event_handler(settings: Settings, manager: ProfileManager):
    sent_times: list[datetime] = []
    send_lock = asyncio.Lock()

    async def finish(client: httpx.AsyncClient, event_key: str, status: str) -> None:
        try:
            response = await _request(client, "POST",
                settings.profile_api_base + "/internal/v1/group-welcome/finish",
                settings.internal_token or "", {"event_key": event_key, "status": status})
            if response.status_code not in {200, 409}:
                response.raise_for_status()
        except (httpx.HTTPError, ValueError):
            LOG.warning("Group welcome result could not be recorded status=%s", status)

    async def handle(event: Any) -> None:
        if not (bool(getattr(event, "user_joined", False)) or bool(getattr(event, "user_added", False))):
            return
        if not settings.internal_token:
            return
        try:
            chat = await event.get_chat()
            chat_id = int(utils.get_peer_id(chat))
            action_message = getattr(event, "action_message", None)
            action_message_id = int(getattr(action_message, "id", 0) or 0)
            if chat_id == 0 or action_message_id <= 0:
                return
            users = list(getattr(event, "users", None) or [])
            if not users:
                user = await event.get_user()
                users = [user] if user is not None else []
        except Exception as exc:
            LOG.warning("Group welcome event could not be resolved category=%s", type(exc).__name__)
            return
        if not users:
            return
        async with httpx.AsyncClient(timeout=10, trust_env=False) as api:
            for user in users[:20]:
                user_id = int(getattr(user, "id", 0) or 0)
                if user_id <= 0 or user_id == settings.expected_profile_id:
                    continue
                user_name = " ".join(str(getattr(user, "first_name", "") or "").split())[:80]
                username = str(getattr(user, "username", "") or "")[:64]
                event_key = f"{chat_id}:{action_message_id}:{user_id}"
                try:
                    response = await _request(api, "POST",
                        settings.profile_api_base + "/internal/v1/group-welcome/claim",
                        settings.internal_token, {"chat_id": chat_id, "event_key": event_key,
                                                  "user_id": user_id, "user_name": user_name})
                    if response.status_code == 204:
                        continue
                    response.raise_for_status()
                    rule = response.json()
                    body = str(rule.get("body") or "").strip()
                    if not body or len(body) > 1000:
                        await finish(api, event_key, "failed")
                        continue
                    async with send_lock:
                        now = datetime.now(UTC)
                        sent_times[:] = [stamp for stamp in sent_times
                                         if now - stamp < timedelta(hours=1)]
                        if len(sent_times) >= max(1, settings.profile_sends_per_hour):
                            await finish(api, event_key, "failed")
                            continue
                        sent_times.append(now)
                    permission = await manager.check_text_permission(chat_id)
                    if permission.get("allowed") is not True:
                        await finish(api, event_key, "failed")
                        LOG.info("Group welcome skipped; Telegram send permission is unavailable")
                        continue
                    rendered = body.replace("{{name}}", user_name or "yangi a’zo")
                    rendered = rendered.replace("{{username}}", username)
                    await manager.client.send_message(chat, rendered[:1000], parse_mode=None)
                    await finish(api, event_key, "sent")
                except (asyncio.TimeoutError, OSError):
                    await finish(api, event_key, "unknown")
                    LOG.warning("Group welcome delivery result is unknown chat_id=%s", chat_id)
                except (httpx.HTTPError, errors.RPCError, ValueError, TypeError, KeyError) as exc:
                    await finish(api, event_key, "failed")
                    LOG.warning("Group welcome failed category=%s", type(exc).__name__)

    return handle


async def run_executor(settings: Settings) -> None:
    from app.executor.management import create_management_app
    import uvicorn

    manager: ProfileManager | None = None
    client: TelegramClient | None = None
    sender: ProfileSender | None = None
    can_connect = (settings.enable_profile_executor or settings.enable_profile_reader)
    required = (settings.data_key, settings.internal_token, settings.tg_api_id,
                settings.tg_api_hash, settings.expected_profile_id)
    if can_connect and all(required) and settings.tg_session_file.is_file():
        try:
            vault = Vault(settings.data_key or "")
            session_value = vault.open_text(settings.tg_session_file.read_bytes())
            client = TelegramClient(StringSession(session_value), settings.tg_api_id, settings.tg_api_hash,
                                   request_retries=0, connection_retries=1, timeout=10)
            await client.connect()
            if not await client.is_user_authorized():
                raise RuntimeError("session_revoked")
            me = await client.get_me()
            if not me or me.id != settings.expected_profile_id:
                raise RuntimeError("wrong_profile")
            manager = ProfileManager(client, settings.expected_profile_id or 0)
            if settings.enable_profile_executor:
                try:
                    contacts = _read_encrypted_contacts(settings.contacts_file, vault)
                except (OSError, ValueError, RuntimeError):
                    contacts = []
                sender = ProfileSender(client, settings.expected_profile_id or 0, contacts)
        except Exception as exc:
            LOG.warning("Telegram profile session unavailable category=%s", type(exc).__name__)
            if client:
                await client.disconnect()
            client = None
            manager = None
    elif can_connect:
        LOG.warning("Telegram profile reader/executor is waiting for API credentials and encrypted session")

    app = create_management_app(settings, manager)
    if manager is not None and settings.enable_profile_executor:
        client.add_event_handler(_group_welcome_event_handler(settings, manager), events.ChatAction)
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=8081,
                                           log_level=settings.log_level.lower(), access_log=False))
    api_task = asyncio.create_task(server.serve())
    tasks = {api_task}
    if sender and settings.enable_profile_executor:
        tasks.add(asyncio.create_task(_poll_profile_jobs(settings, sender)))
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in done:
            exception = task.exception()
            if exception:
                raise exception
    finally:
        for task in tasks:
            task.cancel()
        server.should_exit = True
        await asyncio.gather(*tasks, return_exceptions=True)
        if client:
            await client.disconnect()


async def _poll_profile_jobs(settings: Settings, sender: ProfileSender) -> None:
    if not settings.internal_token:
        LOG.warning("Profile action queue is waiting for INTERNAL_TOKEN")
        while True:
            await asyncio.sleep(60)
    async with httpx.AsyncClient(timeout=10, trust_env=False) as api:
        while True:
            try:
                response = await _request(api, "POST", settings.profile_api_base + "/internal/v1/jobs/claim",
                                          settings.internal_token)
                if response.status_code == 204:
                    await asyncio.sleep(settings.profile_poll_seconds)
                    continue
                response.raise_for_status()
                job = response.json()
                required = {"job_id", "lease_token", "owner_id", "sender_profile_id", "operation", "params", "random_id"}
                if not required.issubset(job) or int(job["owner_id"]) != settings.owner_telegram_id:
                    LOG.error("Invalid claimed job schema")
                    await asyncio.sleep(settings.profile_poll_seconds)
                    continue
                if job["operation"] in {"send_voice", "call_user"}:
                    audio_response = await _request(
                        api, "POST", settings.profile_api_base + f"/internal/v1/jobs/{job['job_id']}/voice",
                        settings.internal_token, {"lease_token": job["lease_token"]})
                    if audio_response.status_code != 200 or len(audio_response.content) > 8_000_000:
                        await _report_result(api, settings, job, "failed", "voice_unavailable")
                        continue
                    if not audio_response.headers.get("content-type", "").startswith("audio/ogg"):
                        await _report_result(api, settings, job, "failed", "voice_format_invalid")
                        continue
                    job["voice_bytes"] = audio_response.content
                dispatch = await _request(api, "POST", settings.profile_api_base + f"/internal/v1/jobs/{job['job_id']}/dispatch",
                                          settings.internal_token, {"lease_token": job["lease_token"]})
                if dispatch.status_code == 409:
                    continue
                dispatch.raise_for_status()
                try:
                    if job["operation"] == "call_user":
                        try:
                            call_response = await _request(api, "POST",
                                settings.profile_api_base + f"/internal/v1/jobs/{job['job_id']}/call-claim",
                                settings.internal_token, {"lease_token": job["lease_token"]})
                        except httpx.HTTPError:
                            await _report_result(api, settings, job, "failed", "call_claim_failed")
                            continue
                        if call_response.status_code != 200:
                            if call_response.status_code < 500:
                                await _report_result(api, settings, job, "failed", "call_claim_failed")
                            else:
                                await _report_result(api, settings, job, "unknown", "call_unknown")
                            continue
                        try:
                            call = call_response.json()
                        except ValueError:
                            await _report_result(api, settings, job, "unknown", "call_unknown")
                            continue
                        params = job.get("params") or {}
                        if (not isinstance(call, dict)
                                or int(call.get("recipient_id") or 0) != int(params.get("chat_id") or 0)
                                or str(call.get("body") or "").strip() != str(params.get("body") or "").strip()):
                            await _report_result(api, settings, job, "failed", "call_claim_failed")
                            continue
                        adapter = PyTgCallsAdapter(sender.client)
                        try:
                            call_outcome = await adapter.call(recipient_id=int(call["recipient_id"]),
                                body=str(call["body"]), timeout_seconds=30,
                                audio_bytes=job["voice_bytes"])
                        finally:
                            await adapter.stop()
                        result_map = {
                            CallOutcome.ANSWERED: ("sent", "call_answered"),
                            CallOutcome.REJECTED: ("failed", "call_rejected"),
                            CallOutcome.NO_ANSWER: ("failed", "call_no_answer"),
                            CallOutcome.FAILED: ("failed", "call_failed"),
                            CallOutcome.UNKNOWN: ("unknown", "call_unknown"),
                        }
                        status, outcome = result_map.get(call_outcome, ("unknown", "call_unknown"))
                    elif job["operation"] in {"set_group_welcome_rule", "disable_group_welcome_rule"}:
                        params = job.get("params") or {}
                        enabled = job["operation"] == "set_group_welcome_rule"
                        try:
                            response = await _request(api, "POST",
                                settings.profile_api_base + "/internal/v1/group-welcome/rule",
                                settings.internal_token, {
                                    "chat_id": int(params["chat_id"]),
                                    "chat_name": str(params.get("chat_name") or "guruh")[:160],
                                    "body": params.get("welcome_body") if enabled else None,
                                    "enabled": enabled,
                                })
                            response.raise_for_status()
                        except httpx.HTTPStatusError as exc:
                            if exc.response.status_code < 500:
                                outcome = "welcome_rule_rejected"
                            else:
                                raise
                        else:
                            saved = response.json()
                            outcome = ("sent" if enabled or saved.get("exists")
                                       else "welcome_rule_missing")
                    else:
                        outcome = await sender.send(job)
                        status = "sent" if outcome == "sent" else "unknown" if outcome == "unknown" else "failed"
                except (asyncio.TimeoutError, OSError):
                    status, outcome = "unknown", "transport_unknown"
                await _report_result(api, settings, job, status, outcome)
                if outcome == "session_revoked":
                    raise RuntimeError("session_revoked")
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                LOG.warning("Executor loop error category=%s", type(exc).__name__)
                await asyncio.sleep(min(10, settings.profile_poll_seconds * 2))


async def _report_result(client: httpx.AsyncClient, settings: Settings, job: dict[str, Any],
                         status: str, category: str) -> None:
    url = settings.profile_api_base + f"/internal/v1/jobs/{job['job_id']}/result"
    data = {"lease_token": job["lease_token"], "status": status, "category": category}
    while True:
        try:
            response = await _request(client, "POST", url, settings.internal_token or "", data)
            if response.status_code in {200, 409}:
                return
            if response.status_code < 500:
                LOG.error("Executor result rejected status=%s", response.status_code)
                return
        except httpx.HTTPError as exc:
            LOG.warning("Executor result retry category=%s", type(exc).__name__)
        await asyncio.sleep(3)


async def login_and_store(settings: Settings) -> None:
    if not settings.data_key or not settings.tg_api_id or not settings.tg_api_hash or not settings.expected_profile_id:
        raise RuntimeError("DATA_KEY_FILE, TG_API_ID, TG_API_HASH_FILE va EXPECTED_PROFILE_ID kerak")
    phone = input("Telegram profilingizning telefon raqami: ").strip()
    client = TelegramClient(StringSession(), settings.tg_api_id, settings.tg_api_hash,
                           request_retries=0, connection_retries=1, timeout=10)
    await client.connect()
    try:
        sent = await client.send_code_request(phone)
        code = getpass.getpass("Telegram tasdiqlash kodi (maxfiy): ").strip()
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
        except errors.SessionPasswordNeededError:
            password = getpass.getpass("Telegram ikki bosqichli parol (maxfiy): ")
            await client.sign_in(password=password)
        me = await client.get_me()
        if not me or me.id != settings.expected_profile_id:
            raise RuntimeError("Kirilgan profil ID kutilgan EXPECTED_PROFILE_ID bilan mos emas")
        encrypted = Vault(settings.data_key).seal(client.session.save())
        settings.tg_session_file.parent.mkdir(parents=True, exist_ok=True)
        settings.tg_session_file.write_bytes(encrypted)
        try:
            os.chmod(settings.tg_session_file, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        print("Profil seansi shifrlanib saqlandi; kod va parol terminalga chiqarilmadi.")
    finally:
        await client.disconnect()

