"""Tests for services/currency.py."""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import date

import app.services.currency as currency_module
from app.services.currency import (
    convert_to_vnd,
    get_cached_rate,
    _rate_cache,
)


@pytest.fixture(autouse=True)
def clear_cache():
    """Reset the in-memory rate cache between tests."""
    _rate_cache.clear()
    currency_module._failed_until.clear()
    currency_module._fallback_warned.clear()
    yield
    _rate_cache.clear()
    currency_module._failed_until.clear()
    currency_module._fallback_warned.clear()


class TestGetCachedRate:
    def test_same_currency_returns_one(self):
        assert get_cached_rate("VND", "VND") == 1.0
        assert get_cached_rate("vnd", "VND") == 1.0

    def test_falls_back_to_hardcoded_when_not_cached(self):
        # Without a cached rate, known currencies return the hardcoded
        # fallback so syncs never stamp grand_total_vnd = NULL.
        assert get_cached_rate("TWD", "VND") == 830.0
        assert get_cached_rate("JPY", "VND") == 165.0

    def test_returns_none_for_unknown_currency(self):
        assert get_cached_rate("XYZ", "VND") is None

    def test_returns_cached_value(self):
        _rate_cache[("TWD", "VND")] = (800.0, date.today())
        assert get_cached_rate("TWD", "VND") == 800.0


class TestConvertToVnd:
    @pytest.mark.asyncio
    async def test_vnd_passthrough(self):
        result = await convert_to_vnd(100.0, "VND")
        assert result == 100.0

    @pytest.mark.asyncio
    async def test_none_amount_returns_none(self):
        result = await convert_to_vnd(None, "TWD")
        assert result is None

    @pytest.mark.asyncio
    async def test_converts_with_cached_rate(self):
        _rate_cache[("TWD", "VND")] = (800.0, date.today())
        result = await convert_to_vnd(100.0, "TWD")
        assert result == 80000.0

    @pytest.mark.asyncio
    async def test_uses_hardcoded_fallback_when_api_fails(self):
        # No cached rate, API unreachable — must still convert using fallback
        # so we never store NULL when native is set.
        with patch("app.services.currency.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(side_effect=Exception("network error"))
            result = await convert_to_vnd(100.0, "JPY")
            assert result == 16500.0

    @pytest.mark.asyncio
    async def test_returns_none_for_unknown_currency_no_fallback(self):
        with patch("app.services.currency.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(side_effect=Exception("network error"))
            result = await convert_to_vnd(100.0, "XYZ")
            assert result is None


class TestFetchRateCaching:
    @pytest.mark.asyncio
    async def test_uses_cached_rate_same_day(self):
        today = date.today()
        _rate_cache[("TWD", "VND")] = (790.0, today)

        # fetch_rate should return cached value without hitting API
        result = await currency_module.fetch_rate("TWD", "VND")
        assert result == 790.0

    @pytest.mark.asyncio
    async def test_same_currency_returns_one(self):
        result = await currency_module.fetch_rate("VND", "VND")
        assert result == 1.0


class TestFetchRateSkipsDoomedCalls:
    """The FX API key has never been set in production.

    Every conversion used to open an HTTPS connection to prove that again
    before returning the hardcoded rate — and Seasonal Campaign converts
    once per ad per money field, so the proof cost more than the page.
    """

    @pytest.mark.asyncio
    async def test_placeholder_key_never_calls_the_api(self, monkeypatch):
        monkeypatch.setattr(currency_module.settings,
                            "EXCHANGE_RATE_API_KEY", "placeholder_key")
        with patch("app.services.currency.httpx.AsyncClient") as mock_client:
            result = await currency_module.fetch_rate("TWD", "VND")
        assert result == 830.0
        mock_client.assert_not_called()

    @pytest.mark.asyncio
    async def test_real_key_still_calls_the_api(self, monkeypatch):
        monkeypatch.setattr(currency_module.settings,
                            "EXCHANGE_RATE_API_KEY", "real_key")
        response = MagicMock()
        response.json.return_value = {"conversion_rates": {"VND": 812.5}}
        response.raise_for_status = MagicMock()
        with patch("app.services.currency.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(
                return_value=MagicMock(get=AsyncMock(return_value=response))
            )
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await currency_module.fetch_rate("TWD", "VND")
        assert result == 812.5
        assert _rate_cache[("TWD", "VND")][0] == 812.5

    @pytest.mark.asyncio
    async def test_failure_puts_the_pair_on_cooldown(self, monkeypatch):
        """One failure is information; a hundred is a slow page."""
        monkeypatch.setattr(currency_module.settings,
                            "EXCHANGE_RATE_API_KEY", "real_key")
        with patch("app.services.currency.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(
                side_effect=Exception("network error")
            )
            first = await currency_module.fetch_rate("TWD", "VND")
            calls_after_first = mock_client.call_count
            second = await currency_module.fetch_rate("TWD", "VND")
            assert mock_client.call_count == calls_after_first

        assert first == 830.0
        assert second == 830.0


class TestIntrospectionHelpers:
    def test_cache_entry_none_when_never_fetched(self):
        assert currency_module.get_cache_entry("TWD", "VND") is None

    def test_cache_entry_returns_rate_and_date(self):
        today = date.today()
        _rate_cache[("TWD", "VND")] = (812.5, today)
        assert currency_module.get_cache_entry("twd", "vnd") == (812.5, today)

    def test_fallback_rate_value(self):
        assert currency_module.get_fallback_rate_value("TWD") == 830.0
        assert currency_module.get_fallback_rate_value("VND") == 1.0
        assert currency_module.get_fallback_rate_value("XYZ") is None

    @pytest.mark.asyncio
    async def test_probe_reports_missing_api_key(self, monkeypatch):
        monkeypatch.setattr(currency_module.settings, "EXCHANGE_RATE_API_KEY", "placeholder_key")
        rate, error = await currency_module.probe_live_rate("TWD", "VND")
        assert rate is None
        assert "EXCHANGE_RATE_API_KEY" in error

    @pytest.mark.asyncio
    async def test_probe_does_not_write_cache(self, monkeypatch):
        monkeypatch.setattr(currency_module.settings, "EXCHANGE_RATE_API_KEY", "real_key")
        with patch("app.services.currency.httpx.AsyncClient") as mock_client:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"conversion_rates": {"VND": 819.0}}
            mock_resp.raise_for_status = MagicMock()
            mock_ctx = AsyncMock()
            mock_ctx.get = AsyncMock(return_value=mock_resp)
            mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

            rate, error = await currency_module.probe_live_rate("TWD", "VND")

        assert (rate, error) == (819.0, None)
        # Checking the live rate must not change what in-flight syncs stamp.
        assert ("TWD", "VND") not in _rate_cache

    @pytest.mark.asyncio
    async def test_refresh_keeps_last_good_rate_when_api_fails(self):
        yesterday = date.today().replace(day=1)
        _rate_cache[("TWD", "VND")] = (812.5, yesterday)

        with patch("app.services.currency.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(side_effect=Exception("network error"))
            result = await currency_module.refresh_rate("TWD", "VND")

        # Never drop to the hardcoded 830 floor just because a refresh failed.
        assert result == 812.5
        assert _rate_cache[("TWD", "VND")] == (812.5, yesterday)
