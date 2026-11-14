from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app.core.config import Settings


@dataclass(frozen=True)
class IntegrationStatus:
    key: str
    name: str
    state: str
    detail: str
    required_settings: tuple[str, ...] = ()


def _status(key: str, name: str, state: str, detail: str,
            required: tuple[str, ...] = ()) -> IntegrationStatus:
    return IntegrationStatus(key, name, state, detail, required)


def _configured(key: str, name: str, configured: bool, required: tuple[str, ...],
                detail: str | None = None) -> IntegrationStatus:
    if configured:
        return _status(key, name, "READY", detail or
                       "Sozlama to‘liq; xizmatga amaliy so‘rov yuborib tekshirish mumkin.")
    return _status(key, name, "BLOCKED_INTEGRATION",
                   "Ulanish uchun ko‘rsatilgan ENV qiymatlarini kiriting.", required)


def integration_statuses(settings: Settings, *,
                         profile_status: str | None = None,
                         server_monitor_status: str | None = None,
                         business_status: str | None = None) -> list[IntegrationStatus]:
    """Return local readiness only. Never makes provider calls or exposes secrets."""
    rows: list[IntegrationStatus] = []

    bot_ready = bool(settings.bot_token and settings.owner_telegram_id > 0 and settings.owner_chat_id > 0)
    rows.append(_status(
        "telegram_bot", "Telegram bot", "READY" if bot_ready else "BLOCKED_INTEGRATION",
        "Asosiy bot API amallari mavjud." if bot_ready else "Bot tokeni yoki egasining ID sozlamasi yetishmaydi.",
        () if bot_ready else ("BOT_TOKEN", "OWNER_TELEGRAM_ID", "OWNER_CHAT_ID"),
    ))

    profile_enabled = settings.enable_profile_executor or settings.enable_profile_reader
    if not profile_enabled:
        rows.append(_status("telegram_profile", "Shaxsiy Telegram profili", "DISABLED",
                            "Profil amallari sozlamada o‘chiq."))
    elif profile_status == "ok":
        rows.append(_status("telegram_profile", "Shaxsiy Telegram profili", "READY",
                            "Executor Telegram sessiyasi ulanganini tasdiqladi."))
    elif profile_status == "waiting_for_profile_session":
        rows.append(_status("telegram_profile", "Shaxsiy Telegram profili", "BLOCKED_INTEGRATION",
                            "Executor ishlayapti, ammo Telegram profil sessiyasi ulanmagan."))
    else:
        rows.append(_status("telegram_profile", "Shaxsiy Telegram profili", "BLOCKED_INTEGRATION",
                            "Bot Telegram sessiyasini tekshira olmadi; Executor health javobi olinmadi."))

    if not settings.enable_openai:
        rows.append(_status("openai", "OpenAI", "DISABLED", "OpenAI funksiyalari sozlamada o‘chiq."))
    elif not settings.openai_api_key:
        rows.append(_status("openai", "OpenAI", "BLOCKED_INTEGRATION",
                            "API kaliti sozlanmagan.", ("OPENAI_API_KEY",)))
    else:
        rows.append(_status("openai", "OpenAI", "READY",
                            f"Model adapteri sozlangan ({settings.openai_model}); kalit qiymati ko‘rsatilmaydi."))

    rows.append(_status(
        "github", "GitHub read-only", "READY",
        ("Ommaviy repositorylar uchun tayyor; private repositorylar ham ochish uchun GITHUB_TOKEN kerak."
         if not settings.github_token else
         "Read-only adapter va token sozlangan; token qiymati ko‘rsatilmaydi."),
        () if settings.github_token else ("GITHUB_TOKEN (faqat private repositorylar uchun)",),
    ))

    if not settings.internal_token:
        rows.append(_status("server_docker", "Server Docker monitoringi", "BLOCKED_INTEGRATION",
                            "Ichki token sozlanmagan.", ("INTERNAL_TOKEN",)))
    elif server_monitor_status == "ok":
        rows.append(_status("server_docker", "Server Docker monitoringi", "READY",
                            "Faqat o‘qiladigan server inventory; volume’ni o‘chirish faqat aniq nomlar bilan alohida tasdiqlanadi."))
    else:
        rows.append(_status("server_docker", "Server Docker monitoringi", "BLOCKED_INTEGRATION",
                            "Server-agent health tekshiruvi muvaffaqiyatsiz yoki hali bajarilmagan.",
                            ("SERVER_AGENT_URL", "INTERNAL_TOKEN")))

    rows.append(_status("weather", "Ob-havo (Open-Meteo)", "READY",
                        "API kalitisiz prognoz adapteri mavjud; bu tekshiruv prognoz so‘rovini yubormadi."))
    try:
        feed_path = Path(settings.news_feeds_file)
        feeds = json.loads(feed_path.read_text(encoding="utf-8"))
        news_ready = isinstance(feeds, list) and bool(feeds)
    except (OSError, UnicodeError, json.JSONDecodeError):
        news_ready = False
    rows.append(_status(
        "news", "Yangiliklar lentasi", "READY" if news_ready else "BLOCKED_INTEGRATION",
        "Feed ro‘yxati bor; manbalar tarmoq orqali tekshirilmadi." if news_ready else
        "Sozlangan news feed ro‘yxati o‘qilmadi.",
        () if news_ready else ("NEWS_FEEDS_FILE",),
    ))

    for key, name, detail in (
        ("crm_local", "Mahalliy CRM", "Mijoz kartalari bot bazasida ishlaydi; tashqi CRM ulanmagan."),
        ("content_local", "Kontent taqvimi", "Faqat mahalliy qoralama; ijtimoiy tarmoqqa nashr qilmaydi."),
        ("finance_local", "Mahalliy moliya va obunalar", "Yozuvlar bot bazasida; bank yoki to‘lov provayderi ulanmagan."),
    ):
        rows.append(_status(key, name, "READY", detail))

    google_ready = bool(settings.google_access_token or (
        settings.google_client_id and settings.google_client_secret and settings.google_refresh_token))
    google_missing = ("GOOGLE_ACCESS_TOKEN or GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET + GOOGLE_REFRESH_TOKEN",)
    for key, name in (("calendar", "Taqvim (Google Calendar)"), ("email", "Elektron pochta (Gmail)"),
                      ("google_drive", "Google Drive"), ("google_sheets", "Google Sheets")):
        rows.append(_configured(key, name, google_ready, google_missing,
            "Google OAuth sozlangan; token scope va ruxsatlar alohida sinov bilan tekshiriladi."))
    rows.extend([
        _configured("notion", "Notion", bool(settings.notion_token), ("NOTION_TOKEN",),
                    "Notion tokeni sozlangan; qaysi sahifa va bazalar ochiqligi alohida tekshiriladi."),
        _configured("trello", "Trello", bool(settings.trello_key and settings.trello_token),
                    ("TRELLO_KEY", "TRELLO_TOKEN"),
                    "Trello kaliti va tokeni sozlangan; board huquqlari alohida tekshiriladi."),
        _configured("external_crm", "HubSpot CRM", bool(settings.hubspot_token), ("HUBSPOT_TOKEN",),
                    "HubSpot tokeni sozlangan; ruxsat doirasi alohida tekshiriladi."),
        _configured("inventory", "Odoo ombor va buyurtmalar",
                    bool(settings.odoo_url and settings.odoo_database and settings.odoo_api_key),
                    ("ODOO_URL", "ODOO_DATABASE", "ODOO_API_KEY"),
                    "Odoo ulanishi sozlangan; ombor modeli va foydalanuvchi huquqi alohida tekshiriladi."),
        _configured("accounting", "Odoo hisob-kitob",
                    bool(settings.odoo_url and settings.odoo_database and settings.odoo_api_key),
                    ("ODOO_URL", "ODOO_DATABASE", "ODOO_API_KEY"),
                    "Odoo ulanishi sozlangan; hisob-kitob ma’lumotlari faqat provider javobidan olinadi."),
        _configured("maps", "Google Maps", bool(settings.maps_api_key), ("MAPS_API_KEY",),
                    "Xarita kaliti sozlangan; geokodlash va yo‘nalish alohida sinov bilan tekshiriladi."),
        _configured("smart_home", "Home Assistant",
                    bool(settings.home_assistant_url and settings.home_assistant_token),
                    ("HOME_ASSISTANT_URL", "HOME_ASSISTANT_TOKEN"),
                    "Home Assistant ulanishi sozlangan; qurilmalar amali doim alohida tasdiq talab qiladi."),
        _configured("dokploy", "Dokploy boshqaruvi",
                    bool(settings.dokploy_url and settings.dokploy_api_key and settings.dokploy_allowed_projects),
                    ("DOKPLOY_URL", "DOKPLOY_API_KEY", "DOKPLOY_ALLOWED_PROJECTS"),
                    "Dokploy tokeni, manzili va allowlist sozlangan; o‘zgartiruvchi amallar alohida tasdiqlanadi."),
        _configured("social_publish", "Meta ijtimoiy tarmoqqa nashr",
                    bool(settings.meta_graph_token and settings.meta_page_id),
                    ("META_GRAPH_TOKEN", "META_PAGE_ID"),
                    "Meta tokeni va sahifa ID si sozlangan; post faqat tasdiqdan so‘ng nashr qilinadi."),
        _status("telegram_calls", "Telegram qo‘ng‘iroqlari", "READY",
                "Qo‘ng‘iroq oqimi sozlangan; haqiqiy qo‘ng‘iroq audio bilan qabul sinovi qolgan."),
    ])
    if (settings.realtime_voice_enabled and settings.realtime_voice_app_url and settings.text_model_ready
            and settings.enable_openai and settings.enable_stt and settings.enable_tts):
        rows.append(_status("live_voice", "Jonli ovozli suhbat", "READY",
            "Telegram Mini App, OpenAI matn modeli va OpenAI ovoz xizmatlari sozlangan."))
    elif settings.realtime_voice_enabled:
        rows.append(_status("live_voice", "Jonli ovozli suhbat", "BLOCKED_INTEGRATION",
            "HTTPS Mini App URL, OpenAI matn kaliti yoki OpenAI ovoz sozlamasi yetishmaydi.",
            tuple(key for key, ready in (("REALTIME_VOICE_APP_URL", bool(settings.realtime_voice_app_url)),
                                         ("OPENAI_API_KEY", bool(settings.openai_api_key)),
                                         ("ENABLE_OPENAI", settings.enable_openai),
                                         ("ENABLE_STT", settings.enable_stt),
                                         ("ENABLE_TTS", settings.enable_tts)) if not ready)))
    else:
        rows.append(_status("live_voice", "Jonli ovozli suhbat", "DISABLED",
            "OpenAI jonli suhbat oynasi o‘chiq; HTTPS URL va rozilik bilan yoqish mumkin.",
            ("ENABLE_REALTIME_VOICE", "REALTIME_VOICE_APP_URL")))
    if business_status == "ok":
        rows.append(_status("telegram_business", "Telegram Business", "READY",
            "Ulanish va o‘qish/javob huquqlari tasdiqlandi; OpenAI tahlili uchun /privacy roziligi ham kerak."))
    elif business_status == "waiting_for_rights":
        rows.append(_status("telegram_business", "Telegram Business", "BLOCKED_INTEGRATION",
            "Ulanish bor, ammo botga o‘qish va javob huquqlari kerak."))
    else:
        rows.append(_status("telegram_business", "Telegram Business", "BLOCKED_INTEGRATION",
            "Telegram sozlamalarida botni Business akkauntga ulash kerak."))
    return rows


def format_integration_statuses(settings: Settings, *,
                                profile_status: str | None = None,
                                server_monitor_status: str | None = None,
                                business_status: str | None = None) -> str:
    rows = integration_statuses(settings, profile_status=profile_status,
                                 server_monitor_status=server_monitor_status,
                                 business_status=business_status)
    labels = {"READY": "tayyor", "DISABLED": "o‘chiq", "BLOCKED_INTEGRATION": "ulanish kerak"}
    lines = ["Integratsiyalar holati", "Bu ro‘yxat faqat sozlamani tekshiradi; tashqi xizmatlarga ulanmagan."]
    for item in rows:
        line = f"• {item.name}: {labels[item.state]}"
        if item.state != "READY" or item.key in {"github", "crm_local", "content_local", "finance_local"}:
            line += f" — {item.detail}"
        if item.required_settings:
            line += " Kerak: " + ", ".join(item.required_settings) + "."
        lines.append(line)
    lines.append("Kerakli ENV qiymatlari ro‘yxatda ko‘rsatiladi. ‘Tayyor’ holati kalit borligini bildiradi; xizmat huquqlari va haqiqiy ulanish birinchi amal paytida tekshiriladi.")
    return "\n".join(lines)
