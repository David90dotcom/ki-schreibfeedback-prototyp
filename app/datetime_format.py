from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


GERMAN_TIME_ZONE = ZoneInfo("Europe/Berlin")


def format_datetime_german(value: datetime | None) -> str:
    """Zeigt gespeicherte UTC-Zeitpunkte als deutsche Ortszeit an."""

    if value is None:
        return "Nicht verfügbar"

    utc_value = (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )
    local_value = utc_value.astimezone(GERMAN_TIME_ZONE)
    timezone_label = "MESZ" if local_value.dst() else "MEZ"

    return local_value.strftime(
        f"%d.%m.%Y, %H:%M {timezone_label}"
    )
