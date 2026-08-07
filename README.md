# x-mrr-banner

Update your X (Twitter) profile banner automatically with App Store Connect and Google Play revenues.

Fork this repo, run setup once, commit a banner template, and let GitHub Actions refresh the image on a schedule. X upload is optional (`upload_to_x` in [`config.yaml`](config.yaml)); when off, CI still saves `output/banner.png` as an artifact.

## Prerequisites

- Git + a GitHub account (fork/clone as usual)
- Python **3.11+** (use `python -m pip` if `pip` isn’t on your PATH)
- [GitHub CLI](https://cli.github.com/) (`gh`) — bootstrap installs it via Homebrew when missing; otherwise `brew install gh` then `gh auth login`

## Quick start

```bash
git clone https://github.com/Tulleb/x-mrr-banner.git
cd x-mrr-banner
./scripts/bootstrap.sh
```

Bootstrap creates `.venv`, runs `pip install -e .`, helps with `gh`, then launches the credential wizard (`.env` + Actions secrets).

```bash
source .venv/bin/activate
python -m x_mrr_banner generate_template
git add assets/template/ config.yaml
git commit -m "Add banner template and schedule config"
git push
```

Then run **Actions → Update X banner** on your fork (or wait for the cron).

Without bootstrap:

```bash
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -e .
python -m x_mrr_banner setup   # --local-only / --github-only / --skip-config
```

## Commands

```bash
python -m x_mrr_banner setup
python -m x_mrr_banner update
python -m x_mrr_banner update --dry-run --force
python -m x_mrr_banner update --upload
python -m x_mrr_banner generate_template
```

## Configuration

[`config.yaml`](config.yaml):

| Key | Purpose |
| ----- | --------- |
| `schedules.monthly` | `false` = monthly cron / dispatch no-ops |
| `upload_to_x` | `false` = compose only (no X API) |
| `currency` | Display label |
| `apple_skus` / `google_package_names` | Optional filters (empty = all) |

Only the **monthly** schedule is supported for now (previous full calendar month). Template prompt notes: [`docs/TEMPLATE.md`](docs/TEMPLATE.md).

## Secrets

| Variables | Role | GitHub Actions |
| ----------- | ------ | ---------------- |
| `ASC_*` | App Store Connect sales (Team key) | Synced by setup |
| `GOOGLE_PLAY_*` | Play bulk sales (`pubsite_prod_*` GCS) | Synced by setup |
| `X_*` | v1.1 `update_profile_banner` (OAuth 1.0a; no v2) | Synced if configured |
| `GEMINI_API_KEY` | Local `generate_template` only | Local `.env` only |

See [`.env.example`](.env.example). Never commit `.env`.

## How it works

1. Monthly cron (1st of month) / workflow dispatch runs (skip if `schedules.monthly` is false).
2. Requires committed `assets/template/{background.png,layout.yaml}`.
3. Fetches Apple + Google revenues for the previous full month, overlays text/numbers/chart, optionally uploads via `POST https://api.x.com/1.1/account/update_profile_banner.json`.
