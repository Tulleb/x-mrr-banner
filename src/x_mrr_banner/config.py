from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv

Period = Literal["daily", "weekly", "monthly"]

VALID_PERIODS: tuple[Period, ...] = ("daily", "weekly", "monthly")


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
TEMPLATE_DIR = REPO_ROOT / "assets" / "template"
BACKGROUND_PATH = TEMPLATE_DIR / "background.png"
LAYOUT_PATH = TEMPLATE_DIR / "layout.yaml"
TEMPLATE_DOC_PATH = REPO_ROOT / "docs" / "TEMPLATE.md"
OUTPUT_DIR = REPO_ROOT / "output"
BANNER_OUTPUT_PATH = OUTPUT_DIR / "banner.png"


@dataclass
class AppConfig:
    upload_to_x: bool = False
    currency: str = "USD"
    apple_skus: list[str] = field(default_factory=list)
    google_package_names: list[str] = field(default_factory=list)


@dataclass
class SeriesPoint:
    label: str
    date: date
    apple_revenue: float
    google_revenue: float
    total_revenue: float


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

    def as_metric_map(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "period_label": _period_label(self),
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "currency": self.currency,
            "apple_revenue": self.apple_revenue,
            "google_revenue": self.google_revenue,
            "total_revenue": self.total_revenue,
            "series": self.series,
        }


def _period_label(snapshot: RevenueSnapshot) -> str:
    if snapshot.period == "daily":
        return snapshot.period_end.strftime("%b %d, %Y")
    if snapshot.period == "weekly":
        return f"Week of {snapshot.period_start.strftime('%b %d')} – {snapshot.period_end.strftime('%b %d, %Y')}"
    return snapshot.period_start.strftime("%B %Y")


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
        apple_skus=[str(item) for item in (raw.get("apple_skus") or [])],
        google_package_names=[str(item) for item in (raw.get("google_package_names") or [])],
    )


def require_template_assets() -> None:
    missing = [path for path in (BACKGROUND_PATH, LAYOUT_PATH) if not path.exists()]
    if not missing:
        return
    names = ", ".join(str(path.relative_to(REPO_ROOT)) for path in missing)
    raise FileNotFoundError(
        f"Template assets missing: {names}. "
        "Run locally: python -m x_mrr_banner generate_template "
        "(requires GEMINI_API_KEY), then commit assets/template/."
    )


def parse_period(value: str) -> Period:
    normalized = value.strip().lower()
    if normalized not in VALID_PERIODS:
        raise ValueError(f"Invalid period {value!r}; expected one of {VALID_PERIODS}")
    return normalized  # type: ignore[return-value]
