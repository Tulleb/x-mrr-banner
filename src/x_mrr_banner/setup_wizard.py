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

from x_mrr_banner.banner.watermark import (
    WATERMARK_AUTHOR_X,
    WATERMARK_BTC,
    WATERMARK_REPO_BIO,
    WATERMARK_TEXT,
)
from x_mrr_banner.config import (
    DEFAULT_CONFIG_PATH,
    REPO_ROOT,
    VALID_WATERMARK_POSITIONS,
    default_content_headline,
    load_config,
    require_banner_config,
)
from x_mrr_banner.prerequisites import (
    ensure_gh_authenticated,
    prepare_environment,
    print_gh_install_help,
    try_install_gh,
)
from x_mrr_banner import ui

logger = logging.getLogger(__name__)

ENV_PATH = REPO_ROOT / ".env"

# Secrets that belong in GitHub Actions (including OpenAI for full-banner generation).
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
    "OPENAI_API_KEY",
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


def _print_header(title: str, *, step: tuple[int, int] | None = None) -> None:
    if step is not None:
        current, total = step
        title = f"{current}/{total}  {title}"
    ui.header(title)


class _WizardProgress:
    """Tracks setup wizard step indices (e.g. 3/9)."""

    def __init__(self, total: int) -> None:
        self.total = max(1, total)
        self.current = 0

    def header(self, title: str) -> None:
        self.current += 1
        _print_header(title, step=(self.current, self.total))


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
OPENAI_API_KEYS_URL = "https://platform.openai.com/api-keys"

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



def _sections(*, include_x: bool, include_openai: bool) -> list[Section]:
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
                name="X",
                optional=True,
                configure_prompt="Configure X banner upload credentials now?",
                intro=(
                    "Create an X developer app, set Read and write, then copy OAuth 1.0a keys.\n"
                    "\n"
                    f"Open: {X_DEVELOPER_PORTAL_URL}\n"
                    "\n"
                    "Step 1 — Create the app\n"
                    "  Path: Apps → Create App → Create your app\n"
                    "  Warning: After creating, it may not show in the main Apps list —\n"
                    "  look under the hidden \"Standalone Apps\" dropdown.\n"
                    "  Warning: The X console allows creating at most 3 apps per day.\n"
                    "\n"
                    "Step 2 — Environment\n"
                    "  When asked for Environment, choose:\n"
                    "    • Production  ← required (updates your real profile banner)\n"
                    "    • Do not use Development or Staging for the live GitHub Action\n"
                    "\n"
                    "Step 3 — App Settings → User authentication settings (do this FIRST)\n"
                    "  Start here before generating access tokens:\n"
                    "    1. App permissions: Read and write  ← required for banner upload\n"
                    "       (Read-only tokens return HTTP 403 on upload)\n"
                    "    2. Type of App: Native App is fine\n"
                    "    3. Callback URI / Redirect URL: https://127.0.0.1\n"
                    "    4. Website URL: https://127.0.0.1\n"
                    "       Tip: the portal requires both fields to Save — use\n"
                    "       https://127.0.0.1 in each. This project never opens a\n"
                    "       browser login; localhost placeholders are enough.\n"
                    "    5. Click Save\n"
                    "  Ignore any OAuth 2.0 Client Secret the portal shows afterward —\n"
                    "  this project uses OAuth 1.0a only.\n"
                    "\n"
                    "Step 4 — Keys and tokens (OAuth 1.0a)\n"
                    "  Open Keys and tokens, then:\n"
                    "    • Copy Consumer Keys → API Key + API Key Secret\n"
                    "    • Generate (or regenerate) Authentication Tokens →\n"
                    "      Access Token + Access Token Secret\n"
                    "  Important: regenerate Access Token + Secret AFTER saving\n"
                    "  Read and write. Tokens created while Read-only stay read-only.\n"
                    "\n"
                    "Banner upload uses v1.1 account/update_profile_banner (no v2 equivalent).\n"
                    "API access is pay-per-use / credit-based for most new apps."
                ),
                fields=[
                    FieldSpec(
                        key="X_API_KEY",
                        title="API Key (Consumer Key) — OAuth 1.0a",
                        help_text=(
                            "Keys and tokens → Consumer Keys → API Key.\n"
                            "Not the OAuth 2.0 Client ID."
                        ),
                        open_url=X_DEVELOPER_PORTAL_URL,
                    ),
                    FieldSpec(
                        key="X_API_SECRET",
                        title="API Secret (Consumer Secret) — OAuth 1.0a",
                        help_text=(
                            "Keys and tokens → Consumer Keys → API Key Secret.\n"
                            "Not the OAuth 2.0 Client Secret."
                        ),
                        open_url=X_DEVELOPER_PORTAL_URL,
                    ),
                    FieldSpec(
                        key="X_ACCESS_TOKEN",
                        title="Access Token — OAuth 1.0a",
                        help_text=(
                            "Keys and tokens → Authentication Tokens → Access Token.\n"
                            "Must be created AFTER App Settings → Read and write.\n"
                            "If you already had tokens, regenerate them now."
                        ),
                        open_url=X_DEVELOPER_PORTAL_URL,
                    ),
                    FieldSpec(
                        key="X_ACCESS_TOKEN_SECRET",
                        title="Access Token Secret — OAuth 1.0a",
                        help_text=(
                            "Same Authentication Tokens section → Access Token Secret.\n"
                            "Pair with the regenerated Access Token from Read and write."
                        ),
                        open_url=X_DEVELOPER_PORTAL_URL,
                    ),
                ],
            )
        )
    if include_openai:
        sections.append(
            Section(
                name="OpenAI",
                optional=False,
                configure_prompt="Configure OpenAI API key for banner generation?",
                intro=(
                    "OpenAI generates the final X profile banner from the rendered\n"
                    "inputs/BANNER.md.j2 prompt (live revenues + your preferences).\n"
                    "Synced to GitHub Actions so the monthly workflow can regenerate the banner."
                ),
                fields=[
                    FieldSpec(
                        key="OPENAI_API_KEY",
                        title="OpenAI API key",
                        help_text="platform.openai.com → API keys → Create new secret key.",
                        open_url=OPENAI_API_KEYS_URL,
                        docs_url="https://platform.openai.com/docs/guides/image-generation",
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
        ("X", ["X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"]),
        ("OpenAI", ["OPENAI_API_KEY"]),
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


def _prompt_text(question: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(ui.prompt(f"{question}{suffix}: ")).strip()
    return value or default


def _prompt_choice(question: str, choices: tuple[str, ...], default: str) -> str:
    ui.info(f"  Options: {', '.join(choices)}")
    while True:
        value = _prompt_text(question, default).strip().lower().replace(" ", "_")
        if value in choices:
            return value
        ui.warn(f"Choose one of: {', '.join(choices)}")


def _prompt_float(question: str, default: float) -> float:
    while True:
        raw = _prompt_text(question, str(default))
        try:
            return float(raw)
        except ValueError:
            ui.warn("Enter a number.")


def _prompt_int(question: str, default: int) -> int:
    while True:
        raw = _prompt_text(question, str(default))
        try:
            return max(1, int(raw))
        except ValueError:
            ui.warn("Enter a whole number.")


def _prompt_iso_date(question: str, default: str) -> str:
    while True:
        raw = _prompt_text(question, default)
        try:
            from datetime import date as date_cls

            date_cls.fromisoformat(raw)
            return raw
        except ValueError:
            ui.warn("Use ISO date YYYY-MM-DD.")


def _yaml_quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _write_config_yaml(
    *,
    upload_to_x: bool,
    currency: str,
    challenge: dict,
    content: dict,
    theme: dict,
    watermark: dict,
    apps: list[dict],
) -> None:
    lines = [
        "# When false, update still generates the banner (and CI uploads an",
        "# artifact) but skip calling the X API. Set true once X credentials are ready.",
        f"upload_to_x: {'true' if upload_to_x else 'false'}",
        "",
        "# Display / aggregation currency label (reports may still be multi-currency).",
        f"currency: {currency}",
        "",
        "# Challenge / progress preferences used by inputs/BANNER.md.j2",
        "challenge:",
        f"  headline: {_yaml_quote(challenge['headline'])}",
        f"  start_date: {_yaml_quote(challenge['start_date'])}",
        f"  deadline: {_yaml_quote(challenge['deadline'])}",
        f"  total_periods: {challenge['total_periods']}",
        f"  start_mrr: {challenge['start_mrr']}",
        f"  target_mrr: {challenge['target_mrr']}",
        "",
        "# Banner copy (period_label / revenue_label are filled from live data at update time)",
        "# Leave headline empty to use: \"{N} MONTHS TO $10K MRR\"",
        "content:",
        f"  top_label: {_yaml_quote(content['top_label'])}",
        f"  headline: {_yaml_quote(content['headline'])}",
        f"  subheadline: {_yaml_quote(content['subheadline'])}",
        f"  apps_label: {_yaml_quote(content['apps_label'])}",
        "",
        "# Visual direction for the OpenAI banner prompt",
        "theme:",
        f"  mood: {_yaml_quote(theme['mood'])}",
        f"  style: {_yaml_quote(theme['style'])}",
        f"  color_mode: {theme['color_mode']}",
        f"  background_color: {_yaml_quote(theme['background_color'])}",
        f"  primary_color: {_yaml_quote(theme['primary_color'])}",
        f"  accent_color: {_yaml_quote(theme['accent_color'])}",
        f"  text_color: {_yaml_quote(theme['text_color'])}",
        f"  chart_color: {_yaml_quote(theme['chart_color'])}",
        "",
        "# Attribution watermark (Pillow overlay after generation)",
        "# position: top_right (default) | bottom_center",
        "watermark:",
        f"  enabled: {'true' if watermark.get('enabled', True) else 'false'}",
        f"  position: {watermark.get('position') or 'top_right'}",
        "",
        "# Apps shown on the banner (empty = portfolio totals only, no per-app breakdown)",
        "apps:",
    ]
    if not apps:
        lines.append("  []")
    else:
        for app in apps:
            lines.append(f"  - name: {_yaml_quote(app['name'])}")
            skus = app.get("apple_skus") or []
            iap_skus = app.get("apple_iap_skus") or []
            packages = app.get("google_package_names") or []
            if skus:
                lines.append("    apple_skus:")
                for sku in skus:
                    lines.append(f"      - {_yaml_quote(sku)}")
            else:
                lines.append("    apple_skus: []")
            if iap_skus:
                lines.append("    # IAP / subscription Product IDs (SKU column in ASC sales reports)")
                lines.append("    apple_iap_skus:")
                for sku in iap_skus:
                    lines.append(f"      - {_yaml_quote(sku)}")
            else:
                lines.append("    apple_iap_skus: []")
            if packages:
                lines.append("    google_package_names:")
                for package in packages:
                    lines.append(f"      - {_yaml_quote(package)}")
            else:
                lines.append("    google_package_names: []")
            logo_path = str(app.get("logo_path") or "").strip()
            logo_url = str(app.get("logo_url") or "").strip()
            if logo_path:
                lines.append(f"    logo_path: {_yaml_quote(logo_path)}")
            if logo_url:
                lines.append(f"    logo_url: {_yaml_quote(logo_url)}")
    lines.append("")
    DEFAULT_CONFIG_PATH.write_text("\n".join(lines), encoding="utf-8")


_DEFAULT_CHALLENGE = {
    "headline": "$10k MRR Target",
    "start_date": "2026-01-01",
    "deadline": "2026-12-31",
    "total_periods": 12,
    "start_mrr": 0.0,
    "target_mrr": 10000.0,
}

_DEFAULT_CONTENT = {
    "top_label": "BUILDING IN PUBLIC",
    "headline": "",
    "subheadline": "Sharing the real numbers, wins & failures",
    "apps_label": "Apps in progress",
}

_DEFAULT_THEME = {
    "mood": "confident and clean",
    "style": "minimal geometric with soft gradients",
    "color_mode": "dark",
    "background_color": "#0B0D10",
    "primary_color": "#4F8CFF",
    "accent_color": "#7CFFB2",
    "text_color": "#FFFFFF",
    "chart_color": "#7CFFB2",
}

_DEFAULT_WATERMARK = {
    "enabled": True,
    "position": "top_right",
}


def _seed_config_state(raw: dict) -> dict:
    apps = _existing_apps(raw)
    currency = str(raw.get("currency") or "USD").strip() or "USD"
    return {
        "upload_to_x": bool(raw.get("upload_to_x", False)),
        "currency": currency,
        "challenge": _existing_challenge(raw) or dict(_DEFAULT_CHALLENGE),
        "content": _existing_content(raw) or dict(_DEFAULT_CONTENT),
        "theme": _existing_theme(raw) or dict(_DEFAULT_THEME),
        "watermark": _existing_watermark(raw) or dict(_DEFAULT_WATERMARK),
        "apps": list(apps if apps is not None else []),
    }


def _persist_config_state(state: dict, *, reason: str) -> None:
    """Rewrite config.yaml after each wizard step so Ctrl+C keeps progress."""
    _write_config_yaml(
        upload_to_x=bool(state["upload_to_x"]),
        currency=str(state["currency"]),
        challenge=state["challenge"],
        content=state["content"],
        theme=state["theme"],
        watermark=state.get("watermark") or dict(_DEFAULT_WATERMARK),
        apps=state["apps"],
    )
    ui.ok(f"Saved {DEFAULT_CONFIG_PATH.relative_to(REPO_ROOT)} ({reason})")


def _persist_upload_to_x(upload: bool) -> None:
    """Update only upload_to_x in config.yaml (used from the X credentials step)."""
    state = _seed_config_state(_load_config_raw())
    state["upload_to_x"] = upload
    _persist_config_state(state, reason="upload_to_x")


def _prompt_upload_to_x(*, default: bool) -> bool:
    return _prompt_yes_no(
        "Upload generated banners to X automatically? (sets upload_to_x in config.yaml)",
        default=default,
    )


def _existing_challenge(raw: dict) -> dict | None:
    existing = raw.get("challenge") if isinstance(raw.get("challenge"), dict) else {}
    if not (str(existing.get("headline") or "").strip() and existing.get("start_date")):
        return None
    return {
        "headline": str(existing.get("headline") or ""),
        "start_date": str(existing.get("start_date") or ""),
        "deadline": str(existing.get("deadline") or ""),
        "total_periods": int(existing.get("total_periods") or 12),
        "start_mrr": float(existing.get("start_mrr") or 0),
        "target_mrr": float(existing.get("target_mrr") or 0),
    }


def _existing_content(raw: dict) -> dict | None:
    existing = raw.get("content") if isinstance(raw.get("content"), dict) else {}
    top_label = str(existing.get("top_label") or "").strip()
    if not top_label and not str(existing.get("subheadline") or "").strip():
        # Treat legacy headline-only configs as present when apps_label/headline set.
        if not (
            str(existing.get("headline") or "").strip()
            or str(existing.get("apps_label") or "").strip()
        ):
            return None
    return {
        "top_label": top_label or str(_DEFAULT_CONTENT["top_label"]),
        "headline": str(existing.get("headline") or ""),
        "subheadline": str(existing.get("subheadline") or _DEFAULT_CONTENT["subheadline"]),
        "apps_label": str(existing.get("apps_label") or ""),
    }


def _existing_theme(raw: dict) -> dict | None:
    existing = raw.get("theme") if isinstance(raw.get("theme"), dict) else {}
    if not (str(existing.get("mood") or "").strip() and existing.get("primary_color")):
        return None
    return {
        "mood": str(existing.get("mood") or "confident and clean"),
        "style": str(existing.get("style") or "minimal geometric with soft gradients"),
        "color_mode": str(existing.get("color_mode") or "dark"),
        "background_color": str(existing.get("background_color") or "#0B0D10"),
        "primary_color": str(existing.get("primary_color") or "#4F8CFF"),
        "accent_color": str(existing.get("accent_color") or "#7CFFB2"),
        "text_color": str(existing.get("text_color") or "#FFFFFF"),
        "chart_color": str(existing.get("chart_color") or "#7CFFB2"),
    }


def _existing_watermark(raw: dict) -> dict | None:
    if "watermark" not in raw:
        return None
    existing = raw.get("watermark") if isinstance(raw.get("watermark"), dict) else {}
    position = str(existing.get("position") or "top_right").strip().lower()
    if position not in VALID_WATERMARK_POSITIONS:
        position = "top_right"
    enabled = existing.get("enabled")
    if enabled is None:
        enabled = True
    return {"enabled": bool(enabled), "position": position}


def _existing_apps(raw: dict) -> list[dict] | None:
    """Return apps list when the key was written before (including empty [])."""
    if "apps" not in raw:
        return None
    if not isinstance(raw.get("apps"), list):
        return None
    return [
        {
            "name": str(a.get("name") or ""),
            "apple_skus": [str(s) for s in (a.get("apple_skus") or [])],
            "apple_iap_skus": [str(s) for s in (a.get("apple_iap_skus") or [])],
            "google_package_names": [str(s) for s in (a.get("google_package_names") or [])],
            "logo_path": str(a.get("logo_path") or ""),
            "logo_url": str(a.get("logo_url") or ""),
        }
        for a in raw["apps"]
        if isinstance(a, dict) and a.get("name")
    ]


def _collect_challenge(
    raw: dict, *, progress: _WizardProgress | None = None
) -> tuple[dict, str]:
    (progress.header if progress else _print_header)("Challenge preferences")
    existing = _existing_challenge(raw)
    currency_existing = str(raw.get("currency") or "USD").strip() or "USD"
    if existing is not None:
        ui.ok(
            f"Current values: headline={existing['headline']!r}, "
            f"{existing['start_date']} → {existing['deadline']}, "
            f"target_mrr={existing['target_mrr']}, currency={currency_existing}"
        )
        if not _prompt_yes_no("Change current values?", default=False):
            _advance_after_step("Keeping Challenge preferences — nice work!")
            return existing, currency_existing
        ui.info("Updating Challenge preferences.")

    defaults = existing or {}
    challenge = {
        "headline": _prompt_text(
            "Challenge headline",
            str(defaults.get("headline") or "$10k MRR Target"),
        ),
        "start_date": _prompt_iso_date(
            "Start date (YYYY-MM-DD)",
            str(defaults.get("start_date") or "2026-01-01"),
        ),
        "deadline": _prompt_iso_date(
            "Deadline (YYYY-MM-DD)",
            str(defaults.get("deadline") or "2026-12-31"),
        ),
        "total_periods": _prompt_int(
            "Total periods (months)",
            int(defaults.get("total_periods") or 12),
        ),
        "start_mrr": _prompt_float(
            "Starting MRR",
            float(defaults.get("start_mrr") or 0),
        ),
        "target_mrr": _prompt_float(
            "Target MRR",
            float(defaults.get("target_mrr") or 10000),
        ),
    }
    currency = _prompt_text("Display currency", currency_existing)
    _advance_after_step("Challenge preferences saved — great job!")
    return challenge, currency


def _collect_content(
    raw: dict, challenge: dict, *, progress: _WizardProgress | None = None
) -> dict:
    (progress.header if progress else _print_header)("Banner content")
    existing = _existing_content(raw)
    if existing is not None:
        headline_display = existing["headline"] or "(auto: N MONTHS TO $NK MRR)"
        ui.ok(
            f"Current values: top_label={existing['top_label']!r}, "
            f"headline={headline_display!r}, "
            f"apps_label={existing['apps_label']!r}"
        )
        if not _prompt_yes_no("Change current values?", default=False):
            _advance_after_step("Keeping Banner content — nice work!")
            return existing
        ui.info("Updating Banner content.")

    defaults = existing or {}
    default_headline = str(defaults.get("headline") or "")
    auto_hint = default_content_headline(
        total_periods=int(challenge.get("total_periods") or 12),
        target_mrr=float(challenge.get("target_mrr") or 10000),
        currency="USD",
    )
    content = {
        "top_label": _prompt_text(
            "Top label",
            str(defaults.get("top_label") or _DEFAULT_CONTENT["top_label"]),
        ),
        "headline": _prompt_text(
            f"Main headline (blank = auto “{auto_hint}”)",
            default_headline,
        ),
        "subheadline": _prompt_text(
            "Subheadline",
            str(defaults.get("subheadline") or _DEFAULT_CONTENT["subheadline"]),
        ),
        "apps_label": _prompt_text(
            "Apps line",
            str(defaults.get("apps_label") or "Apps in progress"),
        ),
    }
    _advance_after_step("Content preferences saved — great job!")
    return content


def _collect_theme(raw: dict, *, progress: _WizardProgress | None = None) -> dict:
    (progress.header if progress else _print_header)("Theme preferences")
    existing = _existing_theme(raw)
    if existing is not None:
        ui.ok(
            f"Current values: mood={existing['mood']!r}, "
            f"color_mode={existing['color_mode']}, "
            f"primary={existing['primary_color']}"
        )
        if not _prompt_yes_no("Change current values?", default=False):
            _advance_after_step("Keeping Theme preferences — nice work!")
            return existing
        ui.info("Updating Theme preferences.")

    defaults = existing or {}
    color_mode = _prompt_choice(
        "Color mode",
        ("dark", "light"),
        str(defaults.get("color_mode") or "dark"),
    )
    default_bg = "#0B0D10" if color_mode == "dark" else "#F5F7FA"
    default_text = "#FFFFFF" if color_mode == "dark" else "#111111"
    accent = _prompt_text("Accent color (hex)", str(defaults.get("accent_color") or "#7CFFB2"))
    theme = {
        "mood": _prompt_text(
            "Mood (e.g. confident, calm, playful)",
            str(defaults.get("mood") or "confident and clean"),
        ),
        "style": _prompt_text(
            "Visual style",
            str(defaults.get("style") or "minimal geometric with soft gradients"),
        ),
        "color_mode": color_mode,
        "background_color": _prompt_text(
            "Background color (hex)",
            str(defaults.get("background_color") or default_bg),
        ),
        "primary_color": _prompt_text(
            "Primary brand color (hex)",
            str(defaults.get("primary_color") or "#4F8CFF"),
        ),
        "accent_color": accent,
        "text_color": _prompt_text(
            "Text color (hex)",
            str(defaults.get("text_color") or default_text),
        ),
        "chart_color": _prompt_text(
            "Chart color (hex)",
            str(defaults.get("chart_color") or accent),
        ),
    }
    _advance_after_step("Theme preferences saved — great job!")
    return theme


def _show_watermark_removal_disclaimer() -> None:
    """Honor-system note after the user opts out of the attribution watermark."""
    s = ui._s()
    paint = ui._paint
    print()
    print(
        f"  {paint('✨💖', s.bold)} "
        f"{paint('Watermark off!', s.bold, s.magenta)} "
        f"{paint('If you can spare a small gesture, it would mean the world 🙏', s.bold, s.yellow)} "
        f"{paint('🚀', s.bold)}"
    )
    print(
        f"  {paint('🐦', s.bold)} "
        f"{paint('Follow on X:', s.bold, s.cyan)} "
        f"{paint(WATERMARK_AUTHOR_X, s.bold, s.blue)} "
        f"{paint('← desperately need the visibility!', s.dim)}"
    )
    print(
        f"  {paint('📣', s.bold)} "
        f"{paint('Add to your X bio:', s.bold, s.green)} "
        f"{paint(f'\"{WATERMARK_REPO_BIO}\"', s.bold, s.white)}"
    )
    print(
        f"  {paint('₿', s.bold, s.yellow)} "
        f"{paint('Tip Bitcoin:', s.bold, s.yellow)} "
        f"{paint(WATERMARK_BTC, s.bold, s.magenta)} "
        f"{paint('🧡', s.bold)}"
    )
    print(
        f"  {paint('🙏💛✨', s.bold)} "
        f"{paint('Thank you — you are awesome!', s.bold, s.green)} "
        f"{paint('🎉', s.bold)}"
    )
    print()


def _collect_watermark(raw: dict, *, progress: _WizardProgress | None = None) -> dict:
    (progress.header if progress else _print_header)("Attribution watermark")
    existing = _existing_watermark(raw)
    if existing is not None:
        status = "on" if existing["enabled"] else "off"
        ui.ok(f"Current values: enabled={status}, position={existing['position']}")
        ui.info(f"Text: {WATERMARK_TEXT}")
        if not _prompt_yes_no("Change current values?", default=False):
            _advance_after_step("Keeping watermark preferences — nice work!")
            return existing
        ui.info("Updating watermark preferences.")

    defaults = existing or dict(_DEFAULT_WATERMARK)
    enabled = _prompt_yes_no(
        f"Show attribution watermark on the banner? (\"{WATERMARK_TEXT}\")",
        default=True,
    )
    if not enabled:
        _show_watermark_removal_disclaimer()

    position = str(defaults.get("position") or "top_right")
    if enabled:
        position = _prompt_choice(
            "Watermark position",
            VALID_WATERMARK_POSITIONS,
            position if position in VALID_WATERMARK_POSITIONS else "top_right",
        )
    watermark = {"enabled": enabled, "position": position}
    _advance_after_step("Watermark preferences saved — great job!")
    return watermark


def _parse_csv_list(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def _collect_apps(raw: dict, *, progress: _WizardProgress | None = None) -> list[dict]:
    (progress.header if progress else _print_header)("Apps")
    existing_configured = _existing_apps(raw)
    existing_apps = existing_configured if existing_configured is not None else []

    if existing_configured is not None:
        if existing_apps:
            names = ", ".join(a["name"] for a in existing_apps)
            ui.ok(f"Current values: {len(existing_apps)} app(s) — {names}")
        else:
            ui.ok("Current values: no per-app breakdown (portfolio totals only)")
        if not _prompt_yes_no("Change current values?", default=False):
            _advance_after_step("Keeping Apps — nice work!")
            return existing_apps
        ui.info("Updating Apps.")

    ui.info("Add apps for per-app MRR on the banner. Leave count at 0 for portfolio totals only.")
    print()
    default_count = str(len(existing_apps)) if existing_apps else "0"
    count = 0
    while True:
        raw_count = _prompt_text("How many apps to configure", default_count)
        try:
            count = max(0, int(raw_count))
            break
        except ValueError:
            ui.warn("Enter a whole number.")

    apps: list[dict] = []
    for index in range(count):
        print()
        ui.info(f"App {index + 1} of {count}")
        default = existing_apps[index] if index < len(existing_apps) else {}
        name = _prompt_text("App display name", str(default.get("name") or f"App {index + 1}"))
        sku_default = ", ".join(str(s) for s in (default.get("apple_skus") or []))
        iap_default = ", ".join(str(s) for s in (default.get("apple_iap_skus") or []))
        pkg_default = ", ".join(str(s) for s in (default.get("google_package_names") or []))

        print()
        _print_intro(
            "Apple app SKU — identifies the app itself:\n"
            "  • App Store Connect → My Apps → your app → App Information → SKU\n"
            "  • Often matches the Bundle ID (e.g. com.example.myapp)"
        )
        apple_skus = _parse_csv_list(
            _prompt_text("Apple app SKU(s), optional", sku_default)
        )

        print()
        _print_intro(
            "Apple IAP / subscription SKUs — required for paid proceeds:\n"
            "  • ASC sales reports usually put the IAP Product ID in the SKU column,\n"
            "    not the app SKU — so subscriptions alone under the app SKU often show $0\n"
            "  • Find them in App Store Connect → your app → Monetization →\n"
            "    In-App Purchases / Subscriptions → Product ID\n"
            "  • Enter every Product ID whose revenue should count for this app\n"
            "    (comma-separated). Leave blank if the app has no IAPs/subscriptions"
        )
        apple_iap_skus = _parse_csv_list(
            _prompt_text("Apple IAP / subscription Product ID(s), optional", iap_default)
        )

        print()
        _print_intro(
            "Google package names — filter Play bulk sales for this app:\n"
            "  • Play Console → your app → Dashboard (application ID),\n"
            "    e.g. com.example.myapp\n"
            "  • Leave blank to skip Play filtering for this app"
        )
        packages = _parse_csv_list(
            _prompt_text("Google package name(s), optional", pkg_default)
        )

        print()
        _print_intro(
            "Logo overrides (optional) — otherwise icons are fetched from the App Store\n"
            "via the first Apple app SKU (iTunes lookup). Use a local path or URL to override."
        )
        logo_path = _prompt_text(
            "Local logo path, optional",
            str(default.get("logo_path") or ""),
        ).strip()
        logo_url = _prompt_text(
            "Logo URL, optional",
            str(default.get("logo_url") or ""),
        ).strip()

        apps.append(
            {
                "name": name,
                "apple_skus": apple_skus,
                "apple_iap_skus": apple_iap_skus,
                "google_package_names": packages,
                "logo_path": logo_path,
                "logo_url": logo_url,
            }
        )
    _advance_after_step("Apps saved — great job!")
    return apps


def update_config_from_prompts(*, progress: _WizardProgress | None = None) -> None:
    raw = _load_config_raw()
    state = _seed_config_state(raw)

    challenge, currency = _collect_challenge(raw, progress=progress)
    state["challenge"] = challenge
    state["currency"] = currency
    _persist_config_state(state, reason="challenge preferences")

    content = _collect_content(raw, challenge, progress=progress)
    state["content"] = content
    _persist_config_state(state, reason="banner content")

    theme = _collect_theme(raw, progress=progress)
    state["theme"] = theme
    _persist_config_state(state, reason="theme preferences")

    watermark = _collect_watermark(raw, progress=progress)
    state["watermark"] = watermark
    _persist_config_state(state, reason="watermark preferences")

    apps = _collect_apps(raw, progress=progress)
    state["apps"] = apps
    _persist_config_state(state, reason="apps")

    _advance_after_step("All banner preferences saved — great job!")


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


def collect_secrets_interactively(
    existing: dict[str, str] | None = None,
    *,
    progress: _WizardProgress | None = None,
) -> dict[str, str]:
    existing = existing or {}
    _print_header("x-mrr-banner setup")
    ui.info("This wizard writes a local .env and can sync the same values to GitHub")
    ui.info("Actions secrets on your fork (via `gh secret set`).")
    ui.warn("Never commit .env.")
    if existing:
        ui.ok(f"Loaded {len([v for v in existing.values() if v])} existing value(s) from .env")

    values = dict(existing)
    sections = _sections(include_x=True, include_openai=True)
    try:
        for section in sections:
            if progress is not None:
                progress.header(section.name)
            else:
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

            if section.name.startswith("X"):
                upload = _prompt_upload_to_x(
                    default=bool(_load_config_raw().get("upload_to_x", True)),
                )
                _persist_upload_to_x(upload)
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
        ui.warn("Setup interrupted — re-run start to continue from where you left off.")
        raise SystemExit(130) from None

    return values


def mandatory_env_complete(values: dict[str, str] | None = None) -> bool:
    """True when every non-optional credential section is filled in .env."""
    existing = values if values is not None else _parse_env_file(ENV_PATH)
    if not existing:
        return False
    for section in _sections(include_x=True, include_openai=True):
        if section.optional:
            continue
        if not _section_is_complete(section, existing):
            return False
    return True


def banner_config_ready() -> bool:
    """True when config.yaml has the prefs required to generate a banner."""
    try:
        require_banner_config(load_config())
    except (OSError, ValueError, TypeError):
        return False
    return True


def can_skip_setup_to_banner() -> bool:
    """True when .env mandatories + banner prefs are already in place."""
    return mandatory_env_complete() and banner_config_ready()


def offer_skip_setup_to_banner() -> bool:
    """
    If setup can be skipped, ask the user. Return True to go straight to banner gen.
    """
    if not can_skip_setup_to_banner():
        return False
    _print_header("Already configured")
    ui.ok(f"Mandatory credentials found in {ENV_PATH.name}")
    ui.ok("Banner preferences look ready in config.yaml")
    return _prompt_yes_no(
        "Skip setup and generate the banner now?",
        default=True,
    )


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

    sections = _sections(include_x=True, include_openai=True)
    config_steps = 0 if skip_config else 5
    progress = _WizardProgress(len(sections) + config_steps)

    values = collect_secrets_interactively(existing, progress=progress)
    env_path = write_env_file(values)
    ui.ok(f"Wrote {env_path.relative_to(REPO_ROOT)}")

    if not skip_config:
        update_config_from_prompts(progress=progress)

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
    ui.step("Generate a banner:  python -m x_mrr_banner update --dry-run  → output/YYYYMM/")
    ui.step("Commit & push:  git add config.yaml && git commit && git push")
    ui.step("GitHub → Actions → Update X banner → Run workflow")
    return 0
