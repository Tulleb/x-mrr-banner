from __future__ import annotations

import logging
from datetime import date, timedelta

from x_mrr_banner.config import AppConfig, Period, RevenueSnapshot, SeriesPoint
from x_mrr_banner.dates import history_windows, period_window
from x_mrr_banner.extract import apple, google_play

logger = logging.getLogger(__name__)

HISTORY_POINTS = {
    "daily": 14,
    "weekly": 8,
    "monthly": 6,
}


def _label_for(period: Period, start: date, end: date) -> str:
    if period == "daily":
        return end.strftime("%m/%d")
    if period == "weekly":
        return start.strftime("%m/%d")
    return start.strftime("%b %y")


def _sum_daily(source: dict[date, float], start: date, end: date) -> float:
    total = 0.0
    day = start
    while day <= end:
        total += source.get(day, 0.0)
        day += timedelta(days=1)
    return total


def _warn(source: str, message: str) -> None:
    """Log a warning and emit a GitHub Actions annotation when running in CI."""
    text = f"{source}: {message}"
    logger.warning(text)
    # Visible under the job's Annotations panel in GitHub Actions.
    safe = text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::warning title={source}::{safe}", flush=True)


def collect_revenues(period: Period, config: AppConfig, as_of: date | None = None) -> RevenueSnapshot:
    primary_start, primary_end = period_window(period, as_of=as_of)
    windows = history_windows(period, HISTORY_POINTS[period], as_of=as_of)
    history_start = windows[0][0]
    history_end = windows[-1][1]

    apple_daily: dict[date, float] = {}
    google_daily: dict[date, float] = {}
    apple_window_totals: dict[tuple[date, date], float] = {}

    try:
        apple_daily = apple.fetch_apple_daily_series(
            history_start, history_end, apple_skus=config.apple_skus
        )
    except Exception as exc:  # noqa: BLE001 — store failures must not abort the run
        _warn("App Store Connect", f"daily series unavailable ({exc}); trying period reports")
        for start, end in windows:
            try:
                apple_window_totals[(start, end)] = apple.fetch_apple_revenue(
                    period, start, end, apple_skus=config.apple_skus
                )
            except Exception as window_exc:  # noqa: BLE001
                _warn(
                    "App Store Connect",
                    f"revenue missing for {start}–{end}: {window_exc}",
                )
                apple_window_totals[(start, end)] = 0.0

    try:
        google_daily = google_play.fetch_google_daily_series(
            history_start, history_end, package_names=config.google_package_names
        )
    except Exception as exc:  # noqa: BLE001
        _warn("Google Play", f"series unavailable: {exc}")

    series: list[SeriesPoint] = []
    for start, end in windows:
        if apple_daily:
            apple_rev = _sum_daily(apple_daily, start, end)
        else:
            apple_rev = apple_window_totals.get((start, end), 0.0)
        google_rev = _sum_daily(google_daily, start, end) if google_daily else 0.0
        series.append(
            SeriesPoint(
                label=_label_for(period, start, end),
                date=end,
                apple_revenue=apple_rev,
                google_revenue=google_rev,
                total_revenue=apple_rev + google_rev,
            )
        )

    try:
        apple_revenue = apple.fetch_apple_revenue(
            period, primary_start, primary_end, apple_skus=config.apple_skus
        )
    except Exception as exc:  # noqa: BLE001
        _warn("App Store Connect", f"primary window failed: {exc}")
        apple_revenue = series[-1].apple_revenue if series else 0.0

    try:
        google_revenue = google_play.fetch_google_revenue(
            primary_start, primary_end, package_names=config.google_package_names
        )
    except Exception as exc:  # noqa: BLE001
        _warn("Google Play", f"primary window failed: {exc}")
        google_revenue = series[-1].google_revenue if series else 0.0

    return RevenueSnapshot(
        period=period,
        period_start=primary_start,
        period_end=primary_end,
        currency=config.currency,
        apple_revenue=apple_revenue,
        google_revenue=google_revenue,
        total_revenue=apple_revenue + google_revenue,
        series=series,
    )
