from __future__ import annotations

import logging
from datetime import date, timedelta

from x_mrr_banner.config import (
    AppConfig,
    AppEntry,
    AppRevenue,
    Period,
    RevenueSnapshot,
    SeriesPoint,
)
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


def _portfolio_filters(config: AppConfig) -> tuple[list[str] | None, list[str] | None]:
    """Return SKU/package filters for portfolio totals (None = unfiltered)."""
    skus = config.all_apple_skus()
    packages = config.all_google_package_names()
    return (skus or None, packages or None)


def _apple_skus_for(app: AppEntry) -> list[str] | None:
    return app.revenue_apple_skus() or None


def _google_packages_for(app: AppEntry) -> list[str] | None:
    return app.google_package_names or None


def _make_app_revenue(
    app: AppEntry,
    *,
    apple_rev: float,
    google_rev: float,
) -> AppRevenue:
    return AppRevenue(
        name=app.name,
        apple_revenue=apple_rev,
        google_revenue=google_rev,
        total_revenue=apple_rev + google_rev,
    )


def _fetch_app_window(
    period: Period,
    start: date,
    end: date,
    app: AppEntry,
    *,
    target_currency: str,
) -> AppRevenue:
    apple_skus = _apple_skus_for(app)
    packages = _google_packages_for(app)

    try:
        apple_rev = apple.fetch_apple_revenue(
            period,
            start,
            end,
            apple_skus=apple_skus,
            target_currency=target_currency,
        )
    except Exception as exc:  # noqa: BLE001
        _warn("App Store Connect", f"app {app.name!r} apple revenue: {exc}")
        apple_rev = 0.0

    try:
        google_rev = google_play.fetch_google_revenue(
            start, end, package_names=packages
        )
    except Exception as exc:  # noqa: BLE001
        _warn("Google Play", f"app {app.name!r} google revenue: {exc}")
        google_rev = 0.0

    return _make_app_revenue(app, apple_rev=apple_rev, google_rev=google_rev)


def _fetch_apple_daily_by_filter(
    history_start: date,
    history_end: date,
    filters: list[tuple[str, list[str] | None]],
    *,
    target_currency: str,
) -> dict[str, dict[date, float]]:
    """Download each daily report once; sum into named filter buckets."""
    from x_mrr_banner.dates import daterange

    days = list(daterange(history_start, history_end))
    total = len(days)
    result: dict[str, dict[date, float]] = {
        name: {day: 0.0 for day in days} for name, _ in filters
    }
    logger.info(
        "App Store Connect: fetching %d daily report(s) %s → %s…",
        total,
        history_start.isoformat(),
        history_end.isoformat(),
    )
    for index, day in enumerate(days, start=1):
        if index == 1 or index == total or index % 10 == 0:
            logger.info("  Apple daily %d/%d: %s", index, total, day.isoformat())
        try:
            rows = apple.download_sales_report(
                period="daily", window_start=day, window_end=day
            )
        except Exception as exc:  # noqa: BLE001
            _warn("App Store Connect", f"daily report missing for {day}: {exc}")
            continue
        for name, skus in filters:
            result[name][day] = apple.sum_apple_proceeds(
                rows,
                apple_skus=skus,
                target_currency=target_currency,
                as_of=day,
            )
    return result


def _fetch_apple_window_rows(
    period: Period,
    windows: list[tuple[date, date]],
) -> dict[tuple[date, date], list[dict[str, str]]]:
    """Download one sales report per history window (weekly/monthly)."""
    logger.info(
        "App Store Connect: fetching %d %s report(s) for chart history…",
        len(windows),
        period,
    )
    rows_by_window: dict[tuple[date, date], list[dict[str, str]]] = {}
    for index, (start, end) in enumerate(windows, start=1):
        logger.info(
            "  Apple %d/%d: %s → %s",
            index,
            len(windows),
            start.isoformat(),
            end.isoformat(),
        )
        try:
            rows_by_window[(start, end)] = apple.download_sales_report(
                period=period, window_start=start, window_end=end
            )
        except Exception as window_exc:  # noqa: BLE001
            _warn(
                "App Store Connect",
                f"revenue missing for {start}–{end}: {window_exc}",
            )
            rows_by_window[(start, end)] = []
    return rows_by_window


def _app_slices_for_window(
    apps: list[AppEntry],
    *,
    period: Period,
    start: date,
    end: date,
    target_currency: str,
    apple_rows: list[dict[str, str]] | None,
    apple_daily_by_key: dict[str, dict[date, float]],
    google_rows: list[dict[str, str]],
) -> list[AppRevenue]:
    slices: list[AppRevenue] = []
    for app in apps:
        if period == "daily":
            apple_rev = _sum_daily(
                apple_daily_by_key.get(f"app:{app.name}", {}), start, end
            )
        else:
            apple_rev = apple.sum_apple_proceeds(
                apple_rows or [],
                apple_skus=_apple_skus_for(app),
                target_currency=target_currency,
                as_of=end,
            )
        google_rev = (
            google_play.sum_google_revenue(
                google_rows,
                start,
                end,
                package_names=_google_packages_for(app),
            )
            if google_rows
            else 0.0
        )
        slices.append(_make_app_revenue(app, apple_rev=apple_rev, google_rev=google_rev))
    return slices


def collect_revenues(period: Period, config: AppConfig, as_of: date | None = None) -> RevenueSnapshot:
    primary_start, primary_end = period_window(period, as_of=as_of)
    windows = history_windows(period, HISTORY_POINTS[period], as_of=as_of)
    history_start = windows[0][0]
    history_end = windows[-1][1]
    target_currency = config.currency

    logger.info(
        "Fetching revenues for %s → %s (%s, %d history points, currency=%s)",
        primary_start.isoformat(),
        primary_end.isoformat(),
        period,
        len(windows),
        target_currency,
    )

    apple_skus, package_names = _portfolio_filters(config)

    google_rows: list[dict[str, str]] = []
    logger.info(
        "Google Play: fetching sales %s → %s…",
        history_start.isoformat(),
        history_end.isoformat(),
    )
    try:
        google_rows = google_play.load_play_sales_rows(history_start, history_end)
    except Exception as exc:  # noqa: BLE001
        _warn("Google Play", f"series unavailable: {exc}")

    apple_daily_by_key: dict[str, dict[date, float]] = {}
    apple_rows_by_window: dict[tuple[date, date], list[dict[str, str]]] = {}

    if period == "daily":
        filters: list[tuple[str, list[str] | None]] = [("portfolio", apple_skus)]
        for app in config.apps:
            filters.append((f"app:{app.name}", _apple_skus_for(app)))
        try:
            apple_daily_by_key = _fetch_apple_daily_by_filter(
                history_start,
                history_end,
                filters,
                target_currency=target_currency,
            )
        except Exception as exc:  # noqa: BLE001
            _warn("App Store Connect", f"daily series unavailable ({exc})")
    else:
        apple_rows_by_window = _fetch_apple_window_rows(period, windows)

    series: list[SeriesPoint] = []
    for start, end in windows:
        if period == "daily":
            apple_rows = None
            apple_rev = _sum_daily(apple_daily_by_key.get("portfolio", {}), start, end)
        else:
            apple_rows = apple_rows_by_window.get((start, end), [])
            apple_rev = apple.sum_apple_proceeds(
                apple_rows,
                apple_skus=apple_skus,
                target_currency=target_currency,
                as_of=end,
            )

        google_rev = (
            google_play.sum_google_revenue(
                google_rows, start, end, package_names=package_names
            )
            if google_rows
            else 0.0
        )

        app_slices = _app_slices_for_window(
            config.apps,
            period=period,
            start=start,
            end=end,
            target_currency=target_currency,
            apple_rows=apple_rows,
            apple_daily_by_key=apple_daily_by_key,
            google_rows=google_rows,
        )

        series.append(
            SeriesPoint(
                label=_label_for(period, start, end),
                date=end,
                apple_revenue=apple_rev,
                google_revenue=google_rev,
                total_revenue=apple_rev + google_rev,
                apps=app_slices,
            )
        )

    latest = series[-1] if series else None
    primary_matches_latest = latest is not None and latest.date == primary_end

    if primary_matches_latest and latest is not None:
        logger.info(
            "Primary window matches latest history point; reusing portfolio + per-app totals."
        )
        apple_revenue = latest.apple_revenue
        google_revenue = latest.google_revenue
        app_revenues = list(latest.apps)
    else:
        logger.info(
            "App Store Connect: primary %s window %s → %s…",
            period,
            primary_start.isoformat(),
            primary_end.isoformat(),
        )
        try:
            apple_revenue = apple.fetch_apple_revenue(
                period,
                primary_start,
                primary_end,
                apple_skus=apple_skus,
                target_currency=target_currency,
            )
        except Exception as exc:  # noqa: BLE001
            _warn("App Store Connect", f"primary window failed: {exc}")
            apple_revenue = latest.apple_revenue if latest else 0.0

        logger.info("Google Play: primary window…")
        try:
            google_revenue = google_play.fetch_google_revenue(
                primary_start, primary_end, package_names=package_names
            )
        except Exception as exc:  # noqa: BLE001
            _warn("Google Play", f"primary window failed: {exc}")
            google_revenue = latest.google_revenue if latest else 0.0

        app_revenues = []
        if config.apps:
            logger.info("Fetching per-app revenues (%d app(s))…", len(config.apps))
        for index, app in enumerate(config.apps, start=1):
            logger.info("  App %d/%d: %s", index, len(config.apps), app.name)
            app_revenues.append(
                _fetch_app_window(
                    period,
                    primary_start,
                    primary_end,
                    app,
                    target_currency=target_currency,
                )
            )

    logger.info("Revenue fetch complete.")
    return RevenueSnapshot(
        period=period,
        period_start=primary_start,
        period_end=primary_end,
        currency=config.currency,
        apple_revenue=apple_revenue,
        google_revenue=google_revenue,
        total_revenue=apple_revenue + google_revenue,
        series=series,
        apps=app_revenues,
    )
