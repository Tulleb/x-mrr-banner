from __future__ import annotations

import argparse
import logging
import sys

from x_mrr_banner.config import (
    BANNER_OUTPUT_PATH,
    load_config,
    load_dotenv_files,
    parse_period,
    require_template_assets,
)
from x_mrr_banner.extract.aggregate import collect_revenues
from x_mrr_banner.render.compose import compose_banner
from x_mrr_banner.setup_wizard import run_setup
from x_mrr_banner.template.generate import generate_template
from x_mrr_banner.upload.x_banner import upload_banner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("x_mrr_banner")


def cmd_update(args: argparse.Namespace) -> int:
    period = parse_period("monthly")
    config = load_config()

    try:
        require_template_assets()
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 1

    snapshot = collect_revenues(period, config)
    logger.info(
        "Revenues for %s (%s → %s): total=%.2f apple=%.2f google=%.2f %s",
        snapshot.period,
        snapshot.period_start,
        snapshot.period_end,
        snapshot.total_revenue,
        snapshot.apple_revenue,
        snapshot.google_revenue,
        snapshot.currency,
    )

    output = compose_banner(snapshot, output_path=BANNER_OUTPUT_PATH)
    logger.info("Composed banner at %s", output)

    should_upload = config.upload_to_x and not args.dry_run
    if args.upload:
        should_upload = not args.dry_run
    if not should_upload:
        reason = "dry-run" if args.dry_run else "upload_to_x is false in config.yaml"
        logger.info("Skipping X upload (%s)", reason)
        return 0

    upload_banner(output)
    return 0


def cmd_generate_template(args: argparse.Namespace) -> int:
    background, layout = generate_template(overwrite=args.overwrite)
    logger.info("Template ready: %s , %s", background, layout)
    logger.info("Commit assets/template/ so GitHub Actions can use it.")
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

    update = sub.add_parser("update", help="Fetch revenues, compose banner, upload to X")
    update.add_argument(
        "--dry-run",
        action="store_true",
        help="Compose the banner but do not upload to X",
    )
    update.add_argument(
        "--upload",
        action="store_true",
        help="Upload to X even if upload_to_x is false in config.yaml",
    )
    update.set_defaults(func=cmd_update)

    generate = sub.add_parser(
        "generate_template",
        help="Local-only: create assets/template via Nano Banana 2 Lite",
    )
    generate.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite an existing background/layout",
    )
    generate.set_defaults(func=cmd_generate_template)

    setup = sub.add_parser(
        "setup",
        help="Interactive wizard: write .env and sync GitHub Actions secrets",
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
        help="Do not prompt to update config.yaml (upload_to_x / currency)",
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
