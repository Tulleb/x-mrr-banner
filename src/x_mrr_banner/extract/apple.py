from __future__ import annotations

import csv
import gzip
import io
import os
import time
from datetime import date

import jwt
import requests

from x_mrr_banner.config import Period
from x_mrr_banner.dates import apple_frequency, apple_report_date


class AppleStoreError(RuntimeError):
    pass


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise AppleStoreError(f"Missing required environment variable: {name}")
    return value


def _normalize_private_key(raw: str) -> str:
    key = raw.strip()
    if "\\n" in key and "-----BEGIN" in key:
        key = key.replace("\\n", "\n")
    return key


def create_asc_token(issuer_id: str, key_id: str, private_key: str, lifetime_seconds: int = 1200) -> str:
    now = int(time.time())
    payload = {
        "iss": issuer_id,
        "iat": now,
        "exp": now + lifetime_seconds,
        "aud": "appstoreconnect-v1",
    }
    headers = {"alg": "ES256", "kid": key_id, "typ": "JWT"}
    try:
        token = jwt.encode(
            payload, _normalize_private_key(private_key), algorithm="ES256", headers=headers
        )
    except Exception as exc:  # noqa: BLE001
        raise AppleStoreError(f"Failed to create App Store Connect JWT: {exc}") from exc
    if isinstance(token, bytes):
        return token.decode("utf-8")
    return token


def download_sales_report(
    *,
    period: Period,
    window_start: date,
    window_end: date,
    vendor_number: str | None = None,
    token: str | None = None,
    session: requests.Session | None = None,
) -> list[dict[str, str]]:
    issuer_id = _require_env("ASC_ISSUER_ID")
    key_id = _require_env("ASC_KEY_ID")
    private_key = _require_env("ASC_PRIVATE_KEY")
    vendor = vendor_number or _require_env("ASC_VENDOR_NUMBER")
    auth = token or create_asc_token(issuer_id, key_id, private_key)

    params = {
        "filter[frequency]": apple_frequency(period),
        "filter[reportType]": "SALES",
        "filter[reportSubType]": "SUMMARY",
        "filter[vendorNumber]": vendor,
        "filter[reportDate]": apple_report_date(period, window_start, window_end),
        "filter[version]": "1_0",
    }
    http = session or requests.Session()
    try:
        response = http.get(
            "https://api.appstoreconnect.apple.com/v1/salesReports",
            headers={"Authorization": f"Bearer {auth}", "Accept": "application/a-gzip"},
            params=params,
            timeout=120,
        )
    except requests.RequestException as exc:
        raise AppleStoreError(f"App Store Connect request failed: {exc}") from exc
    if response.status_code == 404:
        return []
    if response.status_code != 200:
        raise AppleStoreError(
            f"App Store Connect salesReports failed ({response.status_code}): {response.text[:500]}"
        )

    try:
        text = gzip.decompress(response.content).decode("utf-8")
    except OSError as exc:
        raise AppleStoreError("Failed to decompress App Store sales report") from exc

    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    return [dict(row) for row in reader]


def _parse_float(value: str | None) -> float:
    if value is None or value == "":
        return 0.0
    try:
        return float(value.replace(",", ""))
    except ValueError:
        return 0.0


def sum_apple_proceeds(rows: list[dict[str, str]], apple_skus: list[str] | None = None) -> float:
    allowed = set(apple_skus or [])
    total = 0.0
    for row in rows:
        sku = (row.get("SKU") or row.get("sku") or "").strip()
        if allowed and sku not in allowed:
            continue
        proceeds = row.get("Developer Proceeds") or row.get("developer_proceeds") or "0"
        units = row.get("Units") or row.get("units") or "1"
        # Developer Proceeds is per-unit in sales summary reports.
        total += _parse_float(proceeds) * abs(_parse_float(units))
    return total


def fetch_apple_revenue(
    period: Period,
    window_start: date,
    window_end: date,
    apple_skus: list[str] | None = None,
) -> float:
    rows = download_sales_report(period=period, window_start=window_start, window_end=window_end)
    return sum_apple_proceeds(rows, apple_skus=apple_skus)


def fetch_apple_daily_series(
    start: date,
    end: date,
    apple_skus: list[str] | None = None,
) -> dict[date, float]:
    """Fetch daily proceeds for each day in [start, end]. Missing days → 0."""
    from x_mrr_banner.dates import daterange

    result: dict[date, float] = {}
    errors: list[AppleStoreError] = []
    for day in daterange(start, end):
        try:
            rows = download_sales_report(period="daily", window_start=day, window_end=day)
            result[day] = sum_apple_proceeds(rows, apple_skus=apple_skus)
        except AppleStoreError as exc:
            errors.append(exc)
            result[day] = 0.0
    # Auth/config failures hit every day; surface one error so callers can warn + fall back.
    if errors and len(errors) == len(result):
        raise AppleStoreError(str(errors[0])) from errors[0]
    return result
