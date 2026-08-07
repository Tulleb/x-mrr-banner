from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import yaml
from google import genai

from x_mrr_banner.config import (
    BACKGROUND_PATH,
    LAYOUT_PATH,
    TEMPLATE_DIR,
    TEMPLATE_DOC_PATH,
)

logger = logging.getLogger(__name__)

# Nano Banana 2 Lite — cheapest current Gemini image model
NANO_BANANA_MODEL = "gemini-3.1-flash-lite-image"

DEFAULT_PROMPT = """
Create a clean X (Twitter) profile banner background image, exactly 1500x500 pixels.
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


def _save_image_from_response(response: object, destination: Path) -> None:
    # google-genai responses expose generated images via candidates/parts.
    candidates = getattr(response, "candidates", None) or []
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        parts = getattr(content, "parts", None) or []
        for part in parts:
            inline = getattr(part, "inline_data", None)
            if inline is not None and getattr(inline, "data", None):
                destination.write_bytes(inline.data)
                return
            as_image = getattr(part, "as_image", None)
            if callable(as_image):
                image = as_image()
                if image is not None:
                    image.save(destination)
                    return
            if hasattr(part, "save"):
                part.save(destination)
                return

    # Newer convenience helpers
    for attr in ("output_image", "image"):
        image = getattr(response, attr, None)
        if image is None:
            continue
        if hasattr(image, "save"):
            image.save(destination)
            return
        data = getattr(image, "data", None) or getattr(image, "image_bytes", None)
        if data:
            destination.write_bytes(data)
            return

    raise RuntimeError(
        "Gemini returned no image data. Check GEMINI_API_KEY and model availability "
        f"for {NANO_BANANA_MODEL}."
    )


def generate_template(
    *,
    doc_path: Path = TEMPLATE_DOC_PATH,
    background_path: Path = BACKGROUND_PATH,
    layout_path: Path = LAYOUT_PATH,
    overwrite: bool = False,
) -> tuple[Path, Path]:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is required for generate_template")

    TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
    if not overwrite and background_path.exists() and layout_path.exists():
        raise FileExistsError(
            f"Template already exists at {background_path} and {layout_path}. "
            "Pass --overwrite to regenerate."
        )

    prompt = _extract_prompt_from_doc(doc_path)
    logger.info("Generating banner template with %s", NANO_BANANA_MODEL)

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=NANO_BANANA_MODEL,
        contents=prompt,
    )
    _save_image_from_response(response, background_path)

    if overwrite or not layout_path.exists():
        with layout_path.open("w", encoding="utf-8") as handle:
            yaml.safe_dump(DEFAULT_LAYOUT, handle, sort_keys=False)

    logger.info("Wrote %s and %s", background_path, layout_path)
    return background_path, layout_path
