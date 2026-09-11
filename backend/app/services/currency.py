import logging
from datetime import date, datetime, timedelta, timezone
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

# Don't pay for an answer upstream cannot give.
#
# EXCHANGE_RATE_API_KEY has never been set in production, so every call here
# opened a fresh HTTPS connection to .../placeholder_key/latest/TWD, waited
# out the handshake and the rejection, and then returned the hardcoded rate
# it could have returned immediately. Callers that convert row by row pay
# that per row: Seasonal Campaign resolves one rate per ad per money field,
# which turned ~50 ads into a 17-second page load, all of it spent being
# told no.
#
# Two guards, both returning exactly what the failure path already returned:
#
#   _api_configured()  no usable key, no call
#   _failed_until      after a real failure, stop asking for a few minutes
#
# So the numbers do not move — they just arrive. And the day a real key is
# set, the live rate is picked up on the next call with no code change.
_PLACEHOLDER_KEYS = {"", "placeholder_key", "your_api_key", "changeme"}

_FAIL_COOLDOWN = timedelta(minutes=10)
_failed_until: dict[tuple[str, str], datetime] = {}

# One line per currency pair per process, not one per conversion: a fallback
# is worth knowing about, and a hundred identical warnings per request buries
# everything else in the log.
_fallback_warned: set[tuple[str, str]] = set()


def _api_configured() -> bool:
    return (settings.EXCHANGE_RATE_API_KEY or "").strip().lower() \
        not in _PLACEHOLDER_KEYS


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

    if not _api_configured():
        return _get_fallback_rate(cache_key)

    blocked_until = _failed_until.get(cache_key)
    if blocked_until and datetime.now(timezone.utc) < blocked_until:
        return _get_fallback_rate(cache_key)

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
            _failed_until[cache_key] = datetime.now(timezone.utc) + _FAIL_COOLDOWN
            return _get_fallback_rate(cache_key)

        _rate_cache[cache_key] = (rate, today)
        _failed_until.pop(cache_key, None)
        logger.info("Fetched exchange rate %s → %s = %s", from_currency, to_currency, rate)
        return rate

    except Exception as exc:
        logger.warning("Currency API error (%s → %s): %s — using fallback", from_currency, to_currency, exc)
        _failed_until[cache_key] = datetime.now(timezone.utc) + _FAIL_COOLDOWN
        return _get_fallback_rate(cache_key)


def _get_fallback_rate(cache_key: tuple[str, str]) -> Optional[float]:
    """Return last cached rate regardless of date, or hardcoded fallback."""
    if cache_key in _rate_cache:
        rate, cached_date = _rate_cache[cache_key]
        logger.warning("Using stale cached rate from %s", cached_date)
        return rate
    fallback = _FALLBACK_RATES.get(cache_key)
    if fallback is not None and cache_key not in _fallback_warned:
        _fallback_warned.add(cache_key)
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


# ── Introspection helpers (used by GET /api/metrics/fx-rates) ────────────────
# Nothing in HiD used to expose which rate a sync actually stamped onto
# grand_total_vnd, so a stale cache or a dead API key was invisible. These let
# the Settings → Currency panel show the three layers separately: what's in the
# cache right now, what the provider says right now, and the hardcoded floor.

def get_cache_entry(from_currency: str, to_currency: str = "VND") -> Optional[tuple[float, date]]:
    """Raw (rate, fetched_on) cache entry, or None when never fetched."""
    return _rate_cache.get((from_currency.upper(), to_currency.upper()))


def get_fallback_rate_value(from_currency: str, to_currency: str = "VND") -> Optional[float]:
    """The hardcoded rate used when both cache and API are unavailable."""
    from_currency = from_currency.upper()
    to_currency = to_currency.upper()
    if from_currency == to_currency:
        return 1.0
    return _FALLBACK_RATES.get((from_currency, to_currency))


async def probe_live_rate(from_currency: str, to_currency: str = "VND") -> tuple[Optional[float], Optional[str]]:
    """Hit the FX provider directly, bypassing the cache. Returns (rate, error).

    Read-only on purpose — it never writes to ``_rate_cache``, so checking the
    live rate can't change what an in-flight sync is stamping. Use
    ``refresh_rate`` when the cache should actually be updated.
    """
    from_currency = from_currency.upper()
    to_currency = to_currency.upper()

    if from_currency == to_currency:
        return 1.0, None

    if not settings.EXCHANGE_RATE_API_KEY or settings.EXCHANGE_RATE_API_KEY == "placeholder_key":
        return None, "EXCHANGE_RATE_API_KEY is not configured"

    try:
        url = f"{EXCHANGE_RATE_BASE_URL}/{settings.EXCHANGE_RATE_API_KEY}/latest/{from_currency}"
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"

    rate = data.get("conversion_rates", {}).get(to_currency)
    if rate is None:
        return None, f"{to_currency} missing from provider response"
    return float(rate), None


async def refresh_rate(from_currency: str, to_currency: str = "VND") -> Optional[float]:
    """Drop today's cache entry and re-fetch, so later writes use a fresh rate."""
    key = (from_currency.upper(), to_currency.upper())
    previous = _rate_cache.pop(key, None)
    rate = await fetch_rate(key[0], key[1])
    if key not in _rate_cache and previous is not None:
        # Re-fetch failed. Put the last good rate back instead of leaving the
        # cache empty, which would drop every later write onto the hardcoded
        # floor (830 / 165) until the API recovers.
        _rate_cache[key] = previous
        return previous[0]
    return rate
