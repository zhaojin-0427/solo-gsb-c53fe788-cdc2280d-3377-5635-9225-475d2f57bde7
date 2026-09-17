"""ISO8601 datetime helpers — everything is naive UTC internally."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

FAR_FUTURE = datetime(9999, 12, 31)


def parse_dt(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        s = value.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def fmt_dt(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def add_days(dt: datetime, days: int) -> datetime:
    return dt + timedelta(days=days)
