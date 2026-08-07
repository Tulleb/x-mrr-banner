from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from getpass import getpass
from pathlib import Path
from typing import Callable

import yaml

from x_mrr_banner.config import DEFAULT_CONFIG_PATH, REPO_ROOT
from x_mrr_banner.prerequisites import (
    ensure_gh_authenticated,
    prepare_environment,
    print_gh_install_help,
    try_install_gh,
)

logger = logging.getLogger(__name__)

ENV_PATH = REPO_ROOT / ".env"

# Secrets that belong in GitHub Actions (CI never needs Gemini).
GITHUB_SECRET_KEYS = (
    "ASC_ISSUER_ID",
    "ASC_KEY_ID",
    "ASC_PRIVATE_KEY",
    "ASC_VENDOR_NUMBER",
    "GOOGLE_PLAY_SERVICE_ACCOUNT_JSON",
    "GOOGLE_PLAY_REPORTS_BUCKET",
    "X_API_KEY",
    "X_API_SECRET",
    "X_ACCESS_TOKEN",
    "X_ACCESS_TOKEN_SECRET",
)


@dataclass
class FieldSpec:
    key: str
    title: str
    help_text: str
    docs_url: str | None = None
    secret: bool = True
    multiline: bool = False
    file_ok: bool = False
    file_hint: str | None = None
    # If set, store file contents in GitHub secret under this transform
    github_value_from_file: bool = False
    optional: bool = False
    validate: Callable[[str], str] | None = None


@dataclass
class Section:
    name: str
    intro: str
    fields: list[FieldSpec] = field(default_factory=list)
    skip_prompt: str | None = None


def _print_header(title: str) -> None:
    print()
    print("=" * 64)
    print(title)
    print("=" * 64)


def _prompt_yes_no(question: str, default: bool = True) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    while True:
        raw = input(question + suffix).strip().lower()
        if not raw:
            return default
        if raw in {"y", "yes"}:
            return True
        if raw in {"n", "no"}:
            return False
        print("Please answer y or n.")


def _prompt_value(spec: FieldSpec, existing: str | None = None) -> str | None:
    print()
    print(f"— {spec.title} ({spec.key})")
    for line in spec.help_text.strip().splitlines():
        print(f"  {line}")
    if spec.docs_url:
        print(f"  Docs: {spec.docs_url}")
    if existing:
        shown = "(set)" if spec.secret else existing
        print(f"  Current: {shown}")
        if not _prompt_yes_no("  Keep current value?", default=True):
            existing = None
        else:
            return existing

    if spec.optional and _prompt_yes_no("  Skip this field?", default=False):
        return ""

    if spec.file_ok:
        path_raw = input(f"  Path to file{f' ({spec.file_hint})' if spec.file_hint else ''} [Enter to paste]: ").strip()
        if path_raw:
            path = Path(path_raw).expanduser()
            if not path.is_file():
                raise FileNotFoundError(f"File not found: {path}")
            if spec.github_value_from_file or spec.multiline:
                return path.read_text(encoding="utf-8").strip()
            return str(path.resolve())

    if spec.multiline:
        print("  Paste value, then an empty line to finish:")
        lines: list[str] = []
        while True:
            line = input()
            if line == "" and lines:
                break
            lines.append(line)
        value = "\n".join(lines).strip()
    elif spec.secret:
        value = getpass("  Value (hidden): ").strip()
        if not value:
            value = input("  (empty — paste visibly) Value: ").strip()
    else:
        value = input("  Value: ").strip()

    if spec.validate:
        value = spec.validate(value)
    if not value and not spec.optional:
        raise ValueError(f"{spec.key} is required")
    return value


def _validate_json_or_path(value: str) -> str:
    path = Path(value).expanduser()
    if path.is_file():
        raw = path.read_text(encoding="utf-8")
        json.loads(raw)  # validate
        return raw.strip()
    json.loads(value)  # validate raw JSON
    return value.strip()


def _sections(*, include_x: bool, include_gemini: bool) -> list[Section]:
    sections = [
        Section(
            name="App Store Connect",
            intro=(
                "Create a Team API key (Individual keys cannot download sales reports).\n"
                "App Store Connect → Users and Access → Integrations → App Store Connect API."
            ),
            fields=[
                FieldSpec(
                    key="ASC_ISSUER_ID",
                    title="Issuer ID",
                    help_text="Shown at the top of the App Store Connect API keys page.",
                    docs_url="https://appstoreconnect.apple.com/access/integrations/api",
                    secret=False,
                ),
                FieldSpec(
                    key="ASC_KEY_ID",
                    title="Key ID",
                    help_text="The Key ID of the .p8 key you created (Admin or Finance access).",
                    docs_url="https://developer.apple.com/documentation/appstoreconnectapi/creating_api_keys_for_app_store_connect_api",
                    secret=False,
                ),
                FieldSpec(
                    key="ASC_PRIVATE_KEY",
                    title="Private key (.p8)",
                    help_text="Paste the full PEM, or provide the path to AuthKey_XXXXX.p8.",
                    file_ok=True,
                    file_hint="AuthKey_XXXXX.p8",
                    multiline=True,
                    github_value_from_file=True,
                ),
                FieldSpec(
                    key="ASC_VENDOR_NUMBER",
                    title="Vendor number",
                    help_text=(
                        "Payments and Financial Reports → vendor number "
                        "(also in sales report downloads)."
                    ),
                    docs_url="https://appstoreconnect.apple.com/",
                    secret=False,
                ),
            ],
        ),
        Section(
            name="Google Play",
            intro=(
                "Play Console exports sales CSVs to a Cloud Storage bucket (pubsite_prod_*).\n"
                "Create a service account, invite it in Play Console with financial report access,\n"
                "then grant it permission to read that bucket."
            ),
            fields=[
                FieldSpec(
                    key="GOOGLE_PLAY_SERVICE_ACCOUNT_JSON",
                    title="Service account JSON",
                    help_text=(
                        "Path to the downloaded service-account key JSON, or paste the JSON.\n"
                        "Locally and in GitHub Secrets we store the JSON contents "
                        "(not a machine-specific path)."
                    ),
                    docs_url="https://support.google.com/googleplay/android-developer/answer/6135870",
                    file_ok=True,
                    file_hint="service-account.json",
                    multiline=True,
                    github_value_from_file=True,
                    validate=_validate_json_or_path,
                ),
                FieldSpec(
                    key="GOOGLE_PLAY_REPORTS_BUCKET",
                    title="Reports bucket name",
                    help_text=(
                        "Play Console → Download reports → copy Cloud Storage URI.\n"
                        "Use only the bucket name, e.g. pubsite_prod_rev_1234567890"
                    ),
                    secret=False,
                ),
            ],
        ),
    ]
    if include_x:
        sections.append(
            Section(
                name="X (Twitter)",
                intro=(
                    "Create a developer app with Read and write permissions, then create\n"
                    "OAuth 1.0a access tokens for the account whose banner you want to update.\n"
                    "Banner updates use official v1.1 POST account/update_profile_banner\n"
                    "(https://api.x.com/1.1/…); there is no v2 equivalent.\n"
                    "X API access is pay-per-use / credit-based for most new apps."
                ),
                fields=[
                    FieldSpec(
                        key="X_API_KEY",
                        title="API Key (Consumer Key)",
                        help_text="Developer Portal → your app → Keys and tokens.",
                        docs_url="https://developer.x.com/en/portal/dashboard",
                    ),
                    FieldSpec(
                        key="X_API_SECRET",
                        title="API Secret (Consumer Secret)",
                        help_text="Same page as the API Key.",
                        docs_url="https://developer.x.com/en/portal/dashboard",
                    ),
                    FieldSpec(
                        key="X_ACCESS_TOKEN",
                        title="Access Token",
                        help_text="User access token with write permission for your account.",
                    ),
                    FieldSpec(
                        key="X_ACCESS_TOKEN_SECRET",
                        title="Access Token Secret",
                        help_text="Paired with the access token.",
                    ),
                ],
            )
        )
    if include_gemini:
        sections.append(
            Section(
                name="Gemini (local template only)",
                intro=(
                    "Used only by `generate_template` on your machine (Nano Banana 2 Lite).\n"
                    "Not uploaded to GitHub Actions secrets."
                ),
                fields=[
                    FieldSpec(
                        key="GEMINI_API_KEY",
                        title="Gemini API key",
                        help_text="Google AI Studio → Get API key.",
                        docs_url="https://aistudio.google.com/apikey",
                        optional=True,
                    ),
                ],
            )
        )
    return sections


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    # Prefer python-dotenv for standard KEY=value including \n escapes.
    try:
        from dotenv import dotenv_values

        loaded = dotenv_values(path)
        for key, value in loaded.items():
            if key and value is not None:
                values[key] = value
        return values
    except Exception:  # noqa: BLE001
        pass

    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        name, raw = line.split("=", 1)
        values[name.strip()] = raw.strip().strip('"').strip("'").replace("\\n", "\n")
    return values


def _escape_env_value(value: str) -> str:
    stripped = value.strip()
    if stripped.startswith("{"):
        return json.dumps(json.loads(stripped), separators=(",", ":"))
    if "\n" in value or stripped.startswith("-----"):
        return value.replace("\n", "\\n")
    return value


def write_env_file(values: dict[str, str], path: Path = ENV_PATH) -> Path:
    order = [
        ("App Store Connect", ["ASC_ISSUER_ID", "ASC_KEY_ID", "ASC_PRIVATE_KEY", "ASC_VENDOR_NUMBER"]),
        ("Google Play", ["GOOGLE_PLAY_SERVICE_ACCOUNT_JSON", "GOOGLE_PLAY_REPORTS_BUCKET"]),
        ("X (Twitter)", ["X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"]),
        ("Gemini", ["GEMINI_API_KEY"]),
    ]
    lines = [
        "# Generated by `python -m x_mrr_banner setup`",
        "# Do not commit this file.",
        "",
    ]
    for title, keys in order:
        present = [k for k in keys if k in values and values[k] != ""]
        if not present:
            continue
        lines.append(f"# --- {title} ---")
        for key in present:
            lines.append(f"{key}={_escape_env_value(values[key])}")
        lines.append("")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def _load_config_raw() -> dict:
    if not DEFAULT_CONFIG_PATH.exists():
        return {}
    with DEFAULT_CONFIG_PATH.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return data if isinstance(data, dict) else {}


def update_config_from_prompts() -> None:
    _print_header("Schedule & upload preferences (config.yaml)")
    raw = _load_config_raw()
    schedules = dict(raw.get("schedules") or {})
    print("Enable which GitHub Action schedules should actually run?")
    for period in ("daily", "weekly", "monthly"):
        current = bool(schedules.get(period, period == "monthly"))
        schedules[period] = _prompt_yes_no(f"  Enable {period}?", default=current)
    upload = _prompt_yes_no(
        "Upload composed banners to X automatically?",
        default=bool(raw.get("upload_to_x", False)),
    )
    currency = input(f"Display currency [{raw.get('currency') or 'USD'}]: ").strip() or str(
        raw.get("currency") or "USD"
    )
    raw["schedules"] = schedules
    raw["upload_to_x"] = upload
    raw["currency"] = currency
    raw.setdefault("apple_skus", [])
    raw.setdefault("google_package_names", [])

    # Preserve comments by rewriting a clean documented file.
    content = (
        "# Enable only the schedules you want the GitHub Action to actually run.\n"
        "# Disabled periods exit successfully without fetching revenue or uploading.\n"
        "schedules:\n"
        f"  daily: {'true' if schedules.get('daily') else 'false'}\n"
        f"  weekly: {'true' if schedules.get('weekly') else 'false'}\n"
        f"  monthly: {'true' if schedules.get('monthly') else 'false'}\n"
        "\n"
        "# When false, crons/update still fetch + compose the banner (and CI uploads an\n"
        "# artifact) but skip calling the X API. Set true once X credentials are ready.\n"
        f"upload_to_x: {'true' if upload else 'false'}\n"
        "\n"
        "# Display / aggregation currency label (reports may still be multi-currency).\n"
        f"currency: {currency}\n"
        "\n"
        "# Optional: restrict to these Apple SKUs / Google package names (empty = all).\n"
        "apple_skus: []\n"
        "google_package_names: []\n"
    )
    DEFAULT_CONFIG_PATH.write_text(content, encoding="utf-8")
    print(f"Wrote {DEFAULT_CONFIG_PATH.relative_to(REPO_ROOT)}")


def _ensure_gh() -> None:
    if not try_install_gh(interactive=True):
        print_gh_install_help()
        raise RuntimeError(
            "GitHub CLI (`gh`) is required to sync secrets. "
            "Install it (see README.md → Prerequisites), then run: gh auth login"
        )
    if not ensure_gh_authenticated(interactive=True):
        raise RuntimeError(
            "GitHub CLI is not authenticated. Run `gh auth login` then re-run setup."
        )


def _repo_slug() -> str:
    result = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(
            "Could not detect the GitHub repo. Fork the project, `git remote add origin …`, "
            "and ensure `gh` can see it (`gh repo view`)."
        )
    return result.stdout.strip()


def push_github_secrets(values: dict[str, str], *, repo: str | None = None) -> list[str]:
    _ensure_gh()
    slug = repo or _repo_slug()
    uploaded: list[str] = []
    for key in GITHUB_SECRET_KEYS:
        value = values.get(key, "")
        if not value:
            continue
        # Prefer JSON body for Play SA if a path somehow remained.
        if key == "GOOGLE_PLAY_SERVICE_ACCOUNT_JSON":
            path = Path(value).expanduser()
            if path.is_file():
                value = path.read_text(encoding="utf-8").strip()
        proc = subprocess.run(
            ["gh", "secret", "set", key, "--repo", slug],
            input=value,
            text=True,
            capture_output=True,
            check=False,
            cwd=REPO_ROOT,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"Failed to set GitHub secret {key} on {slug}: {proc.stderr.strip() or proc.stdout}"
            )
        uploaded.append(key)
        print(f"  ✓ GitHub Actions secret {key}")
    return uploaded


def collect_secrets_interactively(existing: dict[str, str] | None = None) -> dict[str, str]:
    existing = existing or {}
    _print_header("x-mrr-banner setup")
    print(
        "This wizard writes a local .env and can sync the same values to GitHub\n"
        "Actions secrets on your fork (via `gh secret set`).\n"
        "Never commit .env. Gemini stays local-only."
    )

    include_x = _prompt_yes_no(
        "Configure X (Twitter) banner upload credentials now?",
        default=False,
    )
    include_gemini = _prompt_yes_no(
        "Configure Gemini API key for local template generation?",
        default=True,
    )

    values = dict(existing)
    for section in _sections(include_x=include_x, include_gemini=include_gemini):
        _print_header(section.name)
        print(section.intro)
        for spec in section.fields:
            values[spec.key] = _prompt_value(spec, existing=values.get(spec.key) or None) or ""
    return values


def run_setup(
    *,
    skip_github: bool = False,
    skip_config: bool = False,
    github_only: bool = False,
) -> int:
    prepare_environment(want_github=not skip_github or github_only, interactive=True)
    existing = _parse_env_file(ENV_PATH)

    if github_only:
        if not existing:
            raise RuntimeError(f"No {ENV_PATH} found. Run setup without --github-only first.")
        _print_header("Push existing .env secrets to GitHub")
        print(f"Target repo: {_repo_slug()}")
        uploaded = push_github_secrets(existing)
        print(f"Uploaded {len(uploaded)} secret(s).")
        return 0

    values = collect_secrets_interactively(existing)
    env_path = write_env_file(values)
    print(f"\nWrote {env_path.relative_to(REPO_ROOT)}")

    if not skip_config:
        update_config_from_prompts()

    if skip_github:
        print("\nSkipped GitHub secrets (--local-only).")
    else:
        if _prompt_yes_no(
            "\nUpload secrets to GitHub Actions on this fork now? (requires `gh`)",
            default=True,
        ):
            _print_header("GitHub Actions secrets")
            print(f"Target repo: {_repo_slug()}")
            uploaded = push_github_secrets(values)
            if not uploaded:
                print("No CI secrets to upload (all empty).")
            else:
                print(f"Uploaded {len(uploaded)} secret(s).")
        else:
            print("Skipped GitHub secrets. Re-run with: python -m x_mrr_banner setup --github-only")

    _print_header("Next steps")
    print("1. Activate the venv if needed:")
    print("     source .venv/bin/activate          # macOS / Linux")
    print("     .venv\\Scripts\\activate             # Windows")
    print("2. Generate the banner template once:")
    print("     python -m x_mrr_banner generate_template")
    print("3. Commit and push template + config:")
    print("     git add assets/template/ config.yaml && git commit -m \"Add banner template\" && git push")
    print("4. GitHub → Actions → Update X banner → Run workflow")
    return 0
