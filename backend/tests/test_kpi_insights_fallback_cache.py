"""The Cloudbeds Insights fallback in kpi_engine is called at most once an hour.

Every KPI summary asks for two months — the current one and next month — and
next month normally has no `daily_metrics` rows yet, so it takes the fallback
path. That path is `fetch_occupancy_filtered`, which is six custom reports and
eighteen HTTP round trips against Cloudbeds. Uncached, the Home page paid for
it again on every branch tab switch, which is what made switching branches
take many seconds.

These tests pin the two halves of the fix: a successful fallback is reused,
and — the case that actually bit — an EMPTY fallback is reused too. Caching
only the successful answer would leave a branch with genuinely zero bookings,
or one whose API key is missing, retrying the full round trip forever.
"""
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.services import kpi_engine
from app.services.kpi_engine import _get_insights_filtered

BRANCH_ID = uuid4()
PROPERTY_ID = "12345"

_FULL_RESULT = {
    "total_rev": 900.0, "total_sold": 30, "total_adr": 30.0,
    "room_rev": 900.0, "room_sold": 30, "room_adr": 30.0,
    "dorm_rev": 0, "dorm_sold": 0, "dorm_adr": 0, "has_dorm": False,
}


def _db_with_no_daily_metrics():
    """A session whose daily_metrics sums are all zero — forces the fallback.

    `_get_insights_filtered` makes exactly two calls on it: the six-column sum
    over daily_metrics (`.one()`), then the Branch lookup (`.first()`).
    """
    branch = MagicMock()
    branch.cloudbeds_property_id = PROPERTY_ID
    branch.name = "MEANDER Saigon"

    db = MagicMock()
    db.query.return_value.filter.return_value.one.return_value = (0, 0, 0, 0, 0, 0)
    db.query.return_value.filter_by.return_value.first.return_value = branch
    return db


@pytest.fixture(autouse=True)
def clear_cache():
    kpi_engine._insights_fallback_cache.clear()
    yield
    kpi_engine._insights_fallback_cache.clear()


@pytest.fixture
def api_key():
    # Patched on the class — a pydantic Settings instance rejects the attribute
    # deletion `patch` does on teardown.
    with patch("app.config.Settings.get_api_key_for_property", return_value="test_key"):
        yield


def _call(fetch_mock, year=2026, month=9):
    with patch("app.services.cloudbeds.fetch_occupancy_filtered", fetch_mock):
        return _get_insights_filtered(_db_with_no_daily_metrics(), BRANCH_ID, year, month)


class TestFallbackIsCached:
    def test_second_request_for_the_same_month_makes_no_api_call(self, api_key):
        fetch = MagicMock(return_value=dict(_FULL_RESULT))

        first = _call(fetch)
        second = _call(fetch)

        assert fetch.call_count == 1
        assert first == second == _FULL_RESULT

    def test_an_empty_result_is_cached_too(self, api_key):
        """The case that made branch switching slow: nothing to return, and
        nothing stopping the next page load from asking Cloudbeds again."""
        fetch = MagicMock(return_value={"total_sold": 0, "total_rev": 0, "total_adr": 0})

        first = _call(fetch)
        second = _call(fetch)

        assert fetch.call_count == 1
        assert first["total_sold"] == 0
        assert second["total_sold"] == 0

    def test_a_failing_call_is_not_retried_on_every_page_load(self, api_key):
        fetch = MagicMock(side_effect=TimeoutError("cloudbeds is slow"))

        first = _call(fetch)
        second = _call(fetch)

        assert fetch.call_count == 1
        assert first["total_sold"] == 0 and second["total_sold"] == 0

    def test_each_month_is_cached_separately(self, api_key):
        fetch = MagicMock(return_value=dict(_FULL_RESULT))

        _call(fetch, month=9)
        _call(fetch, month=10)
        _call(fetch, month=9)

        assert fetch.call_count == 2

    def test_a_stale_entry_is_refetched(self, api_key):
        fetch = MagicMock(return_value=dict(_FULL_RESULT))

        _call(fetch)
        # Age the entry past its TTL.
        key, (cached_at, value) = next(iter(kpi_engine._insights_fallback_cache.items()))
        kpi_engine._insights_fallback_cache[key] = (
            cached_at - kpi_engine._INSIGHTS_FALLBACK_TTL_SEC - 1, value,
        )
        _call(fetch)

        assert fetch.call_count == 2

    def test_callers_cannot_mutate_the_cached_entry(self, api_key):
        """Both compute_kpi_summary and compute_next_month_forecast read this
        dict; one of them scribbling on it must not reach the other."""
        fetch = MagicMock(return_value=dict(_FULL_RESULT))

        first = _call(fetch)
        first["total_rev"] = 0

        assert _call(fetch)["total_rev"] == 900.0


class TestDailyMetricsPathIsUnaffected:
    def test_populated_daily_metrics_never_touches_the_api_or_the_cache(self):
        """A sync that fills daily_metrics wins immediately — the DB path
        returns before the cache is consulted, so nothing needs invalidating."""
        db = MagicMock()
        db.query.return_value.filter.return_value.one.return_value = (
            900.0, 30, 30, 0, 900.0, 0,
        )
        fetch = MagicMock()

        with patch("app.services.cloudbeds.fetch_occupancy_filtered", fetch):
            out = _get_insights_filtered(db, BRANCH_ID, 2026, 8)

        assert fetch.call_count == 0
        assert kpi_engine._insights_fallback_cache == {}
        assert out["total_rev"] == 900.0
        assert out["total_adr"] == 30.0
