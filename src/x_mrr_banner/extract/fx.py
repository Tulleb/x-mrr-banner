from __future__ import annotations

import logging
from datetime import date, timedelta
from functools import lru_cache

import requests

logger = logging.getLogger(__name__)

# Free daily FX table (includes BRL and other ASC proceeds currencies).
# Not identical to Apple's payout FX on Payments and Financial Reports.
_CURRENCY_API = (
    "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@"
    "{day}/v1/currencies/{base}.json"
)
_CURRENCY_API_FALLBACK = (
    "https://{day}.currency-api.pages.dev/v1/currencies/{base}.json"
)


@lru_cache(maxsize=256)
def fx_rate(from_currency: str, to_currency: str, on: date) -> float:
    """Return units of `to_currency` per 1 `from_currency` on/near `on`."""
    src = from_currency.strip().upper()
    dst = to_currency.strip().upper()
    if not src or not dst:
        raise ValueError("Currency codes are required for FX conversion")
    if src == dst:
        return 1.0

    cursor = on
    last_error = ""
    for _ in range(10):
        day = cursor.isoformat()
        base = src.lower()
        urls = [
            _CURRENCY_API.format(day=day, base=base),
            _CURRENCY_API_FALLBACK.format(day=day, base=base),
        ]
        for url in urls:
            try:
                response = requests.get(url, timeout=30)
            except requests.RequestException as exc:
                last_error = str(exc)
                continue
            if response.status_code != 200:
                last_error = f"HTTP {response.status_code}"
                continue
            payload = response.json()
            rates = payload.get(base) or payload.get(src) or {}
            rate = rates.get(dst.lower())
            if rate is None:
                last_error = f"no {dst} rate for {src} on {day}"
                continue
            return float(rate)
        cursor -= timedelta(days=1)

    raise RuntimeError(
        f"FX rate {src}→{dst} unavailable near {on.isoformat()}: {last_error or 'no data'}"
    )


def convert_amount(
    amount: float,
    from_currency: str,
    to_currency: str,
    *,
    on: date,
) -> float:
    if amount == 0:
        return 0.0
    return amount * fx_rate(from_currency, to_currency, on)
