from __future__ import annotations

import base64
import logging
import os
from io import BytesIO
from pathlib import Path

from openai import OpenAI
from PIL import Image

from x_mrr_banner.config import (
    BANNER_HEIGHT,
    BANNER_OUTPUT_PATH,
    BANNER_WIDTH,
)

logger = logging.getLogger(__name__)

OPENAI_IMAGE_MODEL = os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-2").strip() or "gpt-image-2"
# gpt-image-2 allows custom WxH (multiples of 16, ratio within 1:3–3:1).
# Prefer a true 3:1 canvas so we do not center-crop content away from 1536x1024.
_DEFAULT_OPENAI_SIZE = "1536x512"
OPENAI_IMAGE_SIZE = (
    os.environ.get("OPENAI_IMAGE_SIZE", _DEFAULT_OPENAI_SIZE).strip() or _DEFAULT_OPENAI_SIZE
)


def _fit_to_banner(image: Image.Image, width: int = BANNER_WIDTH, height: int = BANNER_HEIGHT) -> Image.Image:
    """Scale to exact X banner size.

    When the source is already ~3:1, only resize (no crop). Otherwise center-crop
    to 3:1 first — legacy path for non-banner OpenAI sizes.
    """
    target_ratio = width / height
    src = image.convert("RGB")
    w, h = src.size
    current = w / h
    if abs(current - target_ratio) > 0.02:
        if current > target_ratio:
            new_w = max(1, int(round(h * target_ratio)))
            left = (w - new_w) // 2
            src = src.crop((left, 0, left + new_w, h))
        else:
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
            + "."
        )
    raw = base64.b64decode(b64)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(BytesIO(raw)) as image:
        logger.info("OpenAI image raw size: %sx%s", image.size[0], image.size[1])
        fitted = _fit_to_banner(image)
        fitted.save(destination, format="PNG")
        logger.info("Saved banner size: %sx%s", fitted.size[0], fitted.size[1])


def generate_banner(
    prompt_markdown: str,
    *,
    destination: Path | None = None,
) -> Path:
    """Generate the final X banner image from a fully rendered prompt."""
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required to generate the banner")

    output_path = destination or BANNER_OUTPUT_PATH
    output_path.parent.mkdir(parents=True, exist_ok=True)

    logger.info(
        "Generating banner with OpenAI %s (%s → %sx%s)",
        OPENAI_IMAGE_MODEL,
        OPENAI_IMAGE_SIZE,
        BANNER_WIDTH,
        BANNER_HEIGHT,
    )
    client = OpenAI(api_key=api_key)
    response = client.images.generate(
        model=OPENAI_IMAGE_MODEL,
        prompt=prompt_markdown,
        size=OPENAI_IMAGE_SIZE,
        quality="low",
        output_format="png",
    )
    _save_openai_image(response, output_path)
    logger.info("Banner written to %s", output_path)
    return output_path
