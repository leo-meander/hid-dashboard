import logging
from datetime import date
from typing import Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# In-memory rate cache: {("TWD", "VND"): (rate, date)}
_rate_cache: dict[tuple[str, str], tuple[float, date]] = {}

# Hardcoded fallbacks used only when the in-memory cache is empty (e.g. before
# the first successful FX API call after a fresh server boot). Without these,
# any sync that runs before the cache warms up stamps grand_total_vnd = NULL
# over previously-correct values, which then breaks all-branch revenue rollups.
# Values match marketing_budget.FX_FALLBACK_TO_VND / ads_platform_sync.
_FALLBACK_RATES: dict[tuple[str, str], float] = {
    ("VND", "VND"): 1.0,
    ("TWD", "VND"): 830.0,
    ("JPY", "VND"): 165.0,
}

EXCHANGE_RATE_BASE_URL = "https://v6.exchangerate-api.com/v6"


async def fetch_rate(from_currency: str, to_currency: str = "VND") -> Optional[float]:
    """
    Fetch exchange rate from_currency → to_currency.
    Returns 1.0 if currencies are identical.
    Uses in-memory daily cache; falls back to last known rate on API failure.
    """
    from_currency = from_currency.upper()
    to_currency = to_currency.upper()

    if from_currency == to_currency:
        return 1.0

    cache_key = (from_currency, to_currency)
    today = date.today()

    # Return cached rate if fetched today
    if cache_key in _rate_cache:
        cached_rate, cached_date = _rate_cache[cache_key]
        if cached_date == today:
            return cached_rate

    try:
        url = f"{EXCHANGE_RATE_BASE_URL}/{settings.EXCHANGE_RATE_API_KEY}/latest/{from_currency}"
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()

        rates = data.get("conversion_rates", {})
        rate = rates.get(to_currency)

        if rate is None:
            logger.warning("Rate not found for %s → %s in API response", from_currency, to_currency)
            return _get_fallback_rate(cache_key)

        _rate_cache[cache_key] = (rate, today)
        logger.info("Fetched exchange rate %s → %s = %s", from_currency, to_currency, rate)
        return rate

    except Exception as exc:
        logger.warning("Currency API error (%s → %s): %s — using fallback", from_currency, to_currency, exc)
        return _get_fallback_rate(cache_key)


def _get_fallback_rate(cache_key: tuple[str, str]) -> Optional[float]:
    """Return last cached rate regardless of date, or hardcoded fallback."""
    if cache_key in _rate_cache:
        rate, cached_date = _rate_cache[cache_key]
        logger.warning("Using stale cached rate from %s", cached_date)
        return rate
    fallback = _FALLBACK_RATES.get(cache_key)
    if fallback is not None:
        logger.warning("Using hardcoded fallback rate %s → %s = %s",
                       cache_key[0], cache_key[1], fallback)
    return fallback


async def convert_to_vnd(amount: Optional[float], from_currency: str) -> Optional[float]:
    """Convert an amount to VND. Returns None if rate unavailable."""
    if amount is None:
        return None
    if from_currency.upper() == "VND":
        return amount
    rate = await fetch_rate(from_currency, "VND")
    if rate is None:
        return None
    return round(amount * rate, 2)


def get_cached_rate(from_currency: str, to_currency: str = "VND") -> Optional[float]:
    """Synchronous lookup of cached rate (for use in sync contexts).

    Falls back to a hardcoded rate when the in-memory cache hasn't been
    populated yet — better to use a slightly stale rate than to stamp
    grand_total_vnd = NULL over good values whenever the FX API is briefly
    unreachable. Subsequent syncs overwrite with the live rate.
    """
    from_currency = from_currency.upper()
    to_currency = to_currency.upper()
    if from_currency == to_currency:
        return 1.0
    key = (from_currency, to_currency)
    entry = _rate_cache.get(key)
    if entry:
        return entry[0]
    return _FALLBACK_RATES.get(key)
