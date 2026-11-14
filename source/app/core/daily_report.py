from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.core.sources import load_news
from app.storage.db import Database
from app.storage.personal import PersonalRecords

DAILY_REPORT_SECTIONS = ("calendar", "tasks", "reminders", "weather", "news", "usage")
DAILY_REPORT_SECTION_LABELS = {
    "calendar": "taqvim",
    "tasks": "vazifalar",
    "reminders": "eslatmalar",
    "weather": "ob-havo",
    "news": "yangiliklar",
    "usage": "AI ishlatilishi",
}
_ACTIVE_TASK_STATUSES = {"open", "needs_confirmation", "scheduled", "running"}
_TASK_STATUS_LABELS = {
    "open": "ochiq",
    "needs_confirmation": "tasdiq kutmoqda",
    "scheduled": "rejalangan",
    "running": "bajarilmoqda",
    "unknown": "natijasi noma’lum",
}


def _local_day(value: Any, timezone: Any) -> date | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(timezone).date()
    except (TypeError, ValueError, OverflowError):
        return None


def _task_text(row: dict[str, Any], timezone: Any) -> str:
    title = " ".join(str(row.get("title") or "Nomsiz vazifa").split())[:160]
    project = " ".join(str(row.get("project") or "").split())[:60]
    status = _TASK_STATUS_LABELS.get(str(row.get("status") or ""), str(row.get("status") or "holat noma’lum"))
    due = _local_day(row.get("due_at"), timezone)
    due_text = f" · muddat {due.isoformat()}" if due else ""
    project_text = f" · {project}" if project else ""
    assignee = " ".join(str(row.get("assignee") or "").split())[:60]
    assignee_text = f" · mas’ul {assignee}" if assignee else ""
    result = " ".join(str(row.get("result") or "").split())[:120]
    result_text = f" · natija: {result}" if result and row.get("status") == "succeeded" else ""
    return f"• {title}{project_text}{assignee_text} · {status}{due_text}{result_text}"


def _task_section(db: Database, owner_id: int, timezone: Any, day: date, mode: str) -> list[str]:
    rows = db.list_assistant_tasks(owner_id, status="all", limit=30)
    active = [row for row in rows if row.get("status") in _ACTIVE_TASK_STATUSES]
    unknown = [row for row in rows if row.get("status") == "unknown"]
    if mode == "evening":
        completed = [row for row in rows if row.get("status") == "succeeded"
                     and _local_day(row.get("updated_at"), timezone) == day]
        tomorrow = [row for row in active if _local_day(row.get("due_at"), timezone) == day + timedelta(days=1)]
        undated = [row for row in active if not row.get("due_at")]
        lines = ["Bugun bajarilgan:"]
        lines.extend(_task_text(row, timezone) for row in completed[:6])
        if not completed:
            lines.append("• Bugun bajarilgan vazifa qayd etilmagan.")
        lines.append("Ertaga muddati keladigan:")
        lines.extend(_task_text(row, timezone) for row in tomorrow[:6])
        if not tomorrow:
            lines.append("• Ertaga muddati keladigan vazifa yo‘q.")
        if undated:
            lines.append(f"Muddatsiz ochiq vazifalar: {len(undated)} ta.")
    else:
        overdue = [row for row in active if (due := _local_day(row.get("due_at"), timezone)) and due < day]
        today = [row for row in active if _local_day(row.get("due_at"), timezone) == day]
        undated = [row for row in active if not row.get("due_at")]
        lines = ["Kechikkan ochiq vazifalar:"]
        lines.extend(_task_text(row, timezone) for row in overdue[:5])
        if not overdue:
            lines.append("• Kechikkan vazifa yo‘q.")
        lines.append("Bugun muddati keladigan:")
        lines.extend(_task_text(row, timezone) for row in today[:6])
        if not today:
            lines.append("• Bugun muddati keladigan vazifa yo‘q.")
        lines.append(f"Muddatsiz ochiq vazifalar: {len(undated)} ta.")
    if unknown:
        lines.append("Natijasi tekshirilishi kerak bo‘lgan:")
        lines.extend(_task_text(row, timezone) for row in unknown[:4])
    return lines


async def build_daily_report(db: Database, settings: Any, personal: PersonalRecords,
                             weather: Any, *, mode: str | None = None,
                             include_sources: bool = True,
                             now: datetime | None = None) -> str:
    now = now.astimezone(settings.timezone) if now else datetime.now(settings.timezone)
    mode = mode if mode in {"morning", "evening"} else ("evening" if now.hour >= 17 else "morning")
    mode_label = "kechqurun" if mode == "evening" else "ertalab"
    prefs = personal.preferences()
    city = prefs.get("city") or settings.weather_city
    sections = personal.daily_report_sections()
    lines = [f"Kun tartibi — {mode_label} · {now:%Y-%m-%d %H:%M} · Toshkent vaqti"]

    if "calendar" in sections:
        lines.append("Taqvim: xizmat ulanmagan; uchrashuvlar bo‘yicha ma’lumot yo‘q.")
    if "tasks" in sections:
        lines.extend(["", "Vazifalar:", *_task_section(db, settings.owner_telegram_id,
            settings.timezone, now.date(), mode)])
    if "reminders" in sections:
        reminders = db.get_reminders(settings.owner_telegram_id, limit=10)
        target_days = {now.date(), now.date() + timedelta(days=1)} if mode == "evening" else {now.date()}
        relevant = [row for row in reminders if _local_day(row.get("due_at"), settings.timezone) in target_days]
        label = "Bugun va ertangi eslatmalar" if mode == "evening" else "Bugungi eslatmalar"
        lines.extend(["", f"{label}:"])
        lines.extend(f"• {row['due_at']} — {str(row['body'])[:120]}" for row in relevant[:5])
        if not relevant:
            lines.append("• Shu davr uchun eslatma yo‘q.")
    if "weather" in sections:
        lines.extend(["", f"Ob-havo · {city}:"])
        try:
            lines.append(await weather.forecast(city, tomorrow=(mode == "evening")))
        except Exception:
            lines.append("Prognoz manbasi vaqtincha javob bermadi; ob-havo tekshirilmadi.")
    if "news" in sections:
        lines.extend(["", "Yangiliklar:"])
        if not include_sources:
            lines.append("Yangiliklar manbasi bu rejimda tekshirilmagan.")
        else:
            try:
                items = await load_news(settings.news_feeds_file, max_items=3,
                                        published_after=now.astimezone(UTC) - timedelta(days=1))
            except Exception:
                items = []
            if items:
                lines.extend(f"• {item.title} · {item.link}" for item in items[:3])
            else:
                lines.append("Yangilik manbasi sozlanmagan yoki hozir natija bermadi.")
    if "usage" in sections:
        usage = personal.usage(now.date())
        lines.extend(["", f"AI ishlatilishi: {usage['requests']}/{settings.model_daily_requests} so‘rov, "
                      f"{usage['input_tokens'] + usage['output_tokens']}/{settings.model_daily_tokens} token"])
        if (settings.model_input_usd_per_million_tokens is not None
                and settings.model_output_usd_per_million_tokens is not None):
            estimated_usd = Decimal(usage["estimated_cost_micro_usd"]) / Decimal(1_000_000)
            lines.append(f"OpenAI taxminiy sarfi: ${estimated_usd:.6f} · {settings.openai_model} · "
                         f"tarif sanasi {settings.model_pricing_as_of or 'ko‘rsatilmagan'}.")
            if settings.model_daily_budget_usd is not None:
                lines.append(f"Kunlik chegara: ${settings.model_daily_budget_usd:.2f}, ogohlantirish "
                             f"{settings.model_cost_alert_percent}% da.")
        else:
            lines.append("Puldagi AI sarfi hisoblanmayapti: modelning kirish va chiqish token narxlarini sozlang.")
            if settings.model_daily_budget_usd is not None:
                lines.append("Token narxlari yo‘qligi sabab kunlik pul chegarasi ham hozircha faol emas.")
    return "\n".join(lines)[:3800]
