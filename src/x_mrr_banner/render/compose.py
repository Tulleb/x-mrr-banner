from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import yaml
from PIL import Image, ImageDraw, ImageFont

from x_mrr_banner.config import (
    BACKGROUND_PATH,
    BANNER_OUTPUT_PATH,
    LAYOUT_PATH,
    RevenueSnapshot,
    require_template_assets,
)

BANNER_SIZE = (1500, 500)


def _load_layout(path: Path = LAYOUT_PATH) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"layout.yaml must be a mapping: {path}")
    regions = data.get("regions") or data
    if not isinstance(regions, dict):
        raise ValueError("layout.yaml regions must be a mapping")
    return regions


def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
        if bold
        else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            try:
                return ImageFont.truetype(candidate, size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def format_currency(amount: float, currency: str) -> str:
    symbol = {"USD": "$", "EUR": "€", "GBP": "£"}.get(currency.upper(), f"{currency} ")
    if abs(amount) >= 1_000_000:
        return f"{symbol}{amount / 1_000_000:.2f}M"
    if abs(amount) >= 10_000:
        return f"{symbol}{amount / 1_000:.1f}K"
    if abs(amount) >= 1_000:
        return f"{symbol}{amount:,.0f}"
    return f"{symbol}{amount:,.2f}"


def _resolve_text(region_id: str, region: dict[str, Any], snapshot: RevenueSnapshot) -> str:
    if "text" in region:
        return str(region["text"])
    metrics = snapshot.as_metric_map()
    metric_key = region.get("metric") or region_id
    value = metrics.get(metric_key)
    if value is None:
        return ""
    if isinstance(value, float):
        return format_currency(value, snapshot.currency)
    return str(value)


def _parse_color(value: Any, default: tuple[int, int, int, int] = (255, 255, 255, 255)) -> tuple[int, ...]:
    if value is None:
        return default
    if isinstance(value, str) and value.startswith("#"):
        hex_value = value[1:]
        if len(hex_value) == 6:
            r, g, b = (int(hex_value[i : i + 2], 16) for i in (0, 2, 4))
            return (r, g, b, 255)
        if len(hex_value) == 8:
            r, g, b, a = (int(hex_value[i : i + 2], 16) for i in (0, 2, 4, 6))
            return (r, g, b, a)
    if isinstance(value, (list, tuple)) and len(value) in (3, 4):
        return tuple(int(v) for v in value)
    return default


def _draw_chart(snapshot: RevenueSnapshot, width: int, height: int, color: str = "#FFFFFF") -> Image.Image:
    fig, ax = plt.subplots(figsize=(max(width, 10) / 100, max(height, 10) / 100), dpi=100)
    fig.patch.set_alpha(0.0)
    ax.set_facecolor("none")

    labels = [point.label for point in snapshot.series]
    totals = [point.total_revenue for point in snapshot.series]
    if not totals:
        labels, totals = ["—"], [0.0]

    ax.fill_between(range(len(totals)), totals, color=color, alpha=0.25)
    ax.plot(range(len(totals)), totals, color=color, linewidth=2.5, marker="o", markersize=3)
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, fontsize=7, color=color)
    ax.tick_params(axis="y", labelsize=7, colors=color)
    for spine in ax.spines.values():
        spine.set_color(color)
        spine.set_alpha(0.4)
    ax.grid(True, axis="y", alpha=0.2, color=color)
    fig.tight_layout(pad=0.2)

    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", transparent=True)
    plt.close(fig)
    buffer.seek(0)
    chart = Image.open(buffer).convert("RGBA")
    return chart.resize((width, height), Image.Resampling.LANCZOS)


def compose_banner(
    snapshot: RevenueSnapshot,
    *,
    background_path: Path = BACKGROUND_PATH,
    layout_path: Path = LAYOUT_PATH,
    output_path: Path = BANNER_OUTPUT_PATH,
) -> Path:
    require_template_assets()
    regions = _load_layout(layout_path)

    image = Image.open(background_path).convert("RGBA")
    if image.size != BANNER_SIZE:
        image = image.resize(BANNER_SIZE, Image.Resampling.LANCZOS)

    draw = ImageDraw.Draw(image)

    for region_id, region in regions.items():
        if not isinstance(region, dict):
            continue
        region_type = str(region.get("type") or "text").lower()
        box = region.get("box") or region.get("rect")
        if not box or len(box) != 4:
            continue
        x, y, w, h = (int(v) for v in box)

        if region_type == "chart":
            color = str(region.get("color") or "#FFFFFF")
            chart = _draw_chart(snapshot, w, h, color=color)
            image.alpha_composite(chart, dest=(x, y))
            draw = ImageDraw.Draw(image)
            continue

        text = _resolve_text(region_id, region, snapshot)
        if region.get("prefix"):
            text = f"{region['prefix']}{text}"
        if region.get("suffix"):
            text = f"{text}{region['suffix']}"

        font_size = int(region.get("font_size") or (48 if region_id == "total_revenue" else 28))
        bold = bool(region.get("bold", region_id == "total_revenue"))
        font = _font(font_size, bold=bold)
        color = _parse_color(region.get("color"), (255, 255, 255, 255))
        anchor = str(region.get("anchor") or "lt")
        draw.text((x, y), text, font=font, fill=color, anchor=anchor)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    rgb = Image.new("RGB", image.size, (0, 0, 0))
    rgb.paste(image, mask=image.split()[3])
    rgb.save(output_path, format="PNG", optimize=True)
    return output_path
