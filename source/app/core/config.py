from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo


def read_secret(env_name: str, *, required: bool = False) -> str | None:
    """Read a secret from a *_FILE path, falling back to a direct env value."""
    file_path = os.getenv(f"{env_name}_FILE")
    value = None
    if file_path:
        try:
            if Path(file_path).is_file():
                value = Path(file_path).read_text(encoding="utf-8").strip() or None
        except OSError:
            pass
    value = value or os.getenv(env_name)
    if required and not value:
        raise RuntimeError(f"{env_name}_FILE yoki {env_name} sozlanmagan")
    return value or None


def _int(name: str, default: int = 0) -> int:
    raw = os.getenv(name)
    return int(raw) if raw else default


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, "1" if default else "0").strip().lower() in {"1", "true", "yes", "on"}


def _optional_decimal(name: str) -> Decimal | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise RuntimeError(f"{name} musbat yoki nol son bo‘lishi kerak") from exc
    if not value.is_finite() or value < 0:
        raise RuntimeError(f"{name} manfiy bo‘lmagan chekli son bo‘lishi kerak")
    return value


@dataclass(frozen=True)
class Settings:
    owner_telegram_id: int
    owner_chat_id: int
    database_path: Path
    workspace_path: Path
    timezone: ZoneInfo
    bot_token: str | None
    data_key: str | None
    enable_profile_executor: bool
    expected_profile_id: int | None
    tg_api_id: int | None
    tg_api_hash: str | None
    tg_session_file: Path
    internal_token: str | None
    contacts_file: Path
    enable_stt: bool
    stt_max_file_bytes: int
    stt_max_seconds: int
    stt_timeout_seconds: int
    media_temp_dir: Path
    enable_openai: bool
    openai_api_key: str | None
    github_token: str | None
    server_agent_url: str
    openai_model: str
    openai_timeout_seconds: int
    enable_tts: bool
    openai_tts_model: str
    openai_tts_voice: str
    news_feeds_file: Path
    weather_city: str
    report_time: str | None
    profile_api_base: str
    profile_management_api_base: str
    enable_profile_reader: bool
    profile_poll_seconds: float
    profile_lease_seconds: int
    global_sends_per_hour: int
    profile_sends_per_hour: int
    log_level: str
    model_daily_requests: int = 120
    model_daily_tokens: int = 500000
    model_input_usd_per_million_tokens: Decimal | None = None
    model_output_usd_per_million_tokens: Decimal | None = None
    model_pricing_as_of: str | None = None
    model_daily_budget_usd: Decimal | None = None
    model_cost_alert_percent: int = 80
    google_access_token: str | None = None
    google_client_id: str | None = None
    google_client_secret: str | None = None
    google_refresh_token: str | None = None
    home_assistant_url: str | None = None
    home_assistant_token: str | None = None
    maps_api_key: str | None = None
    maps_provider: str = "google"
    notion_token: str | None = None
    notion_database_id: str | None = None
    trello_key: str | None = None
    trello_token: str | None = None
    hubspot_token: str | None = None
    odoo_url: str | None = None
    odoo_database: str | None = None
    odoo_username: str | None = None
    odoo_api_key: str | None = None
    dokploy_url: str | None = None
    dokploy_api_key: str | None = None
    dokploy_allowed_projects: tuple[str, ...] = ()
    meta_graph_token: str | None = None
    meta_page_id: str | None = None
    meta_graph_api_version: str = "v26.0"
    realtime_voice_enabled: bool = False
    realtime_voice_app_url: str | None = None
    realtime_voice_model: str = "gpt-realtime-2.1-mini"
    realtime_voice_output: str = "marin"
    realtime_voice_max_seconds: int = 300
    realtime_voice_sessions_per_hour: int = 3

    @property
    def text_model_ready(self) -> bool:
        return bool(self.enable_openai and self.openai_api_key)

    @classmethod
    def from_env(cls, *, demo: bool = False) -> "Settings":
        enable_openai = _bool("ENABLE_OPENAI", True)
        pricing_date = os.getenv("MODEL_PRICING_AS_OF", "").strip() or None
        if pricing_date:
            try:
                if date.fromisoformat(pricing_date).isoformat() != pricing_date:
                    raise ValueError
            except ValueError as exc:
                raise RuntimeError("MODEL_PRICING_AS_OF YYYY-MM-DD ko‘rinishida bo‘lishi kerak") from exc
        budget = _optional_decimal("MODEL_DAILY_BUDGET_USD")
        input_rate = _optional_decimal("MODEL_INPUT_USD_PER_MILLION_TOKENS")
        output_rate = _optional_decimal("MODEL_OUTPUT_USD_PER_MILLION_TOKENS")
        if (input_rate is None) != (output_rate is None):
            raise RuntimeError("MODEL_INPUT_USD_PER_MILLION_TOKENS va MODEL_OUTPUT_USD_PER_MILLION_TOKENS ikkalasini ham sozlang")
        return cls(
            owner_telegram_id=_int("OWNER_TELEGRAM_ID", 1 if demo else 0),
            owner_chat_id=_int("OWNER_CHAT_ID", 1 if demo else 0),
            database_path=Path(os.getenv("DATABASE_PATH", "/data/assistant.sqlite3")),
            workspace_path=Path(os.getenv("ASSISTANT_WORKSPACE", "/data/assistant-workspace")),
            timezone=ZoneInfo(os.getenv("TZ", "Asia/Tashkent")),
            bot_token=read_secret("BOT_TOKEN"),
            data_key=read_secret("DATA_KEY"),
            enable_profile_executor=_bool("ENABLE_PROFILE_EXECUTOR"),
            expected_profile_id=_int("EXPECTED_PROFILE_ID") or None,
            tg_api_id=_int("TG_API_ID") or None,
            tg_api_hash=read_secret("TG_API_HASH"),
            tg_session_file=Path(os.getenv("TG_SESSION_FILE", "/run/secrets/profile_session.enc")),
            internal_token=read_secret("INTERNAL_TOKEN"),
            contacts_file=Path(os.getenv("CONTACTS_FILE", "config/contacts.json")),
            enable_stt=_bool("ENABLE_STT"),
            stt_max_file_bytes=_int("STT_MAX_FILE_BYTES", 8 * 1024 * 1024),
            stt_max_seconds=_int("STT_MAX_SECONDS", 300),
            stt_timeout_seconds=_int("STT_TIMEOUT_SECONDS", 45),
            media_temp_dir=Path(os.getenv("MEDIA_TEMP_DIR", "/tmp/assistant-media")),
            enable_openai=enable_openai,
            openai_api_key=read_secret("OPENAI_API_KEY") if enable_openai else None,
            github_token=read_secret("GITHUB_TOKEN"),
            server_agent_url=os.getenv("SERVER_AGENT_URL", "http://server-agent:8090").rstrip("/"),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-6-luna"),
            openai_timeout_seconds=_int("OPENAI_TIMEOUT_SECONDS", 40),
            enable_tts=_bool("ENABLE_TTS"),
            openai_tts_model=os.getenv("OPENAI_TTS_MODEL", "gpt-realtime-2.1-mini").strip(),
            openai_tts_voice=os.getenv("OPENAI_TTS_VOICE", "marin").strip(),
            news_feeds_file=Path(os.getenv("NEWS_FEEDS_FILE", "config/news-feeds.json")),
            weather_city=os.getenv("WEATHER_CITY", "Toshkent"),
            report_time=os.getenv("REPORT_TIME") or None,
            profile_api_base=os.getenv("PROFILE_API_BASE", "http://bot:8080"),
            profile_management_api_base=os.getenv("PROFILE_MANAGEMENT_API_BASE", "http://executor:8081").rstrip("/"),
            enable_profile_reader=_bool("ENABLE_PROFILE_READER"),
            profile_poll_seconds=float(os.getenv("PROFILE_POLL_SECONDS", "2")),
            profile_lease_seconds=_int("PROFILE_LEASE_SECONDS", 30),
            global_sends_per_hour=_int("GLOBAL_SENDS_PER_HOUR", 30),
            profile_sends_per_hour=_int("PROFILE_SENDS_PER_HOUR", 5),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            model_daily_requests=max(1, _int("MODEL_DAILY_REQUESTS", 120)),
            model_daily_tokens=max(1, _int("MODEL_DAILY_TOKENS", 500000)),
            model_input_usd_per_million_tokens=input_rate,
            model_output_usd_per_million_tokens=output_rate,
            model_pricing_as_of=pricing_date,
            model_daily_budget_usd=budget if budget and budget > 0 else None,
            model_cost_alert_percent=min(100, max(1, _int("MODEL_COST_ALERT_PERCENT", 80))),
            google_access_token=read_secret("GOOGLE_ACCESS_TOKEN"),
            google_client_id=os.getenv("GOOGLE_CLIENT_ID") or None,
            google_client_secret=read_secret("GOOGLE_CLIENT_SECRET"),
            google_refresh_token=read_secret("GOOGLE_REFRESH_TOKEN"),
            home_assistant_url=os.getenv("HOME_ASSISTANT_URL", "").strip().rstrip("/") or None,
            home_assistant_token=read_secret("HOME_ASSISTANT_TOKEN"),
            maps_api_key=read_secret("MAPS_API_KEY"),
            maps_provider=os.getenv("MAPS_PROVIDER", "google").strip().lower(),
            notion_token=read_secret("NOTION_TOKEN"),
            notion_database_id=os.getenv("NOTION_DATABASE_ID") or None,
            trello_key=read_secret("TRELLO_KEY"),
            trello_token=read_secret("TRELLO_TOKEN"),
            hubspot_token=read_secret("HUBSPOT_TOKEN"),
            odoo_url=os.getenv("ODOO_URL", "").strip().rstrip("/") or None,
            odoo_database=os.getenv("ODOO_DATABASE") or None,
            odoo_username=os.getenv("ODOO_USERNAME") or None,
            odoo_api_key=read_secret("ODOO_API_KEY"),
            dokploy_url=os.getenv("DOKPLOY_URL", "").strip().rstrip("/") or None,
            dokploy_api_key=read_secret("DOKPLOY_API_KEY"),
            dokploy_allowed_projects=tuple(item.strip() for item in
                os.getenv("DOKPLOY_ALLOWED_PROJECTS", "").split(",") if item.strip()),
            meta_graph_token=read_secret("META_GRAPH_TOKEN"),
            meta_page_id=os.getenv("META_PAGE_ID") or None,
            meta_graph_api_version=os.getenv("META_GRAPH_API_VERSION", "v26.0").strip(),
            realtime_voice_enabled=_bool("ENABLE_REALTIME_VOICE"),
            realtime_voice_app_url=os.getenv("REALTIME_VOICE_APP_URL", "").strip() or None,
            realtime_voice_model=os.getenv("REALTIME_VOICE_MODEL", "gpt-realtime-2.1-mini").strip(),
            realtime_voice_output=os.getenv("REALTIME_VOICE_OUTPUT", "marin").strip(),
            realtime_voice_max_seconds=min(900, max(30, _int("REALTIME_VOICE_MAX_SECONDS", 300))),
            realtime_voice_sessions_per_hour=min(20, max(1, _int("REALTIME_VOICE_SESSIONS_PER_HOUR", 3))),
        )

    def validate_production(self) -> None:
        if not self.bot_token:
            raise RuntimeError("BOT_TOKEN_FILE secret mount kiritilmagan")
        if self.owner_telegram_id <= 0 or self.owner_chat_id <= 0:
            raise RuntimeError("OWNER_TELEGRAM_ID va OWNER_CHAT_ID musbat son bo‘lishi kerak")
        if not self.data_key:
            raise RuntimeError("DATA_KEY_FILE secret mount kiritilmagan")
        if not self.text_model_ready:
            raise RuntimeError("ENABLE_OPENAI=1 va OPENAI_API_KEY kerak")
        if self.realtime_voice_enabled:
            from urllib.parse import urlsplit
            parsed = urlsplit(self.realtime_voice_app_url or "")
            if (not self.text_model_ready
                    or parsed.scheme != "https" or not parsed.netloc
                    or parsed.username or parsed.password or parsed.query or parsed.fragment):
                raise RuntimeError("Jonli ovoz uchun HTTPS URL va sozlangan matn modeli kerak")

