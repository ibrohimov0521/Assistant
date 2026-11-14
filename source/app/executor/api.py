from __future__ import annotations

import asyncio
import hmac
import logging
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field

from app.core.config import Settings
from app.bot.realtime_voice import install_realtime_voice
from app.core.telegram_ids import TelegramChatId
from app.storage.db import Database
from app.storage.personal import PersonalRecords
from app.speech.tts import OpenAITTS
from app.storage.telegram_calls import TelegramCallStore

LOG = logging.getLogger(__name__)


class DispatchRequest(BaseModel):
    lease_token: str = Field(min_length=16, max_length=100)


class ResultRequest(DispatchRequest):
    status: Literal["sent", "failed", "unknown"]
    category: str | None = Field(default=None, max_length=40)


class VoiceRequest(DispatchRequest):
    pass


class GroupWelcomeRuleRequest(BaseModel):
    chat_id: TelegramChatId
    chat_name: str = Field(min_length=1, max_length=160)
    body: str | None = Field(default=None, max_length=1000)
    enabled: bool


class GroupWelcomeClaimRequest(BaseModel):
    chat_id: TelegramChatId
    event_key: str = Field(min_length=1, max_length=120)
    user_id: int = Field(gt=0)
    user_name: str = Field(default="", max_length=100)


class GroupWelcomeResultRequest(BaseModel):
    event_key: str = Field(min_length=1, max_length=120)
    status: Literal["sent", "failed", "unknown"]


def create_internal_app(settings: Settings, db: Database,
                        tts: OpenAITTS | None = None) -> FastAPI:
    api = FastAPI(title="Private executor API", docs_url=None, redoc_url=None, openapi_url=None)
    install_realtime_voice(api, settings, db)

    def require_token(authorization: str | None) -> None:
        expected = settings.internal_token
        actual = authorization.removeprefix("Bearer ") if authorization else ""
        if not expected or not hmac.compare_digest(actual, expected):
            raise HTTPException(status_code=401, detail="Unauthorized")

    @api.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @api.post("/internal/v1/jobs/claim")
    async def claim(authorization: str | None = Header(default=None)):
        require_token(authorization)
        if not settings.enable_profile_executor:
            return Response(status_code=204)
        job = db.claim_action(settings.profile_lease_seconds)
        if job is None:
            return Response(status_code=204)
        recipient = job.get("recipient") or {}
        operation = str(job.get("operation") or "send_message")
        params = job.get("params") or {}
        return {
            "job_id": job["job_id"],
            "lease_token": job["lease_token"],
            "expires_at": job["expires_at"],
            "owner_id": job["owner_id"],
            "sender_profile_id": settings.expected_profile_id,
            "operation": operation,
            "params": params,
            "recipient_id": recipient.get("user_id"),
            "recipient_username": recipient.get("username"),
            "recipient_display_name": recipient.get("display_name"),
            "body": job.get("body") or params.get("body"),
            "random_id": job["random_id"],
        }

    @api.post("/internal/v1/jobs/{job_id}/dispatch")
    async def dispatch(job_id: str, request: DispatchRequest,
                       authorization: str | None = Header(default=None)) -> dict[str, bool]:
        require_token(authorization)
        if not db.mark_dispatching(job_id, request.lease_token):
            raise HTTPException(status_code=409, detail="Lease eskirgan yoki ishlatilgan")
        return {"ok": True}

    @api.post("/internal/v1/jobs/{job_id}/voice")
    async def voice(job_id: str, request: VoiceRequest,
                    authorization: str | None = Header(default=None)) -> Response:
        require_token(authorization)
        if not settings.enable_profile_executor or not settings.enable_tts or tts is None:
            raise HTTPException(status_code=503, detail="Voice delivery is unavailable")
        payload = db.voice_job_payload(job_id, request.lease_token)
        if not payload:
            raise HTTPException(status_code=409, detail="Voice job lease is invalid")
        try:
            personal = PersonalRecords(db, settings.owner_telegram_id)
            preferences = personal.preferences()
            audio = await tts.synthesize_ogg(
                "Bu xabardagi ovoz sun’iy intellekt tomonidan yaratildi. " + payload["body"],
                pronunciations=personal.pronunciations(),
                speed=preferences.get("voice_speed", "normal"))
        except Exception as exc:
            LOG.warning("Scheduled voice synthesis failed category=%s", type(exc).__name__)
            raise HTTPException(status_code=503, detail="Voice synthesis failed") from exc
        return Response(content=audio, media_type="audio/ogg",
                        headers={"Cache-Control": "no-store"})

    @api.post("/internal/v1/jobs/{job_id}/call-claim")
    async def claim_call(job_id: str, request: VoiceRequest,
                         authorization: str | None = Header(default=None)):
        require_token(authorization)
        if not settings.enable_profile_executor:
            raise HTTPException(status_code=503, detail="Profile executor is disabled")
        payload = db.call_job_payload(job_id, request.lease_token)
        if not payload:
            raise HTTPException(status_code=409, detail="Call job lease is invalid")
        claimed = TelegramCallStore(db).claim(owner_id=settings.owner_telegram_id,
            attempt_id=str(payload["attempt_id"]),
            confirmed_recipient_id=int(payload["recipient_id"]))
        if claimed.get("status") != "calling":
            raise HTTPException(status_code=409, detail="Call approval is unavailable")
        return {"recipient_id": int(claimed["recipient_id"]),
                "recipient_name": str(claimed["recipient_name"]),
                "body": str(claimed["body"])}

    @api.post("/internal/v1/jobs/{job_id}/result")
    async def result(job_id: str, request: ResultRequest,
                     authorization: str | None = Header(default=None)) -> dict[str, bool]:
        require_token(authorization)
        accepted = db.finish_action(job_id, request.lease_token, request.status,
                                    request.category, settings.owner_chat_id)
        if not accepted:
            raise HTTPException(status_code=409, detail="Natija lease bilan mos kelmadi")
        LOG.info("profile action result job=%s status=%s category=%s", job_id[:10], request.status, request.category)
        return {"ok": True}

    @api.post("/internal/v1/group-welcome/rule")
    async def save_group_welcome_rule(request: GroupWelcomeRuleRequest,
                                      authorization: str | None = Header(default=None)):
        require_token(authorization)
        if not settings.enable_profile_executor:
            raise HTTPException(status_code=503, detail="Profile executor is disabled")
        try:
            return db.set_group_welcome_rule(
                settings.owner_telegram_id, request.chat_id, request.chat_name,
                request.body, enabled=request.enabled)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @api.post("/internal/v1/group-welcome/claim")
    async def claim_group_welcome(request: GroupWelcomeClaimRequest,
                                  authorization: str | None = Header(default=None)):
        require_token(authorization)
        if not settings.enable_profile_executor:
            return Response(status_code=204)
        rule = db.claim_group_welcome_event(settings.owner_telegram_id, request.chat_id,
            request.event_key, request.user_id, request.user_name)
        if rule is None:
            return Response(status_code=204)
        return {"chat_name": rule.get("chat_name", ""), "body": rule.get("body", "")}

    @api.post("/internal/v1/group-welcome/finish")
    async def finish_group_welcome(request: GroupWelcomeResultRequest,
                                   authorization: str | None = Header(default=None)):
        require_token(authorization)
        if not settings.enable_profile_executor:
            raise HTTPException(status_code=503, detail="Profile executor is disabled")
        accepted = db.finish_group_welcome_event(
            settings.owner_telegram_id, request.event_key, request.status)
        if not accepted:
            raise HTTPException(status_code=409, detail="Welcome event state is unavailable")
        return {"ok": True}

    return api

