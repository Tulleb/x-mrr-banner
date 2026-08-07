from __future__ import annotations

import base64
import logging
import os
import re
from io import BytesIO
from pathlib import Path

import yaml
from openai import OpenAI
from PIL import Image

from x_mrr_banner.config import (
    BACKGROUND_PATH,
    LAYOUT_PATH,
    TEMPLATE_DIR,
    TEMPLATE_DOC_PATH,
)

logger = logging.getLogger(__name__)

# OpenAI image model — landscape then cropped/resized to X banner size.
OPENAI_IMAGE_MODEL = "gpt-image-1"
OPENAI_IMAGE_SIZE = "1536x1024"
BANNER_WIDTH = 1500
BANNER_HEIGHT = 500

DEFAULT_PROMPT = """
Create a clean X (Twitter) profile banner background image.
Final display size is 1500x500 pixels (wide 3:1 banner). Compose for that aspect ratio.
Leave clear empty regions for later overlays:
- large space on the left for a big revenue number
- three smaller label areas for period, Apple revenue, and Google revenue
- a wide horizontal area on the right for a line chart
Do NOT render any real numbers, currency amounts, dates, or chart lines.
Use a modern, high-contrast design suitable for white text overlays.
""".strip()

DEFAULT_LAYOUT = {
    "regions": {
        "period_label": {
            "type": "text",
            "metric": "period_label",
            "box": [48, 40, 500, 40],
            "font_size": 24,
            "bold": False,
            "color": "#FFFFFF",
            "anchor": "lt",
        },
        "total_revenue": {
            "type": "text",
            "metric": "total_revenue",
            "box": [48, 100, 600, 120],
            "font_size": 72,
            "bold": True,
            "color": "#FFFFFF",
            "anchor": "lt",
        },
        "apple_revenue": {
            "type": "text",
            "metric": "apple_revenue",
            "prefix": "Apple  ",
            "box": [48, 250, 400, 40],
            "font_size": 28,
            "bold": False,
            "color": "#FFFFFF",
            "anchor": "lt",
        },
        "google_revenue": {
            "type": "text",
            "metric": "google_revenue",
            "prefix": "Google  ",
            "box": [48, 300, 400, 40],
            "font_size": 28,
            "bold": False,
            "color": "#FFFFFF",
            "anchor": "lt",
        },
        "revenue_chart": {
            "type": "chart",
            "box": [700, 80, 720, 340],
            "color": "#FFFFFF",
        },
    }
}


def _extract_prompt_from_doc(doc_path: Path) -> str:
    if not doc_path.exists():
        return DEFAULT_PROMPT
    text = doc_path.read_text(encoding="utf-8")
    # Prefer content under "## Prompt notes" until the next ## heading.
    match = re.search(
        r"##\s+Prompt notes.*?\n(.*?)(?=\n##\s+|\Z)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    notes = (match.group(1).strip() if match else "").strip()
    notes = re.sub(r"^[-*]\s+", "", notes, flags=re.MULTILINE)
    if not notes or notes.lower().startswith("visual style"):
        # Keep stub bullets as soft guidance even if terse.
        pass
    parts = [DEFAULT_PROMPT]
    if notes:
        parts.append("Additional guidance from docs/TEMPLATE.md:\n" + notes)
    return "\n\n".join(parts)


def _fit_to_banner(image: Image.Image, width: int = BANNER_WIDTH, height: int = BANNER_HEIGHT) -> Image.Image:
    """Center-crop to banner aspect ratio, then resize to exact X banner pixels."""
    target_ratio = width / height
    src = image.convert("RGB")
    w, h = src.size
    current = w / h
    if current > target_ratio:
        new_w = max(1, int(round(h * target_ratio)))
        left = (w - new_w) // 2
        src = src.crop((left, 0, left + new_w, h))
    elif current < target_ratio:
        new_h = max(1, int(round(w / target_ratio)))
        top = (h - new_h) // 2
        src = src.crop((0, top, w, top + new_h))
    return src.resize((width, height), Image.Resampling.LANCZOS)


def _save_openai_image(response: object, destination: Path) -> None:
    data = getattr(response, "data", None) or []
    if not data:
        raise RuntimeError(
            "OpenAI returned no image data. Check OPENAI_API_KEY and model availability "
            f"for {OPENAI_IMAGE_MODEL}."
        )
    item = data[0]
    b64 = getattr(item, "b64_json", None)
    if not b64:
        url = getattr(item, "url", None)
        raise RuntimeError(
            "OpenAI image response missing b64_json"
            + (f" (got url={url!r})" if url else "")
            + ". Request b64_json output."
        )
    raw = base64.b64decode(b64)
    with Image.open(BytesIO(raw)) as image:
        fitted = _fit_to_banner(image)
        fitted.save(destination, format="PNG")


def generate_template(
    *,
    doc_path: Path = TEMPLATE_DOC_PATH,
    background_path: Path = BACKGROUND_PATH,
    layout_path: Path = LAYOUT_PATH,
    overwrite: bool = False,
) -> tuple[Path, Path]:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required for generate_template")

    TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
    if not overwrite and background_path.exists() and layout_path.exists():
        raise FileExistsError(
            f"Template already exists at {background_path} and {layout_path}. "
            "Pass --overwrite to regenerate."
        )

    prompt = _extract_prompt_from_doc(doc_path)
    logger.info(
        "Generating banner template with %s (%s → %sx%s)",
        OPENAI_IMAGE_MODEL,
        OPENAI_IMAGE_SIZE,
        BANNER_WIDTH,
        BANNER_HEIGHT,
    )

    client = OpenAI(api_key=api_key)
    response = client.images.generate(
        model=OPENAI_IMAGE_MODEL,
        prompt=prompt,
        size=OPENAI_IMAGE_SIZE,
        quality="low",
        output_format="png",
    )
    _save_openai_image(response, background_path)

    if overwrite or not layout_path.exists():
        with layout_path.open("w", encoding="utf-8") as handle:
            yaml.safe_dump(DEFAULT_LAYOUT, handle, sort_keys=False)

    logger.info("Wrote %s and %s", background_path, layout_path)
    return background_path, layout_path
