"""Unit tests for chat_tools pure helpers.

The SQL-backed tool handlers need a live DB and are exercised end-to-end, but
the compare-window math is pure and is where off-by-one bugs hide, so it's
covered here."""
from datetime import date

from app.services.chat_tools import (
    _resolve_compare_windows,
    _resolve_window,
    tool_get_country_profile,
    TOOL_DEFS,
    TOOL_HANDLERS,
)


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


def test_resolve_window_default_days_rolls_from_today():
    today = date(2026, 8, 10)
    d_from, d_to = _resolve_window({}, today, default_days=90)
    # Inclusive of today, so 90 days spans today - 89 .. today.
    assert (d_from, d_to) == (date(2026, 5, 13), date(2026, 8, 10))
    assert (d_to - d_from).days + 1 == 90


def test_resolve_window_explicit_range_ignores_days_default():
    # A named historical period (e.g. Q4 last year) must win over the rolling
    # `days` default — this is the bug that made the chatbot silently answer
    # "last 90 days" when asked for "Q4 last year".
    today = date(2026, 8, 10)
    inp = {"date_from": "2025-10-01", "date_to": "2025-12-31"}
    d_from, d_to = _resolve_window(inp, today, default_days=90)
    assert (d_from, d_to) == (date(2025, 10, 1), date(2025, 12, 31))


def test_resolve_window_reversed_dates_are_swapped():
    today = date(2026, 8, 10)
    inp = {"date_from": "2025-12-31", "date_to": "2025-10-01"}
    d_from, d_to = _resolve_window(inp, today)
    assert d_from <= d_to


def test_country_profile_schema_advertises_date_range_and_room_type_los():
    schema = next(t for t in TOOL_DEFS if t["name"] == "get_country_profile")
    props = schema["input_schema"]["properties"]
    assert "date_from" in props and "date_to" in props
    assert "Private Room only" in schema["description"]
    assert "los_avg_nights_room" in schema["description"]


def test_country_profile_schema_advertises_room_type_lead_time():
    # The chatbot used to answer "lead time for Dorm?" with "the tool only
    # splits LOS by room type" — it must now see the lead-time split too.
    schema = next(t for t in TOOL_DEFS if t["name"] == "get_country_profile")
    desc = schema["description"]
    assert "lead_time_avg_days_dorm" in desc
    assert "lead_time_distribution_pct_dorm" in desc


def test_guest_persona_is_advertised_to_the_chat_model():
    # It had a handler but no schema, so the chat model could never call it —
    # branch-wide persona questions (incl. Dorm lead time) went unanswered.
    schema = next(t for t in TOOL_DEFS if t["name"] == "get_guest_persona")
    assert "by_room_type" in schema["description"]
    assert "months" in schema["input_schema"]["properties"]


class _FakeRow:
    def __init__(self, mapping):
        self._mapping = mapping


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeDB:
    """Returns one canned country row, then an empty top-room-types result."""

    def __init__(self, mapping):
        self._results = [_FakeResult([_FakeRow(mapping)]), _FakeResult([])]

    def execute(self, *_args, **_kwargs):
        return self._results.pop(0)


def _country_row(**overrides):
    mapping = {
        "guest_country": "China", "guest_country_code": "CN",
        "bookings": 100, "revenue_vnd": 0,
        "lead_avg": 10.0, "lead_avg_room": 30.0, "lead_avg_dorm": 5.0,
        "los_avg": 2.0, "los_avg_room": 2.0, "los_avg_dorm": 2.0,
        "p_solo": 0, "p_couple": 0, "p_group": 0, "p_family": 0, "p_unknown": 100,
        "rt_dorm": 80, "rt_room": 20, "rt_unknown": 0,
        "lt_0_7": 60, "lt_8_30": 20, "lt_31_60": 10, "lt_60_plus": 10, "lt_unknown": 0,
        "dorm_lt_0_7": 60, "dorm_lt_8_30": 20, "dorm_lt_31_60": 0,
        "dorm_lt_60_plus": 0, "dorm_lt_unknown": 0,
        "room_lt_0_7": 0, "room_lt_8_30": 0, "room_lt_31_60": 10,
        "room_lt_60_plus": 10, "room_lt_unknown": 0,
        "g_male": 0, "g_female": 0, "g_unknown": 100,
        "age_avg": None, "a_18_24": 0, "a_25_34": 0, "a_35_44": 0,
        "a_45_54": 0, "a_55_plus": 0, "a_unknown": 100,
    }
    mapping.update(overrides)
    return mapping


def test_country_profile_splits_lead_time_by_room_type():
    out = tool_get_country_profile(_FakeDB(_country_row()), {"country": "China"}, None)
    c = out["countries"][0]
    assert c["lead_time_avg_days_dorm"] == 5.0
    assert c["lead_time_avg_days_room"] == 30.0
    assert c["bookings_dorm"] == 80 and c["bookings_room"] == 20
    # Buckets are % of that room type's own bookings (60/80), not of all 100 —
    # otherwise a Dorm-heavy country reads as if Dorm books later than it does.
    assert c["lead_time_distribution_pct_dorm"]["0_7_days"] == 75.0
    assert c["lead_time_distribution_pct_dorm"]["8_30_days"] == 25.0
    assert c["lead_time_distribution_pct_room"]["31_60_days"] == 50.0


def test_country_profile_omits_room_type_buckets_when_no_such_bookings():
    # No Dorm bookings must read as "no data", never as a row of 0%.
    row = _country_row(rt_dorm=0, rt_room=100, lead_avg_dorm=None,
                       dorm_lt_0_7=0, dorm_lt_8_30=0, room_lt_0_7=60,
                       room_lt_8_30=20, room_lt_31_60=10, room_lt_60_plus=10)
    c = tool_get_country_profile(_FakeDB(row), {"country": "Japan"}, None)["countries"][0]
    assert c["lead_time_distribution_pct_dorm"] is None
    assert c["lead_time_avg_days_dorm"] is None
    assert c["lead_time_distribution_pct_room"]["0_7_days"] == 60.0
