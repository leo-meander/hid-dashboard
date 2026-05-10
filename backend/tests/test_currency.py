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
    yield
    _rate_cache.clear()


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
