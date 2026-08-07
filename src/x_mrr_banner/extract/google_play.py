from __future__ import annotations

import csv
import io
import json
import os
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any

from google.cloud import storage
from google.oauth2 import service_account

from x_mrr_banner.dates import daterange


class GooglePlayError(RuntimeError):
    pass


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise GooglePlayError(f"Missing required environment variable: {name}")
    return value


def _load_service_account_info() -> dict[str, Any]:
    raw = _require_env("GOOGLE_PLAY_SERVICE_ACCOUNT_JSON")
    path = Path(raw)
    if path.exists() and path.is_file():
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GooglePlayError(
            "GOOGLE_PLAY_SERVICE_ACCOUNT_JSON must be a JSON file path or raw JSON string"
        ) from exc


def _storage_client() -> storage.Client:
    info = _load_service_account_info()
    credentials = service_account.Credentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/devstorage.read_only"],
    )
    project = info.get("project_id")
    return storage.Client(project=project, credentials=credentials)


def _parse_play_date(value: str) -> date | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    for fmt in ("%Y/%m/%d", "%b %d, %Y", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _decode_csv_bytes(payload: bytes) -> str:
    for encoding in ("utf-16", "utf-8-sig", "utf-8"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    return payload.decode("utf-8", errors="replace")


def _parse_amount(value: str | None) -> float:
    if value is None or value == "":
        return 0.0
    cleaned = value.strip().replace(",", "").replace("$", "")
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _month_keys(start: date, end: date) -> list[str]:
    keys: list[str] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        keys.append(f"{year:04d}{month:02d}")
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1
    return keys


def _download_sales_zip(bucket_name: str, yyyymm: str) -> bytes | None:
    client = _storage_client()
    bucket = client.bucket(bucket_name)
    blob_name = f"sales/salesreport_{yyyymm}.zip"
    blob = bucket.blob(blob_name)
    if not blob.exists():
        return None
    return blob.download_as_bytes()


def _rows_from_zip(payload: bytes) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for name in archive.namelist():
            if not name.lower().endswith(".csv"):
                continue
            text = _decode_csv_bytes(archive.read(name))
            reader = csv.DictReader(io.StringIO(text))
            for row in reader:
                rows.append({k.strip(): (v or "").strip() for k, v in row.items() if k})
    return rows


def _row_package(row: dict[str, str]) -> str:
    for key in (
        "Product ID",
        "Package Name",
        "product_id",
        "package_name",
        "Product id",
    ):
        if key in row and row[key]:
            return row[key]
    return ""


def _row_order_date(row: dict[str, str]) -> date | None:
    for key in ("Order Charged Date", "Transaction Date", "Date", "order_charged_date"):
        if key in row and row[key]:
            parsed = _parse_play_date(row[key])
            if parsed:
                return parsed
    return None


def _row_amount(row: dict[str, str]) -> float:
    for key in (
        "Earnings (Merchant Currency)",
        "Amount (Merchant Currency)",
        "Charged Amount",
        "Item Price",
        "charged_amount",
    ):
        if key in row and row[key]:
            return _parse_amount(row[key])
    return 0.0


def load_play_sales_rows(start: date, end: date) -> list[dict[str, str]]:
    bucket = _require_env("GOOGLE_PLAY_REPORTS_BUCKET")
    rows: list[dict[str, str]] = []
    for yyyymm in _month_keys(start, end):
        payload = _download_sales_zip(bucket, yyyymm)
        if payload is None:
            continue
        rows.extend(_rows_from_zip(payload))
    return rows


def sum_google_revenue(
    rows: list[dict[str, str]],
    start: date,
    end: date,
    package_names: list[str] | None = None,
) -> float:
    allowed = set(package_names or [])
    total = 0.0
    for row in rows:
        package = _row_package(row)
        if allowed and package not in allowed:
            continue
        order_date = _row_order_date(row)
        if order_date is None or order_date < start or order_date > end:
            continue
        total += _row_amount(row)
    return total


def fetch_google_revenue(
    start: date,
    end: date,
    package_names: list[str] | None = None,
) -> float:
    rows = load_play_sales_rows(start, end)
    return sum_google_revenue(rows, start, end, package_names=package_names)


def fetch_google_daily_series(
    start: date,
    end: date,
    package_names: list[str] | None = None,
) -> dict[date, float]:
    rows = load_play_sales_rows(start, end)
    allowed = set(package_names or [])
    result = {day: 0.0 for day in daterange(start, end)}
    for row in rows:
        package = _row_package(row)
        if allowed and package not in allowed:
            continue
        order_date = _row_order_date(row)
        if order_date is None or order_date not in result:
            continue
        result[order_date] += _row_amount(row)
    return result
