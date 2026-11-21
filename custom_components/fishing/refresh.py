"""The coordinator's decisions, as pure functions (so they are tested without Home Assistant).

Home Assistant calls the coordinator every few minutes; most of those calls must not touch the
network. What to do on each call is decided here:

* **When to refetch**: the forecast changes hourly and the API is a free shared service, so it is
  refetched on a slow clock while the (cheap, local) score is recomputed on a fast one.
* **When data is stale**: a failed refresh keeps serving the last good forecast - a forecast a
  couple of hours old is still useful - but says so, and gives up past a limit rather than
  presenting yesterday's weather as today's.
* **Which history still needs ingesting**: normals are running averages, so ingesting a day twice
  would bias them. Only days after the last one ingested are ever requested.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from . import const

#: after this long without a successful refresh the data is flagged stale
STALE_AFTER = timedelta(hours=3)
#: after this long a cached forecast is no longer shown at all
EXPIRE_AFTER = timedelta(hours=12)
#: the archive trails real time (reanalysis is published with a delay of several days)
ARCHIVE_LAG_DAYS = 5


def needs_fetch(last_fetch: datetime | None, now: datetime, interval: timedelta = const.UPDATE_INTERVAL) -> bool:
    """True when there is no data yet or it is at least `interval` old (a clock step back refetches)."""
    if last_fetch is None:
        return True
    age = now - last_fetch
    return age >= interval or age < timedelta(0)


def data_state(last_success: datetime | None, now: datetime) -> str:
    """'fresh' | 'stale' | 'expired' | 'none' for the cached forecast."""
    if last_success is None:
        return "none"
    age = now - last_success
    if age < timedelta(0):
        return "fresh"
    if age >= EXPIRE_AFTER:
        return "expired"
    return "stale" if age >= STALE_AFTER else "fresh"


def archive_window(
    last_ingested: date | None, today: date, years: int = const.CLIMATOLOGY_YEARS, lag_days: int = ARCHIVE_LAG_DAYS
) -> tuple[date, date] | None:
    """(start, end) of archive days still to ingest, or None when the store is already up to date."""
    end = today - timedelta(days=lag_days)
    start = today - timedelta(days=365 * years) if last_ingested is None else last_ingested + timedelta(days=1)
    return None if start > end else (start, end)


def parse_day(text: Any) -> date | None:
    """A stored ISO date back to a date; anything else is None (a corrupt store must not crash setup)."""
    if not isinstance(text, str):
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def daily_rows(
    series: list[tuple[datetime, float]], variable: str, after: date | None, before: date
) -> list[tuple[date, dict[str, float]]]:
    """Daily means of an hourly series as climatology rows, for days after `after` and before `before`.

    Only complete-enough days (at least 18 hourly samples) count, and `before` itself is excluded
    (today is still in progress).
    """
    by_day: dict[date, list[float]] = {}
    for when, value in series:
        by_day.setdefault(when.date(), []).append(value)
    rows = []
    for day in sorted(by_day):
        if (after is not None and day <= after) or day >= before or len(by_day[day]) < 18:
            continue
        rows.append((day, {variable: sum(by_day[day]) / len(by_day[day])}))
    return rows
