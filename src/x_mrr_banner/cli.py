from __future__ import annotations

import argparse
import logging

from x_mrr_banner.banner.context import build_banner_context, render_banner_prompt
from x_mrr_banner.banner.generate import generate_banner
from x_mrr_banner.banner.icons import MAX_LOGO_SLOTS, fetch_app_icons
from x_mrr_banner.config import (
    AppConfig,
    RevenueSnapshot,
    format_currency,
    load_config,
    load_dotenv_files,
    output_paths_for_month,
    parse_period,
    require_banner_config,
)
from x_mrr_banner.extract.aggregate import collect_revenues
from x_mrr_banner.setup_wizard import run_setup
from x_mrr_banner.ui import configure_logging
from x_mrr_banner.upload.x_banner import upload_banner

configure_logging()
logger = logging.getLogger("x_mrr_banner")


def _log_banner_numbers(config: AppConfig, snapshot: RevenueSnapshot) -> None:
    """Log every figure that will be rendered into BANNER.md.j2."""
    currency = snapshot.currency
    ctx = build_banner_context(config, snapshot)

    logger.info("======== Banner numbers (will appear on the image) ========")
    logger.info(
        "Period window: %s → %s (%s / %s)",
        snapshot.period_start.isoformat(),
        snapshot.period_end.isoformat(),
        snapshot.period,
        ctx["content"]["period_label"],
    )
    logger.info(
        "Portfolio current: total=%s apple=%s google=%s %s",
        format_currency(snapshot.total_revenue, currency),
        format_currency(snapshot.apple_revenue, currency),
        format_currency(snapshot.google_revenue, currency),
        currency,
    )
    logger.info(
        "Challenge: period %s/%s | start=%s target=%s | progress=%s%%",
        ctx["challenge"]["current_period"],
        ctx["challenge"]["total_periods"],
        ctx["challenge"]["start_mrr_formatted"],
        ctx["challenge"]["target_mrr_formatted"],
        ctx["revenue"]["target_progress_percent"],
    )
    logger.info(
        "Content labels: top=%r headline=%r sub=%r period=%r revenue=%r",
        ctx["content"]["top_label"],
        ctx["content"]["headline"],
        ctx["content"]["subheadline"],
        ctx["content"]["period_label"],
        ctx["content"]["revenue_label"],
    )

    if not snapshot.apps:
        logger.info("Apps: (none configured — no per-app breakdown)")
    else:
        logger.info("Apps (%d):", len(snapshot.apps))
        for app_cfg, app_rev in zip(config.apps, snapshot.apps, strict=False):
            filters = app_cfg.revenue_apple_skus()
            logger.info(
                "  • %s: total=%s apple=%s google=%s | apple_skus=%s iap_skus=%s play=%s",
                app_rev.name,
                format_currency(app_rev.total_revenue, currency),
                format_currency(app_rev.apple_revenue, currency),
                format_currency(app_rev.google_revenue, currency),
                app_cfg.apple_skus or ["(none)"],
                app_cfg.apple_iap_skus or ["(none)"],
                app_cfg.google_package_names or ["(none)"],
            )
            if filters:
                logger.info("    ASC filter SKUs used: %s", filters)

    if not snapshot.series:
        logger.info("Revenue history: (empty)")
    else:
        logger.info("Revenue history (%d points, chronological):", len(snapshot.series))
        for point in snapshot.series:
            if point.apps:
                app_parts = ", ".join(
                    f"{app.name}={format_currency(app.total_revenue, currency)}"
                    for app in point.apps
                )
                logger.info(
                    "  • %s (%s): total=%s | apps: %s",
                    point.label,
                    point.date.isoformat(),
                    format_currency(point.total_revenue, currency),
                    app_parts,
                )
            else:
                logger.info(
                    "  • %s (%s): apple=%s google=%s total=%s",
                    point.label,
                    point.date.isoformat(),
                    format_currency(point.apple_revenue, currency),
                    format_currency(point.google_revenue, currency),
                    format_currency(point.total_revenue, currency),
                )

    logger.info("===========================================================")


def cmd_update(args: argparse.Namespace) -> int:
    period = parse_period("monthly")
    config = load_config()
    require_banner_config(config)

    logger.info("Starting banner update (period=%s)…", period)
    snapshot = collect_revenues(period, config)
    _log_banner_numbers(config, snapshot)

    prompt = render_banner_prompt(config, snapshot)
    banner_png, banner_md = output_paths_for_month(snapshot.period_start)
    banner_md.parent.mkdir(parents=True, exist_ok=True)
    banner_md.write_text(prompt, encoding="utf-8")
    logger.info("Wrote rendered prompt (%d chars) → %s", len(prompt), banner_md)

    app_names = [app.name for app in config.apps[:MAX_LOGO_SLOTS]]
    icons = fetch_app_icons(config.apps) if app_names else []
    if app_names:
        logger.info(
            "Will detect %d pure-red icon marker(s) for: %s",
            len(app_names),
            ", ".join(app_names),
        )

    output = generate_banner(
        prompt,
        destination=banner_png,
        icons=icons or None,
        app_names=app_names or None,
        background_color=config.theme.background_color,
        text_color=config.theme.text_color,
        watermark=config.watermark,
    )
    logger.info("Generated banner at %s", output)

    should_upload = config.upload_to_x and not args.dry_run
    if args.upload:
        should_upload = not args.dry_run
    if not should_upload:
        reason = "dry-run" if args.dry_run else "upload_to_x is false in config.yaml"
        logger.info("Skipping X upload (%s)", reason)
        return 0

    upload_banner(output)
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    return run_setup(
        skip_github=args.local_only,
        skip_config=args.skip_config,
        github_only=args.github_only,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="x-mrr-banner",
        description="Generate and upload an X banner from App Store + Google Play revenues.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    update = sub.add_parser(
        "update",
        help="Fetch revenues, render BANNER.md.j2, generate banner via OpenAI, upload to X",
    )
    update.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate the banner but do not upload to X",
    )
    update.add_argument(
        "--upload",
        action="store_true",
        help="Upload to X even if upload_to_x is false in config.yaml",
    )
    update.set_defaults(func=cmd_update)

    setup = sub.add_parser(
        "setup",
        help="Interactive wizard: write .env, config.yaml preferences, and sync GitHub secrets",
    )
    setup.add_argument(
        "--local-only",
        action="store_true",
        help="Write .env / config.yaml only; do not call `gh secret set`",
    )
    setup.add_argument(
        "--github-only",
        action="store_true",
        help="Push secrets from an existing .env to the fork via `gh`",
    )
    setup.add_argument(
        "--skip-config",
        action="store_true",
        help="Do not prompt to update config.yaml preferences",
    )
    setup.set_defaults(func=cmd_setup)

    return parser


def main(argv: list[str] | None = None) -> None:
    load_dotenv_files()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        code = args.func(args)
    except Exception as exc:  # noqa: BLE001 — CLI boundary
        logger.error("%s", exc)
        raise SystemExit(1) from exc
    raise SystemExit(code)


if __name__ == "__main__":
    main()
