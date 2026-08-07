from __future__ import annotations

import json
import logging
import os
import subprocess
import time
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
from x_mrr_banner import ui

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
    open_url: str | None = None  # Page to open in the browser right now
    docs_url: str | None = None  # Optional background documentation
    secret: bool = True
    multiline: bool = False
    file_ok: bool = False
    file_hint: str | None = None
    # If set, store file contents in GitHub secret under this transform
    github_value_from_file: bool = False
    # Ask only for a filename dropped into the repo root (no multiline paste).
    repo_filename: bool = False
    # Instruction-only gate: press Enter when the manual action is done.
    ack_only: bool = False
    optional: bool = False
    validate: Callable[[str], str] | None = None


@dataclass
class Section:
    name: str
    intro: str
    fields: list[FieldSpec] = field(default_factory=list)
    skip_prompt: str | None = None
    optional: bool = False
    configure_prompt: str | None = None


def _print_header(title: str) -> None:
    ui.header(title)


def _print_intro(text: str) -> None:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            print()
        elif stripped.lower().startswith("open:"):
            rest = stripped.split(":", 1)[1].strip()
            ui.step(f"Open: {ui.url(rest)}")
        elif stripped.lower().startswith("warning:"):
            ui.warn(stripped.split(":", 1)[1].strip())
        elif stripped.startswith("•") or stripped.startswith("-"):
            body = stripped.lstrip("•- ").strip()
            if "preferred" in body.lower():
                ui.bullet(ui.emphasize(body))
            else:
                ui.bullet(body)
        elif "←" in stripped:
            ui.bullet(ui.emphasize(stripped.lstrip("• ").strip()))
        else:
            ui.info(line if line.startswith(" ") else stripped)


def _prompt_yes_no(question: str, default: bool = True) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    while True:
        raw = input(ui.prompt(question + suffix)).strip().lower()
        if not raw:
            return default
        if raw in {"y", "yes"}:
            return True
        if raw in {"n", "no"}:
            return False
        ui.warn("Please answer y or n.")


def _wait_for_enter(message: str = "Press Enter when done") -> None:
    input(ui.prompt(f"  {message}… "))


def _resolve_repo_file(name: str) -> Path:
    """Resolve a dropped filename to a file inside the repo (never outside)."""
    cleaned = name.strip().strip('"').strip("'")
    if not cleaned:
        raise ValueError("Filename is required")
    path = Path(cleaned).expanduser()
    if not path.is_absolute():
        path = (REPO_ROOT / path).resolve()
    else:
        path = path.resolve()
    try:
        path.relative_to(REPO_ROOT.resolve())
    except ValueError as exc:
        raise ValueError(
            f"File must be inside the repo folder ({REPO_ROOT}), got: {path}"
        ) from exc
    if not path.is_file():
        raise FileNotFoundError(
            f"File not found: {path}\n"
            f"  Drop the file into {REPO_ROOT} and type its name "
            f"(e.g. AuthKey_XXXXXXXXXX.p8)."
        )
    return path


def _field_is_set(values: dict[str, str], key: str) -> bool:
    return bool((values.get(key) or "").strip())


def _section_is_complete(section: Section, values: dict[str, str]) -> bool:
    required = [
        spec for spec in section.fields if not spec.optional and not spec.ack_only
    ]
    # Ack gates that persist a flag must also be done when present in the section.
    acks = [spec for spec in section.fields if spec.ack_only]
    if not required:
        return any(_field_is_set(values, spec.key) for spec in section.fields)
    if not all(_field_is_set(values, spec.key) for spec in required):
        return False
    return all(_field_is_set(values, spec.key) for spec in acks)


def _prompt_value(spec: FieldSpec, existing: str | None = None) -> str | None:
    print()
    ui.field_heading(spec.title, spec.key)

    if existing and not spec.ack_only:
        shown = ui.success_text("(already set)") if spec.secret else existing
        ui.ok(f"Current: {shown}")
        if _prompt_yes_no(
            "  Keep this and continue to the next step?",
            default=True,
        ):
            return existing
        ui.info("  Reconfigure — enter a new value.")
    elif existing and spec.ack_only:
        ui.ok("Already marked done previously")
        if _prompt_yes_no("  Keep this and continue to the next step?", default=True):
            return existing
        ui.info("  Do this checklist again.")

    for line in spec.help_text.strip().splitlines():
        ui.info(f"  {line}")
    if spec.open_url:
        ui.step(f"Open: {ui.url(spec.open_url)}")
    if spec.docs_url and spec.docs_url != spec.open_url:
        ui.info(f"  Docs: {ui.url(spec.docs_url)}")

    if spec.ack_only:
        print()
        _wait_for_enter("Press Enter when you have done this")
        ui.ok("Marked as done")
        return "done"

    if spec.optional and not existing and _prompt_yes_no("  Skip this field?", default=False):
        return ""

    if spec.repo_filename:
        example = spec.file_hint or "filename.ext"
        name = input(ui.prompt(f"  Filename in this repo (e.g. {example}): ")).strip()
        path = _resolve_repo_file(name)
        ui.ok(f"Reading {path.relative_to(REPO_ROOT)}")
        return path.read_text(encoding="utf-8").strip()

    if spec.file_ok:
        hint = f" ({spec.file_hint})" if spec.file_hint else ""
        path_raw = input(
            ui.prompt(f"  Path to file{hint} — or press Enter to paste the file contents: ")
        ).strip()
        if path_raw:
            path = Path(path_raw).expanduser()
            if not path.is_absolute():
                candidate = REPO_ROOT / path
                if candidate.is_file():
                    path = candidate
            if not path.is_file():
                raise FileNotFoundError(f"File not found: {path}")
            if spec.github_value_from_file or spec.multiline:
                return path.read_text(encoding="utf-8").strip()
            return str(path.resolve())

    if spec.multiline:
        ui.info("  Paste the full file contents below.")
        ui.info("  When finished, press Enter on an empty line:")
        lines: list[str] = []
        while True:
            line = input()
            if line == "" and lines:
                break
            lines.append(line)
        value = "\n".join(lines).strip()
    elif spec.secret:
        value = getpass(ui.prompt("  Value (hidden): ")).strip()
        if not value:
            value = input(ui.prompt("  (empty — paste visibly) Value: ")).strip()
    else:
        value = input(ui.prompt("  Value: ")).strip()

    if spec.validate:
        before = value
        value = spec.validate(value)
        if value != before:
            ui.ok(f"Parsed as: {value}")
    if not value and not spec.optional:
        raise ValueError(f"{spec.key} is required")
    return value


def _extract_gcs_bucket(value: str) -> str:
    """Accept a bucket name or full gs:// URI and return the bucket only."""
    cleaned = value.strip().strip('"').strip("'")
    if not cleaned:
        raise ValueError("Cloud Storage URI or bucket name is required")
    if cleaned.startswith("gs://"):
        rest = cleaned[len("gs://") :]
        bucket = rest.split("/", 1)[0].strip()
    else:
        bucket = cleaned.split("/", 1)[0].strip()
    if not bucket:
        raise ValueError(
            "Could not parse a bucket name. Paste the full URI, e.g. "
            "gs://pubsite_prod_rev_1234567890/sales/..."
        )
    return bucket


def _validate_json_or_path(value: str) -> str:
    stripped = value.strip()
    if not stripped.startswith("{"):
        path = Path(stripped).expanduser()
        try:
            if path.is_file():
                raw = path.read_text(encoding="utf-8")
                json.loads(raw)  # validate
                return raw.strip()
        except OSError:
            pass
    json.loads(stripped)  # validate raw JSON
    return stripped


ASC_API_KEYS_URL = "https://appstoreconnect.apple.com/access/integrations/api"
ASC_HOME_URL = "https://appstoreconnect.apple.com/"
ASC_PAYMENTS_DOCS = (
    "https://developer.apple.com/help/app-store-connect/getting-paid/view-payments-and-proceeds/"
)
GCP_SERVICE_ACCOUNTS_URL = "https://console.cloud.google.com/iam-admin/serviceaccounts"
PLAY_CONSOLE_URL = "https://play.google.com/console/"
PLAY_USERS_URL = "https://play.google.com/console/developers/users-and-permissions"
X_DEVELOPER_PORTAL_URL = "https://console.x.com/"
GEMINI_API_KEYS_URL = "https://aistudio.google.com/apikey"

# Copy-paste text for X Developer Portal "Describe all of your use cases" (≥100 chars).
X_API_USE_CASE_TEXT = (
    "I use the official X API solely to update my own X profile banner image on a "
    "scheduled basis. It's using a private automation. No third-party data resale. "
    "Access is limited to my authenticating account."
)


def _maybe_show_x_use_case_text() -> None:
    ui.info(
        "X may ask you to describe your API use cases (Developer Agreement & Policy, "
        "often 100+ characters)."
    )
    if not _prompt_yes_no(
        "Show a ready-to-paste purpose text for that form?",
        default=True,
    ):
        return
    print()
    ui.step("Copy everything between the lines below:")
    ui.info("-" * 64)
    # Print unstyled so copy-paste has no ANSI codes.
    print(X_API_USE_CASE_TEXT)
    ui.info("-" * 64)
    ui.info(f"({len(X_API_USE_CASE_TEXT)} characters)")
    _wait_for_enter("Press Enter after you have pasted it into the X developer form")



def _sections(*, include_x: bool, include_gemini: bool) -> list[Section]:
    sections = [
        Section(
            name="App Store Connect",
            intro=(
                "Create a Team API key (Individual keys cannot download sales reports).\n"
                f"Open: {ASC_API_KEYS_URL}\n"
                "Path: Users and Access → Integrations → App Store Connect API.\n"
                "\n"
                "When generating the key, under Access select:\n"
                "  • Sales and Reports  ← preferred (least privilege for this project)\n"
                "  • Finance or Admin   ← also work if you already use those roles\n"
                "Name it something like \"X MRR Banner\", then Generate and download the .p8 once.\n"
                "\n"
                "Drop the downloaded AuthKey_XXXXX.p8 file into this repo folder\n"
                f"({REPO_ROOT.name}/). You will type only the filename next — do not paste the key."
            ),
            fields=[
                FieldSpec(
                    key="ASC_ISSUER_ID",
                    title="Issuer ID",
                    help_text=(
                        "On the API keys page, copy Issuer ID from the top of the page\n"
                        "(above the list of Active keys)."
                    ),
                    open_url=ASC_API_KEYS_URL,
                    docs_url="https://developer.apple.com/documentation/appstoreconnectapi/creating_api_keys_for_app_store_connect_api",
                    secret=False,
                ),
                FieldSpec(
                    key="ASC_KEY_ID",
                    title="Key ID",
                    help_text=(
                        "In the Active keys table, copy the Key ID for the key you just created.\n"
                        "Access must include Sales and Reports (or Finance / Admin)."
                    ),
                    open_url=ASC_API_KEYS_URL,
                    docs_url="https://developer.apple.com/documentation/appstoreconnectapi/creating_api_keys_for_app_store_connect_api",
                    secret=False,
                ),
                FieldSpec(
                    key="ASC_PRIVATE_KEY",
                    title="Private key (.p8 file)",
                    help_text=(
                        "1. Download AuthKey_XXXXX.p8 from the API keys page (once only).\n"
                        "2. Drag it into this repository folder (e.g. secrets/).\n"
                        "3. Type the path/filename here, including .p8\n"
                        "   Example: secrets/AuthKey_AB12CD34EF.p8\n"
                        "The script reads the file (do not paste the key contents).\n"
                        "*.p8 files are gitignored — never commit them."
                    ),
                    open_url=ASC_API_KEYS_URL,
                    file_hint="secrets/AuthKey_XXXXXXXXXX.p8",
                    repo_filename=True,
                    github_value_from_file=True,
                ),
                FieldSpec(
                    key="ASC_VENDOR_NUMBER",
                    title="Vendor number",
                    help_text=(
                        "1. Open App Store Connect home\n"
                        "2. Click Payments and Financial Reports\n"
                        "3. Top-left: under your Legal Entity Name, copy the Vendor Number\n"
                        "   (digits only, e.g. 12345678)\n"
                        "If several entities appear, open the name menu and pick the right one."
                    ),
                    open_url=ASC_HOME_URL,
                    docs_url=ASC_PAYMENTS_DOCS,
                    secret=False,
                ),
            ],
        ),
        Section(
            name="Google Play",
            intro=(
                "Play Console exports sales CSVs to a Cloud Storage bucket (pubsite_prod_*).\n"
                "\n"
                "Flow:\n"
                "  1. Create a GCP service account + JSON key (no special GCP roles needed)\n"
                "  2. Invite that SA email in Play Console with bulk report download access\n"
                "  3. Copy the reports bucket name from Play Console → Download reports"
            ),
            fields=[
                FieldSpec(
                    key="GOOGLE_PLAY_SERVICE_ACCOUNT_JSON",
                    title="Service account JSON",
                    help_text=(
                        "Create a service account in Google Cloud (any project is fine).\n"
                        "\n"
                        "GCP Step 2 “Permissions (optional)”: SKIP IT — click Continue/Done\n"
                        "with no role. Do NOT pick Financial Services Admin/Viewer\n"
                        "(those are unrelated Cloud APIs).\n"
                        "\n"
                        "Then: Keys → Add key → Create new key → JSON → download the file.\n"
                        "Drop it into this repo (e.g. secrets/play-sa.json) and enter the path."
                    ),
                    open_url=GCP_SERVICE_ACCOUNTS_URL,
                    docs_url="https://support.google.com/googleplay/android-developer/answer/6135870",
                    file_ok=True,
                    file_hint="secrets/play-service-account.json",
                    multiline=True,
                    github_value_from_file=True,
                    validate=_validate_json_or_path,
                ),
                FieldSpec(
                    key="GOOGLE_PLAY_SA_INVITED",
                    title="Invite service account in Play Console",
                    help_text=(
                        "Grant the service account access to download bulk reports:\n"
                        "\n"
                        "1. In Google Cloud → your service account → Details tab,\n"
                        "   copy the Email (…@….iam.gserviceaccount.com)\n"
                        "2. Open Play Console → Users and permissions → Invite new users\n"
                        "3. Paste that email as the invitee\n"
                        "4. Under Account permissions, enable:\n"
                        "     “View app information and download bulk reports”\n"
                        "5. Send the invite / save\n"
                        "\n"
                        "(Permissions can take up to ~24h to apply.)"
                    ),
                    open_url=PLAY_USERS_URL,
                    docs_url="https://support.google.com/googleplay/android-developer/answer/6135870",
                    ack_only=True,
                    secret=False,
                ),
                FieldSpec(
                    key="GOOGLE_PLAY_REPORTS_BUCKET",
                    title="Reports bucket (Cloud Storage URI)",
                    help_text=(
                        "1. Open Play Console → Download reports (any report type)\n"
                        "2. Click Copy Cloud Storage URI\n"
                        "3. Paste the full URI here — we extract the bucket name\n"
                        "   Example paste:\n"
                        "   gs://pubsite_prod_rev_1234567890/sales/salesreport_202601.zip\n"
                        "   → saved as pubsite_prod_rev_1234567890"
                    ),
                    open_url=PLAY_CONSOLE_URL,
                    docs_url="https://support.google.com/googleplay/android-developer/answer/6135870",
                    secret=False,
                    validate=_extract_gcs_bucket,
                ),
            ],
        ),
    ]
    if include_x:
        sections.append(
            Section(
                name="X (Twitter)",
                optional=True,
                configure_prompt="Configure X (Twitter) banner upload credentials now?",
                intro=(
                    "Create an X developer app, then copy OAuth 1.0a keys for your account.\n"
                    "\n"
                    f"Open: {X_DEVELOPER_PORTAL_URL}\n"
                    "Path: Apps → Create App → Create your app\n"
                    "\n"
                    "Warning: After creating an app, it may not show in the main Apps list —\n"
                    "look under the hidden \"Standalone Apps\" dropdown.\n"
                    "Warning: The X console allows creating at most 3 apps per day.\n"
                    "\n"
                    "When asked for Environment, choose:\n"
                    "  • Production  ← required (this updates your real profile banner)\n"
                    "  • Do not use Development or Staging for the live GitHub Action\n"
                    "\n"
                    "App settings: enable Read and write user permissions, then create\n"
                    "OAuth 1.0a access tokens for the account whose banner you update.\n"
                    "Banner upload uses v1.1 account/update_profile_banner (no v2 equivalent).\n"
                    "API access is pay-per-use / credit-based for most new apps."
                ),
                fields=[
                    FieldSpec(
                        key="X_API_KEY",
                        title="API Key (Consumer Key)",
                        help_text=(
                            "console.x.com → Apps → your app (or Standalone Apps) →\n"
                            "Keys and tokens → Consumer Keys → API Key."
                        ),
                        open_url=X_DEVELOPER_PORTAL_URL,
                    ),
                    FieldSpec(
                        key="X_API_SECRET",
                        title="API Secret (Consumer Secret)",
                        help_text=(
                            "Same Keys and tokens page → Consumer Keys → API Key Secret."
                        ),
                        open_url=X_DEVELOPER_PORTAL_URL,
                    ),
                    FieldSpec(
                        key="X_ACCESS_TOKEN",
                        title="Access Token",
                        help_text=(
                            "Keys and tokens → Authentication Tokens → Access Token\n"
                            "(app must have Read and write; regenerate tokens after changing permissions)."
                        ),
                        open_url=X_DEVELOPER_PORTAL_URL,
                    ),
                    FieldSpec(
                        key="X_ACCESS_TOKEN_SECRET",
                        title="Access Token Secret",
                        help_text="Same Authentication Tokens section → Access Token Secret.",
                        open_url=X_DEVELOPER_PORTAL_URL,
                    ),
                ],
            )
        )
    if include_gemini:
        sections.append(
            Section(
                name="Gemini (local template only)",
                optional=True,
                configure_prompt="Configure Gemini API key for local template generation?",
                intro=(
                    "Used only by `generate_template` on your machine (Nano Banana 2 Lite).\n"
                    "Not uploaded to GitHub Actions secrets."
                ),
                fields=[
                    FieldSpec(
                        key="GEMINI_API_KEY",
                        title="Gemini API key",
                        help_text="Google AI Studio → Create API key → copy the key.",
                        open_url=GEMINI_API_KEYS_URL,
                        docs_url="https://ai.google.dev/gemini-api/docs/api-key",
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
        ("Google Play", [
            "GOOGLE_PLAY_SERVICE_ACCOUNT_JSON",
            "GOOGLE_PLAY_SA_INVITED",
            "GOOGLE_PLAY_REPORTS_BUCKET",
        ]),
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
    ui.info("Only the monthly schedule is supported for now (previous full calendar month).")
    schedules = {
        "monthly": _prompt_yes_no(
            "Enable monthly GitHub Action runs?",
            default=bool(schedules.get("monthly", True)),
        )
    }
    upload = _prompt_yes_no(
        "Upload composed banners to X automatically?",
        default=bool(raw.get("upload_to_x", False)),
    )
    currency = input(
        ui.prompt(f"Display currency [{raw.get('currency') or 'USD'}]: ")
    ).strip() or str(raw.get("currency") or "USD")
    raw["schedules"] = schedules
    raw["upload_to_x"] = upload
    raw["currency"] = currency
    raw.setdefault("apple_skus", [])
    raw.setdefault("google_package_names", [])

    # Preserve comments by rewriting a clean documented file.
    content = (
        "# Monthly schedule for the GitHub Action (previous full calendar month).\n"
        "# When false, the Action exits successfully without fetching or uploading.\n"
        "schedules:\n"
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
    ui.ok(f"Wrote {DEFAULT_CONFIG_PATH.relative_to(REPO_ROOT)}")


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
            stripped = value.strip()
            if not stripped.startswith("{"):
                path = Path(stripped).expanduser()
                try:
                    if path.is_file():
                        value = path.read_text(encoding="utf-8").strip()
                except OSError:
                    pass
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
                f"Failed to set GitHub secret {key} on {slug}: "
                f"{(proc.stderr or proc.stdout or '').strip()[:500]}"
            )
        uploaded.append(key)
        ui.ok(f"GitHub Actions secret {key}")
    return uploaded


def _advance_after_step(message: str, *, seconds: float = 2.0) -> None:
    ui.celebrate(message)
    ui.info("Moving on…")
    time.sleep(seconds)


def _persist_progress(values: dict[str, str], *, reason: str) -> None:
    """Write .env after each section so Ctrl+C doesn't lose completed steps."""
    path = write_env_file(values)
    ui.ok(f"Saved progress to {path.relative_to(REPO_ROOT)} ({reason})")


def collect_secrets_interactively(existing: dict[str, str] | None = None) -> dict[str, str]:
    existing = existing or {}
    _print_header("x-mrr-banner setup")
    ui.info("This wizard writes a local .env and can sync the same values to GitHub")
    ui.info("Actions secrets on your fork (via `gh secret set`).")
    ui.warn("Never commit .env. Gemini stays local-only.")
    if existing:
        ui.ok(f"Loaded {len([v for v in existing.values() if v])} existing value(s) from .env")

    values = dict(existing)
    sections = _sections(include_x=True, include_gemini=True)
    try:
        for section in sections:
            _print_header(section.name)

            if _section_is_complete(section, values):
                set_count = sum(1 for spec in section.fields if _field_is_set(values, spec.key))
                ui.ok(f"Already configured ({set_count}/{len(section.fields)} values in .env)")
                if not _prompt_yes_no(
                    f"Reconfigure {section.name}?",
                    default=False,
                ):
                    _advance_after_step(f"{section.name} looks good — nice work!")
                    continue
                ui.info(f"Reconfiguring {section.name}.")
            elif section.optional:
                question = section.configure_prompt or f"Configure {section.name} now?"
                if not _prompt_yes_no(question, default=True):
                    ui.info(f"Skipping {section.name}.")
                    _advance_after_step(f"{section.name} skipped — all good!")
                    continue

            _print_intro(section.intro)
            if section.name.startswith("X"):
                _maybe_show_x_use_case_text()
            for spec in section.fields:
                values[spec.key] = (
                    _prompt_value(spec, existing=values.get(spec.key) or None) or ""
                )
            _persist_progress(values, reason=f"{section.name} complete")
            _advance_after_step(f"{section.name} setup complete — great job!")
    except KeyboardInterrupt:
        print()
        _persist_progress(values, reason="interrupted")
        ui.warn("Setup interrupted — re-run bootstrap to continue from where you left off.")
        raise SystemExit(130) from None

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
        ui.info(f"Target repo: {ui.url(_repo_slug())}")
        uploaded = push_github_secrets(existing)
        ui.ok(f"Uploaded {len(uploaded)} secret(s).")
        return 0

    values = collect_secrets_interactively(existing)
    env_path = write_env_file(values)
    ui.ok(f"Wrote {env_path.relative_to(REPO_ROOT)}")

    if not skip_config:
        update_config_from_prompts()

    if skip_github:
        ui.info("Skipped GitHub secrets (--local-only).")
    else:
        if _prompt_yes_no(
            "Upload secrets to GitHub Actions on this fork now? (requires `gh`)",
            default=True,
        ):
            _print_header("GitHub Actions secrets")
            ui.info(f"Target repo: {ui.url(_repo_slug())}")
            uploaded = push_github_secrets(values)
            if not uploaded:
                ui.warn("No CI secrets to upload (all empty).")
            else:
                ui.ok(f"Uploaded {len(uploaded)} secret(s).")
        else:
            ui.info("Skipped GitHub secrets. Re-run with:")
            ui.bullet("python -m x_mrr_banner setup --github-only")

    _print_header("Next steps")
    ui.step("Activate the venv if needed:  source .venv/bin/activate")
    ui.step("Generate the banner template:  python -m x_mrr_banner generate_template")
    ui.step("Commit & push:  git add assets/template/ config.yaml && git commit && git push")
    ui.step("GitHub → Actions → Update X banner → Run workflow")
    return 0
