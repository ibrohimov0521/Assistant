from calendar import monthrange
from datetime import UTC, datetime, timedelta


def next_occurrence(first: datetime, frequency: str, after: datetime, *,
                    weekday: int | None = None, interval_minutes: int | None = None) -> datetime:
    """Return the first matching time on or after first, strictly after after."""
    if frequency == "interval":
        if interval_minutes is None or not 15 <= interval_minutes <= 43200:
            raise ValueError("Takror oralig‘i 15 daqiqadan 30 kungacha bo‘lishi kerak.")
        first_utc = first.astimezone(UTC)
        interval = timedelta(minutes=interval_minutes)
        steps = max(0, (after - first_utc) // interval + 1)
        return first_utc + steps * interval
    if frequency == "weekly" and (weekday is None or not 0 <= weekday <= 6):
        raise ValueError("Hafta kunini ko‘rsating.")
    if frequency not in {"daily", "weekdays", "weekly", "month_end"}:
        raise ValueError("Takrorlash tartibi noto‘g‘ri.")

    after_local = after.astimezone(first.tzinfo)
    day = max(first.date(), after_local.date())
    while True:
        if frequency == "month_end":
            day = day.replace(day=monthrange(day.year, day.month)[1])
        candidate = datetime.combine(day, first.timetz())
        matches = (frequency in {"daily", "month_end"} or
                   (frequency == "weekdays" and day.weekday() < 5) or
                   (frequency == "weekly" and day.weekday() == weekday))
        if matches and candidate.astimezone(UTC) > after:
            return candidate.astimezone(UTC)
        day += timedelta(days=1)
