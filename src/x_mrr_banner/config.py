from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv

Period = Literal["daily", "weekly", "monthly"]

VALID_PERIODS: tuple[Period, ...] = ("daily", "weekly", "monthly")

BANNER_WIDTH = 1500
BANNER_HEIGHT = 500
BANNER_ASPECT_RATIO = "3:1"


def _find_repo_root() -> Path:
    """Prefer a directory that contains config.yaml (cwd walk, then package walk)."""
    candidates: list[Path] = []
    cwd = Path.cwd().resolve()
    candidates.extend([cwd, *cwd.parents])
    here = Path(__file__).resolve().parent
    candidates.extend([here, *here.parents])
    for path in candidates:
        if (path / "config.yaml").is_file():
            return path
    # Editable / source tree: src/x_mrr_banner/config.py → repo root is parents[2]
    source_root = Path(__file__).resolve().parents[2]
    if (source_root / "pyproject.toml").is_file():
        return source_root
    return cwd


REPO_ROOT = _find_repo_root()
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.yaml"
BANNER_DIR = REPO_ROOT / "assets" / "banner"
BANNER_TEMPLATE_PATH = REPO_ROOT / "inputs" / "BANNER.md.j2"
OUTPUT_DIR = REPO_ROOT / "output"
# Legacy flat path; prefer output_paths_for_month() → output/YYYYMM/
BANNER_OUTPUT_PATH = OUTPUT_DIR / "banner.png"


def output_month_key(period_start: date) -> str:
    """Folder name for the target reporting month (YYYYMM)."""
    return period_start.strftime("%Y%m")


def output_dir_for_month(period_start: date) -> Path:
    """Return output/YYYYMM for the target reporting month."""
    return OUTPUT_DIR / output_month_key(period_start)


def output_paths_for_month(period_start: date) -> tuple[Path, Path]:
    """Return (banner.png, BANNER.md) under output/YYYYMM/."""
    folder = output_dir_for_month(period_start)
    return folder / "banner.png", folder / "BANNER.md"


@dataclass
class ChallengeConfig:
    headline: str = ""
    start_date: date | None = None
    deadline: date | None = None
    total_periods: int = 12
    start_mrr: float = 0.0
    target_mrr: float = 0.0


@dataclass
class ContentConfig:
    headline: str = "$10k MRR Target"
    apps_label: str = ""


@dataclass
class ThemeConfig:
    mood: str = "confident and clean"
    style: str = "minimal geometric with soft gradients"
    color_mode: str = "dark"
    background_color: str = "#0B0D10"
    primary_color: str = "#4F8CFF"
    accent_color: str = "#7CFFB2"
    text_color: str = "#FFFFFF"
    chart_color: str = "#7CFFB2"


@dataclass
class AppEntry:
    name: str
    apple_skus: list[str] = field(default_factory=list)
    # IAP / auto-renewable subscription Product IDs as they appear in ASC sales SKU column.
    apple_iap_skus: list[str] = field(default_factory=list)
    google_package_names: list[str] = field(default_factory=list)

    def revenue_apple_skus(self) -> list[str]:
        """App SKU + IAP/subscription SKUs used to filter ASC sales rows."""
        skus: list[str] = []
        seen: set[str] = set()
        for sku in [*self.apple_skus, *self.apple_iap_skus]:
            if sku and sku not in seen:
                seen.add(sku)
                skus.append(sku)
        return skus


@dataclass
class AppConfig:
    upload_to_x: bool = False
    currency: str = "USD"
    challenge: ChallengeConfig = field(default_factory=ChallengeConfig)
    content: ContentConfig = field(default_factory=ContentConfig)
    theme: ThemeConfig = field(default_factory=ThemeConfig)
    apps: list[AppEntry] = field(default_factory=list)

    def all_apple_skus(self) -> list[str]:
        skus: list[str] = []
        seen: set[str] = set()
        for app in self.apps:
            for sku in app.revenue_apple_skus():
                if sku not in seen:
                    seen.add(sku)
                    skus.append(sku)
        return skus

    def all_google_package_names(self) -> list[str]:
        packages: list[str] = []
        seen: set[str] = set()
        for app in self.apps:
            for package in app.google_package_names:
                if package and package not in seen:
                    seen.add(package)
                    packages.append(package)
        return packages


@dataclass
class AppRevenue:
    name: str
    apple_revenue: float
    google_revenue: float
    total_revenue: float


@dataclass
class SeriesPoint:
    label: str
    date: date
    apple_revenue: float
    google_revenue: float
    total_revenue: float
    apps: list[AppRevenue] = field(default_factory=list)


@dataclass
class RevenueSnapshot:
    period: Period
    period_start: date
    period_end: date
    currency: str
    apple_revenue: float
    google_revenue: float
    total_revenue: float
    series: list[SeriesPoint] = field(default_factory=list)
    apps: list[AppRevenue] = field(default_factory=list)

    def as_metric_map(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "period_label": period_label(self),
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "currency": self.currency,
            "apple_revenue": self.apple_revenue,
            "google_revenue": self.google_revenue,
            "total_revenue": self.total_revenue,
            "series": self.series,
            "apps": self.apps,
        }


def period_label(snapshot: RevenueSnapshot) -> str:
    if snapshot.period == "daily":
        return snapshot.period_end.strftime("%b %d, %Y")
    if snapshot.period == "weekly":
        return (
            f"Week of {snapshot.period_start.strftime('%b %d')} – "
            f"{snapshot.period_end.strftime('%b %d, %Y')}"
        )
    return snapshot.period_start.strftime("%B %Y")


def format_currency(amount: float, currency: str) -> str:
    symbol = {
        "USD": "$",
        "EUR": "€",
        "GBP": "£",
        "JPY": "¥",
    }.get(currency.upper(), f"{currency} ")
    if abs(amount) >= 1000:
        return f"{symbol}{amount:,.0f}"
    if abs(amount - round(amount)) < 1e-9:
        return f"{symbol}{amount:,.0f}"
    return f"{symbol}{amount:,.2f}"


def months_between(start: date, end: date) -> int:
    """Whole calendar months from start to end (0 if end <= start month)."""
    return (end.year - start.year) * 12 + (end.month - start.month)


def challenge_current_period(challenge: ChallengeConfig, as_of: date | None = None) -> int:
    today = as_of or datetime.now(timezone.utc).date()
    if challenge.start_date is None:
        return 1
    elapsed = months_between(challenge.start_date, today) + 1
    total = max(1, challenge.total_periods)
    return max(1, min(elapsed, total))


def target_progress_percent(
    current_mrr: float,
    start_mrr: float,
    target_mrr: float,
) -> float:
    span = target_mrr - start_mrr
    if abs(span) < 1e-9:
        return 100.0 if current_mrr >= target_mrr else 0.0
    raw = (current_mrr - start_mrr) / span * 100.0
    return max(0.0, min(100.0, raw))


def _parse_iso_date(value: Any) -> date | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return date.fromisoformat(text)


def _parse_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _load_challenge(raw: dict[str, Any]) -> ChallengeConfig:
    data = raw.get("challenge") or {}
    if not isinstance(data, dict):
        data = {}
    return ChallengeConfig(
        headline=str(data.get("headline") or ""),
        start_date=_parse_iso_date(data.get("start_date")),
        deadline=_parse_iso_date(data.get("deadline")),
        total_periods=max(1, int(data.get("total_periods") or 12)),
        start_mrr=_parse_float(data.get("start_mrr")),
        target_mrr=_parse_float(data.get("target_mrr")),
    )


def _load_content(raw: dict[str, Any]) -> ContentConfig:
    data = raw.get("content") or {}
    if not isinstance(data, dict):
        data = {}
    defaults = ContentConfig()
    return ContentConfig(
        headline=str(data.get("headline") or defaults.headline),
        apps_label=str(data.get("apps_label") or ""),
    )


def _load_theme(raw: dict[str, Any]) -> ThemeConfig:
    data = raw.get("theme") or {}
    if not isinstance(data, dict):
        data = {}
    defaults = ThemeConfig()
    return ThemeConfig(
        mood=str(data.get("mood") or defaults.mood),
        style=str(data.get("style") or defaults.style),
        color_mode=str(data.get("color_mode") or defaults.color_mode),
        background_color=str(data.get("background_color") or defaults.background_color),
        primary_color=str(data.get("primary_color") or defaults.primary_color),
        accent_color=str(data.get("accent_color") or defaults.accent_color),
        text_color=str(data.get("text_color") or defaults.text_color),
        chart_color=str(data.get("chart_color") or defaults.chart_color),
    )


def _load_apps(raw: dict[str, Any]) -> list[AppEntry]:
    items = raw.get("apps") or []
    if not isinstance(items, list):
        return []
    apps: list[AppEntry] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        apps.append(
            AppEntry(
                name=name,
                apple_skus=[str(s).strip() for s in (item.get("apple_skus") or []) if str(s).strip()],
                apple_iap_skus=[
                    str(s).strip() for s in (item.get("apple_iap_skus") or []) if str(s).strip()
                ],
                google_package_names=[
                    str(s).strip()
                    for s in (item.get("google_package_names") or [])
                    if str(s).strip()
                ],
            )
        )
    # Legacy top-level filters → single synthetic app if apps missing
    if not apps:
        legacy_skus = [str(s).strip() for s in (raw.get("apple_skus") or []) if str(s).strip()]
        legacy_packages = [
            str(s).strip() for s in (raw.get("google_package_names") or []) if str(s).strip()
        ]
        if legacy_skus or legacy_packages:
            apps.append(
                AppEntry(
                    name="Portfolio",
                    apple_skus=legacy_skus,
                    google_package_names=legacy_packages,
                )
            )
    return apps


def load_dotenv_files() -> None:
    load_dotenv(REPO_ROOT / ".env")


def load_config(path: Path | None = None) -> AppConfig:
    config_path = path or DEFAULT_CONFIG_PATH
    raw: dict[str, Any] = {}
    if config_path.exists():
        with config_path.open(encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
            if not isinstance(loaded, dict):
                raise ValueError(f"Config at {config_path} must be a mapping")
            raw = loaded

    return AppConfig(
        upload_to_x=bool(raw.get("upload_to_x", False)),
        currency=str(raw.get("currency") or "USD"),
        challenge=_load_challenge(raw),
        content=_load_content(raw),
        theme=_load_theme(raw),
        apps=_load_apps(raw),
    )


def require_banner_config(config: AppConfig) -> None:
    missing: list[str] = []
    if not config.challenge.headline.strip():
        missing.append("challenge.headline")
    if config.challenge.start_date is None:
        missing.append("challenge.start_date")
    if config.challenge.deadline is None:
        missing.append("challenge.deadline")
    if config.challenge.target_mrr <= 0:
        missing.append("challenge.target_mrr")
    if not config.theme.mood.strip():
        missing.append("theme.mood")
    if missing:
        raise ValueError(
            "Banner preferences incomplete in config.yaml: "
            + ", ".join(missing)
            + ". Re-run: python -m x_mrr_banner setup"
        )


def parse_period(value: str) -> Period:
    normalized = value.strip().lower()
    if normalized not in VALID_PERIODS:
        raise ValueError(f"Invalid period {value!r}; expected one of {VALID_PERIODS}")
    return normalized  # type: ignore[return-value]
