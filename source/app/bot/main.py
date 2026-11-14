from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from difflib import SequenceMatcher
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlsplit

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import (TelegramAPIError, TelegramConflictError,
                                TelegramNetworkError, TelegramRetryAfter,
                                TelegramServerError)
from aiogram.types import BotCommand

from app.core.assistant import OpenAIAssistant
from app.bot.handlers import build_report, make_router, process_speech_jobs
from app.core.config import Settings
from app.core.provider_adapters import ProviderError, ProviderHub
from app.core.sources import (WeatherService, load_news, probe_public_page,
                              public_certificate_expiry, public_domain_expiry)
from app.storage.business import BusinessRecords
from app.executor.api import create_internal_app
from app.storage.db import Database
from app.storage.personal import PersonalRecords
from app.storage.security import Vault
from app.speech.openai_audio import OpenAITranscriber
from app.speech.service import cleanup_abandoned_media
from app.speech.tts import OpenAITTS, send_assistant_message

LOG = logging.getLogger("assistant")


async def _check_weather_alerts(personal: PersonalRecords, weather: WeatherService,
                                db: Database, settings: Settings, now: datetime) -> None:
    now = now.astimezone(UTC)
    for alert in personal.weather_alerts(due_at=now):
        interval = max(15, min(1440, int(alert.get("interval_minutes") or 60)))
        try:
            result = await weather.forecast_result(str(alert["city"]),
                                                   tomorrow=bool(alert.get("tomorrow")))
            if not result.available:
                personal.advance_weather_alert(alert["id"],
                    next_check_at=now + timedelta(minutes=min(interval, 15)))
                continue
            metric = str(alert.get("metric") or "precipitation_probability")
            threshold = float(alert["threshold"])
            if metric == "temperature_above":
                observed = result.maximum
                comparison = observed is not None and observed >= threshold
                measure = "kunlik yuqori harorat"
                unit = "°C"
            elif metric == "temperature_below":
                observed = result.minimum
                comparison = observed is not None and observed <= threshold
                measure = "kunlik past harorat"
                unit = "°C"
            elif metric == "wind_speed":
                observed = result.wind_speed
                comparison = observed is not None and observed >= threshold
                measure = "kunlik eng kuchli shamol"
                unit = "km/soat"
            else:
                observed = result.precipitation_probability
                comparison = observed is not None and observed >= threshold
                measure = "yog‘ingarchilik ehtimoli"
                unit = "%"
            last = datetime.fromisoformat(alert["last_notified_at"]).astimezone(UTC) if alert.get("last_notified_at") else None
            may_notify = last is None or now - last >= timedelta(hours=24)
            notified_at = None
            if comparison and may_notify:
                day = result.forecast_date or now.astimezone(settings.timezone).date().isoformat()
                shown_unit = "foiz" if unit == "%" else unit
                text = (f"Ob-havo ogohlantirishi · {result.city or alert['city']} · {day}\n"
                        f"{measure.capitalize()} {observed:g} {shown_unit}; belgilangan chegara {threshold:g} {shown_unit}.\n"
                        f"{result.text}")
                db.enqueue_message(dedupe_key=f"weather-alert:{alert['id']}:{day}",
                                   owner_id=settings.owner_telegram_id,
                                   chat_id=settings.owner_chat_id, text=text)
                notified_at = now
            personal.advance_weather_alert(alert["id"],
                next_check_at=now + timedelta(minutes=interval),
                last_notified_at=notified_at)
        except Exception as exc:
            LOG.warning("weather alert check failed id=%s category=%s",
                        str(alert.get("id", ""))[:8], type(exc).__name__)
            personal.advance_weather_alert(alert["id"], next_check_at=now + timedelta(minutes=15))


async def _refresh_scheduled_weather_messages(db: Database, weather: WeatherService,
                                               owner_id: int, now: datetime) -> None:
    """Refresh approved weather-message drafts at delivery time; never send a stale snapshot."""
    for item in db.due_scheduled_weather_messages(owner_id, now=now):
        query = item.get("weather_query") or {}
        try:
            target = date.fromisoformat(str(query.get("target_date") or ""))
            forecast = await weather.forecast_result(
                str(query.get("city") or ""), target_date=target,
                hour=str(query["hour"]) if query.get("hour") else None,
                force_refresh=True)
        except (TypeError, ValueError, KeyError):
            forecast = None
        except Exception as exc:
            LOG.warning("scheduled weather refresh failed id=%s category=%s",
                        str(item.get("id", ""))[:8], type(exc).__name__)
            forecast = None
        schedule_id = str(item.get("id") or "")
        if forecast is not None and forecast.available:
            db.update_scheduled_weather_message(owner_id, schedule_id, forecast.text, now=now)
        else:
            db.fail_scheduled_weather_message(owner_id, schedule_id, now=now)


async def _check_home_watches(personal: PersonalRecords, db: Database, settings: Settings,
                              now: datetime) -> None:
    if not settings.home_assistant_url or not settings.home_assistant_token:
        return
    watches = personal.home_watches(due_at=now)
    if not watches:
        return
    provider = ProviderHub(settings)
    try:
        for watch in watches:
            watch_id = str(watch.get("id") or "")
            permission = personal.permission_profile("integration", "home_assistant", "read")
            if permission and permission["mode"] == "deny":
                if personal.cancel_home_watch(watch_id):
                    db.enqueue_message(dedupe_key=f"home-watch-permission-revoked:{watch_id}",
                        owner_id=settings.owner_telegram_id, chat_id=settings.owner_chat_id,
                        text=f"Home Assistant kuzatuvi {watch_id} ruxsat bekor qilingani uchun to‘xtatildi.")
                continue
            try:
                state = await provider.execute("home.state", {"entity_id": watch["entity_id"]})
            except ProviderError as exc:
                personal.defer_home_watch(watch_id, now + timedelta(minutes=5))
                LOG.warning("home watch check failed id=%s category=%s", watch_id[:8], type(exc).__name__)
                continue
            observed = personal.observe_home_watch(watch_id, str(state.get("state") or "unknown"),
                                                  observed_at=now)
            if observed and observed.get("triggered"):
                local_time = now.astimezone(settings.timezone).strftime("%Y-%m-%d %H:%M %Z")
                label = str(state.get("name") or observed.get("entity_id"))[:160]
                previous = str(observed.get("previous_state") or "—")[:100]
                current = str(observed.get("state") or "—")[:100]
                db.enqueue_message(dedupe_key=f"home-watch:{watch_id}:{observed.get('observed_at')}",
                    owner_id=settings.owner_telegram_id, chat_id=settings.owner_chat_id,
                    text=f"Home Assistant kuzatuvi · {label}\n"
                         f"Holat: {previous} → {current}\nVaqt: {local_time}.")
    finally:
        await provider.close()


def _queue_important_date_reminders(personal: PersonalRecords, db: Database, settings: Settings,
                                   now_local: datetime) -> None:
    today = now_local.date()
    for item in personal.due_important_dates(today):
        target = datetime.fromisoformat(item["next_date"]).date()
        lead = int(item.get("remind_days_before") or 0)
        months = int(item.get("remind_months_before") or 0)
        if months:
            notify = datetime.fromisoformat(item.get("notify_date") or target.isoformat()).date()
            days_remaining = (target - today).days
            timing = f"{days_remaining} kundan keyin" if notify < today else f"{months} kalendar oydan keyin"
        else:
            timing = "bugun" if lead == 0 else f"{(target - today).days} kundan keyin"
        text = (f"Muhim sana eslatmasi · {item['title']}\n"
                f"Sana: {target:%Y-%m-%d} ({timing}).")
        db.enqueue_message(dedupe_key=f"important-date:{item['id']}:{target.year}",
                           owner_id=settings.owner_telegram_id,
                           chat_id=settings.owner_chat_id, text=text)


def _queue_subscription_reminders(business: BusinessRecords, db: Database, settings: Settings,
                                  today: date) -> None:
    for item in business.due_subscription_reminders(today):
        digits = int(item["digits"])
        divisor = 10 ** digits
        whole, fraction = divmod(int(item["minor"]), divisor)
        amount = f"{whole:,}".replace(",", " ")
        if fraction:
            amount += f".{fraction:0{digits}d}"
        days = int(item["days_remaining"])
        when = "bugun" if days == 0 else (f"{days} kundan keyin" if days > 0 else f"{abs(days)} kun oldin")
        text = (f"Obuna/to‘lov eslatmasi · {item['name']}\n"
                f"Summa: {amount} {item['currency']}\n"
                f"To‘lov sanasi: {item['next_due']} ({when}).\n"
                "Bu faqat eslatma; to‘lov amalga oshirilmadi.")
        db.enqueue_message(dedupe_key=f"subscription-due:{item['id']}:{item['next_due']}",
                           owner_id=settings.owner_telegram_id,
                           chat_id=settings.owner_chat_id, text=text)


async def _poll_updates(bot: Bot, dispatcher: Dispatcher, db: Database) -> None:
    offset = db.poll_offset()
    await bot.delete_webhook(drop_pending_updates=False)
    while True:
        try:
            updates = await bot.get_updates(offset=offset, timeout=30,
                allowed_updates=["message", "callback_query", "business_connection", "business_message",
                                 "edited_business_message", "deleted_business_messages"])
            for update in updates:
                if db.is_update_processed(update.update_id):
                    db.commit_update_offset(update.update_id)
                    offset = max(offset, update.update_id + 1)
                    continue
                await dispatcher.feed_update(bot, update)
                db.commit_update_offset(update.update_id)
                offset = max(offset, update.update_id + 1)
        except TelegramConflictError:
            LOG.error("Bot API polling conflict; another poller uses this bot token")
            raise
        except (TelegramAPIError, TelegramNetworkError, TimeoutError, OSError) as exc:
            LOG.warning("polling error category=%s", type(exc).__name__)
            await asyncio.sleep(3)
        except Exception as exc:
            LOG.exception("update handler failed category=%s; update offset was not advanced", type(exc).__name__)
            await asyncio.sleep(2)


async def _scheduler(bot: Bot, settings: Settings, db: Database, tts: OpenAITTS | None = None) -> None:
    personal = PersonalRecords(db, settings.owner_telegram_id)
    business = BusinessRecords(db, settings.owner_telegram_id)
    weather = WeatherService()
    while True:
        try:
            now_local = datetime.now(settings.timezone)
            await _queue_news_digest(personal, db, settings, now_local)
            await _check_site_monitors(personal, db, settings, now_local)
            await _check_home_watches(personal, db, settings, datetime.now(UTC))
            if not db.daily_report_queued(settings.owner_telegram_id, now_local.date().isoformat()):
                report_time = db.get_report_time(settings.owner_telegram_id)
                if report_time and now_local.strftime("%H:%M") >= report_time:
                    report = await build_report(db, settings)
                    db.queue_daily_report(settings.owner_telegram_id, settings.owner_chat_id,
                                          now_local.date().isoformat(), now_local.strftime("%H:%M"), report)
            db.queue_due_reminders(settings.owner_telegram_id, settings.owner_chat_id)
            check_time = datetime.now(UTC)
            await _refresh_scheduled_weather_messages(db, weather, settings.owner_telegram_id, check_time)
            db.queue_due_profile_messages(max_per_hour=settings.profile_sends_per_hour)
            await _check_weather_alerts(personal, weather, db, settings, check_time)
            _queue_important_date_reminders(personal, db, settings, now_local)
            _queue_subscription_reminders(business, db, settings, now_local.date())
            await _publish_due_content_posts(business, db, settings, datetime.now(UTC))
            for item in db.due_outbox(limit=50, max_per_hour=settings.global_sends_per_hour):
                try:
                    mode = (db.get_delivery_format(settings.owner_telegram_id, "owner_reply")
                            if int(item["chat_id"]) == settings.owner_chat_id else "text")
                    prefs = personal.preferences() if mode != "text" else {}
                    await send_assistant_message(
                        bot, item["chat_id"], item["text"], tts, mode=mode,
                        pronunciations=personal.pronunciations() if mode != "text" else None,
                        speed=prefs.get("voice_speed", "normal"),
                        voice_length=prefs.get("voice_length", "normal"))
                except TelegramRetryAfter as exc:
                    if not db.retry_outbox(item["id"], int(exc.retry_after), "retry_after"):
                        db.mark_outbox(item["id"], "failed", "retry_limit")
                except TelegramServerError:
                    db.mark_outbox(item["id"], "unknown", "telegram_server_unknown")
                except (TelegramNetworkError, TimeoutError, OSError):
                    db.mark_outbox(item["id"], "unknown", "transport_unknown")
                except TelegramAPIError as exc:
                    category = type(exc).__name__.lower()[:40]
                    db.mark_outbox(item["id"], "failed", category)
                else:
                    db.mark_outbox(item["id"], "sent")
        except Exception as exc:
            LOG.exception("scheduler error category=%s", type(exc).__name__)
        await asyncio.sleep(5)


async def _publish_due_content_posts(business: BusinessRecords, db: Database,
                                     settings: Settings, now: datetime) -> int:
    """Publish only the exact approved, due Facebook Page version; never replay unknown writes."""
    for record in business.recover_stale_content_publishes(now):
        title = str(record.get("title") or "Post")[:160]
        attempt = str(record.get("publish_attempt_id") or "recovered")
        db.enqueue_message(dedupe_key=f"content-publish:{record.get('id')}:{attempt}:unknown",
            owner_id=settings.owner_telegram_id, chat_id=settings.owner_chat_id,
            text=(f"Post nashri bo‘yicha javob uzilib qolgan: {title}. Dublikat chiqmasligi uchun qayta yuborilmadi. "
                  "Facebook sahifasini tekshiring; agar post chiqqan bo‘lsa, botga ‘nashr qilindi’ deb ayting."))

    due = business.claim_due_content_publishes(now, limit=5)
    for record in due:
        record_id = str(record.get("id") or "")
        attempt_id = str(record.get("publish_attempt_id") or "")
        title = str(record.get("title") or "Post")[:160]
        platform = " ".join(str(record.get("platform") or "").casefold().split())
        if platform not in {"facebook", "facebook page", "facebook sahifa", "facebook sahifasi", "meta page"}:
            state = "failed"
            error = ("Avtomatik nashr hozir Facebook Page uchun ishlaydi. Instagram, Telegram va boshqa "
                     "platformalar hozircha qoralama sifatida qoladi.")
            result = {"status": state, "error": error}
        else:
            hub = ProviderHub(settings)
            try:
                response = await hub.execute("social.publish",
                    {"message": str(record.get("body") or "")}, approved=True)
                if isinstance(response, dict) and response.get("status") == "succeeded":
                    state = "published"
                    result = response
                    error = None
                elif isinstance(response, dict) and response.get("status") == "unknown":
                    state = "unknown"
                    result = response
                    error = "Meta javobi noma’lum. Postni sahifadan tekshiring; avtomatik qayta urinish o‘chirildi."
                else:
                    state = "failed"
                    result = response if isinstance(response, dict) else {}
                    error = str(result.get("error") or "Meta nashrni tasdiqlamadi.")[:400]
            except ProviderError as exc:
                state = "unknown" if exc.uncertain else "failed"
                error = str(exc)[:400]
                result = {"error": error}
            except asyncio.CancelledError:
                business.finish_content_publish(record_id, attempt_id, "unknown",
                    error="Nashr paytida bot to‘xtadi; platformadagi holatni tekshiring.", now=now)
                raise
            except Exception as exc:
                LOG.warning("Scheduled content publish failed category=%s", type(exc).__name__)
                state = "unknown"
                error = "Xizmat javobi noma’lum; takroriy nashr avtomatik bajarilmadi."
                result = {"error": error}
            finally:
                await hub.close()

        post_id = str(result.get("post_id") or "") if state == "published" else None
        finished = business.finish_content_publish(record_id, attempt_id, state,
            post_id=post_id, error=error, now=now)
        if not finished:
            continue
        if state == "published":
            text = f"Rejalangan Facebook posti nashr qilindi: {title}."
            if post_id:
                text += f" Post ID: {post_id}."
        elif state == "unknown":
            text = (f"Facebook postning nashr holati noma’lum: {title}. Dublikat oldini olish uchun qayta yuborilmadi. "
                    "Sahifani tekshiring; post chiqqan bo‘lsa, botga ‘nashr qilindi’ deb ayting.")
        else:
            text = (f"Rejalangan post nashr qilinmadi: {title}. {error or 'Sabab ko‘rsatilmagan'} "
                    f"Qayta urinish uchun post ID: {record_id}.")
        db.enqueue_message(dedupe_key=f"content-publish:{record_id}:{attempt_id}:{state}",
            owner_id=settings.owner_telegram_id, chat_id=settings.owner_chat_id, text=text[:1000])
    return len(due)


async def _queue_news_digest(personal: PersonalRecords, db: Database, settings: Settings,
                             now_local: datetime) -> bool:
    prefs = personal.news_digest_settings()
    frequency = prefs.get("frequency")
    if frequency not in {"daily", "weekly"}:
        return False
    digest_time = prefs.get("time")
    if not digest_time or now_local.strftime("%H:%M") < digest_time:
        return False
    if frequency == "daily":
        period_key = now_local.date().isoformat()
        period_days = 1
        label = "Kunlik"
    else:
        period_key = now_local.strftime("%G-W%V")
        period_days = 7
        label = "Haftalik"
    dedupe_key = f"news-digest:{settings.owner_telegram_id}:{frequency}:{period_key}"
    if db.has_outbox_dedupe(dedupe_key):
        return False
    try:
        items = await load_news(
            settings.news_feeds_file, max_items=5,
            topic=prefs.get("topic"), region=prefs.get("region"),
            language=prefs.get("language"), source=prefs.get("source"),
            published_after=now_local - timedelta(days=period_days))
    except Exception as exc:
        LOG.warning("news digest fetch failed category=%s", type(exc).__name__)
        items = []
    if not items:
        text = (f"{label} yangiliklar dayjesti · {period_key}\n"
                "Bu davrga mos yangilik topilmadi. RSS manbalari ulanmagan yoki tanlangan filtrga mos maqola yo‘q.")
    else:
        lines = [f"{label} yangiliklar dayjesti · {period_key}"]
        for item in items:
            lines.extend([f"• {item.title}",
                          f"  {item.source} · {item.published or 'sana ko‘rsatilmagan'}",
                          f"  {item.link}"])
            if item.summary:
                lines.append(f"  {item.summary}")
            for source_name, source_link in item.other_sources[:3]:
                lines.append(f"  Qo‘shimcha manba: {source_name} · {source_link}")
        text = "\n".join(lines)[:3800]
    db.enqueue_message(dedupe_key=dedupe_key, owner_id=settings.owner_telegram_id,
                       chat_id=settings.owner_chat_id, text=text)
    return True


def _changed_fragments(previous: str, current: str) -> tuple[str, str]:
    matcher = SequenceMatcher(None, previous, current, autojunk=True)
    for tag, left_start, left_end, right_start, right_end in matcher.get_opcodes():
        if tag != "equal":
            left_start = max(0, left_start - 60)
            left_end = min(len(previous), left_end + 60)
            right_start = max(0, right_start - 60)
            right_end = min(len(current), right_end + 60)
            left = " ".join(previous[left_start:left_end].split())[:260] or "(yo‘q)"
            right = " ".join(current[right_start:right_end].split())[:260] or "(yo‘q)"
            return left, right
    return "(farq topilmadi)", "(farq topilmadi)"


def _site_structured_summary(item: dict[str, object], change: str) -> str:
    type_names = {"JobPosting": "Ish e’loni", "Product": "Mahsulot/narx",
                  "Offer": "Taklif", "Event": "Tadbir",
                  "GovernmentService": "Davlat xizmati/tender", "Dataset": "Ma’lumotlar to‘plami"}
    kind = str(item.get("type") or "Yozuv")
    title = " ".join(str(item.get("title") or kind).split())[:180]
    parts = [f"{change}: {type_names.get(kind, kind)} · {title}"]
    for field, label in (("price", "Narx"), ("currency", "Valyuta"), ("date", "Sana"),
                         ("deadline", "Muddat"), ("location", "Joy")):
        value = " ".join(str(item.get(field) or "").split())[:120]
        if value:
            parts.append(f"{label}: {value}")
    link = str(item.get("url") or "")
    if link.startswith(("https://", "http://")):
        parts.append(f"Manba: {link[:500]}")
    return "\n".join(parts)


async def _check_site_monitors(personal: PersonalRecords, db: Database, settings: Settings,
                               now_local: datetime) -> int:
    now_utc = now_local.astimezone(UTC)
    checked = 0
    for monitor in personal.site_monitors(active_only=True):
        previous_check = monitor.get("last_checked_at")
        if previous_check:
            try:
                last_checked = datetime.fromisoformat(str(previous_check))
                if last_checked.tzinfo is None:
                    last_checked = last_checked.replace(tzinfo=UTC)
                if now_utc - last_checked.astimezone(UTC) < timedelta(minutes=int(monitor["interval_minutes"])):
                    continue
            except (TypeError, ValueError, KeyError):
                pass
        checked += 1
        certificate_expires_at = monitor.get("certificate_expires_at")
        certificate_error = None
        try:
            certificate_expires_at = await public_certificate_expiry(str(monitor["url"]))
        except Exception as exc:
            certificate_error = type(exc).__name__
            LOG.warning("site monitor certificate lookup failed id=%s category=%s",
                        str(monitor.get("id", ""))[:12], certificate_error)

        domain_checked_at = monitor.get("domain_checked_at")
        try:
            last_domain_check = datetime.fromisoformat(str(domain_checked_at)) if domain_checked_at else None
            if last_domain_check and last_domain_check.tzinfo is None:
                last_domain_check = last_domain_check.replace(tzinfo=UTC)
        except (TypeError, ValueError):
            last_domain_check = None
        domain_expires_at = monitor.get("domain_expires_at")
        domain_checked_at_next = domain_checked_at
        if not last_domain_check or now_utc - last_domain_check.astimezone(UTC) >= timedelta(days=7):
            domain_checked_at_next = now_utc.isoformat(timespec="seconds")
            try:
                current_domain_expiry = await public_domain_expiry(urlsplit(str(monitor["url"])).hostname or "")
                if current_domain_expiry:
                    domain_expires_at = current_domain_expiry
            except Exception as exc:
                LOG.warning("site monitor domain lookup failed id=%s category=%s",
                            str(monitor.get("id", ""))[:12], type(exc).__name__)

        probe: dict[str, object]
        try:
            probe = await probe_public_page(str(monitor["url"]))
        except Exception as exc:
            probe = {"status_code": None, "response_time_ms": None, "is_up": False,
                     "text": None, "error": type(exc).__name__}
            LOG.warning("site monitor fetch failed id=%s category=%s",
                        str(monitor.get("id", ""))[:12], probe["error"])

        is_up = probe.get("is_up") is True
        previous_is_up = monitor.get("is_up")
        failures = int(monitor.get("consecutive_failures") or 0)
        outage_started_at = monitor.get("outage_started_at")
        last_error = None if is_up else str(probe.get("error") or "site_unavailable")[:80]
        if is_up:
            if previous_is_up is False and outage_started_at:
                text = (f"Sayt qayta ishlayapti · {monitor['name']}\n"
                        f"Tekshirildi: {now_local:%Y-%m-%d %H:%M} (Toshkent vaqti)\n"
                        f"HTTP {probe.get('status_code')} · javob {probe.get('response_time_ms')} ms\n"
                        f"Manba: {monitor['url']}")
                db.enqueue_message(dedupe_key=f"site-monitor:recovery:{monitor['id']}:{outage_started_at}",
                                   owner_id=settings.owner_telegram_id,
                                   chat_id=settings.owner_chat_id, text=text)
            failures = 0
            outage_started_at = None
        else:
            failures += 1
            if failures >= 3 and previous_is_up is not False:
                outage_started_at = now_utc.isoformat(timespec="seconds")
                text = (f"Sayt ketma-ket 3 tekshiruvda javob bermadi · {monitor['name']}\n"
                        f"Tekshirildi: {now_local:%Y-%m-%d %H:%M} (Toshkent vaqti)\n"
                        f"Oxirgi holat: {last_error} · HTTP {probe.get('status_code') or '—'}\n"
                        f"Manba: {monitor['url']}")
                db.enqueue_message(dedupe_key=f"site-monitor:outage:{monitor['id']}:{outage_started_at}",
                                   owner_id=settings.owner_telegram_id,
                                   chat_id=settings.owner_chat_id, text=text)

        current = probe.get("text")
        snapshot_text = str(monitor.get("snapshot_text") or "")
        snapshot_hash = str(monitor.get("snapshot_sha256") or "")
        structured_items_raw = probe.get("structured_items")
        structured_items = ([item for item in structured_items_raw[:50] if isinstance(item, dict)]
                            if isinstance(structured_items_raw, list) else None)
        prior_structured = monitor.get("structured_items")
        prior_structured_items = ([item for item in prior_structured[:50] if isinstance(item, dict)]
                                  if isinstance(prior_structured, list) else [])
        structured_initialized = monitor.get("structured_initialized") is True
        if is_up and structured_items is not None:
            if structured_initialized:
                old_by_id = {str(item.get("id")): item for item in prior_structured_items if item.get("id")}
                new_by_id = {str(item.get("id")): item for item in structured_items if item.get("id")}
                added = [item for key, item in new_by_id.items() if key not in old_by_id]
                changed = [item for key, item in new_by_id.items()
                           if key in old_by_id and item.get("content_sha256") != old_by_id[key].get("content_sha256")]
                removed = [item for key, item in old_by_id.items() if key not in new_by_id]
                changes = ([(_site_structured_summary(item, "Yangi yozuv"), str(item.get("content_sha256") or ""))
                            for item in added] +
                           [(_site_structured_summary(item, "O‘zgardi"), str(item.get("content_sha256") or ""))
                            for item in changed] +
                           [(_site_structured_summary(item, "Sahifadan olib tashlandi"), "removed")
                            for item in removed])
                if changes:
                    rendered = [text for text, _ in changes[:8]]
                    if len(changes) > len(rendered):
                        rendered.append(f"Yana {len(changes) - len(rendered)} ta o‘zgarish bor.")
                    change_digest = hashlib.sha256(json.dumps(changes, ensure_ascii=False,
                        sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
                    message = (f"Tuzilmali e’lonlar o‘zgardi · {monitor['name']}\n"
                               f"Tekshirildi: {now_local:%Y-%m-%d %H:%M} (Toshkent vaqti)\n\n" +
                               "\n\n".join(rendered))
                    db.enqueue_message(dedupe_key=f"site-monitor:structured:{monitor['id']}:{change_digest}",
                                       owner_id=settings.owner_telegram_id,
                                       chat_id=settings.owner_chat_id, text=message)
            structured_initialized = True
        elif structured_items is None:
            structured_items = prior_structured_items
        if is_up and isinstance(current, str) and current:
            digest = hashlib.sha256(current.encode("utf-8")).hexdigest()
            previous_hash = snapshot_hash
            if previous_hash and digest != previous_hash and not (structured_items or []):
                old_value, new_value = _changed_fragments(snapshot_text, current)
                checked_label = now_local.strftime("%Y-%m-%d %H:%M")
                text = (f"Sayt kuzatuvida o‘zgarish · {monitor['name']}\n"
                        f"Tekshirildi: {checked_label} (Toshkent vaqti)\n"
                        f"Oldingi matn: {old_value}\nYangi matn: {new_value}\n"
                        f"Manba: {monitor['url']}")
                db.enqueue_message(dedupe_key=f"site-monitor:{monitor['id']}:{digest}",
                                   owner_id=settings.owner_telegram_id,
                                   chat_id=settings.owner_chat_id, text=text)
            snapshot_text, snapshot_hash = current, digest

        certificate_alert_keys = list(monitor.get("certificate_alert_keys") or [])
        domain_alert_keys = list(monitor.get("domain_alert_keys") or [])
        for label, expiry_value, kind, stored_keys in (
                ("SSL sertifikati", certificate_expires_at, "certificate", certificate_alert_keys),
                ("Domen", domain_expires_at, "domain", domain_alert_keys)):
            if not expiry_value:
                continue
            try:
                expiry = datetime.fromisoformat(str(expiry_value).replace("Z", "+00:00"))
                if expiry.tzinfo is None:
                    continue
                days_left = (expiry.astimezone(UTC).date() - now_utc.date()).days
            except (TypeError, ValueError):
                continue
            threshold = "expired" if days_left <= 0 else next(
                (days for days in (1, 3, 7, 14, 30) if days_left <= days), None)
            if threshold is None:
                continue
            alert_key = f"{kind}:{expiry.astimezone(UTC).date().isoformat()}:{threshold}"
            if alert_key in stored_keys:
                continue
            stored_keys.append(alert_key)
            stored_keys[:] = stored_keys[-20:]
            remaining = "muddati o‘tgan" if days_left <= 0 else f"{days_left} kun qoldi"
            text = (f"{label} muddati yaqin · {monitor['name']}\n"
                    f"Holat: {remaining}\nTugash sanasi: {expiry.astimezone(UTC):%Y-%m-%d}\n"
                    f"Manba: {monitor['url']}")
            db.enqueue_message(dedupe_key=f"site-monitor:{kind}:{monitor['id']}:{alert_key}",
                               owner_id=settings.owner_telegram_id,
                               chat_id=settings.owner_chat_id, text=text)

        personal.update_site_monitor(
            str(monitor["id"]), snapshot_text=snapshot_text, snapshot_sha256=snapshot_hash,
            last_checked_at=now_utc.isoformat(timespec="seconds"), last_error=last_error,
            updates={"is_up": is_up if failures >= 3 or is_up else previous_is_up,
                     "consecutive_failures": failures, "outage_started_at": outage_started_at,
                     "last_status_code": probe.get("status_code"),
                     "response_time_ms": probe.get("response_time_ms"),
                     "certificate_expires_at": certificate_expires_at,
                     "certificate_error": certificate_error, "domain_expires_at": domain_expires_at,
                     "domain_checked_at": domain_checked_at_next,
                     "certificate_alert_keys": certificate_alert_keys,
            "domain_alert_keys": domain_alert_keys,
            "structured_items": structured_items or [],
            "structured_initialized": structured_initialized})
    return checked


async def _wait_for_owner_configuration() -> None:
    from fastapi import FastAPI
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "waiting_for_owner_configuration"}

    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=8080,
                                           log_level="warning", access_log=False))
    LOG.warning("Waiting for Dokploy secrets and owner IDs; add them and redeploy this service")
    await server.serve()


async def run_bot() -> None:
    settings = Settings.from_env()
    if (not settings.bot_token or not settings.data_key or settings.owner_telegram_id <= 0
            or settings.owner_chat_id <= 0):
        missing = []
        if not settings.bot_token:
            missing.append("BOT_TOKEN_FILE")
        if not settings.data_key:
            missing.append("DATA_KEY_FILE")
        if settings.owner_telegram_id <= 0:
            missing.append("OWNER_TELEGRAM_ID")
        if settings.owner_chat_id <= 0:
            missing.append("OWNER_CHAT_ID")
        LOG.warning("Waiting for required configuration keys: %s", ", ".join(missing))
        await _wait_for_owner_configuration()
        return
    settings.validate_production()
    if settings.enable_profile_executor and not settings.internal_token:
        raise RuntimeError("ENABLE_PROFILE_EXECUTOR=1 uchun INTERNAL_TOKEN_FILE secret kerak")
    if settings.owner_telegram_id <= 0 or settings.owner_chat_id != settings.owner_telegram_id:
        # Telegram private owner chat ID equals the owner user ID for a normal private bot chat.
        raise RuntimeError("Private chat uchun OWNER_CHAT_ID egasining Telegram ID raqamiga teng bo‘lishi kerak")
    vault = Vault(settings.data_key or "")
    db = Database(settings.database_path, vault)
    db.recover_interrupted()
    if settings.report_time:
        try:
            normalized_report_time = datetime.strptime(settings.report_time, "%H:%M").strftime("%H:%M")
        except ValueError as exc:
            raise RuntimeError("REPORT_TIME HH:MM formatida bo‘lishi kerak") from exc
        db.set_report_time(settings.owner_telegram_id, normalized_report_time)
    cleanup_abandoned_media(settings.media_temp_dir)
    bot = Bot(settings.bot_token or "", default=DefaultBotProperties(parse_mode=None))
    assistant = OpenAIAssistant(settings, db) if settings.enable_openai else None
    speech = OpenAITranscriber(settings) if settings.enable_stt and assistant else None
    if not settings.enable_stt:
        db.set_consent(settings.owner_telegram_id, "voice", False)
    tts = OpenAITTS(settings) if settings.enable_tts and assistant else None
    dispatcher = Dispatcher()
    pending_workspace_moves: dict[str, dict] = {}
    dispatcher.include_router(make_router(settings, db, speech, assistant, tts,
                                          pending_workspace_moves=pending_workspace_moves))
    await bot.set_my_commands([
        BotCommand(command="start", description="Yordamchi haqida"),
        BotCommand(command="panel", description="Shaxsiy yordamchi paneli"),
        BotCommand(command="ovoz", description="Jonli ovozli suhbatni ochish"),
        BotCommand(command="yordam", description="Buyruqlar ro‘yxati"),
        BotCommand(command="sana", description="Bugungi sanani ko‘rish"),
        BotCommand(command="eslat", description="Eslatma qo‘yish"),
        BotCommand(command="eslatmalar", description="Eslatmalarni ko‘rish"),
        BotCommand(command="bekor", description="Eslatmani bekor qilish"),
        BotCommand(command="ob_havo", description="Ob-havoni ko‘rish"),
        BotCommand(command="yangiliklar", description="Yangiliklarni ko‘rish"),
        BotCommand(command="hisobot", description="Qisqa hisobot"),
        BotCommand(command="hisobot_vaqti", description="Hisobot vaqtini sozlash"),
        BotCommand(command="hisobot_off", description="Hisobotni o‘chirish"),
        BotCommand(command="xabar", description="Xabarni tasdiq bilan yuborish"),
        BotCommand(command="sozlamalar", description="Javob va yuborish formatlari"),
        BotCommand(command="rejalangan", description="Rejalangan xabarlar"),
        BotCommand(command="qo_ngiroq_qoralamalar", description="Javobsiz qo‘ng‘iroq ovozli qoralamalari"),
        BotCommand(command="privacy", description="Maxfiylik va ovoz roziligi"),
        BotCommand(command="privacy_off", description="Ovoz roziligini o‘chirish"),
        BotCommand(command="delete_data", description="Mahalliy ma’lumotni o‘chirish"),
        BotCommand(command="fayllar", description="Bilim bazasidagi fayllar"),
        BotCommand(command="fayl_ochir", description="Bilim bazasidan faylni o‘chirish"),
    ])
    internal = create_internal_app(settings, db, tts)
    api_server = uvicorn.Server(uvicorn.Config(internal, host="0.0.0.0", port=8080,
                                               log_level=settings.log_level.lower(), access_log=False))
    bot_task = asyncio.create_task(_poll_updates(bot, dispatcher, db))
    api_task = asyncio.create_task(api_server.serve())
    scheduler_task = asyncio.create_task(_scheduler(bot, settings, db, tts))
    speech_task = (asyncio.create_task(process_speech_jobs(
        bot, settings, db, speech, assistant, tts, pending_workspace_moves))
                   if speech else None)
    tasks = {bot_task, api_task, scheduler_task}
    if speech_task:
        tasks.add(speech_task)
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in done:
            exception = task.exception()
            if exception:
                raise exception
    finally:
        for task in (bot_task, scheduler_task, speech_task):
            if not task:
                continue
            task.cancel()
        api_server.should_exit = True
        await asyncio.gather(bot_task, api_task, scheduler_task,
                             *([speech_task] if speech_task else []), return_exceptions=True)
        if speech:
            await speech.close()
        if assistant:
            await assistant.close()
        if tts:
            await tts.close()
        await bot.session.close()


def main() -> None:
    logging.basicConfig(level=Settings.from_env().log_level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()

