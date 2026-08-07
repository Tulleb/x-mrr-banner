from __future__ import annotations

import base64
import logging
import os
from pathlib import Path

from requests_oauthlib import OAuth1Session

logger = logging.getLogger(__name__)

# Official X API v1.1 — no v2 equivalent for banner updates.
# https://developer.x.com/ (search: update_profile_banner)
UPDATE_BANNER_URL = "https://api.x.com/1.1/account/update_profile_banner.json"


class XUploadError(RuntimeError):
    pass


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise XUploadError(f"Missing required environment variable: {name}")
    return value


def upload_banner(image_path: Path) -> None:
    """Upload a profile banner via POST account/update_profile_banner (OAuth 1.0a user context)."""
    if not image_path.exists():
        raise XUploadError(f"Banner file not found: {image_path}")

    api_key = _require_env("X_API_KEY")
    api_secret = _require_env("X_API_SECRET")
    access_token = _require_env("X_ACCESS_TOKEN")
    access_token_secret = _require_env("X_ACCESS_TOKEN_SECRET")

    session = OAuth1Session(
        api_key,
        client_secret=api_secret,
        resource_owner_key=access_token,
        resource_owner_secret=access_token_secret,
    )

    payload = base64.b64encode(image_path.read_bytes()).decode("ascii")
    # Form body (not query string) — large base64 banners break URL length limits.
    response = session.post(
        UPDATE_BANNER_URL,
        data={"banner": payload},
        timeout=120,
    )
    # X processes banners asynchronously; 200/201/202 are success.
    if response.status_code in (200, 201, 202):
        logger.info("Updated X profile banner from %s", image_path)
        return

    hint = ""
    if response.status_code == 400:
        hint = " (missing or invalid image data)"
    elif response.status_code == 422:
        hint = " (image too large or could not be processed — use ~1500×500 PNG/JPG)"
    elif response.status_code in (401, 403):
        hint = " (check OAuth 1.0a user tokens and app Read+Write permissions / API credits)"

    raise XUploadError(
        f"X banner upload failed ({response.status_code}){hint}: {response.text[:800]}"
    )
