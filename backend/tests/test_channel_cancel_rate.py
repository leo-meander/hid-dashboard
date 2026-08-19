"""Cancel rate split by booking channel.

Before this, the only cancel rate HiD could serve was the blended one on
daily_metrics: no channel dimension, and its two halves counted on different
date bases — new_bookings by booking date, cancellations by check-in date — so
dividing one by the other compares a period's bookings against a different
period's arrivals. Asked for the cancel rate of guests who booked on the
website, the assistant had nothing to answer with and said so.

reservations carries source and status on the same row, so the split is a
grouping, not a new pipeline. What must not regress is the arithmetic, the
cohort (numerator and denominator on one date basis, one window), and Website
surviving as its own row instead of dissolving into "Direct".

The db is faked: the query is built and inspected, never executed, because the
grouping and the rates are the whole of what these assert and neither needs
Postgres.
"""
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from app.services import chat_tools
from app.services.metrics_engine import get_channel_rates


# ── Fake query plumbing ─────────────────────────────────────────────────────

class _FakeQuery:
    def __init__(self, rows, seen):
        self._rows = rows
        self._seen = seen

    def filter(self, *criteria):
        self._seen["filters"].extend(str(c) for c in criteria)
        return self

    def group_by(self, *cols):
        return self

    def all(self):
        return self._rows


class _FakeDB:
    """Records the SQL text of the columns and filters, hands back fixed rows."""

    def __init__(self, rows):
        self._rows = rows
        self.seen = {"columns": [], "filters": []}

    def query(self, *cols):
        self.seen["columns"] = [str(c) for c in cols]
        return _FakeQuery(self._rows, self.seen)


def _row(source, category, total, cancelled, no_show, checked_in):
    return SimpleNamespace(
        source=source, source_category=category,
        total=total, cancelled=cancelled, no_show=no_show, checked_in=checked_in,
    )


def _channel(rows, name):
    return next(r for r in rows if r["channel"] == name)


_FROM, _TO = date(2026, 6, 11), date(2026, 8, 18)

# One Direct family (Website + Extension) plus an OTA, so the grouping choice
# and the per-channel rates are both visible.
_ROWS = [
    _row("Website",     "Direct", 200, 30, 6, 90),
    _row("Extension",   "Direct",  50,  1, 0, 40),
    _row("Agoda",       "OTA",    100, 25, 5, 40),
]


# ── The rates themselves ────────────────────────────────────────────────────

def test_cancel_rate_is_cancellations_over_that_channels_own_bookings():
    rows = get_channel_rates(_FakeDB(_ROWS), None, _FROM, _TO, group_by="source")

    website = _channel(rows, "Website")
    assert website["total"] == 200
    assert website["cancelled"] == 30
    assert website["cancel_rate"] == 0.15          # 30 / 200, not 30 / 350
    assert _channel(rows, "Agoda")["cancel_rate"] == 0.25


def test_valid_bookings_drop_cancellations_and_no_shows():
    rows = get_channel_rates(_FakeDB(_ROWS), None, _FROM, _TO, group_by="source")

    website = _channel(rows, "Website")
    assert website["valid"] == 200 - 30 - 6
    assert website["valid_rate"] == 0.82


def test_channels_are_ordered_by_size():
    rows = get_channel_rates(_FakeDB(_ROWS), None, _FROM, _TO, group_by="source")
    assert [r["channel"] for r in rows] == ["Website", "Agoda", "Extension"]


# ── Grouping ────────────────────────────────────────────────────────────────

def test_source_grouping_keeps_website_apart_from_the_rest_of_direct():
    rows = get_channel_rates(_FakeDB(_ROWS), None, _FROM, _TO, group_by="source")
    assert {r["channel"] for r in rows} == {"Website", "Extension", "Agoda"}


def test_channel_grouping_rolls_direct_up():
    rows = get_channel_rates(_FakeDB(_ROWS), None, _FROM, _TO, group_by="channel")

    assert {r["channel"] for r in rows} == {"Direct", "Agoda"}
    direct = _channel(rows, "Direct")
    assert direct["total"] == 250
    assert direct["cancelled"] == 31


def test_a_source_less_booking_is_not_dropped():
    rows = get_channel_rates(
        _FakeDB([_row(None, "OTA", 10, 2, 0, 5)]), None, _FROM, _TO, group_by="source"
    )
    assert _channel(rows, "Unknown")["total"] == 10


# ── The query it builds ─────────────────────────────────────────────────────

def test_reservation_basis_windows_on_the_booking_date():
    db = _FakeDB(_ROWS)
    get_channel_rates(db, None, _FROM, _TO, date_basis="reservation")

    windowed = " ".join(db.seen["filters"])
    assert "reservations.reservation_date" in windowed
    assert "reservations.check_in_date" not in windowed


def test_checkin_basis_is_the_default_and_windows_on_arrival():
    db = _FakeDB(_ROWS)
    get_channel_rates(db, None, _FROM, _TO)

    windowed = " ".join(db.seen["filters"])
    assert "reservations.check_in_date" in windowed
    assert "reservations.reservation_date" not in windowed


def test_status_matching_is_case_insensitive():
    """Cloudbeds writes "Cancelled" as well as "cancelled". A case-sensitive IN
    counted the capitalised ones as bookings that still stand."""
    db = _FakeDB(_ROWS)
    get_channel_rates(db, None, _FROM, _TO)

    status_cols = [c for c in db.seen["columns"] if "reservations.status" in c]
    assert len(status_cols) == 3  # cancelled, no-show, checked-in
    assert all("lower(" in c for c in status_cols)


def test_source_exclusion_survives_a_null_source():
    """lower(NULL) is NULL, so an un-coalesced NOT IN threw source-less bookings
    out of the denominator instead of excluding only House Use / Maintenance."""
    db = _FakeDB(_ROWS)
    get_channel_rates(db, None, _FROM, _TO)

    exclusion = next(f for f in db.seen["filters"] if "NOT IN" in f and "source" in f)
    assert "coalesce" in exclusion.lower()


# ── The assistant-facing tool ───────────────────────────────────────────────

@pytest.fixture
def calls(monkeypatch):
    """Capture the windows the tool asks the engine for, per call."""
    seen = []

    def fake(db, branch_id, date_from, date_to, **kwargs):
        seen.append({"date_from": date_from, "date_to": date_to, **kwargs})
        return _ENGINE_OUT[len(seen) - 1]

    monkeypatch.setattr(chat_tools, "get_channel_rates", fake)
    return seen


def _engine_row(channel, total, cancelled, no_show=0, checked_in=0, category="Direct"):
    valid = total - cancelled - no_show
    non_cancelled = total - cancelled
    return {
        "channel": channel, "category": category, "total": total,
        "cancelled": cancelled, "no_show": no_show, "checked_in": checked_in,
        "confirmed": max(0, non_cancelled - no_show - checked_in), "valid": valid,
        "cancel_rate": round(cancelled / total, 4) if total else 0,
        "valid_rate": round(valid / total, 4) if total else 0,
        "checkin_rate": round(checked_in / non_cancelled, 4) if non_cancelled else 0,
        "noshow_rate": round(no_show / non_cancelled, 4) if non_cancelled else 0,
    }


# [current window, prior window]
_ENGINE_OUT = [
    [_engine_row("Website", 200, 30, no_show=6, checked_in=90),
     _engine_row("Klook", 10, 1, category="Local travel agency")],
    [_engine_row("Website", 250, 25, no_show=5, checked_in=200)],
]


def test_the_prior_window_is_the_same_length_immediately_before(calls):
    chat_tools.tool_get_channel_rates(
        None, {"date_from": "2026-06-11", "date_to": "2026-08-18"}, None
    )

    current, prior = calls
    assert (current["date_from"], current["date_to"]) == (date(2026, 6, 11), date(2026, 8, 18))
    assert prior["date_to"] == date(2026, 6, 10)
    length = (current["date_to"] - current["date_from"]).days
    assert (prior["date_to"] - prior["date_from"]).days == length


def test_both_windows_are_read_on_the_same_basis(calls):
    chat_tools.tool_get_channel_rates(None, {"date_basis": "checkin"}, None)
    assert [c["date_basis"] for c in calls] == ["checkin", "checkin"]


def test_change_is_reported_in_percentage_points(calls):
    out = chat_tools.tool_get_channel_rates(
        None, {"date_from": "2026-06-11", "date_to": "2026-08-18"}, None
    )

    website = _channel(out["channels"], "Website")
    assert website["cancel_rate_pct"] == 15.0
    assert website["prior_cancel_rate_pct"] == 10.0
    assert website["cancel_rate_delta_pp"] == 5.0


def test_a_channel_absent_last_period_has_no_prior_rate(calls):
    out = chat_tools.tool_get_channel_rates(None, {}, None)

    klook = _channel(out["channels"], "Klook")
    assert klook["prior_bookings"] == 0
    assert klook["prior_cancel_rate_pct"] is None
    assert klook["cancel_rate_delta_pp"] is None


def test_totals_span_every_channel(calls):
    out = chat_tools.tool_get_channel_rates(None, {}, None)

    assert out["current_period"]["bookings"] == 210
    assert out["current_period"]["cancelled"] == 31
    assert out["current_period"]["cancel_rate_pct"] == 14.76
    assert out["prior_period"]["cancel_rate_pct"] == 10.0
    assert out["cancel_rate_delta_pp"] == 4.76


def test_booked_basis_withholds_check_in_rate(calls):
    """A cohort that has barely started arriving has a check-in rate near zero.
    Reported next to the cancel rate it reads as a collapse that never happened."""
    out = chat_tools.tool_get_channel_rates(None, {}, None)

    assert out["date_basis"] == "reservation_date"
    assert "checkin_rate_pct" not in _channel(out["channels"], "Website")


def test_check_in_basis_reports_check_in_rate(calls):
    out = chat_tools.tool_get_channel_rates(None, {"date_basis": "checkin"}, None)

    assert out["date_basis"] == "check_in_date"
    assert _channel(out["channels"], "Website")["checkin_rate_pct"] == 52.94


def test_the_default_window_is_thirty_days_against_the_thirty_before(calls):
    chat_tools.tool_get_channel_rates(None, {}, None)

    current, prior = calls
    assert (current["date_to"] - current["date_from"]).days == 29
    assert prior["date_to"] == current["date_from"] - timedelta(days=1)


def test_the_tool_is_reachable_by_name():
    assert chat_tools.TOOL_HANDLERS["get_channel_rates"] is chat_tools.tool_get_channel_rates
    assert any(d["name"] == "get_channel_rates" for d in chat_tools.TOOL_DEFS)
