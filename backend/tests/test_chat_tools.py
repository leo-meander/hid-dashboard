"""Unit tests for chat_tools pure helpers.

The SQL-backed tool handlers need a live DB and are exercised end-to-end, but
the compare-window math is pure and is where off-by-one bugs hide, so it's
covered here."""
from datetime import date

from app.services.chat_tools import _resolve_compare_windows, TOOL_DEFS, TOOL_HANDLERS


def test_default_days_window_is_last_7_vs_prior_7():
    today = date(2026, 5, 25)
    d_from, d_to, prev_from, prev_to = _resolve_compare_windows({}, today, default_days=7)
    # Current: the 7 days ending today, inclusive.
    assert (d_from, d_to) == (date(2026, 5, 19), date(2026, 5, 25))
    # Prior: the 7 days immediately before, no gap, no overlap.
    assert (prev_from, prev_to) == (date(2026, 5, 12), date(2026, 5, 18))


def test_explicit_range_makes_equal_length_prior_window():
    today = date(2026, 5, 25)
    inp = {"date_from": "2026-05-01", "date_to": "2026-05-10"}  # 10-day window
    d_from, d_to, prev_from, prev_to = _resolve_compare_windows(inp, today)
    assert (d_from, d_to) == (date(2026, 5, 1), date(2026, 5, 10))
    assert (prev_from, prev_to) == (date(2026, 4, 21), date(2026, 4, 30))


def test_reversed_dates_are_swapped():
    today = date(2026, 5, 25)
    inp = {"date_from": "2026-05-10", "date_to": "2026-05-01"}
    d_from, d_to, _, _ = _resolve_compare_windows(inp, today)
    assert d_from <= d_to


def test_source_by_country_tool_is_registered_and_consistent():
    names = {t["name"] for t in TOOL_DEFS}
    assert "get_source_by_country" in names
    # Every advertised tool must have a handler, and vice versa.
    assert names == set(TOOL_HANDLERS.keys())
