from __future__ import annotations

import csv
import io
import json
import logging
import os
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any

from google.cloud import storage
from google.oauth2 import service_account

from x_mrr_banner.dates import daterange

logger = logging.getLogger(__name__)


class GooglePlayError(RuntimeError):
    pass


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise GooglePlayError(f"Missing required environment variable: {name}")
    return value


def _reports_bucket() -> str:
    """Normalize GOOGLE_PLAY_REPORTS_BUCKET to a bare bucket id."""
    raw = _require_env("GOOGLE_PLAY_REPORTS_BUCKET")
    # Accept full URIs pasted from Play Console ("Copy Cloud Storage URI").
    cleaned = raw.removeprefix("gs://").strip("/")
    bucket = cleaned.split("/", 1)[0].strip()
    if not bucket:
        raise GooglePlayError("GOOGLE_PLAY_REPORTS_BUCKET is empty after normalization")
    return bucket


def _load_service_account_info() -> dict[str, Any]:
    raw = _require_env("GOOGLE_PLAY_SERVICE_ACCOUNT_JSON")
    stripped = raw.strip()
    if not stripped.startswith("{"):
        path = Path(stripped).expanduser()
        try:
            if path.is_file():
                with path.open(encoding="utf-8") as handle:
                    return json.load(handle)
        except OSError:
            pass
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise GooglePlayError(
            "GOOGLE_PLAY_SERVICE_ACCOUNT_JSON must be a JSON file path or raw JSON string"
        ) from exc


def _storage_client() -> storage.Client:
    try:
        info = _load_service_account_info()
        credentials = service_account.Credentials.from_service_account_info(
            info,
            scopes=["https://www.googleapis.com/auth/devstorage.read_only"],
        )
        project = info.get("project_id")
        return storage.Client(project=project, credentials=credentials)
    except GooglePlayError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise GooglePlayError(f"Failed to initialize Google Play storage client: {exc}") from exc


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
    # Prefer BOM detection: blind utf-16 decode can "succeed" on utf-8 bytes
    # and garble column names so every amount/package lookup returns empty.
    if payload.startswith((b"\xff\xfe", b"\xfe\xff")):
        return payload.decode("utf-16")
    if payload.startswith(b"\xef\xbb\xbf"):
        return payload.decode("utf-8-sig")
    for encoding in ("utf-8-sig", "utf-8", "utf-16"):
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
    """Download Play Console *estimated sales* report (not earnings).

    Path: gs://{bucket}/sales/salesreport_{YYYYMM}.zip
    Earnings (`earnings/earnings_*.zip`) publish later (~5th of next month) and
    are intentionally unused so monthly banner runs work early in the month.
    """
    client = _storage_client()
    bucket = client.bucket(bucket_name)
    blob_name = f"sales/salesreport_{yyyymm}.zip"
    blob = bucket.blob(blob_name)
    try:
        if not blob.exists():
            return None
        return blob.download_as_bytes()
    except GooglePlayError:
        raise
    except Exception as exc:  # noqa: BLE001 — surface GCS/auth failures as store errors
        raise GooglePlayError(
            f"Failed to read gs://{bucket_name}/{blob_name}: {exc}"
        ) from exc


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
    # Prefer Package ID / Package Name (current salesreport columns). Legacy
    # reports used Product ID for the app package name before Google renamed it.
    for key in (
        "Package ID",
        "Package Name",
        "package_id",
        "package_name",
        "Product ID",
        "product_id",
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


def _row_amount_and_currency(
    row: dict[str, str],
    *,
    fallback_currency: str,
) -> tuple[float, str]:
    """Return (signed amount, currency code) for one estimated-sales row.

    Prefer merchant-currency / estimated-proceeds columns when present; otherwise
    use Item Price (buyer currency, pre-tax) then Charged Amount.
    """
    amount = 0.0
    currency = ""

    for key in (
        "Merchant Receives",
        "Earnings (Merchant Currency)",
        "Amount (Merchant Currency)",
    ):
        if key in row and row[key]:
            amount = _parse_amount(row[key])
            currency = (
                row.get("Merchant Currency") or row.get("merchant_currency") or ""
            ).strip().upper()
            break
    else:
        for key in ("Item Price", "Charged Amount", "charged_amount"):
            if key in row and row[key]:
                amount = _parse_amount(row[key])
                currency = (
                    row.get("Currency of Sale") or row.get("currency_of_sale") or ""
                ).strip().upper()
                break

    if amount == 0:
        return 0.0, (currency or fallback_currency).upper()

    status = (row.get("Financial Status") or row.get("financial_status") or "").lower()
    if "refund" in status and amount > 0:
        amount = -amount

    return amount, (currency or fallback_currency).upper()


def load_play_sales_rows(start: date, end: date) -> list[dict[str, str]]:
    bucket = _reports_bucket()
    rows: list[dict[str, str]] = []
    errors: list[str] = []
    loaded_months: list[str] = []
    missing_months: list[str] = []

    logger.info(
        "Google Play: loading estimated sales reports from gs://%s/sales/ (%s → %s)",
        bucket,
        start.isoformat(),
        end.isoformat(),
    )

    for yyyymm in _month_keys(start, end):
        try:
            payload = _download_sales_zip(bucket, yyyymm)
        except GooglePlayError as exc:
            errors.append(str(exc))
            missing_months.append(yyyymm)
            continue
        if payload is None:
            missing_months.append(yyyymm)
            continue
        month_rows = _rows_from_zip(payload)
        loaded_months.append(f"{yyyymm} ({len(month_rows)} rows)")
        rows.extend(month_rows)

    if loaded_months:
        logger.info(
            "Google Play estimated sales: loaded %d row(s) from %d month(s): %s",
            len(rows),
            len(loaded_months),
            "; ".join(loaded_months),
        )
    if missing_months:
        msg = (
            f"missing estimated sales zip(s) for {', '.join(missing_months)} "
            f"under gs://{bucket}/sales/salesreport_YYYYMM.zip"
        )
        if rows:
            logger.warning("Google Play: %s; continuing with partial data", msg)
            safe = msg.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
            print(f"::warning title=Google Play::{safe}", flush=True)
        else:
            logger.warning("Google Play: %s", msg)

    if errors and not rows:
        raise GooglePlayError("; ".join(errors[:3]))
    if errors and rows:
        msg = (
            f"skipped {len(errors)} month(s) due to download errors; "
            f"continuing with partial data. First error: {errors[0]}"
        )
        logger.warning("Google Play: %s", msg)
        safe = msg.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::warning title=Google Play::{safe}", flush=True)
    return rows


def sum_google_revenue(
    rows: list[dict[str, str]],
    start: date,
    end: date,
    package_names: list[str] | None = None,
    *,
    target_currency: str = "USD",
    as_of: date | None = None,
) -> float:
    """Sum estimated sales into target_currency (FX on buyer-currency amounts)."""
    from x_mrr_banner.extract.fx import convert_amount

    # None = unfiltered (portfolio with no package list). [] = explicitly no packages → $0.
    if package_names is not None and not package_names:
        return 0.0
    allowed = set(package_names or [])
    target = target_currency.strip().upper() or "USD"
    by_currency: dict[str, float] = {}

    for row in rows:
        package = _row_package(row)
        if allowed and package not in allowed:
            continue
        order_date = _row_order_date(row)
        if order_date is None or order_date < start or order_date > end:
            continue
        amount, currency = _row_amount_and_currency(row, fallback_currency=target)
        if amount == 0:
            continue
        by_currency[currency] = by_currency.get(currency, 0.0) + amount

    if not by_currency:
        return 0.0

    on = as_of or end
    total = 0.0
    converted_parts: list[str] = []
    for currency, amount in sorted(by_currency.items()):
        try:
            converted = convert_amount(amount, currency, target, on=on)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Google FX %s→%s failed for %.2f (%s); leaving unconverted",
                currency,
                target,
                amount,
                exc,
            )
            converted = amount if currency == target else 0.0
        total += converted
        if currency == target:
            converted_parts.append(f"{amount:.2f} {currency}")
        else:
            converted_parts.append(f"{amount:.2f} {currency}→{converted:.2f} {target}")

    if len(by_currency) > 1 or next(iter(by_currency.keys())) != target:
        logger.info(
            "Google estimated sales: %s (total %.2f %s)",
            "; ".join(converted_parts),
            total,
            target,
        )
    return total


def fetch_google_revenue(
    start: date,
    end: date,
    package_names: list[str] | None = None,
    *,
    target_currency: str = "USD",
) -> float:
    rows = load_play_sales_rows(start, end)
    return sum_google_revenue(
        rows,
        start,
        end,
        package_names=package_names,
        target_currency=target_currency,
        as_of=end,
    )


def fetch_google_daily_series(
    start: date,
    end: date,
    package_names: list[str] | None = None,
    *,
    target_currency: str = "USD",
) -> dict[date, float]:
    rows = load_play_sales_rows(start, end)
    result = {day: 0.0 for day in daterange(start, end)}
    if package_names is not None and not package_names:
        return result
    # Convert per day via the shared summer for consistent FX + refund handling.
    for day in list(result):
        result[day] = sum_google_revenue(
            rows,
            day,
            day,
            package_names=package_names,
            target_currency=target_currency,
            as_of=day,
        )
    return result
