from __future__ import annotations

import base64
import binascii
from email.message import EmailMessage
import json
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from app.core.config import Settings


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, uncertain: bool = False):
        super().__init__(message)
        self.uncertain = uncertain


READ_OPERATIONS = frozenset({
    "calendar.events", "calendar.freebusy", "email.search", "email.read", "email.attachments",
    "email.attachment_download", "email.calendar_invite",
    "drive.search", "sheets.read", "notion.search", "trello.boards", "trello.cards",
    "crm.search", "inventory.search", "inventory.orders", "inventory.quote", "accounting.summary",
    "maps.geocode", "maps.route", "home.states", "home.state", "dokploy.projects", "dokploy.deployments",
})

WRITE_OPERATIONS = frozenset({
    "calendar.create", "calendar.update", "calendar.cancel", "email.draft", "email.send",
    "sheets.update", "notion.create", "notion.update", "trello.create", "trello.update",
    "trello.archive", "crm.create", "crm.update", "home.call", "dokploy.restart",
    "dokploy.deploy", "dokploy.rollback", "social.publish", "docker.cleanup",
})

HOME_ASSISTANT_ALLOWED_SERVICES = {
    "light": frozenset({"turn_on", "turn_off", "toggle"}),
    "switch": frozenset({"turn_on", "turn_off", "toggle"}),
    "climate": frozenset({"set_temperature", "set_hvac_mode", "turn_off"}),
    "cover": frozenset({"open_cover", "close_cover", "stop_cover", "set_cover_position"}),
    "fan": frozenset({"turn_on", "turn_off", "toggle", "set_percentage"}),
}
HOME_ASSISTANT_ALLOWED_FIELDS = {
    "light": frozenset({"brightness", "brightness_pct", "color_temp_kelvin", "rgb_color", "hs_color", "transition", "effect"}),
    "switch": frozenset(),
    "climate": frozenset({"temperature", "hvac_mode", "preset_mode", "fan_mode"}),
    "cover": frozenset({"position"}),
    "fan": frozenset({"percentage", "percentage_step", "preset_mode"}),
}

_DOCKER_CLEANUP_BATCH = 8
_DOCKER_VOLUME_NAME_MAX = 96

OPERATION_PROVIDER = {
    **{name: "google" for name in (
        "calendar.events", "calendar.freebusy", "calendar.create", "calendar.update", "calendar.cancel",
        "email.search", "email.read", "email.attachments", "email.attachment_download", "email.calendar_invite",
        "email.draft", "email.send", "drive.search",
        "sheets.read", "sheets.update")},
    **{name: "notion" for name in ("notion.search", "notion.create", "notion.update")},
    **{name: "trello" for name in ("trello.boards", "trello.cards", "trello.create", "trello.update", "trello.archive")},
    **{name: "hubspot" for name in ("crm.search", "crm.create", "crm.update")},
    **{name: "odoo" for name in ("inventory.search", "inventory.orders", "inventory.quote", "accounting.summary")},
    **{name: "maps" for name in ("maps.geocode", "maps.route")},
    **{name: "home_assistant" for name in ("home.states", "home.state", "home.call")},
    **{name: "dokploy" for name in ("dokploy.projects", "dokploy.deployments", "dokploy.restart", "dokploy.deploy", "dokploy.rollback")},
    "docker.cleanup": "server_docker",
    "social.publish": "meta",
}


def _clean(value: object, limit: int = 300) -> str:
    return " ".join(str(value or "").split())[:limit]


def _format_size_bytes(size: int) -> str:
    value = max(0, size)
    if value >= 1024 ** 3:
        return f"{value / (1024 ** 3):.2f} GiB"
    if value >= 1024 ** 2:
        return f"{value / (1024 ** 2):.2f} MiB"
    if value >= 1024:
        return f"{value / 1024:.2f} KiB"
    return f"{value} B"


def _decode_base64url(value: object, limit: int = 12000) -> str:
    raw = str(value or "").encode("ascii", errors="ignore")[:limit * 2]
    try:
        decoded = base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4))
        return decoded.decode("utf-8", errors="replace")[:limit]
    except (ValueError, binascii.Error):
        return ""


_ICAL_MAX_BYTES = 64 * 1024
_ICAL_WEEKDAYS = {"MO", "TU", "WE", "TH", "FR", "SA", "SU"}


def _decode_ical_base64url(value: object) -> bytes:
    try:
        raw = str(value or "").encode("ascii", errors="strict")
    except UnicodeEncodeError as exc:
        raise ProviderError("Taklifnoma .ics ilovasining kodlanishi yaroqsiz.") from exc
    if len(raw) > ((_ICAL_MAX_BYTES + 2) // 3) * 4 + 8:
        raise ProviderError("Taklifnoma .ics ilovasi 64 KiB chegaradan katta.")
    raw += b"=" * (-len(raw) % 4)
    try:
        decoded = base64.b64decode(raw, altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ProviderError("Taklifnoma .ics ilovasining kodlanishi yaroqsiz.") from exc
    if len(decoded) > _ICAL_MAX_BYTES:
        raise ProviderError("Taklifnoma .ics ilovasi 64 KiB chegaradan katta.")
    return decoded


def _ical_unescape(value: str, limit: int) -> str:
    value = re.sub(r"\\[nN]", "\n", value)
    value = re.sub(r"\\([\\,;])", r"\1", value)
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)
    return value[:limit].strip()


def _ical_property(line: str) -> tuple[str, dict[str, str], str] | None:
    left, separator, value = line.partition(":")
    if not separator:
        return None
    parts = left.split(";")
    name = parts[0].upper()
    if not re.fullmatch(r"[A-Z0-9-]+", name):
        return None
    params: dict[str, str] = {}
    for part in parts[1:]:
        key, equals, raw = part.partition("=")
        if equals and re.fullmatch(r"[A-Z0-9-]+", key.upper()):
            params[key.upper()] = raw.strip().strip('"')
    return name, params, value


def _ical_time(params: dict[str, str], value: str) -> date | datetime:
    if params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", value):
        try:
            return datetime.strptime(value, "%Y%m%d").date()
        except ValueError as exc:
            raise ProviderError("Taklifnoma sanasi noto‘g‘ri formatda.") from exc
    utc_value = value.endswith("Z")
    raw = value[:-1] if utc_value else value
    fmt = "%Y%m%dT%H%M%S" if re.fullmatch(r"\d{8}T\d{6}", raw) else "%Y%m%dT%H%M"
    try:
        local_value = datetime.strptime(raw, fmt)
    except ValueError as exc:
        raise ProviderError("Taklifnoma boshlanish/tugash vaqti noto‘g‘ri.") from exc
    if utc_value:
        return local_value.replace(tzinfo=UTC)
    tzid = params.get("TZID")
    if not tzid:
        raise ProviderError("Taklifnomada vaqt zonasi yo‘q; tadbir vaqti xavfsiz aniqlanmadi.")
    try:
        zone = ZoneInfo(tzid)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ProviderError("Taklifnomadagi vaqt zonasi tanilmadi.") from exc
    first = local_value.replace(tzinfo=zone, fold=0)
    second = local_value.replace(tzinfo=zone, fold=1)
    if first.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != local_value:
        raise ProviderError("Taklifnomadagi mahalliy vaqt mavjud bo‘lmagan soatga to‘g‘ri keldi.")
    if first.utcoffset() != second.utcoffset():
        raise ProviderError("Taklifnomadagi mahalliy vaqt ikki xil UTC siljishiga mos; aniqlashtirish kerak.")
    return first


def _ical_duration(value: str) -> timedelta:
    match = re.fullmatch(
        r"P(?:(\d+)W|(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?)", value
    )
    if (not match or not any(group is not None for group in match.groups())
            or ("T" in value and not any(group is not None for group in match.groups()[2:]))):
        raise ProviderError("Taklifnomadagi tadbir davomiyligi qo‘llab-quvvatlanmaydi.")
    weeks, days, hours, minutes, seconds = (int(item or 0) for item in match.groups())
    duration = timedelta(weeks=weeks, days=days, hours=hours, minutes=minutes, seconds=seconds)
    if duration <= timedelta(0) or duration > timedelta(days=366):
        raise ProviderError("Taklifnomadagi tadbir davomiyligi yaroqsiz.")
    return duration


def _ical_recurrence(value: str) -> list[str]:
    if len(value) > 512 or "\n" in value or "\r" in value:
        raise ProviderError("Taklifnomadagi takrorlanish qoidasi qo‘llab-quvvatlanmaydi.")
    fields: dict[str, str] = {}
    for part in value.upper().split(";"):
        key, separator, raw = part.partition("=")
        if not separator or key not in {"FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY",
                                         "BYMONTHDAY", "BYMONTH", "BYSETPOS", "WKST"} or key in fields:
            raise ProviderError("Taklifnomadagi takrorlanish qoidasi yaroqsiz.")
        fields[key] = raw
    if fields.get("FREQ") not in {"DAILY", "WEEKLY", "MONTHLY", "YEARLY"}:
        raise ProviderError("Taklifnomadagi takrorlanish turi qo‘llab-quvvatlanmaydi.")
    for key, low, high in (("INTERVAL", 1, 999), ("COUNT", 1, 9999)):
        if key in fields and (not fields[key].isdigit() or not low <= int(fields[key]) <= high):
            raise ProviderError("Taklifnomadagi takrorlanish soni yaroqsiz.")
    if "COUNT" in fields and "UNTIL" in fields:
        raise ProviderError("Taklifnomadagi COUNT va UNTIL birga ishlatilmaydi.")
    if "UNTIL" in fields and not re.fullmatch(r"\d{8}(?:T\d{6}Z)?", fields["UNTIL"]):
        raise ProviderError("Taklifnomadagi takrorlanish tugash sanasi yaroqsiz.")
    if "WKST" in fields and fields["WKST"] not in _ICAL_WEEKDAYS:
        raise ProviderError("Taklifnomadagi hafta boshlanish kuni yaroqsiz.")
    for key, pattern in (("BYDAY", r"(?:[+-]?\d{1,2})?(?:MO|TU|WE|TH|FR|SA|SU)"),
                         ("BYMONTHDAY", r"-?(?:[1-9]|[12]\d|3[01])"),
                         ("BYMONTH", r"(?:[1-9]|1[0-2])"),
                         ("BYSETPOS", r"-?(?:[1-9]|[1-9]\d|[12]\d\d|3[0-5]\d|36[0-6])")):
        if key in fields and (not fields[key] or any(not re.fullmatch(pattern, item)
                                                     for item in fields[key].split(","))):
            raise ProviderError("Taklifnomadagi takrorlanish tafsiloti yaroqsiz.")
    return ["RRULE:" + ";".join(f"{key}={fields[key]}" for key in sorted(fields))]


def _parse_calendar_invite(raw: bytes) -> dict[str, Any]:
    if not raw or len(raw) > _ICAL_MAX_BYTES:
        raise ProviderError("Taklifnoma .ics ilovasi bo‘sh yoki 64 KiB chegaradan katta.")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ProviderError("Taklifnoma .ics ilovasi UTF-8 matn emas.") from exc
    unfolded: list[str] = []
    for line in re.split(r"\r\n|\n|\r", text):
        if line.startswith((" ", "\t")) and unfolded:
            unfolded[-1] += line[1:]
        elif line:
            unfolded.append(line)
    if (not any(line.upper() == "BEGIN:VCALENDAR" for line in unfolded)
            or not any(line.upper() == "END:VCALENDAR" for line in unfolded)):
        raise ProviderError("Gmail xatida iCalendar (.ics) taklifnomasi topilmadi.")
    method = next((prop[2].upper() for line in unfolded
                   if (prop := _ical_property(line)) and prop[0] == "METHOD"), "")
    if method == "CANCEL":
        raise ProviderError("Bu .ics xati bekor qilingan taklif; yangi tadbir yaratilmadi.")
    events: list[list[str]] = []
    current: list[str] | None = None
    for line in unfolded:
        if line.upper() == "BEGIN:VEVENT":
            if current is not None:
                raise ProviderError("Taklifnoma tarkibi noto‘g‘ri tuzilgan.")
            current = []
        elif line.upper() == "END:VEVENT":
            if current is None:
                raise ProviderError("Taklifnoma tarkibi noto‘g‘ri tuzilgan.")
            events.append(current)
            current = None
        elif current is not None:
            current.append(line)
    if current is not None or len(events) != 1:
        raise ProviderError("Taklifnomada aynan bitta tadbir bo‘lishi kerak; bir nechta tadbir avtomatik tanlanmadi.")
    props: dict[str, list[tuple[dict[str, str], str]]] = {}
    for line in events[0]:
        parsed = _ical_property(line)
        if parsed:
            name, params, value = parsed
            props.setdefault(name, []).append((params, value))

    def one(name: str, *, required: bool = False) -> tuple[dict[str, str], str] | None:
        values = props.get(name, [])
        if len(values) > 1:
            raise ProviderError(f"Taklifnomada {name} maydoni bir nechta; aniqlashtirish kerak.")
        if required and not values:
            raise ProviderError(f"Taklifnomada {name} maydoni yo‘q.")
        return values[0] if values else None

    summary = _ical_unescape(one("SUMMARY", required=True)[1], 300)  # type: ignore[index]
    if not summary:
        raise ProviderError("Taklifnoma sarlavhasi bo‘sh.")
    start_prop = one("DTSTART", required=True)
    assert start_prop is not None
    start_value = _ical_time(*start_prop)
    end_prop = one("DTEND")
    duration_prop = one("DURATION")
    if end_prop and duration_prop:
        raise ProviderError("Taklifnomada DTEND va DURATION birga berilgan.")
    if end_prop:
        end_value = _ical_time(*end_prop)
    elif duration_prop:
        duration = _ical_duration(duration_prop[1])
        if isinstance(start_value, date) and not isinstance(start_value, datetime) and duration.seconds:
            raise ProviderError("Kun bo‘yi tadbir davomiyligi butun kunlarda berilishi kerak.")
        end_value = start_value + duration
    else:
        raise ProviderError("Taklifnomada tugash vaqti yoki davomiyligi yo‘q; vaqt taxmin qilinmadi.")
    all_day = isinstance(start_value, date) and not isinstance(start_value, datetime)
    if all_day != (isinstance(end_value, date) and not isinstance(end_value, datetime)):
        raise ProviderError("Taklifnomadagi boshlanish va tugash sanalarining turlari mos emas.")
    if end_value <= start_value:
        raise ProviderError("Taklifnomadagi tugash vaqti boshlanishidan keyin bo‘lishi kerak.")

    def google_time(value: date | datetime) -> dict[str, str]:
        if isinstance(value, datetime):
            tz_name = getattr(value.tzinfo, "key", None) or ("UTC" if value.utcoffset() == timedelta(0) else None)
            if not tz_name:
                raise ProviderError("Taklifnoma vaqti uchun aniq vaqt zonasi talab qilinadi.")
            return {"dateTime": value.isoformat(timespec="seconds"), "timeZone": tz_name}
        return {"date": value.isoformat()}

    event: dict[str, Any] = {"summary": summary, "start": google_time(start_value), "end": google_time(end_value)}
    location = one("LOCATION")
    if location:
        text_value = _ical_unescape(location[1], 500)
        if text_value:
            event["location"] = text_value
    description = one("DESCRIPTION")
    notes = _ical_unescape(description[1], 4000) if description else ""
    event_url = one("URL")
    if event_url:
        url = _ical_unescape(event_url[1], 1000)
        if url and url not in notes:
            notes = (notes + "\n" if notes else "") + "Uchrashuv havolasi: " + url
    if notes:
        event["description"] = notes
    recurrence = one("RRULE")
    if recurrence:
        event["recurrence"] = _ical_recurrence(recurrence[1])
    return event


class ProviderHub:
    """ENV-driven, allowlisted API adapters. Model inputs never select arbitrary URLs."""

    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(20, connect=7),
            follow_redirects=False, trust_env=False, transport=transport)
        self._google_token: str | None = settings.google_access_token

    async def close(self) -> None:
        await self.client.aclose()

    async def execute(self, operation: str, payload: dict[str, Any], *, approved: bool = False) -> dict[str, Any]:
        if operation not in READ_OPERATIONS | WRITE_OPERATIONS:
            raise ProviderError("Bu xizmat amali qo‘llab-quvvatlanmaydi.")
        if operation in WRITE_OPERATIONS and not approved:
            if operation == "docker.cleanup":
                return await self._prepare_docker_cleanup(payload)
            return {"status": "needs_confirmation", "operation": operation,
                    "preview": self.preview(operation, payload), "payload": payload}
        provider = OPERATION_PROVIDER[operation]
        if provider == "google":
            return await self._google(operation, payload)
        if provider == "notion":
            return await self._notion(operation, payload)
        if provider == "trello":
            return await self._trello(operation, payload)
        if provider == "hubspot":
            return await self._hubspot(operation, payload)
        if provider == "odoo":
            return await self._odoo(operation, payload)
        if provider == "maps":
            return await self._maps(operation, payload)
        if provider == "home_assistant":
            return await self._home_assistant(operation, payload)
        if provider == "dokploy":
            return await self._dokploy(operation, payload)
        if provider == "meta":
            return await self._meta(operation, payload)
        if provider == "server_docker":
            return await self._docker_cleanup(operation, payload)
        raise ProviderError("Integratsiya adapteri topilmadi.")

    @staticmethod
    def preview(operation: str, payload: dict[str, Any]) -> str:
        if operation == "docker.cleanup":
            names = payload.get("volume_names")
            details = payload.get("volume_details")
            if not isinstance(names, list) or not isinstance(details, list) or len(names) != len(details):
                raise ProviderError("Docker tasdiq oynasi uchun hajmi va loyiha ma’lumoti bilan aniq ro‘yxat kerak.")
            lines = ["Foydalanilmayotgan Docker volume’larni o‘chirish", "Tasdiqlanadigan aniq ro‘yxat:"]
            sizes: list[int] = []
            for index, item in enumerate(details, start=1):
                if not isinstance(item, dict) or item.get("name") != names[index - 1]:
                    raise ProviderError("Docker tasdiq oynasidagi nomlar mos emas.")
                size = item.get("estimated_size_bytes")
                if type(size) is int and size >= 0:
                    sizes.append(size)
                    size_text = _format_size_bytes(size)
                else:
                    size_text = "hajmi noma’lum"
                project = _clean(item.get("project"), 80) or "loyiha noma’lum"
                service = _clean(item.get("service"), 60)
                label = project + (f"/{service}" if service else "")
                lines.append(f"{index}. {names[index - 1]} · {size_text} · {label}")
            if sizes and len(sizes) == len(details):
                lines.append(f"Taxminiy jami: {_format_size_bytes(sum(sizes))}")
            else:
                lines.append("Taxminiy jami: noma’lum (kamida bitta volume’da hajm ko‘rsatilmagan)")
            more = payload.get("more_candidates")
            if type(more) is int and more > 0:
                lines.append(f"Yana {more} ta nol-havolali nomzod bor; ular bu tasdiqqa kirmaydi.")
            lines.extend((
                "Tasdiqlashdan oldin server hozirgi foydalanish sonini qayta tekshiradi.",
                "Faqat shu nomlar ko‘rib chiqiladi; Docker image’lari o‘zgarmaydi.",
            ))
            return "\n".join(lines)
        labels = {
            "calendar.create": "Google Calendar’da tadbir yaratish",
            "calendar.update": "Google Calendar tadbirini tahrirlash",
            "calendar.cancel": "Google Calendar tadbirini bekor qilish",
            "email.draft": "Gmail’da qoralama yaratish",
            "email.send": "Gmail’dan xat yuborish",
            "sheets.update": "Google Sheets kataklarini yangilash",
            "notion.create": "Notion’da sahifa yaratish",
            "notion.update": "Notion sahifasini tahrirlash",
            "trello.create": "Trello kartasi yaratish",
            "trello.update": "Trello kartasini tahrirlash",
            "trello.archive": "Trello kartasini arxivlash",
            "crm.create": "HubSpot kontaktini yaratish",
            "crm.update": "HubSpot kontaktini tahrirlash",
            "home.call": "Aqlli uy qurilmasini boshqarish",
            "dokploy.restart": "Dokploy xizmatini qayta yuklash",
            "dokploy.deploy": "Dokploy’da qayta deploy qilish",
            "dokploy.rollback": "Dokploy deployini qaytarish",
            "social.publish": "Meta sahifasida post chiqarish",
            "docker.cleanup": "Aniq ko‘rsatilgan, foydalanilmayotgan Docker volume’larni o‘chirish",
        }
        lines = [labels.get(operation, operation)]
        fields = {
            "calendar.create": ("summary", "start", "end", "location", "attendees"),
            "calendar.update": ("event_id", "summary", "start", "end", "location", "attendees"),
            "calendar.cancel": ("event_id",),
            "email.draft": ("to", "subject", "body"),
            "email.send": ("to", "subject", "body"),
            "sheets.update": ("spreadsheet_id", "range", "values"),
            "notion.create": ("title", "body"),
            "notion.update": ("page_id", "title", "body"),
            "trello.create": ("list_id", "title", "body", "due"),
            "trello.update": ("card_id", "title", "body", "due"),
            "trello.archive": ("card_id",),
            "crm.create": ("firstname", "lastname", "email", "phone", "company"),
            "crm.update": ("contact_id", "firstname", "lastname", "email", "phone", "company"),
            "home.call": ("entity_id", "domain", "service", "service_data"),
            "dokploy.restart": ("project_id", "resource_type", "resource_id"),
            "dokploy.deploy": ("project_id", "resource_type", "resource_id"),
            "dokploy.rollback": ("project_id", "resource_type", "resource_id", "rollback_id"),
            "social.publish": ("message",),
        }.get(operation, ())
        for key in fields:
            if key not in payload or payload[key] is None:
                continue
            value = payload[key]
            if isinstance(value, (dict, list)):
                import json
                rendered = json.dumps(value, ensure_ascii=False, default=str)
            else:
                rendered = str(value)
            lines.append(f"{key}: {rendered[:700]}")
        lines.append("Tasdiqdan so‘ng shu ko‘rsatilgan qiymatlar ishlatiladi.")
        return "\n".join(lines)[:1800]

    async def _request(self, method: str, url: str, *, provider: str,
                       headers: dict[str, str] | None = None,
                       params: dict[str, Any] | None = None,
                       json: Any = None, data: dict[str, Any] | None = None) -> Any:
        try:
            response = await self.client.request(method, url, headers=headers, params=params, json=json, data=data)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ProviderError(f"{provider} xizmatiga ulanishda vaqtinchalik tarmoq xatosi.",
                                uncertain=method.upper() in {"POST", "PUT", "PATCH", "DELETE"}) from exc
        if response.status_code >= 400:
            code = response.status_code
            if code in {401, 403}:
                raise ProviderError(f"{provider} ruxsati yoki tokeni rad etildi (HTTP {code}).")
            if code == 404:
                raise ProviderError(f"{provider} so‘ralgan yozuvni topmadi (HTTP 404).")
            if code == 429:
                raise ProviderError(f"{provider} so‘rov limiti vaqtincha tugadi (HTTP 429).")
            raise ProviderError(f"{provider} so‘rovi bajarilmadi (HTTP {code}); xizmat javobi yashirildi.",
                                uncertain=code >= 500 and method.upper() in {"POST", "PUT", "PATCH", "DELETE"})
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderError(f"{provider} yaroqsiz JSON javob qaytardi.") from exc

    async def _bounded_google_json(self, url: str, *, headers: dict[str, str],
                                   params: dict[str, Any] | None, max_bytes: int,
                                   purpose: str) -> dict[str, Any]:
        chunks: list[bytes] = []
        size = 0
        try:
            async with self.client.stream("GET", url, headers=headers, params=params) as response:
                if response.status_code >= 400:
                    code = response.status_code
                    if code in {401, 403}:
                        raise ProviderError(f"Gmail ruxsati yoki tokeni rad etildi (HTTP {code}).")
                    if code == 404:
                        raise ProviderError(f"Gmail {purpose} topilmadi (HTTP 404).")
                    if code == 429:
                        raise ProviderError("Gmail so‘rov limiti vaqtincha tugadi (HTTP 429).")
                    raise ProviderError(f"Gmail {purpose} HTTP {code} xatosi bilan tugadi; javob yashirildi.")
                length = response.headers.get("content-length")
                if length:
                    try:
                        if int(length) > max_bytes:
                            raise ProviderError(f"Gmail {purpose} hajmi xavfsiz chegaradan katta.")
                    except ValueError as exc:
                        raise ProviderError(f"Gmail {purpose} javobining hajmi yaroqsiz.") from exc
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise ProviderError(f"Gmail {purpose} hajmi xavfsiz chegaradan katta.")
                    chunks.append(chunk)
        except ProviderError:
            raise
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ProviderError(f"Gmail {purpose} uchun vaqtinchalik tarmoq xatosi.") from exc
        try:
            result = json.loads(b"".join(chunks))
        except (ValueError, TypeError, UnicodeDecodeError) as exc:
            raise ProviderError(f"Gmail {purpose} yaroqsiz JSON javob qaytardi.") from exc
        if not isinstance(result, dict):
            raise ProviderError(f"Gmail {purpose} javobi obyekt emas.")
        return result

    async def _google_auth(self) -> dict[str, str]:
        token = self._google_token
        if not token and self.settings.google_client_id and self.settings.google_client_secret and self.settings.google_refresh_token:
            data = await self._request("POST", "https://oauth2.googleapis.com/token", provider="Google OAuth",
                data={"client_id": self.settings.google_client_id,
                        "client_secret": self.settings.google_client_secret,
                        "refresh_token": self.settings.google_refresh_token,
                        "grant_type": "refresh_token"})
            token = _clean(data.get("access_token"), 5000)
            if not token:
                raise ProviderError("Google OAuth yangilangan access token bermadi.")
            self._google_token = token
        if not token:
            raise ProviderError("Google Calendar/Gmail/Drive/Sheets uchun Google OAuth tokeni sozlanmagan.")
        return {"Authorization": f"Bearer {token}"}

    async def _google(self, operation: str, p: dict[str, Any]) -> dict[str, Any]:
        headers = await self._google_auth()
        if operation == "calendar.events":
            calendar_id = quote(_clean(p.get("calendar_id") or "primary", 200), safe="")
            params = {"maxResults": max(1, min(50, int(p.get("limit") or 10))),
                      "singleEvents": "true", "orderBy": "startTime"}
            if p.get("time_min"):
                params["timeMin"] = _clean(p["time_min"], 64)
            if p.get("time_max"):
                params["timeMax"] = _clean(p["time_max"], 64)
            data = await self._request("GET", f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events",
                                       provider="Google Calendar", headers=headers, params=params)
            items = data.get("items") if isinstance(data, dict) else []
            return {"provider": "google_calendar", "events": [{
                "id": _clean(x.get("id"), 300), "title": _clean(x.get("summary"), 300),
                "start": (x.get("start") or {}).get("dateTime") or (x.get("start") or {}).get("date"),
                "end": (x.get("end") or {}).get("dateTime") or (x.get("end") or {}).get("date"),
                "status": _clean(x.get("status"), 50), "url": _clean(x.get("htmlLink"), 800),
            } for x in (items or [])[:50] if isinstance(x, dict)]}
        if operation == "calendar.freebusy":
            body = {"timeMin": _clean(p.get("time_min"), 64), "timeMax": _clean(p.get("time_max"), 64),
                    "timeZone": _clean(p.get("time_zone") or "Asia/Tashkent", 80),
                    "items": [{"id": _clean(p.get("calendar_id") or "primary", 200)}]}
            data = await self._request("POST", "https://www.googleapis.com/calendar/v3/freeBusy",
                                       provider="Google Calendar", headers=headers, json=body)
            calendar = ((data.get("calendars") or {}).get(body["items"][0]["id"]) or {})
            return {"provider": "google_calendar", "busy": calendar.get("busy") or [],
                    "errors": calendar.get("errors") or [], "time_min": body["timeMin"],
                    "time_max": body["timeMax"]}
        if operation == "email.calendar_invite":
            message_id = _clean(p.get("message_id"), 200)
            if not message_id:
                raise ProviderError("Taklifnomani topish uchun email.search natijasidagi message_id kerak.")
            message = await self._bounded_google_json(
                f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{quote(message_id, safe='')}",
                headers=headers, params={"format": "full"}, max_bytes=1024 * 1024,
                purpose="taklifnoma xati")
            payload = message.get("payload") or {}
            headers_map = {str(item.get("name", "")).lower(): _clean(item.get("value"), 500)
                           for item in payload.get("headers", []) if isinstance(item, dict)}
            candidates: list[dict[str, Any]] = []

            def find_ical_parts(part: dict[str, Any]) -> None:
                if len(candidates) > 1:
                    return
                filename = _clean(part.get("filename"), 240)
                mime_type = _clean(part.get("mimeType"), 120).lower()
                body = part.get("body") or {}
                is_calendar = (mime_type in {"text/calendar", "application/ics", "text/x-vcalendar"}
                               or filename.lower().endswith(".ics"))
                if is_calendar and (body.get("data") or body.get("attachmentId")):
                    candidates.append({"filename": filename or "email-invite.ics", "body": body})
                for child in part.get("parts") or []:
                    if isinstance(child, dict) and len(candidates) < 2:
                        find_ical_parts(child)

            if isinstance(payload, dict):
                find_ical_parts(payload)
            if len(candidates) != 1:
                if len(candidates) > 1:
                    raise ProviderError("Xatda bir nechta .ics taklifnomasi bor; qaysi biri kerakligini aniqlashtiring.")
                raise ProviderError("Xatda .ics taklifnomasi topilmadi. Avval email.search yoki email.attachments bilan xatni aniqlang.")
            body = candidates[0]["body"]
            reported_size = body.get("size")
            if type(reported_size) is int and not 0 <= reported_size <= _ICAL_MAX_BYTES:
                raise ProviderError("Taklifnoma .ics ilovasi 64 KiB chegaradan katta.")
            if body.get("data"):
                try:
                    raw_ical = _decode_ical_base64url(body["data"])
                except UnicodeEncodeError as exc:
                    raise ProviderError("Taklifnoma .ics ilovasining kodlanishi yaroqsiz.") from exc
            else:
                attachment_id = _clean(body.get("attachmentId"), 300)
                if not attachment_id:
                    raise ProviderError("Taklifnoma .ics ilovasining Gmail ID si yo‘q.")
                url = ("https://gmail.googleapis.com/gmail/v1/users/me/messages/"
                       f"{quote(message_id, safe='')}/attachments/{quote(attachment_id, safe='')}")
                encoded_limit = ((_ICAL_MAX_BYTES + 2) // 3) * 4 + 2048
                attachment = await self._bounded_google_json(
                    url, headers=headers, params=None, max_bytes=encoded_limit,
                    purpose="taklifnoma ilovasi")
                attachment_size = attachment.get("size")
                if type(attachment_size) is int and not 0 <= attachment_size <= _ICAL_MAX_BYTES:
                    raise ProviderError("Taklifnoma .ics ilovasi 64 KiB chegaradan katta.")
                raw_ical = _decode_ical_base64url(attachment.get("data"))
                if type(attachment_size) is int and attachment_size != len(raw_ical):
                    raise ProviderError("Gmail ilova hajmi javobdagi baytlar soniga mos emas.")
            event = _parse_calendar_invite(raw_ical)
            return {"provider": "gmail", "message_id": message_id,
                    "email_subject": headers_map.get("subject", ""),
                    "email_from": headers_map.get("from", ""),
                    "calendar_event": event, "attendees_included": False,
                    "attendee_notifications_sent": False,
                    "note": "Bu faqat tadbir qoralamasi. Ishtirokchilar nusxalanmadi va hech kimga taklif yuborilmadi."}
        if operation in {"calendar.create", "calendar.update", "calendar.cancel"}:
            calendar_id = quote(_clean(p.get("calendar_id") or "primary", 200), safe="")
            event_id = quote(_clean(p.get("event_id"), 300), safe="")
            if operation == "calendar.cancel":
                if not event_id:
                    raise ProviderError("Tadbirni bekor qilish uchun event_id kerak.")
                await self._request("DELETE", f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events/{event_id}",
                                    provider="Google Calendar", headers=headers)
                return {"status": "succeeded", "event_id": event_id}
            event = {key: p[key] for key in ("summary", "description", "location", "start", "end", "attendees", "recurrence") if key in p}
            if operation == "calendar.create":
                if not _clean(p.get("summary"), 300) or not isinstance(p.get("start"), dict) or not isinstance(p.get("end"), dict):
                    raise ProviderError("Tadbir yaratish uchun sarlavha, boshlanish va tugash vaqti kerak.")
                data = await self._request("POST", f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events",
                    provider="Google Calendar", headers=headers, json=event)
            else:
                if not event_id:
                    raise ProviderError("Taqvim tadbirini yangilash uchun event_id kerak.")
                if len(event) == 0:
                    raise ProviderError("Tadbirni o‘zgartirish uchun yangilanadigan maydon kerak.")
                data = await self._request("PATCH", f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events/{event_id}",
                    provider="Google Calendar", headers=headers, json=event)
            return {"status": "succeeded", "event_id": _clean(data.get("id"), 300),
                    "title": _clean(data.get("summary"), 300), "url": _clean(data.get("htmlLink"), 800)}
        if operation == "email.search":
            data = await self._request("GET", "https://gmail.googleapis.com/gmail/v1/users/me/messages",
                provider="Gmail", headers=headers, params={"q": _clean(p.get("query"), 500),
                "maxResults": max(1, min(30, int(p.get("limit") or 10)))})
            messages = data.get("messages") if isinstance(data, dict) else []
            results = []
            for item in (messages or [])[:30]:
                mid = _clean(item.get("id"), 200)
                if not mid:
                    continue
                msg = await self._request("GET", f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{quote(mid, safe='')}",
                    provider="Gmail", headers=headers, params={"format": "metadata",
                    "metadataHeaders": ["From", "To", "Subject", "Date"]})
                headers_map = {str(h.get("name", "")).lower(): _clean(h.get("value"), 500)
                               for h in (msg.get("payload") or {}).get("headers", []) if isinstance(h, dict)}
                results.append({"id": mid, "thread_id": _clean(msg.get("threadId"), 200),
                    "from": headers_map.get("from", ""), "to": headers_map.get("to", ""),
                    "subject": headers_map.get("subject", ""), "date": headers_map.get("date", ""),
                    "snippet": _clean(msg.get("snippet"), 600)})
            return {"provider": "gmail", "messages": results}
        if operation == "email.attachments":
            query = _clean(p.get("query"), 500)
            if "has:attachment" not in query.casefold():
                query = (query + " has:attachment").strip()
            data = await self._request("GET", "https://gmail.googleapis.com/gmail/v1/users/me/messages",
                provider="Gmail", headers=headers, params={"q": query,
                "maxResults": max(1, min(20, int(p.get("limit") or 10)))})
            messages = data.get("messages") if isinstance(data, dict) else []
            attachments: list[dict[str, Any]] = []
            for item in (messages or [])[:20]:
                message_id = _clean(item.get("id"), 200)
                if not message_id:
                    continue
                message = await self._request("GET", f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{quote(message_id, safe='')}",
                    provider="Gmail", headers=headers, params={"format": "full"})
                payload = message.get("payload") or {}
                headers_map = {str(h.get("name", "")).lower(): _clean(h.get("value"), 500)
                               for h in payload.get("headers", []) if isinstance(h, dict)}
                def walk_attachments(part: dict[str, Any]) -> None:
                    if len(attachments) >= 100:
                        return
                    filename = _clean(part.get("filename"), 240)
                    body = part.get("body") or {}
                    if filename:
                        attachments.append({"message_id": message_id,
                            "thread_id": _clean(message.get("threadId"), 200),
                            "from": headers_map.get("from", ""), "subject": headers_map.get("subject", ""),
                            "date": headers_map.get("date", ""), "filename": filename,
                            "mime_type": _clean(part.get("mimeType"), 120),
                            "size_bytes": body.get("size"),
                            "attachment_id": _clean(body.get("attachmentId"), 300)})
                    for child in part.get("parts") or []:
                        if isinstance(child, dict) and len(attachments) < 100:
                            walk_attachments(child)
                if isinstance(payload, dict):
                    walk_attachments(payload)
            return {"provider": "gmail", "query": query, "attachments": attachments}
        if operation == "email.attachment_download":
            message_id = _clean(p.get("message_id"), 200)
            attachment_id = _clean(p.get("attachment_id"), 300)
            if not message_id or not attachment_id:
                raise ProviderError("Gmail ilovasini olish uchun xat ID si va ilova ID si kerak.")
            filename = _clean(p.get("filename"), 200).replace("\\", "/").rsplit("/", 1)[-1].strip(" .")
            if not filename or filename in {".", ".."}:
                filename = "gmail-attachment"
            mime_type = _clean(p.get("mime_type"), 120).lower()
            if not re.fullmatch(r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+", mime_type):
                mime_type = "application/octet-stream"
            url = ("https://gmail.googleapis.com/gmail/v1/users/me/messages/"
                   f"{quote(message_id, safe='')}/attachments/{quote(attachment_id, safe='')}")
            encoded_chunks: list[bytes] = []
            encoded_size = 0
            encoded_limit = ((8 * 1024 * 1024 + 2) // 3) * 4 + 16
            try:
                async with self.client.stream("GET", url, headers=headers) as response:
                    if response.status_code >= 400:
                        code = response.status_code
                        if code in {401, 403}:
                            raise ProviderError(f"Gmail ruxsati yoki tokeni rad etildi (HTTP {code}).")
                        if code == 404:
                            raise ProviderError("Gmail so‘ralgan xat yoki ilovani topmadi (HTTP 404).")
                        if code == 429:
                            raise ProviderError("Gmail so‘rov limiti vaqtincha tugadi (HTTP 429).")
                        raise ProviderError(f"Gmail ilovasini yuklashda HTTP {code} xatosi; javob yashirildi.")
                    length = response.headers.get("content-length")
                    if length:
                        try:
                            if int(length) > encoded_limit:
                                raise ProviderError("Gmail ilovasi 8 MB chegaradan katta.")
                        except ValueError as exc:
                            raise ProviderError("Gmail ilova javobining hajmi yaroqsiz.") from exc
                    async for chunk in response.aiter_bytes():
                        encoded_size += len(chunk)
                        if encoded_size > encoded_limit:
                            raise ProviderError("Gmail ilovasi 8 MB chegaradan katta.")
                        encoded_chunks.append(chunk)
            except ProviderError:
                raise
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                raise ProviderError("Gmail ilovasini yuklashda vaqtinchalik tarmoq xatosi.") from exc
            try:
                attachment = json.loads(b"".join(encoded_chunks))
                if not isinstance(attachment, dict):
                    raise ProviderError("Gmail ilova javobi noto‘g‘ri obyekt formatida.")
                encoded_data = str(attachment.get("data") or "")
                reported_size = attachment.get("size")
                if type(reported_size) is int and not 0 <= reported_size <= 8 * 1024 * 1024:
                    raise ProviderError("Gmail ilovasi 8 MB chegaradan katta.")
                raw_data = encoded_data.encode("ascii")
                raw_data += b"=" * (-len(raw_data) % 4)
                content = base64.b64decode(raw_data, altchars=b"-_", validate=True)
            except ProviderError:
                raise
            except (ValueError, TypeError, UnicodeEncodeError, binascii.Error) as exc:
                raise ProviderError("Gmail ilova ma’lumoti noto‘g‘ri formatda.") from exc
            if len(content) > 8 * 1024 * 1024:
                raise ProviderError("Gmail ilovasi 8 MB chegaradan katta.")
            if type(reported_size) is int and reported_size != len(content):
                raise ProviderError("Gmail ilova hajmi javobdagi baytlar soniga mos emas.")
            return {"provider": "gmail", "filename": filename, "mime_type": mime_type,
                    "size_bytes": len(content), "_attachment_filename": filename,
                    "_attachment_bytes": content,
                    "message": "Gmail ilovasi faqat egasining Telegram chatiga qaytariladi."}
        if operation == "email.read":
            mid = quote(_clean(p.get("message_id"), 200), safe="")
            if not mid:
                raise ProviderError("Xatni o‘qish uchun message_id kerak.")
            msg = await self._request("GET", f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{mid}",
                provider="Gmail", headers=headers, params={"format": "full"})
            payload = msg.get("payload") or {}
            headers_map = {str(h.get("name", "")).lower(): _clean(h.get("value"), 500)
                           for h in payload.get("headers", []) if isinstance(h, dict)}
            pieces: list[str] = []
            def walk(part: dict[str, Any]) -> None:
                body = part.get("body") or {}
                if str(part.get("mimeType") or "").lower() == "text/plain" and body.get("data"):
                    pieces.append(_decode_base64url(body.get("data"), 8000))
                for child in part.get("parts") or []:
                    if isinstance(child, dict) and len(pieces) < 3:
                        walk(child)
            walk(payload)
            return {"provider": "gmail", "id": mid, "from": headers_map.get("from", ""),
                "to": headers_map.get("to", ""), "subject": headers_map.get("subject", ""),
                "date": headers_map.get("date", ""), "body": "\n".join(x for x in pieces if x)[:12000],
                "snippet": _clean(msg.get("snippet"), 700)}
        if operation == "email.draft":
            encoded = _gmail_raw(p)
            result = await self._request("POST", "https://gmail.googleapis.com/gmail/v1/users/me/drafts",
                provider="Gmail", headers=headers, json={"message": {"raw": encoded}})
            return {"status": "succeeded", "draft_id": _clean(result.get("id"), 200)}
        if operation == "email.send":
            encoded = _gmail_raw(p)
            result = await self._request("POST", "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
                provider="Gmail", headers=headers, json={"raw": encoded})
            return {"status": "succeeded", "message_id": _clean(result.get("id"), 200),
                    "thread_id": _clean(result.get("threadId"), 200)}
        if operation == "drive.search":
            query = _clean(p.get("query"), 200).replace("\\", "\\\\").replace("'", "\\'")
            data = await self._request("GET", "https://www.googleapis.com/drive/v3/files", provider="Google Drive",
                headers=headers, params={"q": f"name contains '{query}' and trashed = false",
                    "pageSize": max(1, min(50, int(p.get("limit") or 10))),
                    "fields": "files(id,name,mimeType,modifiedTime,webViewLink,size),nextPageToken"})
            return {"provider": "google_drive", "files": data.get("files") or []}
        if operation in {"sheets.read", "sheets.update"}:
            spreadsheet = quote(_clean(p.get("spreadsheet_id"), 300), safe="")
            cell_range = quote(_clean(p.get("range"), 300), safe="')!:")
            if not spreadsheet or not cell_range:
                raise ProviderError("Google Sheets uchun spreadsheet_id va range kerak.")
            url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet}/values/{cell_range}"
            if operation == "sheets.read":
                data = await self._request("GET", url, provider="Google Sheets", headers=headers)
            else:
                data = await self._request("PUT", url, provider="Google Sheets", headers=headers,
                    params={"valueInputOption": "USER_ENTERED"}, json={"values": p.get("values") or []})
            return {"provider": "google_sheets", "range": _clean(data.get("range"), 300),
                    "values": data.get("values") or [], "updated_cells": data.get("updatedCells")}
        raise ProviderError("Bu Google amali qo‘llab-quvvatlanmaydi.")

    async def _docker_cleanup(self, operation: str, p: dict[str, Any]) -> dict[str, Any]:
        if operation != "docker.cleanup":
            raise ProviderError("Server Docker uchun bu amal mavjud emas.")
        base, token = self.settings.server_agent_url.rstrip("/"), self.settings.internal_token
        names = p.get("volume_names")
        if not token:
            raise ProviderError("Server Docker amali uchun INTERNAL_TOKEN sozlanmagan.")
        if (not isinstance(names, list) or not 1 <= len(names) <= _DOCKER_CLEANUP_BATCH
                or any(not isinstance(name, str) or len(name) > _DOCKER_VOLUME_NAME_MAX
                       or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,179}", name)
                       for name in names)
                or len(set(names)) != len(names)):
            raise ProviderError(f"Faqat 1–{_DOCKER_CLEANUP_BATCH} ta noyob va aniq Docker volume nomini ko‘rsating.")
        details = p.get("volume_details")
        if (not isinstance(details, list) or len(details) != len(names)
                or any(not isinstance(item, dict) for item in details)
                or [item.get("name") for item in details] != names):
            raise ProviderError("Tasdiq oynasidagi Docker volume ma’lumoti aynan nomlar ro‘yxatiga mos bo‘lishi kerak.")
        result = await self._request("POST", f"{base}/v1/docker/cleanup", provider="Server Docker",
            headers={"Authorization": f"Bearer {token}"}, json={"volume_names": names})
        if not isinstance(result, dict) or result.get("status") not in {"succeeded", "partial", "failed", "unknown"}:
            raise ProviderError("Server Docker tozalash javobi kutilgan aniq ro‘yxatga mos emas.", uncertain=True)
        deleted = result.get("deleted")
        remaining = result.get("remaining")
        if (not isinstance(deleted, list) or not isinstance(remaining, list)
                or any(not isinstance(row, dict) for row in deleted)):
            raise ProviderError("Server Docker tozalash javobidagi nomlar yaroqsiz.", uncertain=True)
        deleted_names = [row.get("name") for row in deleted]
        if deleted_names != names[:len(deleted_names)] or remaining != names[len(deleted_names):]:
            raise ProviderError("Server Docker tozalash javobi kutilgan nomlar ketma-ketligiga mos emas.", uncertain=True)
        state = result.get("status")
        if state == "succeeded" and (deleted_names != names or remaining):
            raise ProviderError("Docker muvaffaqiyat javobida barcha tasdiqlangan nomlar yo‘q.", uncertain=True)
        if state in {"partial", "failed"} and result.get("failed_volume") != (remaining[0] if remaining else None):
            raise ProviderError("Docker qisman natijadagi to‘xtagan volume nomi yaroqsiz.", uncertain=True)
        if state == "unknown" and result.get("uncertain_volume") != (remaining[0] if remaining else None):
            raise ProviderError("Docker noaniq natijadagi volume nomi yaroqsiz.", uncertain=True)
        return {"provider": "server_docker", "operation": operation, **result}

    async def _prepare_docker_cleanup(self, payload: dict[str, Any]) -> dict[str, Any]:
        base, token = self.settings.server_agent_url.rstrip("/"), self.settings.internal_token
        if not token:
            raise ProviderError("Server Docker amali uchun INTERNAL_TOKEN sozlanmagan.")
        wanted = payload.get("volume_names")
        if wanted is not None and (not isinstance(wanted, list) or not wanted
                or len(wanted) > _DOCKER_CLEANUP_BATCH
                or any(not isinstance(name, str) or len(name) > _DOCKER_VOLUME_NAME_MAX
                       or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,179}", name)
                       for name in wanted)
                or len(set(wanted)) != len(wanted)):
            raise ProviderError(f"Faqat 1–{_DOCKER_CLEANUP_BATCH} ta noyob va aniq Docker volume nomi ko‘rsating.")
        inventory = await self._request("GET", f"{base}/v1/docker/cleanup-candidates", provider="Server Docker",
            headers={"Authorization": f"Bearer {token}"}, params={"limit": "1000"})
        rows = inventory.get("volumes") if isinstance(inventory, dict) else None
        if not isinstance(rows, list):
            raise ProviderError("Server Docker nomzodlar ro‘yxati noto‘g‘ri formatda.")
        candidates = [row for row in rows if isinstance(row, dict)
                      and row.get("reference_count") == 0 and isinstance(row.get("name"), str)
                      and len(row["name"]) <= _DOCKER_VOLUME_NAME_MAX]
        candidate_count = inventory.get("total_volume_candidates")
        if type(candidate_count) is not int or candidate_count < len(candidates):
            candidate_count = len(candidates)
        if wanted is not None:
            by_name = {row["name"]: row for row in candidates}
            if any(name not in by_name for name in wanted):
                raise ProviderError("Ko‘rsatilgan volume’lardan biri hozir nol-havolali nomzod emas; hech narsa o‘chirilmadi.")
            selected = [by_name[name] for name in wanted]
            more = 0
        else:
            selected = candidates[:_DOCKER_CLEANUP_BATCH]
            more = max(0, candidate_count - len(selected))
        if not selected:
            return {"provider": "server_docker", "operation": "docker.cleanup",
                    "status": "no_candidates", "candidate_count": 0}
        names = [str(row["name"]) for row in selected]
        details = [{"name": row["name"], "estimated_size_bytes": row.get("estimated_size_bytes"),
                    "project": row.get("project"), "service": row.get("service")}
                   for row in selected]
        action_payload = {"volume_names": names, "volume_details": details,
                          "more_candidates": more}
        return {"status": "needs_confirmation", "operation": "docker.cleanup",
                "preview": self.preview("docker.cleanup", action_payload),
                "payload": action_payload}

    async def _notion(self, operation: str, p: dict[str, Any]) -> dict[str, Any]:
        token = self.settings.notion_token
        if not token:
            raise ProviderError("Notion tokeni sozlanmagan.")
        headers = {"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28"}
        if operation == "notion.search":
            data = await self._request("POST", "https://api.notion.com/v1/search", provider="Notion",
                headers=headers, json={"query": _clean(p.get("query"), 500), "page_size": 30})
            rows = data.get("results") or []
            return {"provider": "notion", "results": [{"id": _clean(row.get("id"), 100),
                "url": _clean(row.get("url"), 800), "object": _clean(row.get("object"), 30),
                "title": _notion_title(row)} for row in rows[:30] if isinstance(row, dict)]}
        if operation == "notion.create":
            database_id = _clean(p.get("database_id") or self.settings.notion_database_id, 100)
            if not database_id:
                raise ProviderError("Notion sahifasi yaratish uchun database_id yoki NOTION_DATABASE_ID kerak.")
            title = _clean(p.get("title"), 300)
            body = _clean(p.get("body"), 4000)
            if not title:
                raise ProviderError("Notion sahifasi uchun sarlavha kerak.")
            schema = await self._request("GET", f"https://api.notion.com/v1/databases/{quote(database_id, safe='')}",
                provider="Notion", headers=headers)
            title_property = next((name for name, prop in (schema.get("properties") or {}).items()
                                   if isinstance(prop, dict) and prop.get("type") == "title"), None)
            if not title_property:
                raise ProviderError("Notion bazasida sarlavha maydoni topilmadi.")
            result = await self._request("POST", "https://api.notion.com/v1/pages", provider="Notion",
                headers=headers, json={"parent": {"database_id": database_id},
                    "properties": {title_property: {"title": [{"text": {"content": title}}]}},
                    "children": ([{"object": "block", "type": "paragraph", "paragraph": {
                        "rich_text": [{"type": "text", "text": {"content": body}}]}}] if body else [])})
            return {"status": "succeeded", "page_id": _clean(result.get("id"), 100), "url": _clean(result.get("url"), 800)}
        if operation == "notion.update":
            page_id = quote(_clean(p.get("page_id"), 100), safe="")
            if not page_id:
                raise ProviderError("Notion sahifasini tahrirlash uchun page_id kerak.")
            if not _clean(p.get("title"), 300) and not _clean(p.get("body"), 4000):
                raise ProviderError("Notion sahifasida o‘zgaradigan sarlavha yoki matn kerak.")
            current = await self._request("GET", f"https://api.notion.com/v1/pages/{page_id}",
                                          provider="Notion", headers=headers)
            title_property = next((name for name, prop in (current.get("properties") or {}).items()
                                   if isinstance(prop, dict) and prop.get("type") == "title"), None)
            if p.get("title") and not title_property:
                raise ProviderError("Notion sahifasida sarlavha maydoni topilmadi.")
            properties = ({title_property: {"title": [{"text": {"content": _clean(p.get("title"), 300)}}]}}
                          if p.get("title") and title_property else {})
            result = current
            if properties:
                result = await self._request("PATCH", f"https://api.notion.com/v1/pages/{page_id}",
                    provider="Notion", headers=headers, json={"properties": properties})
            if p.get("body"):
                await self._request("PATCH", f"https://api.notion.com/v1/blocks/{page_id}/children",
                    provider="Notion", headers=headers, json={"children": [{"object": "block", "type": "paragraph",
                        "paragraph": {"rich_text": [{"type": "text", "text": {"content": _clean(p.get("body"), 4000)}}]}}]})
            return {"status": "succeeded", "page_id": _clean(result.get("id"), 100),
                    "url": _clean(result.get("url"), 800)}
        raise ProviderError("Notion uchun hozircha faqat qidiruv va yangi yozuv qo‘llanadi.")

    async def _trello(self, operation: str, p: dict[str, Any]) -> dict[str, Any]:
        key, token = self.settings.trello_key, self.settings.trello_token
        if not key or not token:
            raise ProviderError("Trello API key va tokeni sozlanmagan.")
        params = {"key": key, "token": token}
        if operation == "trello.boards":
            data = await self._request("GET", "https://api.trello.com/1/members/me/boards", provider="Trello",
                params={**params, "fields": "id,name,url,closed", "filter": "open"})
            return {"provider": "trello", "boards": data[:50] if isinstance(data, list) else []}
        if operation == "trello.cards":
            board_id = quote(_clean(p.get("board_id"), 100), safe="")
            if not board_id:
                raise ProviderError("Trello kartalarini ko‘rish uchun board_id kerak.")
            data = await self._request("GET", f"https://api.trello.com/1/boards/{board_id}/cards", provider="Trello",
                params={**params, "fields": "id,name,desc,url,idList,due,closed", "filter": "open"})
            return {"provider": "trello", "cards": data[:50] if isinstance(data, list) else []}
        if operation == "trello.create":
            if not _clean(p.get("list_id"), 100) or not _clean(p.get("title"), 300):
                raise ProviderError("Trello kartasi uchun list_id va title kerak.")
            create_params = {**params, "idList": _clean(p.get("list_id"), 100),
                "name": _clean(p.get("title"), 300), "desc": _clean(p.get("body"), 4000)}
            if p.get("due"):
                create_params["due"] = _clean(p.get("due"), 64)
            data = await self._request("POST", "https://api.trello.com/1/cards", provider="Trello",
                params=create_params)
            return {"status": "succeeded", "card_id": _clean(data.get("id"), 100), "url": _clean(data.get("url"), 800)}
        if operation == "trello.update":
            card_id = quote(_clean(p.get("card_id"), 100), safe="")
            if not card_id:
                raise ProviderError("Trello kartasini tahrirlash uchun card_id kerak.")
            fields = {key: (_clean(p.get(source), limit) if p.get(source) is not None else "null")
                      for key, source, limit in (("name", "title", 300), ("desc", "body", 4000), ("due", "due", 64))
                      if source in p}
            if not fields:
                raise ProviderError("Trello kartasini tahrirlash uchun o‘zgaradigan maydon kerak.")
            data = await self._request("PUT", f"https://api.trello.com/1/cards/{card_id}", provider="Trello",
                params={**params, **fields})
            return {"status": "succeeded", "card_id": _clean(data.get("id"), 100), "url": _clean(data.get("url"), 800)}
        if operation == "trello.archive":
            card_id = quote(_clean(p.get("card_id"), 100), safe="")
            if not card_id:
                raise ProviderError("Trello kartasini arxivlash uchun card_id kerak.")
            data = await self._request("PUT", f"https://api.trello.com/1/cards/{card_id}", provider="Trello",
                params={**params, "closed": "true"})
            return {"status": "succeeded", "card_id": _clean(data.get("id"), 100), "archived": bool(data.get("closed"))}
        raise ProviderError("Trello uchun bu amal hali yo‘q.")

    async def _hubspot(self, operation: str, p: dict[str, Any]) -> dict[str, Any]:
        token = self.settings.hubspot_token
        if not token:
            raise ProviderError("HubSpot tokeni sozlanmagan.")
        headers = {"Authorization": f"Bearer {token}"}
        if operation == "crm.search":
            body = {"query": _clean(p.get("query"), 300), "limit": max(1, min(50, int(p.get("limit") or 10))),
                    "properties": ["firstname", "lastname", "email", "phone", "company"]}
            data = await self._request("POST", "https://api.hubapi.com/crm/v3/objects/contacts/search",
                provider="HubSpot", headers=headers, json=body)
            return {"provider": "hubspot", "total": data.get("total", 0),
                "contacts": data.get("results") or []}
        if operation == "crm.create":
            properties = {key: _clean(p.get(key), 500) for key in
                          ("firstname", "lastname", "email", "phone", "company") if p.get(key)}
            if not any(properties.get(key) for key in ("firstname", "lastname", "email")):
                raise ProviderError("HubSpot kontaktini yaratish uchun ism yoki email kerak.")
            data = await self._request("POST", "https://api.hubapi.com/crm/v3/objects/contacts",
                provider="HubSpot", headers=headers, json={"properties": properties})
            return {"status": "succeeded", "contact_id": _clean(data.get("id"), 100)}
        if operation == "crm.update":
            contact_id = quote(_clean(p.get("contact_id"), 100), safe="")
            if not contact_id:
                raise ProviderError("HubSpot kontaktini tahrirlash uchun contact_id kerak.")
            properties = {key: _clean(p.get(key), 500) for key in
                          ("firstname", "lastname", "email", "phone", "company") if key in p}
            if not properties:
                raise ProviderError("HubSpot kontaktida yangilanadigan maydon yo‘q.")
            data = await self._request("PATCH", f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}",
                provider="HubSpot", headers=headers, json={"properties": properties})
            return {"status": "succeeded", "contact_id": _clean(data.get("id"), 100)}
        raise ProviderError("HubSpot uchun hozircha qidiruv va yangi kontakt qo‘llanadi.")

    async def _odoo(self, operation: str, p: dict[str, Any]) -> dict[str, Any]:
        if not all((self.settings.odoo_url, self.settings.odoo_database, self.settings.odoo_api_key)):
            raise ProviderError("Odoo URL, database va API key sozlanmagan.")
        base = str(self.settings.odoo_url).rstrip("/")
        headers = {"Authorization": f"bearer {self.settings.odoo_api_key}",
                   "X-Odoo-Database": str(self.settings.odoo_database),
                   "User-Agent": "PersonalTelegramAssistant/1"}
        if operation == "inventory.quote":
            lines = p.get("items")
            if not isinstance(lines, list) or not 1 <= len(lines) <= 20:
                raise ProviderError("Taklif uchun 1–20 ta aniq mahsulot va miqdor kerak.")
            quote_lines: list[dict[str, Any]] = []
            totals: dict[str, Decimal] = {}
            ambiguous: list[dict[str, Any]] = []
            for item in lines:
                if not isinstance(item, dict):
                    raise ProviderError("Har bir taklif bandida name va quantity bo‘lishi kerak.")
                name = _clean(item.get("name"), 100)
                try:
                    quantity = Decimal(str(item.get("quantity")))
                except (InvalidOperation, ValueError):
                    raise ProviderError("Mahsulot miqdori musbat son bo‘lishi kerak.") from None
                if not name or not quantity.is_finite() or quantity <= 0 or quantity > 100000:
                    raise ProviderError("Mahsulot nomi va 0 dan katta, 100000 dan oshmaydigan miqdor kerak.")
                products = await self._request("POST", f"{base}/json/2/product.product/search_read", provider="Odoo",
                    headers=headers, json={"domain": [["name", "ilike", name]],
                        "fields": ["name", "default_code", "qty_available", "list_price", "currency_id", "write_date"],
                        "limit": 30, "order": "id asc"})
                normalized_name = name.casefold()
                matches = [row for row in products if isinstance(row, dict)
                           and _clean(row.get("name"), 100).casefold() == normalized_name]
                if len(matches) != 1:
                    ambiguous.append({"requested": name, "matches": [{"id": row.get("id"),
                        "name": _clean(row.get("name"), 100), "default_code": _clean(row.get("default_code"), 80)}
                        for row in (matches if matches else products[:10]) if isinstance(row, dict)]})
                    continue
                product = matches[0]
                currency_value = product.get("currency_id")
                currency = _clean(currency_value[1] if isinstance(currency_value, list) and len(currency_value) > 1
                                   else currency_value, 20) or "valyuta ko‘rsatilmagan"
                try:
                    unit_price = Decimal(str(product.get("list_price")))
                    available = Decimal(str(product.get("qty_available")))
                except (InvalidOperation, ValueError):
                    raise ProviderError(f"Odoo {name} mahsulotining narx yoki qoldiq qiymatini o‘qiy olmadi.") from None
                if not unit_price.is_finite() or not available.is_finite():
                    raise ProviderError(f"Odoo {name} mahsuloti uchun narx yoki qoldiq yaroqsiz.")
                line_total = unit_price * quantity
                totals[currency] = totals.get(currency, Decimal(0)) + line_total
                quote_lines.append({"product_id": product.get("id"), "name": _clean(product.get("name"), 100),
                    "sku": _clean(product.get("default_code"), 80), "quantity": str(quantity),
                    "available": str(available), "in_stock": available >= quantity,
                    "unit_price": str(unit_price), "currency": currency, "line_total": str(line_total),
                    "source_updated_at": _clean(product.get("write_date"), 80)})
            if ambiguous:
                return {"provider": "odoo", "quote_status": "needs_clarification", "items": ambiguous}
            return {"provider": "odoo", "quote_status": "draft", "captured_at": datetime.now(UTC).isoformat(),
                    "lines": quote_lines, "totals": {currency: str(total) for currency, total in totals.items()},
                    "sent": False, "recorded_in_odoo": False}
        if operation == "inventory.search":
            model, domain, fields = "product.product", [["name", "ilike", _clean(p.get("query"), 100)]], [
                "name", "default_code", "qty_available", "virtual_available", "list_price"]
        elif operation == "inventory.orders":
            model, domain, fields = "sale.order", [], [
                "name", "state", "date_order", "partner_id", "amount_total", "currency_id"]
            if p.get("query"):
                domain = [["name", "ilike", _clean(p.get("query"), 100)]]
        elif operation == "accounting.summary":
            model, domain, fields = "account.move", [["move_type", "in", ["out_invoice", "in_invoice"]]], [
                "name", "state", "invoice_date", "amount_total", "payment_state", "partner_id", "currency_id"]
        else:
            raise ProviderError("Odoo adapteri faqat ombor, buyurtma va hisob-kitobni o‘qiydi.")
        data = await self._request("POST", f"{base}/json/2/{model}/search_read", provider="Odoo",
            headers=headers, json={"domain": domain, "fields": fields,
                "limit": max(1, min(50, int(p.get("limit") or 10))), "order": "id desc"})
        return {"provider": "odoo", "model": model, "records": data if isinstance(data, list) else []}

    async def _maps(self, operation: str, p: dict[str, Any]) -> dict[str, Any]:
        if self.settings.maps_provider != "google":
            raise ProviderError("Hozirgi xarita adapteri faqat MAPS_PROVIDER=google ni qo‘llaydi.")
        key = self.settings.maps_api_key
        if not key:
            raise ProviderError("Google Maps API kaliti sozlanmagan.")
        if operation == "maps.geocode":
            data = await self._request("GET", "https://maps.googleapis.com/maps/api/geocode/json",
                provider="Google Maps", params={"address": _clean(p.get("address"), 300), "key": key})
            results = data.get("results") or []
            return {"provider": "google_maps", "results": [{"place_id": _clean(row.get("place_id"), 150),
                "address": _clean(row.get("formatted_address"), 400),
                "latitude": (row.get("geometry") or {}).get("location", {}).get("lat"),
                "longitude": (row.get("geometry") or {}).get("location", {}).get("lng")}
                for row in results[:5] if isinstance(row, dict)]}
        if operation == "maps.route":
            origin, destination = _clean(p.get("origin"), 300), _clean(p.get("destination"), 300)
            if not origin or not destination:
                raise ProviderError("Yo‘nalish uchun boshlanish va manzil kerak.")
            mode = str(p.get("mode") or "DRIVE").upper()
            if mode not in {"DRIVE", "WALK", "BICYCLE", "TRANSIT", "TWO_WHEELER"}:
                raise ProviderError("Transport turi noto‘g‘ri.")
            route_body: dict[str, Any] = {"origin": {"address": origin}, "destination": {"address": destination},
                                          "travelMode": mode}
            departure_time = _clean(p.get("departure_time"), 40)
            if departure_time:
                try:
                    parsed_departure = datetime.fromisoformat(departure_time.replace("Z", "+00:00"))
                except ValueError:
                    raise ProviderError("Marshrut jo‘nash vaqti ISO sana-vaqt bo‘lishi kerak.") from None
                if parsed_departure.tzinfo is None:
                    raise ProviderError("Marshrut jo‘nash vaqti vaqt mintaqasi bilan kelishi kerak.")
                route_body["departureTime"] = parsed_departure.astimezone(UTC).isoformat().replace("+00:00", "Z")
            if mode == "DRIVE":
                route_body["routingPreference"] = "TRAFFIC_AWARE"
            data = await self._request("POST", "https://routes.googleapis.com/directions/v2:computeRoutes",
                provider="Google Maps", headers={"X-Goog-Api-Key": key,
                    "X-Goog-FieldMask": "routes.distanceMeters,routes.duration,routes.legs.startLocation,routes.legs.endLocation"},
                json=route_body)
            return {"provider": "google_maps", "departure_time": route_body.get("departureTime"),
                    "travel_mode": mode, "routes": data.get("routes") or []}
        raise ProviderError("Google Maps amali qo‘llab-quvvatlanmaydi.")

    async def _home_assistant(self, operation: str, p: dict[str, Any]) -> dict[str, Any]:
        base, token = self.settings.home_assistant_url, self.settings.home_assistant_token
        if not base or not token:
            raise ProviderError("Home Assistant URL yoki tokeni sozlanmagan.")
        if not re.match(r"^https?://[A-Za-z0-9.\-:\[\]]+$", base):
            raise ProviderError("HOME_ASSISTANT_URL manzili yaroqsiz.")
        headers = {"Authorization": f"Bearer {token}"}
        if operation == "home.state":
            entity = _clean(p.get("entity_id"), 120).lower()
            if not re.fullmatch(r"[a-z0-9_]+\.[a-z0-9_]+", entity):
                raise ProviderError("Home Assistant sensori uchun entity_id aniq bo‘lishi kerak.")
            row = await self._request("GET", f"{base}/api/states/{quote(entity, safe='.')}",
                provider="Home Assistant", headers=headers)
            attrs = row.get("attributes") or {}
            return {"provider": "home_assistant", "entity_id": _clean(row.get("entity_id"), 150),
                    "state": _clean(row.get("state"), 100),
                    "name": _clean(attrs.get("friendly_name"), 200),
                    "unit": _clean(attrs.get("unit_of_measurement"), 30),
                    "last_changed": _clean(row.get("last_changed"), 80)}
        if operation == "home.states":
            data = await self._request("GET", f"{base}/api/states", provider="Home Assistant", headers=headers)
            query = _clean(p.get("query"), 100).casefold()
            rows = data if isinstance(data, list) else []
            if query:
                rows = [row for row in rows if query in str(row.get("entity_id", "")).casefold()
                        or query in str((row.get("attributes") or {}).get("friendly_name", "")).casefold()]
            return {"provider": "home_assistant", "entities": [{"entity_id": _clean(row.get("entity_id"), 150),
                "state": _clean(row.get("state"), 100),
                "name": _clean((row.get("attributes") or {}).get("friendly_name"), 200),
                "unit": _clean((row.get("attributes") or {}).get("unit_of_measurement"), 30),
                "last_changed": _clean(row.get("last_changed"), 80)} for row in rows[:50] if isinstance(row, dict)]}
        if operation == "home.call":
            domain = _clean(p.get("domain"), 40).lower()
            service = _clean(p.get("service"), 40).lower()
            entity = _clean(p.get("entity_id"), 120)
            if not re.fullmatch(r"[a-z_]+", domain) or not re.fullmatch(r"[a-z_]+", service) or not re.fullmatch(r"[a-z0-9_]+\.[a-z0-9_]+", entity):
                raise ProviderError("Home Assistant amalida qurilma yoki service ID noto‘g‘ri.")
            if (domain not in HOME_ASSISTANT_ALLOWED_SERVICES
                    or service not in HOME_ASSISTANT_ALLOWED_SERVICES[domain]
                    or entity.split(".", 1)[0] != domain):
                raise ProviderError("Home Assistant amali ruxsat etilgan qurilma va service ro‘yxatida yo‘q.")
            service_data = p.get("service_data") or {}
            allowed_data = HOME_ASSISTANT_ALLOWED_FIELDS[domain]
            if not isinstance(service_data, dict) or set(service_data) - allowed_data:
                raise ProviderError("Home Assistant service_data ruxsat etilgan maydonlardan iborat bo‘lishi kerak.")
            data = await self._request("POST", f"{base}/api/services/{domain}/{service}", provider="Home Assistant",
                headers=headers, json={"entity_id": entity, **service_data})
            return {"status": "succeeded", "entity_id": entity, "service": f"{domain}.{service}",
                    "result_count": len(data) if isinstance(data, list) else 0}
        raise ProviderError("Home Assistant amali qo‘llab-quvvatlanmaydi.")

    async def _dokploy_deployment_rows(self, base: str, headers: dict[str, str],
                                       resource_type: str, resource_id: str) -> list[dict[str, Any]]:
        endpoint = "all" if resource_type == "application" else "allByCompose"
        parameter = "applicationId" if resource_type == "application" else "composeId"
        data = await self._request("GET", f"{base}/api/deployment.{endpoint}", provider="Dokploy",
                                   headers=headers, params={parameter: resource_id})
        if isinstance(data, dict):
            data = data.get("deployments") or data.get("data") or []
        if not isinstance(data, list):
            return []
        safe_rows = []
        for row in data[:30]:
            if not isinstance(row, dict):
                continue
            deployment_id = _clean(row.get("deploymentId") or row.get("id"), 100)
            safe_rows.append({
                "id": deployment_id,
                "status": _clean(row.get("status") or row.get("state"), 40),
                "title": _clean(row.get("title") or row.get("name"), 160),
                "created_at": _clean(row.get("createdAt") or row.get("created_at"), 80),
                "started_at": _clean(row.get("startedAt") or row.get("started_at"), 80),
                "finished_at": _clean(row.get("finishedAt") or row.get("finished_at"), 80),
                "rollback_id": _clean(row.get("rollbackId") or row.get("rollback_id"), 100),
            })
        safe_rows.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return safe_rows

    async def _dokploy_latest_after_write(self, base: str, headers: dict[str, str],
                                          resource_type: str, resource_id: str) -> dict[str, Any]:
        try:
            rows = await self._dokploy_deployment_rows(base, headers, resource_type, resource_id)
        except ProviderError as exc:
            raise ProviderError("Dokploy amaliga so‘rov qabul qilindi, ammo yangi holatni tekshirish javobi kelmadi.",
                                uncertain=True) from exc
        return rows[0] if rows else {}

    async def _dokploy(self, operation: str, p: dict[str, Any]) -> dict[str, Any]:
        base, key = self.settings.dokploy_url, self.settings.dokploy_api_key
        if not base or not key or not self.settings.dokploy_allowed_projects:
            raise ProviderError("Dokploy URL, API key va DOKPLOY_ALLOWED_PROJECTS allowlist sozlanishi kerak.")
        if not base.startswith("https://") and not re.match(r"^http://(localhost|127\.0\.0\.1)(:\d+)?$", base):
            raise ProviderError("Dokploy manzili HTTPS bo‘lishi kerak.")
        headers = {"x-api-key": key}
        allowed = set(self.settings.dokploy_allowed_projects)
        if operation == "dokploy.projects":
            data = await self._request("GET", f"{base}/api/project.all", provider="Dokploy", headers=headers)
            items = data if isinstance(data, list) else []
            return {"provider": "dokploy", "projects": [{"project_id": _clean(row.get("projectId"), 100),
                "name": _clean(row.get("name"), 200), "description": _clean(row.get("description"), 500),
                "applications": [{"id": _clean(x.get("applicationId"), 100), "name": _clean(x.get("name"), 200),
                                  "app_name": _clean(x.get("appName"), 100)} for x in row.get("applications", [])[:30]],
                "composes": [{"id": _clean(x.get("composeId"), 100), "name": _clean(x.get("name"), 200),
                              "app_name": _clean(x.get("appName"), 100)} for x in row.get("compose", row.get("composes", []))[:30]]}
                for row in items if isinstance(row, dict) and str(row.get("projectId")) in allowed]}
        project_id = _clean(p.get("project_id"), 100)
        if project_id not in allowed:
            raise ProviderError("Dokploy loyihasi allowlist ichida emas.")
        project = await self._request("GET", f"{base}/api/project.one", provider="Dokploy", headers=headers,
                                      params={"projectId": project_id})
        resources: list[tuple[str, dict[str, Any]]] = []
        for item in project.get("applications", []) if isinstance(project, dict) else []:
            resources.append(("application", item))
        for item in (project.get("compose", project.get("composes", [])) if isinstance(project, dict) else []):
            resources.append(("compose", item))
        target_id = _clean(p.get("resource_id"), 100)
        resource_type = _clean(p.get("resource_type"), 20)
        target = next((item for kind, item in resources if kind == resource_type and
                       _clean(item.get("applicationId") or item.get("composeId"), 100) == target_id), None)
        if not target:
            raise ProviderError("Allowlistdagi Dokploy loyiha ichidan tanlangan xizmat topilmadi.")
        field = "applicationId" if resource_type == "application" else "composeId"
        if operation == "dokploy.deployments":
            rows = await self._dokploy_deployment_rows(base, headers, resource_type, target_id)
            return {"provider": "dokploy", "project_id": project_id,
                "project_name": _clean(project.get("name"), 200), "resource_id": target_id,
                "resource_name": _clean(target.get("name"), 200), "resource_type": resource_type,
                "deployments": rows}
        if operation == "dokploy.restart" and resource_type == "application":
            result = await self._request("POST", f"{base}/api/application.reload", provider="Dokploy",
                headers=headers, json={"applicationId": target_id, "appName": _clean(target.get("appName"), 63)})
            latest = await self._dokploy_latest_after_write(base, headers, resource_type, target_id)
            response_id = _clean(result.get("deploymentId"), 100) if isinstance(result, dict) else ""
            return {"status": "succeeded", "project_id": project_id, "resource_id": target_id,
                    "resource_name": _clean(target.get("name"), 200),
                    "deployment_id": latest.get("id") or response_id,
                    "deployment_status": latest.get("status") or "restart_requested",
                    "deployment_created_at": latest.get("created_at")}
        if operation == "dokploy.deploy":
            endpoint = "application.redeploy" if resource_type == "application" else "compose.redeploy"
            result = await self._request("POST", f"{base}/api/{endpoint}", provider="Dokploy",
                headers=headers, json={field: target_id, "title": "Assistant confirmed redeploy"})
            latest = await self._dokploy_latest_after_write(base, headers, resource_type, target_id)
            response_id = _clean(result.get("deploymentId"), 100) if isinstance(result, dict) else ""
            return {"status": "succeeded", "project_id": project_id, "resource_id": target_id,
                    "resource_name": _clean(target.get("name"), 200),
                    "deployment_id": latest.get("id") or response_id,
                    "deployment_status": latest.get("status") or "queued_or_not_returned",
                    "deployment_created_at": latest.get("created_at")}
        if operation == "dokploy.restart" and resource_type == "compose":
            await self._request("POST", f"{base}/api/compose.stop", provider="Dokploy",
                headers=headers, json={"composeId": target_id})
            try:
                result = await self._request("POST", f"{base}/api/compose.start", provider="Dokploy",
                    headers=headers, json={"composeId": target_id})
            except ProviderError as exc:
                raise ProviderError("Compose xizmatini to‘xtatdim, ammo qayta ishga tushirish natijasi noma’lum.",
                                    uncertain=True) from exc
            latest = await self._dokploy_latest_after_write(base, headers, resource_type, target_id)
            response_id = _clean(result.get("deploymentId"), 100) if isinstance(result, dict) else ""
            return {"status": "succeeded", "project_id": project_id, "resource_id": target_id,
                    "resource_name": _clean(target.get("name"), 200),
                    "deployment_id": latest.get("id") or response_id,
                    "deployment_status": latest.get("status") or "restart_requested",
                    "deployment_created_at": latest.get("created_at")}
        if operation == "dokploy.rollback":
            rollback_id = _clean(p.get("rollback_id"), 100)
            if not rollback_id:
                raise ProviderError("Qaytarish uchun aniq rollback_id kerak.")
            deployment_rows = await self._dokploy_deployment_rows(base, headers, resource_type, target_id)
            known_ids = {str(item.get("rollback_id")) for item in deployment_rows if item.get("rollback_id")}
            known_ids.update(str(item.get("id")) for item in deployment_rows if item.get("id"))
            if rollback_id not in known_ids:
                raise ProviderError("rollback_id tanlangan allowlist xizmatining deployment ro‘yxatida yo‘q.")
            result = await self._request("POST", f"{base}/api/rollback.rollback", provider="Dokploy",
                headers=headers, json={"rollbackId": rollback_id})
            latest = await self._dokploy_latest_after_write(base, headers, resource_type, target_id)
            response_id = _clean(result.get("deploymentId"), 100) if isinstance(result, dict) else ""
            return {"status": "succeeded", "project_id": project_id,
                    "resource_id": target_id, "rollback_id": rollback_id,
                    "deployment_id": latest.get("id") or response_id,
                    "deployment_status": latest.get("status") or "rollback_requested",
                    "deployment_created_at": latest.get("created_at")}
        raise ProviderError("Dokploy amali qo‘llab-quvvatlanmaydi.")

    async def _meta(self, operation: str, p: dict[str, Any]) -> dict[str, Any]:
        if operation != "social.publish":
            raise ProviderError("Meta adapteri uchun bu amal yo‘q.")
        token, page = self.settings.meta_graph_token, self.settings.meta_page_id
        if not token or not page:
            raise ProviderError("Meta Graph tokeni va Page ID sozlanmagan.")
        message = _clean(p.get("message"), 5000)
        if not message:
            raise ProviderError("Meta sahifasiga chiqarish uchun matn kerak.")
        version = _clean(p.get("api_version") or self.settings.meta_graph_api_version, 20)
        if not re.fullmatch(r"v\d+\.\d+", version):
            raise ProviderError("Meta Graph API versiyasi yaroqsiz.")
        result = await self._request("POST", f"https://graph.facebook.com/{version}/{quote(page, safe='')}/feed",
            provider="Meta", headers={"Authorization": f"Bearer {token}"},
            params={"message": message})
        return {"status": "succeeded", "post_id": _clean(result.get("id"), 200)}

def _notion_title(page: dict[str, Any]) -> str:
    properties = page.get("properties") or {}
    for prop in properties.values():
        if isinstance(prop, dict) and prop.get("type") == "title":
            return _clean("".join(str(item.get("plain_text") or "") for item in prop.get("title", [])
                                  if isinstance(item, dict)), 300)
    return _clean(page.get("title"), 300)


def _gmail_raw(payload: dict[str, Any]) -> str:
    recipient = _clean(payload.get("to"), 500)
    subject = _clean(payload.get("subject"), 500)
    body = str(payload.get("body") or "").replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")[:10000].strip()
    if not recipient or "@" not in recipient or not subject or not body:
        raise ProviderError("Gmail qoralama/xat uchun qabul qiluvchi, mavzu va matn kerak.")
    message = EmailMessage()
    try:
        message["To"] = recipient
        message["Subject"] = subject
    except (ValueError, TypeError) as exc:
        raise ProviderError("Gmail qabul qiluvchi yoki mavzu maydoni yaroqsiz.") from exc
    message.set_content(body)
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii").rstrip("=")
