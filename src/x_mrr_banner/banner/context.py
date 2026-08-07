from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from x_mrr_banner.banner.icons import ICON_MARKER_HEX, MAX_LOGO_SLOTS
from x_mrr_banner.config import (
    BANNER_ASPECT_RATIO,
    BANNER_HEIGHT,
    BANNER_TEMPLATE_PATH,
    BANNER_WIDTH,
    AppConfig,
    RevenueSnapshot,
    challenge_current_period,
    default_content_headline,
    format_currency,
    period_label,
    target_progress_percent,
)


def build_banner_context(config: AppConfig, snapshot: RevenueSnapshot) -> dict[str, Any]:
    currency = snapshot.currency
    progress = target_progress_percent(
        snapshot.total_revenue,
        config.challenge.start_mrr,
        config.challenge.target_mrr,
    )
    current_period = challenge_current_period(config.challenge)
    month_label = f"Month {current_period:02d} / {config.challenge.total_periods}"

    start_date = config.challenge.start_date
    deadline = config.challenge.deadline

    history = [
        {
            "label": point.label,
            "apple_formatted": format_currency(point.apple_revenue, currency),
            "google_formatted": format_currency(point.google_revenue, currency),
            "total_formatted": format_currency(point.total_revenue, currency),
            "apps": [
                {
                    "name": app.name,
                    "total_formatted": format_currency(app.total_revenue, currency),
                }
                for app in point.apps
            ],
        }
        for point in snapshot.series
    ]

    apps = [
        {
            "name": app.name,
            "current_mrr_formatted": format_currency(app.total_revenue, currency),
            "apple_mrr_formatted": format_currency(app.apple_revenue, currency),
            "google_mrr_formatted": format_currency(app.google_revenue, currency),
        }
        for app in snapshot.apps
    ]

    chart_app_names = [app.name for app in snapshot.apps]
    icon_apps = [app.name for app in config.apps[:MAX_LOGO_SLOTS]]

    period = period_label(snapshot)
    revenue_formatted = format_currency(snapshot.total_revenue, currency)

    target_mrr_formatted = format_currency(config.challenge.target_mrr, currency)
    content_headline = config.content.headline.strip() or default_content_headline(
        total_periods=config.challenge.total_periods,
        target_mrr=config.challenge.target_mrr,
        currency=currency,
    )
    return {
        "banner": {
            "width": BANNER_WIDTH,
            "height": BANNER_HEIGHT,
            "aspect_ratio": BANNER_ASPECT_RATIO,
        },
        "challenge": {
            "headline": config.challenge.headline,
            "start_date": start_date.isoformat() if start_date else "",
            "deadline": deadline.isoformat() if deadline else "",
            "current_period": current_period,
            "month_label": month_label,
            "total_periods": config.challenge.total_periods,
            "target_mrr_formatted": target_mrr_formatted,
            "start_mrr_formatted": format_currency(config.challenge.start_mrr, currency),
        },
        "revenue": {
            "current_mrr_formatted": revenue_formatted,
            "target_progress_percent": round(progress, 1),
            "history": history,
            "chart_app_names": chart_app_names,
        },
        "apps": apps,
        "icon_apps": icon_apps,
        "icon_marker_hex": ICON_MARKER_HEX,
        "content": {
            "top_label": config.content.top_label.strip() or "BUILDING IN PUBLIC",
            "headline": content_headline,
            "subheadline": (
                config.content.subheadline.strip()
                or "Sharing the real numbers, wins & failures"
            ),
            "apps_label": config.content.apps_label,
            "period_label": period,
            "revenue_label": revenue_formatted,
        },
        "theme": {
            "mood": config.theme.mood,
            "style": config.theme.style,
            "color_mode": config.theme.color_mode,
            "background_color": config.theme.background_color,
            "primary_color": config.theme.primary_color,
            "accent_color": config.theme.accent_color,
            "text_color": config.theme.text_color,
            "chart_color": config.theme.chart_color,
        },
        "watermark": {
            "enabled": config.watermark.enabled,
            "position": config.watermark.position,
        },
    }


def render_banner_prompt(
    config: AppConfig,
    snapshot: RevenueSnapshot,
    template_path: Path | None = None,
) -> str:
    path = template_path or BANNER_TEMPLATE_PATH
    if not path.is_file():
        raise FileNotFoundError(f"Banner template missing: {path}")

    env = Environment(
        loader=FileSystemLoader(str(path.parent)),
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
    )
    template = env.get_template(path.name)
    context = build_banner_context(config, snapshot)
    return template.render(**context)
