from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Iterable

from x_mrr_banner.config import Period


def utc_today() -> date:
    return datetime.now(timezone.utc).date()


def period_window(period: Period, as_of: date | None = None) -> tuple[date, date]:
    """Return inclusive [start, end] for the latest complete reporting window."""
    today = as_of or utc_today()
    if period == "daily":
        end = today - timedelta(days=1)
        return end, end
    if period == "weekly":
        # App Store Connect weekly sales reports are weeks ending on Sunday.
        # weekday(): Mon=0 … Sun=6 → days since last Sunday.
        days_since_sunday = (today.weekday() + 1) % 7
        if days_since_sunday == 0:
            # Today is Sunday; use the previous completed week.
            end = today - timedelta(days=7)
        else:
            end = today - timedelta(days=days_since_sunday)
        start = end - timedelta(days=6)
        return start, end
    # monthly: previous calendar month
    first_of_this_month = today.replace(day=1)
    end = first_of_this_month - timedelta(days=1)
    start = end.replace(day=1)
    return start, end


def history_windows(period: Period, count: int, as_of: date | None = None) -> list[tuple[date, date]]:
    """Oldest → newest inclusive windows for chart history."""
    windows: list[tuple[date, date]] = []
    cursor = as_of or utc_today()
    for _ in range(count):
        start, end = period_window(period, as_of=cursor)
        windows.append((start, end))
        cursor = start
    windows.reverse()
    return windows


def daterange(start: date, end: date) -> Iterable[date]:
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def apple_report_date(period: Period, window_start: date, window_end: date) -> str:
    if period == "daily":
        return window_end.isoformat()
    if period == "weekly":
        # ASC weekly filter uses the Sunday date of the week in YYYY-MM-DD.
        return window_end.isoformat()
    return window_start.strftime("%Y-%m")


def apple_frequency(period: Period) -> str:
    return {"daily": "DAILY", "weekly": "WEEKLY", "monthly": "MONTHLY"}[period]
