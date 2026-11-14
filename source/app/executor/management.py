from __future__ import annotations

import hmac
import io
import math
import secrets
import base64
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field
from telethon import TelegramClient, utils
from telethon.errors import RPCError
from telethon.tl.functions.channels import EditBannedRequest
from telethon.tl.functions.account import UpdateNotifySettingsRequest
from telethon.tl.functions.messages import GetForumTopicsRequest, SendMediaRequest, SendMessageRequest
from telethon.tl.types import (ChatBannedRights, InputMessagesFilterDocument, InputMessagesFilterPhotos,
                               InputMessagesFilterVideo, InputMessagesFilterVoice, InputMessagesFilterMusic,
                               InputNotifyPeer,
                               InputPeerNotifySettings, InputReplyToMessage,
                               InputGeoPoint, InputMediaGeoPoint, InputMediaContact,
                               InputMediaPoll, Poll, PollAnswer, TextWithEntities)

from app.core.config import Settings
from app.core.telegram_ids import TelegramChatId


class ResolveChatRequest(BaseModel):
    query: str = Field(min_length=1, max_length=100)


class ChatMessagesRequest(BaseModel):
    chat_name: str | None = Field(default=None, min_length=1, max_length=100)
    chat_id: TelegramChatId | None = None
    limit: int = Field(default=10, ge=1, le=100)
    since: datetime | None = None
    until: datetime | None = None


class SearchMessagesRequest(BaseModel):
    query: str = Field(min_length=1, max_length=120)
    limit: int = Field(default=5, ge=1, le=10)


class ChatScanRequest(BaseModel):
    chat_name: str | None = Field(default=None, min_length=1, max_length=100)
    priority_person: str | None = Field(default=None, min_length=1, max_length=100)
    priority_phrase: str | None = Field(default=None, min_length=1, max_length=120)
    limit: int = Field(default=10, ge=1, le=20)
    days: int = Field(default=7, ge=1, le=30)


class FileSearchRequest(BaseModel):
    query: str | None = Field(default=None, max_length=120)
    chat_name: str | None = Field(default=None, min_length=1, max_length=100)
    sender: str | None = Field(default=None, min_length=1, max_length=100)
    date_from: str | None = Field(default=None, max_length=10)
    date_to: str | None = Field(default=None, max_length=10)
    file_type: str = Field(default="document", max_length=16)
    limit: int = Field(default=5, ge=1, le=20)


class FileDownloadRequest(BaseModel):
    query: str = Field(min_length=1, max_length=120)
    chat_name: str | None = Field(default=None, min_length=1, max_length=100)
    message_id: int | None = Field(default=None, gt=0)


class MessagePreviewRequest(BaseModel):
    chat_id: TelegramChatId
    message_id: int = Field(gt=0)


class MediaPermissionRequest(BaseModel):
    chat_id: TelegramChatId
    media_type: str = Field(min_length=1, max_length=30)


class TextPermissionRequest(BaseModel):
    chat_id: TelegramChatId


class ForumTopicsRequest(BaseModel):
    chat_id: TelegramChatId


class ModerationPermissionRequest(BaseModel):
    chat_id: TelegramChatId
    action: str = Field(min_length=1, max_length=30)
    message_id: int | None = Field(default=None, gt=0)


class SpamScanRequest(BaseModel):
    chat_name: str = Field(min_length=1, max_length=100)
    days: int = Field(default=7, ge=1, le=30)
    limit: int = Field(default=10, ge=1, le=20)


class ProfileQueryError(Exception):
    def __init__(self, status_code: int, detail: Any):
        self.status_code = status_code
        self.detail = detail


def _dialog_payload(dialog: Any) -> dict[str, Any]:
    entity = dialog.entity
    return {"id": int(dialog.id), "name": str(dialog.name or "Noma’lum")[:160],
            "username": str(getattr(entity, "username", "") or "")[:64],
            "unread_count": int(getattr(dialog, "unread_count", 0) or 0),
            "is_user": bool(getattr(entity, "id", None) and entity.__class__.__name__ == "User")}


def _message_media_summary(message: Any) -> dict[str, Any] | None:
    document = getattr(message, "document", None)
    if document is not None:
        mime_type = str(getattr(document, "mime_type", "") or "")[:100]
        attributes = list(getattr(document, "attributes", None) or [])
        file_name = next((str(getattr(item, "file_name", "")) for item in attributes
                          if getattr(item, "file_name", None)), "")[:180]
        kind = "video" if mime_type.startswith("video/") or any(
            type(item).__name__ == "DocumentAttributeVideo" for item in attributes) else "document"
        return {"type": kind, "name": file_name or ("Video" if kind == "video" else "Hujjat"),
                "mime_type": mime_type, "size": max(0, int(getattr(document, "size", 0) or 0))}
    photo = getattr(message, "photo", None)
    if photo is not None:
        sizes = list(getattr(photo, "sizes", None) or [])
        size = max((int(getattr(item, "size", 0) or 0) for item in sizes), default=0)
        return {"type": "photo", "name": "Surat", "mime_type": "image/jpeg", "size": size}
    geo = getattr(message, "geo", None)
    if geo is not None:
        return {"type": "location", "name": "Joylashuv", "latitude": getattr(geo, "lat", None),
                "longitude": getattr(geo, "long", None)}
    contact = getattr(message, "contact", None)
    if contact is not None:
        name = " ".join(str(getattr(contact, key, "") or "").strip()
                        for key in ("first_name", "last_name")).strip()[:120]
        return {"type": "contact", "name": name or "Kontakt",
                "phone": str(getattr(contact, "phone_number", "") or "")[:80]}
    poll_message = getattr(message, "poll", None)
    poll = getattr(poll_message, "poll", None) if poll_message is not None else None
    if poll is not None:
        question = str(getattr(poll, "question", "") or "So‘rovnoma")[:180]
        return {"type": "poll", "name": question}
    if getattr(message, "media", None) is not None:
        return {"type": "media", "name": "Telegram media", "size": 0}
    return None


class ProfileManager:
    """Read and execute narrowly scoped actions on an already authorized profile."""

    def __init__(self, client: TelegramClient, expected_profile_id: int):
        self.client = client
        self.expected_profile_id = expected_profile_id

    async def list_chats(self, limit: int) -> dict[str, Any]:
        rows = []
        async for dialog in self.client.iter_dialogs(limit=min(max(limit, 1), 30)):
            rows.append(_dialog_payload(dialog))
        return {"chats": rows}

    async def _all_dialogs(self, limit: int = 500) -> list[Any]:
        return [dialog async for dialog in self.client.iter_dialogs(limit=limit)]

    async def _find_dialog(self, query: str) -> Any:
        query = query.strip().lstrip("@").casefold()
        if not query:
            raise ProfileQueryError(404, "Chat nomi bo‘sh.")
        dialogs = await self._all_dialogs()
        exact = [d for d in dialogs if str(getattr(d.entity, "username", "") or "").casefold() == query]
        if not exact:
            exact = [d for d in dialogs if str(d.name or "").strip().casefold() == query]
        matches = exact or [d for d in dialogs if query in str(d.name or "").casefold()]
        if not matches:
            raise ProfileQueryError(404, "Chat topilmadi. Faqat profilingizda mavjud chatlar qidiriladi.")
        if len(matches) != 1:
            raise ProfileQueryError(409, {"message": "Bir nechta chat mos keldi.",
                                          "matches": [_dialog_payload(d) for d in matches[:8]]})
        return matches[0]

    async def resolve_chat(self, query: str) -> dict[str, Any]:
        dialog = await self._find_dialog(query)
        return {"chat": _dialog_payload(dialog)}

    async def forum_topics(self, chat_id: int) -> dict[str, Any]:
        dialog = await self._dialog_by_id(chat_id)
        entity = dialog.entity
        if entity.__class__.__name__ != "Channel" or not bool(getattr(entity, "megagroup", False)):
            return {"error": "Mavzular faqat forum yoqilgan Telegram superguruhida mavjud."}
        try:
            peer = await self.client.get_input_entity(entity)
            response = await self.client(GetForumTopicsRequest(
                peer=peer, offset_date=None, offset_id=0, offset_topic=0, limit=100))
        except (RPCError, TypeError, ValueError, AttributeError) as exc:
            return {"error": "Bu guruh mavzularini Telegram’dan o‘qib bo‘lmadi.",
                    "category": type(exc).__name__[:40]}
        topics = []
        for topic in list(getattr(response, "topics", None) or [])[:100]:
            topic_id = getattr(topic, "id", None)
            title = " ".join(str(getattr(topic, "title", "") or "").split())
            if isinstance(topic_id, int) and topic_id > 0 and title:
                topics.append({"id": topic_id, "title": title[:128],
                               "closed": bool(getattr(topic, "closed", False))})
        return {"chat_id": int(dialog.id), "chat_name": str(dialog.name or "Noma’lum"),
                "topics": topics}

    async def check_moderation_permission(self, chat_id: int, action: str) -> dict[str, Any]:
        required_right = {"delete_group_message": "delete_messages",
                          "mute_group_member": "ban_users",
                          "ban_group_member": "ban_users"}.get(action)
        if required_right is None:
            return {"allowed": False, "reason": "Moderatsiya amal turi tan olinmadi."}
        try:
            dialog = await self._dialog_by_id(chat_id)
        except ProfileQueryError:
            return {"allowed": False, "reason": "Guruh profilingizda topilmadi."}
        entity = dialog.entity
        if entity.__class__.__name__ != "Channel" or not bool(getattr(entity, "megagroup", False)):
            return {"allowed": False, "reason": "Moderatsiya faqat superguruhda qo‘llab-quvvatlanadi."}
        try:
            permissions = await self.client.get_permissions(entity, self.expected_profile_id)
        except (RPCError, TypeError, ValueError, AttributeError):
            return {"allowed": False, "reason": "Telegram administrator huquqini tasdiqlay olmadi."}
        if permissions is None or bool(getattr(permissions, "has_left", False)):
            return {"allowed": False, "reason": "Profilingiz guruh a’zosi emas yoki huquq holati noma’lum."}
        if bool(getattr(permissions, "is_creator", False)):
            return {"allowed": True, "reason": "Guruh egasining huquqi tasdiqlandi."}
        participant = getattr(permissions, "participant", None)
        rights = getattr(participant, "admin_rights", None)
        if bool(getattr(permissions, "is_admin", False)) and bool(getattr(rights, required_right, False)):
            return {"allowed": True, "reason": f"Administrator huquqi tasdiqlandi: {required_right}."}
        return {"allowed": False, "reason": f"Moderatsiya uchun Telegram {required_right} huquqini bermagan."}

    async def check_moderation_target(self, chat_id: int, message_id: int) -> dict[str, Any]:
        try:
            dialog = await self._dialog_by_id(chat_id)
            message = await self.client.get_messages(dialog.entity, ids=message_id)
        except (ProfileQueryError, RPCError, TypeError, ValueError):
            message = None
            dialog = None
        if dialog is None or message is None or bool(getattr(message, "out", False)):
            return {"allowed": False, "reason": "Guruhdagi aniq kiruvchi xabar yoki uning muallifi topilmadi."}
        try:
            target = await message.get_sender()
        except (RPCError, TypeError, ValueError):
            target = None
        if target is None or target.__class__.__name__ != "User":
            return {"allowed": False, "reason": "Tanlangan xabar muallifini ishonchli aniqlay olmadim."}
        if int(target.id) == self.expected_profile_id:
            return {"allowed": False, "reason": "Profil egasini guruhdan cheklash xavfsizlik uchun bloklangan."}
        try:
            target_permissions = await self.client.get_permissions(dialog.entity, target)
        except (RPCError, TypeError, ValueError, AttributeError):
            return {"allowed": False, "reason": "Xabar muallifining guruh huquqini tekshira olmadim."}
        if target_permissions is None or bool(getattr(target_permissions, "has_left", False)) or bool(
                getattr(target_permissions, "is_banned", False)):
            return {"allowed": False, "reason": "Xabar muallifi endi guruhning faol a’zosi emas."}
        if bool(getattr(target_permissions, "is_creator", False)) or bool(
                getattr(target_permissions, "is_admin", False)):
            return {"allowed": False, "reason": "Guruh egasi yoki administratorni ushbu amal bilan cheklamayman."}
        return {"allowed": True, "user_id": int(target.id),
                "user_name": " ".join(str(getattr(target, "first_name", "") or "").split())[:80],
                "username": str(getattr(target, "username", "") or "")[:64]}

    async def scan_group_spam(self, chat_name: str, days: int, limit: int) -> dict[str, Any]:
        dialog = await self._find_dialog(chat_name)
        entity = dialog.entity
        if entity.__class__.__name__ != "Channel" or not bool(getattr(entity, "megagroup", False)):
            return {"error": "Spam tekshiruvi faqat superguruh chatlarida ishlaydi."}
        import re
        cutoff = datetime.now(UTC) - timedelta(days=min(max(days, 1), 30))
        rows: list[tuple[Any, str]] = []
        normalized_counts: dict[str, int] = {}
        async for message in self.client.iter_messages(entity, limit=300):
            if message.date:
                message_time = (message.date.replace(tzinfo=UTC) if message.date.tzinfo is None
                                else message.date.astimezone(UTC))
                if message_time < cutoff:
                    break
            text = " ".join(str(message.message or "").split())[:700]
            if not text:
                continue
            key = re.sub(r"https?://\S+", "<url>", text.casefold())
            key = " ".join(key.split())
            rows.append((message, text))
            normalized_counts[key] = normalized_counts.get(key, 0) + 1
        candidates = []
        trigger = re.compile(r"\b(?:invest(?:ment)?|crypto|bitcoin|giveaway|airdrop|bonus|yutuq|sovg'a|sovg‘a|tez boy|pul ishlash|garant)\b", re.I)
        for message, text in rows:
            reasons = []
            key = re.sub(r"https?://\S+", "<url>", text.casefold())
            key = " ".join(key.split())
            has_link = bool(re.search(r"https?://|t\.me/|www\.", text, re.I))
            if normalized_counts.get(key, 0) >= 3:
                reasons.append("bir xil mazmundagi xabar bir necha marta yuborilgan")
            if has_link and trigger.search(text):
                reasons.append("havola bilan reklama yoki moliyaviy va’da iboralari bor")
            if has_link and bool(re.search(r"\b(?:claim|free|bonus|airdrop|yutuq|sovg'a|pul ishlash)\b", text, re.I)):
                reasons.append("havola va shubhali taklif so‘zlari birga kelgan")
            if not reasons:
                continue
            try:
                sender = await message.get_sender()
            except (RPCError, ValueError, TypeError):
                sender = None
            sender_id = int(sender.id) if sender is not None and sender.__class__.__name__ == "User" else None
            sender_name = " ".join(str(getattr(sender, "first_name", "") or "").split())[:80] if sender else "Noma’lum"
            username = str(getattr(sender, "username", "") or "")[:64] if sender else ""
            candidates.append({"message_id": int(message.id), "sender_id": sender_id,
                               "sender_name": sender_name, "username": username,
                               "date": message.date.isoformat() if message.date else "",
                               "reasons": reasons, "text": text[:300]})
        candidates.sort(key=lambda item: (len(item["reasons"]), item["date"]), reverse=True)
        return {"chat_name": str(dialog.name or "Noma’lum"), "days": min(max(days, 1), 30),
                "candidates": candidates[:min(max(limit, 1), 20)],
                "note": "Bu evristik tekshiruv; xabarlar avtomatik o‘chirilmaydi yoki foydalanuvchilar bloklanmaydi."}

    async def _dialog_by_id(self, chat_id: int) -> Any:
        async for dialog in self.client.iter_dialogs(limit=1000):
            if int(dialog.id) == int(chat_id):
                return dialog
        raise ProfileQueryError(404, "Chat profilingizdagi oxirgi chatlar ro‘yxatidan topilmadi.")

    async def chat_messages(self, chat_name: str | None, limit: int, chat_id: int | None = None,
                            since: datetime | None = None, until: datetime | None = None) -> dict[str, Any]:
        if since and since.tzinfo is None or until and until.tzinfo is None:
            raise ProfileQueryError(422, "Sana UTC ofseti bilan bo‘lishi kerak.")
        if since and until and since >= until:
            raise ProfileQueryError(422, "Sana oralig‘i noto‘g‘ri.")
        if chat_id is not None:
            dialog = await self._dialog_by_id(chat_id)
        elif chat_name:
            dialog = await self._find_dialog(chat_name)
        else:
            raise ProfileQueryError(422, "Chat nomi yoki ID si kerak.")
        rows = []
        async for message in self.client.iter_messages(dialog.entity, limit=min(max(limit, 1), 100)):
            if message.date and since and message.date < since:
                break
            if message.date and until and message.date >= until:
                continue
            if message.message or message.media:
                sender = "Siz" if message.out else str(message.sender_id or "Noma’lum")
                rows.append({"id": int(message.id), "date": message.date.isoformat() if message.date else "",
                             "sender": sender, "text": (message.message or "[media]")[:500]})
        return {"chat_name": str(dialog.name or "Noma’lum"), "messages": rows}

    async def search_messages(self, query: str, limit: int) -> dict[str, Any]:
        dialogs = await self._all_dialogs(limit=500)
        names = {int(dialog.id): str(dialog.name or "Noma’lum")[:120] for dialog in dialogs}
        rows = []
        async for message in self.client.iter_messages(None, search=query, limit=min(max(limit, 1), 10)):
            rows.append({"chat_name": names.get(int(message.chat_id or 0), "Telegram chat"),
                         "id": int(message.id), "date": message.date.isoformat() if message.date else "",
                         "sender": "Siz" if message.out else str(message.sender_id or "Noma’lum"),
                         "text": str(message.message or "[media]")[:400]})
        return {"results": rows}

    async def search_files(self, query: str | None, limit: int, *, chat_name: str | None = None,
                           sender: str | None = None, date_from: str | None = None,
                           date_to: str | None = None, file_type: str = "document") -> dict[str, Any]:
        query = " ".join(str(query or "").split())[:120] or None
        sender_query = " ".join(str(sender or "").split())[:100] or None
        chat_name = " ".join(str(chat_name or "").split())[:100] or None
        if file_type not in {"any", "document", "photo", "video", "audio", "voice"}:
            return {"error": "Fayl turi any, document, photo, video, audio yoki voice bo‘lishi kerak."}
        if not query and not sender_query and not chat_name and file_type == "any":
            return {"error": "Global qidiruv uchun so‘z, jo‘natuvchi yoki fayl turini aniqlang."}
        try:
            local_tz = ZoneInfo("Asia/Tashkent")
            start_day = date.fromisoformat(date_from) if date_from else None
            end_day = date.fromisoformat(date_to) if date_to else None
        except (ValueError, TypeError):
            return {"error": "Sana filtrlari YYYY-MM-DD formatida bo‘lishi kerak."}
        if start_day and end_day and start_day > end_day:
            return {"error": "Boshlanish sanasi tugash sanasidan keyin bo‘la olmaydi."}
        start_at = (datetime.combine(start_day, time.min, tzinfo=local_tz).astimezone(UTC)
                    if start_day else None)
        end_exclusive = (datetime.combine(end_day + timedelta(days=1), time.min, tzinfo=local_tz).astimezone(UTC)
                         if end_day else None)
        sender_entity = None
        if sender_query:
            try:
                sender_entity = await self.client.get_entity(
                    int(sender_query) if sender_query.isdigit() else sender_query.lstrip("@"))
            except (RPCError, TypeError, ValueError, AttributeError):
                return {"error": "Jo‘natuvchi topilmadi. Username yoki raqamli Telegram ID ni ishlating."}
        filters = {
            "document": InputMessagesFilterDocument(),
            "photo": InputMessagesFilterPhotos(),
            "video": InputMessagesFilterVideo(),
            "audio": InputMessagesFilterMusic(),
            "voice": InputMessagesFilterVoice(),
            "any": None,
        }
        dialogs = await self._all_dialogs(limit=500)
        names = {int(dialog.id): str(dialog.name or "Noma’lum")[:120] for dialog in dialogs}
        if chat_name:
            try:
                dialog = await self._find_dialog(chat_name)
            except ProfileQueryError as exc:
                return {"error": str(exc.detail)}
            entities = [dialog.entity]
            names[int(dialog.id)] = str(dialog.name or chat_name)[:120]
        else:
            entities = [None]
        rows = []
        for entity in entities:
            kwargs: dict[str, Any] = {
                "search": query, "filter": filters[file_type],
                "limit": min(max(limit * 20, 50), 400),
            }
            if sender_entity is not None:
                kwargs["from_user"] = sender_entity
            if end_exclusive is not None:
                kwargs["offset_date"] = end_exclusive
            async for message in self.client.iter_messages(entity, **kwargs):
                stamped = getattr(message, "date", None)
                if stamped is not None:
                    stamped = stamped if stamped.tzinfo else stamped.replace(tzinfo=UTC)
                    stamped = stamped.astimezone(UTC)
                    if start_at and stamped < start_at:
                        # Results are newest-first, so older messages cannot match either.
                        break
                    if end_exclusive and stamped >= end_exclusive:
                        continue
                elif start_at or end_exclusive:
                    continue
                media = getattr(message, "media", None)
                document = getattr(media, "document", None)
                photo = getattr(media, "photo", None)
                attributes = getattr(document, "attributes", None) or []
                has_video = any(type(attribute).__name__ == "DocumentAttributeVideo" for attribute in attributes)
                audio_attributes = [attribute for attribute in attributes
                                    if type(attribute).__name__ == "DocumentAttributeAudio"]
                is_voice = any(bool(getattr(attribute, "voice", False)) for attribute in audio_attributes)
                mime_type = str(getattr(document, "mime_type", "") or "").casefold()
                kind = ("photo" if photo is not None else "voice" if is_voice else "audio" if audio_attributes
                        or mime_type.startswith("audio/") else "video" if has_video or mime_type.startswith("video/")
                        else "document" if document is not None else None)
                if kind is None or (file_type != "any" and kind != file_type):
                    continue
                filename = next((str(getattr(attribute, "file_name", ""))
                                 for attribute in attributes if getattr(attribute, "file_name", None)), "")
                filename = filename or {"photo": "Rasm", "video": "Video", "audio": "Audio",
                                        "voice": "Ovozli xabar"}.get(kind, "Fayl nomi yo‘q")
                rows.append({"chat_id": int(message.chat_id or 0),
                             "chat_name": names.get(int(message.chat_id or 0), "Telegram chat"),
                             "message_id": int(message.id), "date": stamped.isoformat() if stamped else "",
                             "sender": "Siz" if message.out else str(message.sender_id or "Noma’lum"),
                             "filename": filename[:180], "file_type": kind,
                             "size": int(getattr(document, "size", 0) or 0),
                             "caption": str(message.message or "")[:200]})
                if len(rows) >= limit:
                    break
            if len(rows) >= limit:
                break
        return {"files": rows}

    async def download_file(self, query: str, chat_name: str | None = None,
                            message_id: int | None = None) -> dict[str, Any]:
        """Download one unambiguous document from the connected profile, bounded to 20 MiB."""
        from urllib.parse import quote

        max_bytes = 20 * 1024 * 1024
        dialogs = await self._all_dialogs(limit=500)
        names = {int(dialog.id): str(dialog.name or "Noma’lum")[:120] for dialog in dialogs}
        if message_id is not None:
            if not chat_name:
                return {"error": "Xabar ID bilan fayl olish uchun aniq chat nomi ham kerak."}
            dialog = await self._find_dialog(chat_name)
            message = await self.client.get_messages(dialog.entity, ids=message_id)
            messages = [message] if message is not None else []
        elif chat_name:
            dialog = await self._find_dialog(chat_name)
            messages = [message async for message in self.client.iter_messages(
                dialog.entity, search=query, filter=InputMessagesFilterDocument(), limit=10)]
        else:
            messages = [message async for message in self.client.iter_messages(
                None, search=query, filter=InputMessagesFilterDocument(), limit=10)]

        candidates = []
        for message in messages:
            document = getattr(getattr(message, "media", None), "document", None)
            if document is None:
                continue
            attributes = getattr(document, "attributes", None) or []
            raw_name = next((str(getattr(item, "file_name", "")) for item in attributes
                             if getattr(item, "file_name", None)), "Fayl")
            filename = raw_name.replace("\\", "/").split("/")[-1]
            filename = "".join(char for char in filename if char.isprintable() and char not in '\"<>|')[:150]
            filename = filename or "telegram-file"
            chat_id = int(message.chat_id or getattr(getattr(message, "peer_id", None), "channel_id", 0) or
                          getattr(getattr(message, "peer_id", None), "chat_id", 0) or
                          getattr(getattr(message, "peer_id", None), "user_id", 0) or 0)
            candidates.append((message, document, filename, chat_id))

        if not candidates:
            return {"error": "Shu qidiruvga mos yuklab olinadigan fayl topilmadi."}
        if len(candidates) > 1:
            return {"error": "Bir nechta fayl mos keldi. Keraklisini chat nomi va xabar ID si bilan tanlang.",
                    "candidates": [{"chat_id": chat_id,
                                    "chat_name": names.get(chat_id, str(chat_name or "Telegram chat")),
                                    "message_id": int(message.id), "filename": filename,
                                    "size": int(getattr(document, "size", 0) or 0)}
                                   for message, document, filename, chat_id in candidates[:10]]}

        message, document, filename, chat_id = candidates[0]
        declared_size = int(getattr(document, "size", 0) or 0)
        if declared_size > max_bytes:
            return {"error": "Fayl 20 MB dan katta. Server xotirasini va Telegram bot cheklovlarini asrash uchun yuklamadim."}
        content = await self.client.download_media(message, bytes)
        if not isinstance(content, bytes):
            return {"error": "Telegram faylni baytlar ko‘rinishida qaytarmadi."}
        if len(content) > max_bytes:
            return {"error": "Yuklangan fayl 20 MB dan katta chiqdi; uni yubormadim."}
        mime_type = str(getattr(document, "mime_type", "") or "application/octet-stream")[:100]
        if not all(char.isprintable() for char in mime_type):
            mime_type = "application/octet-stream"
        return {"filename": filename, "mime_type": mime_type, "size": len(content),
                "chat_id": chat_id, "chat_name": names.get(chat_id, str(chat_name or "Telegram chat")),
                "message_id": int(message.id), "content": content}

    async def important_messages(self, chat_name: str | None, limit: int, days: int,
                                 priority_person: str | None = None,
                                 priority_phrase: str | None = None) -> dict[str, Any]:
        import re
        from datetime import timedelta

        dialogs = [await self._find_dialog(chat_name)] if chat_name else await self._all_dialogs(limit=30)
        owner = await self.client.get_me()
        username = str(getattr(owner, "username", "") or "").casefold()
        cutoff = datetime.now(UTC) - timedelta(days=min(max(days, 1), 30))
        deadline = re.compile(r"\b(ertaga|bugun|indin|muddat|deadline|soat\s*\d|\d{1,2}[./-]\d{1,2})\b", re.I)
        urgent = re.compile(r"\b(muhim|shoshilinch|tezroq|zudlik bilan|urgent)\b", re.I)
        wanted_person = " ".join(str(priority_person or "").casefold().split())
        wanted_phrase = " ".join(str(priority_phrase or "").casefold().split())
        sender_cache: dict[int, str] = {}
        rows: list[dict[str, Any]] = []
        for dialog in dialogs:
            async for message in self.client.iter_messages(dialog.entity, limit=40):
                if not message.date or message.date < cutoff:
                    break
                text = str(message.message or "")
                reasons = []
                if not message.out and "?" in text:
                    reasons.append("savol")
                if not message.out and deadline.search(text):
                    reasons.append("muddat yoki vaqt")
                if not message.out and urgent.search(text):
                    reasons.append("muhimlik belgisi")
                if not message.out and message.media:
                    reasons.append("fayl yoki media")
                if not message.out and username and f"@{username}" in text.casefold():
                    reasons.append("sizni belgilagan")
                if not message.out and any(getattr(entity, "user_id", None) == self.expected_profile_id
                                           for entity in (getattr(message, "entities", None) or [])):
                    reasons.append("sizni belgilagan")
                if not message.out and wanted_phrase and wanted_phrase in " ".join(text.casefold().split()):
                    reasons.append("ustuvor ibora")
                if not message.out and wanted_person:
                    dialog_name = " ".join(str(dialog.name or "").casefold().split())
                    sender_id = getattr(message, "sender_id", None)
                    person_match = wanted_person in dialog_name or wanted_person == str(sender_id or "")
                    if not person_match and sender_id:
                        try:
                            sender_id_int = int(sender_id)
                        except (TypeError, ValueError):
                            sender_id_int = 0
                        if sender_id_int and sender_id_int not in sender_cache:
                            sender = getattr(message, "sender", None)
                            get_sender = getattr(message, "get_sender", None)
                            if sender is None and callable(get_sender):
                                try:
                                    sender = await get_sender()
                                except Exception:
                                    sender = None
                            sender_cache[sender_id_int] = " ".join(str(value or "") for value in (
                                getattr(sender, "first_name", ""), getattr(sender, "last_name", ""),
                                getattr(sender, "username", "")) if value).casefold()
                        sender_label = sender_cache.get(sender_id_int, "") if sender_id_int else ""
                        person_match = wanted_person in sender_label
                    if person_match:
                        reasons.append("ustuvor shaxs")
                if reasons:
                    rows.append({"chat_name": str(dialog.name or "Noma’lum")[:120],
                                 "message_id": int(message.id),
                                 "date": message.date.isoformat(),
                                 "sender": str(message.sender_id or "Noma’lum"),
                                 "text": text[:400] or "[media yoki matnsiz xabar]",
                                 "reasons": reasons,
                                 "_priority": (reasons.count("ustuvor shaxs") + reasons.count("ustuvor ibora")) * 5
                                             + len(reasons)})
                    if len(rows) >= limit and not (wanted_person or wanted_phrase):
                        break
            if len(rows) >= limit and not (wanted_person or wanted_phrase):
                break
        rows.sort(key=lambda row: (int(row.get("_priority") or 0), str(row.get("date") or "")), reverse=True)
        for row in rows:
            row.pop("_priority", None)
        return {"messages": rows[:limit], "days": min(max(days, 1), 30),
                "priority_person": priority_person, "priority_phrase": priority_phrase,
                "chat_name": chat_name}

    async def unanswered_chats(self, limit: int, days: int) -> dict[str, Any]:
        from datetime import timedelta

        cutoff = datetime.now(UTC) - timedelta(days=min(max(days, 1), 30))
        owner = await self.client.get_me()
        username = str(getattr(owner, "username", "") or "").casefold()
        needs_reply, waiting = [], []
        for dialog in await self._all_dialogs(limit=100):
            messages = []
            async for message in self.client.iter_messages(dialog.entity, limit=20):
                if not message.date or message.date < cutoff:
                    break
                if message.message or message.media:
                    messages.append(message)
            if not messages:
                continue
            latest = messages[0]
            entity = getattr(dialog, "entity", None)
            is_user = bool(entity and entity.__class__.__name__ == "User")
            text = str(latest.message or "")
            addressed_in_group = bool(username and f"@{username}" in text.casefold())
            reply_to_outgoing = False
            reply_target_ids = []
            for attr in ("reply_to_msg_id", "reply_to_top_id"):
                value = getattr(latest, attr, None)
                if value and value not in reply_target_ids:
                    reply_target_ids.append(value)
            for reply_target_id in reply_target_ids:
                replied = next((item for item in messages if item.id == reply_target_id), None)
                if replied is None:
                    try:
                        replied = await self.client.get_messages(dialog.entity, ids=reply_target_id)
                    except Exception:
                        replied = None
                if replied and replied.out:
                    reply_to_outgoing = True
                    break
            entry = {"chat_name": str(dialog.name or "Noma’lum")[:120],
                     "message_id": int(latest.id), "date": latest.date.isoformat(),
                     "text": text[:300] or "[media yoki matnsiz xabar]"}
            if latest.out and is_user:
                waiting.append(entry)
            elif not latest.out and (is_user or addressed_in_group or reply_to_outgoing):
                needs_reply.append(entry)
        return {"needs_reply": needs_reply[:limit], "waiting_for_reply": waiting[:limit],
                "days": min(max(days, 1), 30), "group_scope": "faqat sizni belgilagan yoki javob qilingan xabarlar"}

    async def preview_message(self, chat_id: int, message_id: int) -> dict[str, Any]:
        dialog = await self._dialog_by_id(chat_id)
        message = await self.client.get_messages(dialog.entity, ids=message_id)
        if message is None:
            raise ProfileQueryError(404, "Xabar topilmadi.")
        message_ids = [int(message.id)]
        grouped_id = getattr(message, "grouped_id", None)
        if grouped_id:
            nearby = await self.client.get_messages(dialog.entity, min_id=max(0, int(message.id) - 10),
                                                     max_id=int(message.id) + 11, limit=20)
            candidates = nearby if isinstance(nearby, (list, tuple)) else [nearby]
            album = sorted({int(item.id) for item in candidates if item is not None
                            and getattr(item, "grouped_id", None) == grouped_id})
            if int(message.id) not in album:
                album.append(int(message.id))
                album.sort()
            if len(album) > 10:
                raise ProfileQueryError(422, "Telegram albomi 10 tadan ko‘p elementli; bitta xabarni tanlang.")
            message_ids = album
        source_messages = await self.client.get_messages(dialog.entity, ids=message_ids)
        if not isinstance(source_messages, (list, tuple)):
            source_messages = [source_messages]
        media = [_message_media_summary(item) for item in source_messages if item is not None]
        sender = getattr(message, "sender", None)
        if sender is None and hasattr(message, "get_sender"):
            try:
                sender = await message.get_sender()
            except (RPCError, TypeError, ValueError):
                sender = None
        is_user_sender = sender is not None and sender.__class__.__name__ == "User"
        return {"chat_name": str(dialog.name or "Noma’lum"), "message_id": int(message.id),
                "message_ids": message_ids, "media": media,
                "text": str(message.message or "[media yoki matnsiz xabar]")[:1000],
                "outgoing": bool(message.out),
                "sender_id": int(sender.id) if is_user_sender else None,
                "sender_name": " ".join(str(getattr(sender, "first_name", "") or "").split())[:80],
                "sender_username": str(getattr(sender, "username", "") or "")[:64]}

    async def check_media_permission(self, chat_id: int, media_type: str) -> dict[str, Any]:
        allowed_types = {"document", "photo", "video", "audio", "animation", "voice",
                         "video_note", "location", "contact", "poll"}
        if media_type not in allowed_types:
            return {"allowed": None, "reason": "Media turi uchun huquqni aniqlab bo‘lmadi."}
        try:
            dialog = await self._dialog_by_id(chat_id)
        except ProfileQueryError:
            return {"allowed": False, "reason": "Qabul qiluvchi chat profilingizda topilmadi."}
        entity = dialog.entity
        if entity.__class__.__name__ == "User":
            return {"allowed": True, "reason": "Shaxsiy chat ochiq; qabul qiluvchining maxfiylik cheklovi yuborishda aniqlanishi mumkin."}
        try:
            permissions = await self.client.get_permissions(entity, self.expected_profile_id)
        except (RPCError, TypeError, ValueError, AttributeError):
            return {"allowed": None, "reason": "Telegram huquqini oldindan to‘liq tekshira olmadi; yuborishda yana tekshiriladi."}
        if permissions is None:
            return {"allowed": None, "reason": "Telegram huquq holatini qaytarmadi; yuborishda yana tekshiriladi."}
        if bool(getattr(permissions, "has_left", False)) or bool(getattr(permissions, "is_banned", False)):
            return {"allowed": False, "reason": "Profilingiz bu guruh yoki kanalda a’zo emas yoki cheklangan."}
        if bool(getattr(entity, "broadcast", False)):
            if bool(getattr(permissions, "is_creator", False)) or bool(getattr(permissions, "post_messages", False)):
                return {"allowed": True, "reason": "Kanalga post yuborish huquqi bor."}
            return {"allowed": False, "reason": "Kanalga post yuborish uchun administrator huquqi kerak."}
        participant = getattr(permissions, "participant", None)
        restricted = getattr(participant, "banned_rights", None) or getattr(participant, "restricted_rights", None)
        defaults = getattr(entity, "default_banned_rights", None)
        is_admin = bool(getattr(permissions, "is_admin", False))
        for rights in (restricted, None if is_admin else defaults):
            if rights is None:
                continue
            if bool(getattr(rights, "send_messages", False)):
                return {"allowed": False, "reason": "Telegram profilingizga bu chatda xabar yuborishni taqiqlagan."}
            if bool(getattr(rights, "send_media", False)):
                return {"allowed": False, "reason": "Telegram profilingizga bu chatda media yuborishni taqiqlagan."}
        return {"allowed": True, "reason": "Telegram a’zolik va media huquqlarini tasdiqladi."}

    async def check_text_permission(self, chat_id: int) -> dict[str, Any]:
        try:
            dialog = await self._dialog_by_id(chat_id)
        except ProfileQueryError:
            return {"allowed": False, "reason": "Guruh profilingizda topilmadi."}
        entity = dialog.entity
        if entity.__class__.__name__ == "User":
            return {"allowed": False, "reason": "Kutib olish qoidasi faqat guruh yoki kanalga sozlanadi."}
        try:
            permissions = await self.client.get_permissions(entity, self.expected_profile_id)
        except (RPCError, TypeError, ValueError, AttributeError):
            return {"allowed": False, "reason": "Telegram xabar yuborish huquqini tasdiqlay olmadi."}
        if permissions is None or bool(getattr(permissions, "has_left", False)) or bool(
                getattr(permissions, "is_banned", False)):
            return {"allowed": False, "reason": "Profilingiz guruh a’zosi emas yoki cheklangan."}
        if bool(getattr(entity, "broadcast", False)):
            if bool(getattr(permissions, "is_creator", False)) or bool(getattr(permissions, "post_messages", False)):
                return {"allowed": True, "reason": "Kanalga post yuborish huquqi bor."}
            return {"allowed": False, "reason": "Kanalga post yuborish uchun admin huquqi kerak."}
        participant = getattr(permissions, "participant", None)
        restricted = getattr(participant, "banned_rights", None) or getattr(
            participant, "restricted_rights", None)
        defaults = getattr(entity, "default_banned_rights", None)
        is_admin = bool(getattr(permissions, "is_admin", False))
        for rights in (restricted, None if is_admin else defaults):
            if rights is not None and bool(getattr(rights, "send_messages", False)):
                return {"allowed": False, "reason": "Telegram profilingizga bu guruhda xabar yuborishni taqiqlagan."}
        return {"allowed": True, "reason": "Guruhga xabar yuborish huquqi tasdiqlandi."}

    async def execute(self, job: dict[str, Any]) -> str:
        operation = str(job.get("operation") or "send_message")
        params = job.get("params") or {}
        chat_id = int(params.get("chat_id") or (job.get("recipient") or {}).get("user_id") or 0)
        try:
            dialog = await self._dialog_by_id(chat_id)
        except ProfileQueryError:
            return "chat_not_found"
        entity = dialog.entity
        try:
            if operation == "send_message":
                body = str(params.get("body") or job.get("body") or "")
                if not body or len(body) > 2000:
                    return "invalid_message"
                peer = await self.client.get_input_entity(entity)
                topic_id = params.get("topic_id")
                reply_to = None
                if topic_id is not None:
                    try:
                        topic_id = int(topic_id)
                    except (TypeError, ValueError):
                        return "invalid_topic"
                    if topic_id <= 0 or entity.__class__.__name__ != "Channel" or not bool(
                            getattr(entity, "megagroup", False)):
                        return "invalid_topic"
                    reply_to = InputReplyToMessage(reply_to_msg_id=topic_id, top_msg_id=topic_id)
                await self.client(SendMessageRequest(peer=peer, message=body,
                                                     random_id=int(job["random_id"]), no_webpage=True,
                                                     reply_to=reply_to))
            elif operation == "send_voice":
                voice_bytes = job.get("voice_bytes")
                if not isinstance(voice_bytes, bytes) or not voice_bytes or len(voice_bytes) > 8_000_000:
                    return "invalid_voice"
                voice_file = io.BytesIO(voice_bytes)
                voice_file.name = "xabar.ogg"
                voice_options: dict[str, Any] = {
                    "mime_type": "audio/ogg", "voice_note": True,
                    "caption": "Ovoz sun’iy intellekt tomonidan yaratildi.",
                }
                if params.get("message_id"):
                    voice_options["reply_to"] = int(params["message_id"])
                await self.client.send_file(entity, voice_file, **voice_options)
            elif operation == "send_media":
                media = params.get("profile_media") or {}
                raw = str(params.get("media_content_b64") or "")
                kind = str(media.get("kind") or "")
                if kind not in {"document", "photo", "video", "audio", "animation", "voice", "video_note"}:
                    return "invalid_media_type"
                try:
                    media_bytes = base64.b64decode(raw, validate=True)
                except (ValueError, TypeError):
                    return "invalid_media_payload"
                if not media_bytes or len(media_bytes) > 8 * 1024 * 1024:
                    return "file_too_large" if len(media_bytes) > 8 * 1024 * 1024 else "empty_media"
                permission = await self.check_media_permission(chat_id, kind)
                if permission.get("allowed") is False:
                    return "media_forbidden"
                filename = str(media.get("filename") or "fayl").replace("\\", "/").split("/")[-1]
                filename = "".join(char for char in filename if char.isprintable() and char not in '"<>|')[:160] or "fayl"
                mime_type = str(media.get("mime_type") or "application/octet-stream")[:100]
                stream = io.BytesIO(media_bytes)
                stream.name = filename
                options: dict[str, Any] = {"caption": str(params.get("body") or "")[:900],
                                           "mime_type": mime_type}
                if kind == "document":
                    options["force_document"] = True
                if kind == "voice":
                    options["voice_note"] = True
                    options["mime_type"] = "audio/ogg"
                if kind == "video_note":
                    options["video_note"] = True
                    options["mime_type"] = "video/mp4"
                await self.client.send_file(entity, stream, **options)
            elif operation == "send_location":
                try:
                    latitude = float(params.get("latitude"))
                    longitude = float(params.get("longitude"))
                except (TypeError, ValueError):
                    return "invalid_location"
                if (not math.isfinite(latitude) or not math.isfinite(longitude)
                        or not -90 <= latitude <= 90 or not -180 <= longitude <= 180):
                    return "invalid_location"
                permission = await self.check_media_permission(chat_id, "location")
                if permission.get("allowed") is False:
                    return "media_forbidden"
                peer = await self.client.get_input_entity(entity)
                media = InputMediaGeoPoint(geo_point=InputGeoPoint(lat=latitude, long=longitude))
                await self.client(SendMediaRequest(peer=peer, media=media,
                    message=str(params.get("body") or "")[:900], random_id=int(job["random_id"])))
            elif operation == "send_contact":
                phone = "".join(char for char in str(params.get("phone_number") or "") if char.isdigit() or char == "+")
                first_name = " ".join(str(params.get("first_name") or "").split())[:100]
                last_name = " ".join(str(params.get("last_name") or "").split())[:100]
                if not 5 <= len(phone) <= 32 or not first_name:
                    return "invalid_contact"
                permission = await self.check_media_permission(chat_id, "contact")
                if permission.get("allowed") is False:
                    return "media_forbidden"
                peer = await self.client.get_input_entity(entity)
                media = InputMediaContact(phone_number=phone, first_name=first_name,
                                          last_name=last_name, vcard="")
                await self.client(SendMediaRequest(peer=peer, media=media,
                    message=str(params.get("body") or "")[:900], random_id=int(job["random_id"])))
            elif operation == "send_poll":
                question = " ".join(str(params.get("question") or "").split())[:255]
                options = params.get("options")
                if (not question or not isinstance(options, list) or not 2 <= len(options) <= 10
                        or any(not str(item or "").strip() or len(str(item)) > 100 for item in options)
                        or len({str(item).casefold() for item in options}) != len(options)):
                    return "invalid_poll"
                permission = await self.check_media_permission(chat_id, "poll")
                if permission.get("allowed") is False:
                    return "media_forbidden"
                peer = await self.client.get_input_entity(entity)
                poll_id = secrets.randbits(63) or 1
                answers = [PollAnswer(text=TextWithEntities(text=str(option), entities=[]),
                                      option=bytes([index]))
                           for index, option in enumerate(options)]
                poll = Poll(id=poll_id, question=TextWithEntities(text=question, entities=[]),
                            answers=answers, hash=0,
                            public_voters=False, multiple_choice=bool(params.get("multiple_choice")), quiz=False)
                await self.client(SendMediaRequest(peer=peer, media=InputMediaPoll(poll=poll),
                    message=str(params.get("body") or "")[:900], random_id=int(job["random_id"])))
            elif operation == "reply_message":
                body = str(params.get("body") or "")
                if not body or len(body) > 2000:
                    return "invalid_message"
                source_message = await self.client.get_messages(entity, ids=int(params["message_id"]))
                if source_message is None:
                    return "message_not_found"
                peer = await self.client.get_input_entity(entity)
                await self.client(SendMessageRequest(peer=peer, message=body,
                    random_id=int(job["random_id"]), no_webpage=True,
                    reply_to=InputReplyToMessage(reply_to_msg_id=source_message.id)))
            elif operation == "edit_message":
                message = await self._owned_message(entity, params.get("message_id"))
                await self.client.edit_message(entity, message, str(params.get("body") or ""), parse_mode=None)
            elif operation == "delete_message":
                message = await self._owned_message(entity, params.get("message_id"))
                await self.client.delete_messages(entity, [message.id], revoke=False)
            elif operation in {"delete_group_message", "mute_group_member", "ban_group_member"}:
                permission = await self.check_moderation_permission(chat_id, operation)
                if permission.get("allowed") is not True:
                    return "moderation_forbidden"
                try:
                    message_id = int(params.get("message_id"))
                except (TypeError, ValueError):
                    return "message_not_found"
                message = await self.client.get_messages(entity, ids=message_id)
                if message is None:
                    return "message_not_found"
                if operation == "delete_group_message":
                    await self.client.delete_messages(entity, [message.id], revoke=True)
                else:
                    try:
                        target = await message.get_sender()
                    except (RPCError, TypeError, ValueError):
                        target = None
                    if target is None or target.__class__.__name__ != "User":
                        return "moderation_target_unknown"
                    if int(target.id) == self.expected_profile_id:
                        return "moderation_target_protected"
                    target_permissions = await self.client.get_permissions(entity, target)
                    if (target_permissions is None or bool(getattr(target_permissions, "has_left", False))
                            or bool(getattr(target_permissions, "is_banned", False))):
                        return "moderation_target_unknown"
                    if (bool(getattr(target_permissions, "is_creator", False))
                            or bool(getattr(target_permissions, "is_admin", False))):
                        return "moderation_target_protected"
                    if params.get("target_user_id") is not None and int(params["target_user_id"]) != int(target.id):
                        return "moderation_target_changed"
                    if operation == "mute_group_member":
                        try:
                            duration = int(params.get("duration_minutes"))
                        except (TypeError, ValueError):
                            return "invalid_duration"
                        if not 1 <= duration <= 43_200:
                            return "invalid_duration"
                        banned_rights = ChatBannedRights(
                            until_date=datetime.now(UTC) + timedelta(minutes=duration),
                            send_messages=True)
                    else:
                        banned_rights = ChatBannedRights(until_date=None, view_messages=True)
                    target_peer = await self.client.get_input_entity(target)
                    await self.client(EditBannedRequest(
                        channel=utils.get_input_channel(entity), participant=target_peer,
                        banned_rights=banned_rights))
            elif operation == "forward_message":
                raw_ids = params.get("message_ids") or [params.get("message_id")]
                if not isinstance(raw_ids, (list, tuple)) or not 1 <= len(raw_ids) <= 10:
                    return "invalid_media_selection"
                try:
                    message_ids = [int(value) for value in raw_ids]
                except (TypeError, ValueError):
                    return "invalid_media_selection"
                messages = await self.client.get_messages(entity, ids=message_ids)
                if not isinstance(messages, (list, tuple)):
                    messages = [messages]
                messages = [message for message in messages if message is not None]
                if len(messages) != len(message_ids):
                    return "message_not_found"
                target = await self._dialog_by_id(int(params["target_id"]))
                grouped_ids = {getattr(message, "grouped_id", None) for message in messages}
                if len(messages) > 1 and (len(grouped_ids) != 1 or None in grouped_ids):
                    return "album_changed"
                await self.client.forward_messages(target.entity, messages, from_peer=entity,
                                                   as_album=len(messages) > 1)
            elif operation == "archive_chat":
                await self.client.edit_folder(entity, 1)
            elif operation == "unarchive_chat":
                await self.client.edit_folder(entity, 0)
            elif operation in {"mute_chat", "unmute_chat"}:
                peer = await self.client.get_input_entity(entity)
                until = datetime.fromtimestamp(2147483647, UTC) if operation == "mute_chat" else datetime.now(UTC)
                await self.client(UpdateNotifySettingsRequest(
                    peer=InputNotifyPeer(peer), settings=InputPeerNotifySettings(mute_until=until)))
            elif operation == "mark_read":
                await self.client.send_read_acknowledge(entity)
            elif operation == "pin_message":
                message = await self.client.get_messages(entity, ids=int(params["message_id"]))
                if message is None:
                    return "message_not_found"
                await self.client.pin_message(entity, int(params["message_id"]), notify=False, pm_oneside=True)
            elif operation == "unpin_message":
                await self.client.unpin_message(entity, int(params["message_id"]), notify=False)
            elif operation == "leave_chat":
                await self.client.delete_dialog(entity)
            else:
                return "unsupported_operation"
            return "sent"
        except ProfileQueryError as exc:
            return "message_not_owned" if exc.status_code == 403 else "message_not_found"
        except RPCError as exc:
            # The concrete Telegram error name is safe to store; request contents stay encrypted.
            name = type(exc).__name__.lower()
            if any(marker in name for marker in ("sendmediaforbidden", "writeforbidden",
                                                  "adminrequired", "bannedinchannel")):
                return "media_forbidden"
            if any(marker in name for marker in ("filepartstoolarge", "filetoobig", "filepartsinvalid")):
                return "file_too_large"
            return name[:40]
        except (OSError, TimeoutError):
            return "unknown"

    async def _owned_message(self, entity: Any, message_id: Any) -> Any:
        try:
            message = await self.client.get_messages(entity, ids=int(message_id))
        except (TypeError, ValueError):
            message = None
        if message is None:
            raise ProfileQueryError(404, "Xabar topilmadi.")
        if not message.out:
            raise ProfileQueryError(403, "Faqat o‘zingiz yuborgan xabarni o‘zgartirish yoki o‘chirish mumkin.")
        return message


def create_management_app(settings: Settings, manager: ProfileManager | None) -> FastAPI:
    api = FastAPI(title="Private profile management API", docs_url=None, redoc_url=None, openapi_url=None)

    def require_token(authorization: str | None) -> None:
        expected = settings.internal_token
        actual = authorization.removeprefix("Bearer ") if authorization else ""
        if not expected or not hmac.compare_digest(actual, expected):
            raise HTTPException(status_code=401, detail="Unauthorized")

    def require_manager(*, reader: bool = False) -> ProfileManager:
        enabled = settings.enable_profile_reader if reader else (settings.enable_profile_reader or settings.enable_profile_executor)
        if manager is None or not enabled:
            raise HTTPException(status_code=503, detail="Telegram profilini ulash va yoqish kerak.")
        return manager

    @api.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok" if manager else "waiting_for_profile_session"}

    @api.get("/internal/v1/profile/chats")
    async def chats(limit: int = 10, authorization: str | None = Header(default=None)):
        require_token(authorization)
        return await require_manager(reader=True).list_chats(limit)

    @api.post("/internal/v1/profile/chat-messages")
    async def chat_messages(request: ChatMessagesRequest, authorization: str | None = Header(default=None)):
        require_token(authorization)
        try:
            return await require_manager(reader=True).chat_messages(
                request.chat_name, request.limit, request.chat_id, request.since, request.until)
        except ProfileQueryError as exc:
            raise HTTPException(exc.status_code, detail=exc.detail) from exc

    @api.post("/internal/v1/profile/search-messages")
    async def search_messages(request: SearchMessagesRequest, authorization: str | None = Header(default=None)):
        require_token(authorization)
        return await require_manager(reader=True).search_messages(request.query, request.limit)

    @api.post("/internal/v1/profile/search-files")
    async def search_files(request: FileSearchRequest, authorization: str | None = Header(default=None)):
        require_token(authorization)
        return await require_manager(reader=True).search_files(
            request.query, request.limit, chat_name=request.chat_name, sender=request.sender,
            date_from=request.date_from, date_to=request.date_to, file_type=request.file_type)

    @api.post("/internal/v1/profile/download-file")
    async def download_file(request: FileDownloadRequest, authorization: str | None = Header(default=None)):
        from urllib.parse import quote

        require_token(authorization)
        try:
            result = await require_manager(reader=True).download_file(
                request.query, request.chat_name, request.message_id)
        except ProfileQueryError as exc:
            raise HTTPException(exc.status_code, detail=exc.detail) from exc
        if result.get("error"):
            return result
        filename = str(result["filename"])
        return Response(content=result["content"], media_type=str(result["mime_type"]), headers={
            "X-File-Name": quote(filename, safe=""),
            "X-File-Chat": quote(str(result["chat_name"]), safe=""),
            "X-File-Message-Id": str(result["message_id"]),
        })

    @api.post("/internal/v1/profile/important-messages")
    async def important_messages(request: ChatScanRequest, authorization: str | None = Header(default=None)):
        require_token(authorization)
        try:
            return await require_manager(reader=True).important_messages(
                request.chat_name, request.limit, request.days,
                request.priority_person, request.priority_phrase)
        except ProfileQueryError as exc:
            raise HTTPException(exc.status_code, detail=exc.detail) from exc

    @api.post("/internal/v1/profile/unanswered-chats")
    async def unanswered_chats(request: ChatScanRequest, authorization: str | None = Header(default=None)):
        require_token(authorization)
        return await require_manager(reader=True).unanswered_chats(request.limit, request.days)

    @api.post("/internal/v1/profile/message-preview")
    async def message_preview(request: MessagePreviewRequest, authorization: str | None = Header(default=None)):
        require_token(authorization)
        try:
            return await require_manager().preview_message(request.chat_id, request.message_id)
        except ProfileQueryError as exc:
            raise HTTPException(exc.status_code, detail=exc.detail) from exc

    @api.post("/internal/v1/profile/media-permission")
    async def media_permission(request: MediaPermissionRequest, authorization: str | None = Header(default=None)):
        require_token(authorization)
        try:
            return await require_manager().check_media_permission(request.chat_id, request.media_type)
        except ProfileQueryError as exc:
            raise HTTPException(exc.status_code, detail=exc.detail) from exc

    @api.post("/internal/v1/profile/text-permission")
    async def text_permission(request: TextPermissionRequest,
                              authorization: str | None = Header(default=None)):
        require_token(authorization)
        try:
            return await require_manager().check_text_permission(request.chat_id)
        except ProfileQueryError as exc:
            raise HTTPException(exc.status_code, detail=exc.detail) from exc

    @api.post("/internal/v1/profile/forum-topics")
    async def forum_topics(request: ForumTopicsRequest, authorization: str | None = Header(default=None)):
        require_token(authorization)
        try:
            return await require_manager().forum_topics(request.chat_id)
        except ProfileQueryError as exc:
            raise HTTPException(exc.status_code, detail=exc.detail) from exc

    @api.post("/internal/v1/profile/moderation-permission")
    async def moderation_permission(request: ModerationPermissionRequest,
                                    authorization: str | None = Header(default=None)):
        require_token(authorization)
        try:
            profile_manager = require_manager()
            result = await profile_manager.check_moderation_permission(request.chat_id, request.action)
            if result.get("allowed") is not True:
                return result
            if request.action in {"mute_group_member", "ban_group_member"}:
                if request.message_id is None:
                    return {"allowed": False, "reason": "Moderatsiya uchun aniq guruh xabar ID si kerak."}
                target = await profile_manager.check_moderation_target(request.chat_id, request.message_id)
                if target.get("allowed") is not True:
                    return target
                result["target"] = target
            return result
        except ProfileQueryError as exc:
            raise HTTPException(exc.status_code, detail=exc.detail) from exc

    @api.post("/internal/v1/profile/scan-spam")
    async def scan_spam(request: SpamScanRequest, authorization: str | None = Header(default=None)):
        require_token(authorization)
        try:
            return await require_manager(reader=True).scan_group_spam(
                request.chat_name, request.days, request.limit)
        except ProfileQueryError as exc:
            raise HTTPException(exc.status_code, detail=exc.detail) from exc

    @api.post("/internal/v1/profile/resolve-chat")
    async def resolve_chat(request: ResolveChatRequest, authorization: str | None = Header(default=None)):
        require_token(authorization)
        try:
            return await require_manager().resolve_chat(request.query)
        except ProfileQueryError as exc:
            raise HTTPException(exc.status_code, detail=exc.detail) from exc

    return api
