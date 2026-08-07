from __future__ import annotations

import logging
import re
from collections import deque
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

from x_mrr_banner.config import (
    AppEntry,
    OUTPUT_DIR,
    REPO_ROOT,
)

logger = logging.getLogger(__name__)

LOGO_CACHE_DIR = OUTPUT_DIR / "logos"

# Pure-red chroma key GPT paints for app icon slots (replaced after generation).
ICON_MARKER_RGB = (255, 0, 0)
ICON_MARKER_HEX = "#FF0000"
# Generative models rarely hit exact #FF0000 — tolerate nearby reds.
_RED_R_MIN, _RED_G_MAX, _RED_B_MAX = 200, 60, 60
_MIN_APP_MARKER_AREA = 200
_MAX_LOGO_SLOTS = 6
_DEFAULT_ICON_SIZE = 48
_ICON_CORNER_RATIO = 0.22

ITUNES_LOOKUP_URL = "https://itunes.apple.com/lookup"
_REQUEST_TIMEOUT = 30

# Kept for template/context fallbacks and tests.
LOGO_SLOT_SIZE = _DEFAULT_ICON_SIZE
LOGO_SLOT_RADIUS = 10
MAX_LOGO_SLOTS = _MAX_LOGO_SLOTS


@dataclass(frozen=True)
class LogoSlot:
    """Detected (or fallback) icon placement on the final banner canvas."""

    index: int
    app_name: str
    x: int
    y: int
    size: int
    radius: int


@dataclass(frozen=True)
class _RedBlob:
    x: int
    y: int
    width: int
    height: int
    pixels: tuple[tuple[int, int], ...]

    @property
    def area(self) -> int:
        return len(self.pixels)

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.width // 2, self.y + self.height // 2


def compute_logo_slots(apps: list[AppEntry]) -> list[LogoSlot]:
    """Fallback fixed slots when red-marker detection finds nothing."""
    origin_x, origin_y, column = 72, 168, 150
    slots: list[LogoSlot] = []
    for index, app in enumerate(apps[:_MAX_LOGO_SLOTS]):
        slots.append(
            LogoSlot(
                index=index,
                app_name=app.name,
                x=origin_x + index * column,
                y=origin_y,
                size=_DEFAULT_ICON_SIZE,
                radius=max(4, int(_DEFAULT_ICON_SIZE * _ICON_CORNER_RATIO)),
            )
        )
    return slots


def _slug(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "_", name.strip()).strip("_").lower()
    return cleaned or "app"


def _download_bytes(url: str) -> bytes:
    response = requests.get(url, timeout=_REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.content


def _itunes_artwork_url(bundle_id: str) -> str | None:
    response = requests.get(
        ITUNES_LOOKUP_URL,
        params={"bundleId": bundle_id},
        timeout=_REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    results = payload.get("results") or []
    if not results:
        return None
    row = results[0]
    return (
        str(row.get("artworkUrl512") or "").strip()
        or str(row.get("artworkUrl100") or "").strip()
        or None
    )


def _load_image_bytes(data: bytes) -> Image.Image:
    with Image.open(BytesIO(data)) as image:
        return image.convert("RGBA")


def _load_local_logo(path: Path) -> Image.Image | None:
    if not path.is_file():
        return None
    try:
        with Image.open(path) as image:
            return image.convert("RGBA")
    except OSError as exc:
        logger.warning("Could not open logo %s: %s", path, exc)
        return None


def _resolve_logo_source(app: AppEntry) -> Image.Image | None:
    """Resolve logo_path → logo_url → iTunes (first apple_sku)."""
    if app.logo_path:
        local = Path(app.logo_path)
        if not local.is_absolute():
            local = REPO_ROOT / local
        image = _load_local_logo(local)
        if image is not None:
            logger.info("Logo for %s: logo_path=%s", app.name, local)
            return image
        logger.warning("logo_path missing for %s: %s", app.name, local)

    if app.logo_url:
        try:
            image = _load_image_bytes(_download_bytes(app.logo_url))
            logger.info("Logo for %s: logo_url", app.name)
            return image
        except Exception as exc:  # noqa: BLE001
            logger.warning("logo_url failed for %s: %s", app.name, exc)

    for sku in app.apple_skus:
        try:
            artwork = _itunes_artwork_url(sku)
            if not artwork:
                logger.info("iTunes lookup empty for bundleId=%s (%s)", sku, app.name)
                continue
            image = _load_image_bytes(_download_bytes(artwork))
            logger.info("Logo for %s: iTunes bundleId=%s", app.name, sku)
            return image
        except Exception as exc:  # noqa: BLE001
            logger.warning("iTunes logo fetch failed for %s (%s): %s", app.name, sku, exc)

    return None


def _monogram_color(name: str) -> tuple[int, int, int, int]:
    digest = sum(ord(ch) for ch in name) or 1
    palette = (
        (79, 140, 255, 255),
        (124, 255, 178, 255),
        (255, 159, 67, 255),
        (186, 140, 255, 255),
        (255, 107, 129, 255),
    )
    return palette[digest % len(palette)]


def _load_font(size: int, *, bold: bool = False) -> ImageFont.ImageFont:
    candidates = [
        "/System/Library/Fonts/SFNS.ttf",
        "/System/Library/Fonts/SFNSText.ttf",
        "/Library/Fonts/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        ),
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def make_monogram(name: str, size: int = LOGO_SLOT_SIZE) -> Image.Image:
    """Fallback rounded tile with the app's initial."""
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    fill = _monogram_color(name)
    radius = max(4, int(size * _ICON_CORNER_RATIO))
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=fill)
    initial = (name.strip()[:1] or "?").upper()
    font = _load_font(max(10, size // 2), bold=True)
    bbox = draw.textbbox((0, 0), initial, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(
        ((size - tw) / 2 - bbox[0], (size - th) / 2 - bbox[1]),
        initial,
        fill=(11, 13, 16, 255),
        font=font,
    )
    return image


def rounded_icon(source: Image.Image, size: int, radius: int | None = None) -> Image.Image:
    """Resize and apply a rounded-rect alpha mask."""
    corner = radius if radius is not None else max(4, int(size * _ICON_CORNER_RATIO))
    icon = source.convert("RGBA").resize((size, size), Image.Resampling.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=corner, fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(icon, (0, 0), mask=mask)
    return out


def fetch_app_icon(app: AppEntry, *, cache_dir: Path | None = None) -> Image.Image:
    """Fetch (or load cached) app icon; fall back to a monogram tile."""
    cache = cache_dir or LOGO_CACHE_DIR
    cache.mkdir(parents=True, exist_ok=True)
    cached = cache / f"{_slug(app.name)}.png"

    if cached.is_file():
        image = _load_local_logo(cached)
        if image is not None:
            logger.info("Logo for %s: cache %s", app.name, cached)
            return image

    source = _resolve_logo_source(app)
    if source is None:
        logger.warning("No logo for %s — using monogram fallback", app.name)
        mono = make_monogram(app.name, size=_DEFAULT_ICON_SIZE * 4)
        try:
            mono.save(cached, format="PNG")
        except OSError:
            pass
        return mono

    try:
        source.convert("RGBA").resize(
            (_DEFAULT_ICON_SIZE * 4, _DEFAULT_ICON_SIZE * 4), Image.Resampling.LANCZOS
        ).save(cached, format="PNG")
    except OSError as exc:
        logger.warning("Could not cache logo for %s: %s", app.name, exc)
    return source


def fetch_app_icons(apps: list[AppEntry], *, cache_dir: Path | None = None) -> list[Image.Image]:
    """Fetch icons for each configured app (same order as apps)."""
    return [fetch_app_icon(app, cache_dir=cache_dir) for app in apps[:_MAX_LOGO_SLOTS]]


def _parse_hex_color(value: str, fallback: tuple[int, int, int] = (11, 13, 16)) -> tuple[int, int, int]:
    text = (value or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        return fallback
    try:
        return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    except ValueError:
        return fallback


def _is_marker_red(r: int, g: int, b: int) -> bool:
    return r >= _RED_R_MIN and g <= _RED_G_MAX and b <= _RED_B_MAX


def detect_color_blobs(
    image: Image.Image,
    matches,
    *,
    min_area: int,
) -> list[_RedBlob]:
    """Find connected regions matching a color predicate, left → right."""
    rgb = image.convert("RGB")
    width, height = rgb.size
    pixels = rgb.load()
    visited = [[False] * width for _ in range(height)]
    blobs: list[_RedBlob] = []

    for y in range(height):
        for x in range(width):
            if visited[y][x]:
                continue
            r, g, b = pixels[x, y]
            if not matches(r, g, b):
                visited[y][x] = True
                continue

            queue: deque[tuple[int, int]] = deque([(x, y)])
            visited[y][x] = True
            members: list[tuple[int, int]] = []
            min_x = max_x = x
            min_y = max_y = y
            while queue:
                cx, cy = queue.popleft()
                members.append((cx, cy))
                min_x = min(min_x, cx)
                max_x = max(max_x, cx)
                min_y = min(min_y, cy)
                max_y = max(max_y, cy)
                for nx, ny in (
                    (cx - 1, cy),
                    (cx + 1, cy),
                    (cx, cy - 1),
                    (cx, cy + 1),
                ):
                    if nx < 0 or ny < 0 or nx >= width or ny >= height:
                        continue
                    if visited[ny][nx]:
                        continue
                    nr, ng, nb = pixels[nx, ny]
                    if matches(nr, ng, nb):
                        visited[ny][nx] = True
                        queue.append((nx, ny))
                    else:
                        visited[ny][nx] = True

            if len(members) < min_area:
                continue
            blobs.append(
                _RedBlob(
                    x=min_x,
                    y=min_y,
                    width=max_x - min_x + 1,
                    height=max_y - min_y + 1,
                    pixels=tuple(members),
                )
            )

    blobs.sort(key=lambda blob: (blob.x, blob.y))
    return blobs


def detect_red_marker_blobs(image: Image.Image) -> list[_RedBlob]:
    return detect_color_blobs(image, _is_marker_red, min_area=_MIN_APP_MARKER_AREA)


def _erase_blobs(canvas: Image.Image, blobs: list[_RedBlob], bg: tuple[int, int, int]) -> None:
    px = canvas.load()
    for blob in blobs:
        for x, y in blob.pixels:
            px[x, y] = (*bg, 255)


def slots_from_red_markers(
    image: Image.Image,
    app_names: list[str],
) -> tuple[list[LogoSlot], list[_RedBlob]]:
    """Map left→right red markers to apps; return slots + blobs used."""
    names = app_names[:_MAX_LOGO_SLOTS]
    if not names:
        return [], []

    blobs = detect_red_marker_blobs(image)
    if not blobs:
        logger.warning("No red icon markers (#FF0000) detected — using fallback slots")
        return compute_logo_slots(
            [AppEntry(name=name) for name in names]
        ), []

    if len(blobs) < len(names):
        logger.warning(
            "Found %d red marker(s) but need %d — pairing left-to-right, then fallback",
            len(blobs),
            len(names),
        )
    elif len(blobs) > len(names):
        logger.warning(
            "Found %d red marker(s) for %d app(s) — using the leftmost %d",
            len(blobs),
            len(names),
            len(names),
        )

    slots: list[LogoSlot] = []
    used: list[_RedBlob] = []
    for index, name in enumerate(names):
        if index < len(blobs):
            blob = blobs[index]
            size = max(24, int(round((blob.width + blob.height) / 2)))
            cx, cy = blob.center
            slots.append(
                LogoSlot(
                    index=index,
                    app_name=name,
                    x=cx - size // 2,
                    y=cy - size // 2,
                    size=size,
                    radius=max(4, int(size * _ICON_CORNER_RATIO)),
                )
            )
            used.append(blob)
        else:
            fallback = compute_logo_slots([AppEntry(name=n) for n in names])[index]
            slots.append(fallback)

    return slots, used


def overlay_app_icons(
    banner: Image.Image,
    icons: list[Image.Image],
    app_names: list[str],
    *,
    background_color: str = "#0B0D10",
    text_color: str = "#FFFFFF",
) -> Image.Image:
    """Replace pure-red app markers with real app icons (store badges are drawn by the model)."""
    del text_color  # names / amounts / store icons are drawn by the model
    canvas = banner.convert("RGBA")
    bg = _parse_hex_color(background_color)
    slots, red_blobs = slots_from_red_markers(canvas, app_names)
    _erase_blobs(canvas, red_blobs, bg)

    for icon, slot in zip(icons, slots, strict=False):
        prepared = rounded_icon(icon, slot.size, slot.radius)
        x = max(0, min(slot.x, canvas.size[0] - slot.size))
        y = max(0, min(slot.y, canvas.size[1] - slot.size))
        canvas.paste(prepared, (x, y), mask=prepared)
        logger.info(
            "Overlayed app icon for %s at (%s,%s) size=%s (marker-detect=%s)",
            slot.app_name,
            x,
            y,
            slot.size,
            bool(red_blobs),
        )

    return canvas.convert("RGB")
