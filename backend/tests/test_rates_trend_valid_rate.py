"""Valid-booking rate on the OTA Channel Mix "By Date Booked" pivot.

The booked-basis cohort used to reuse checkin_cells — a channel's share of the
period's check-ins — which mostly restated the By Check-in Date view, because a
week's bookings have barely started arriving. valid_cells answers the question
the booked cohort is actually asked: of the bookings we took, how many still
stand once cancellations and no-shows come out. That is the same population
Cloudbeds gives you when you filter Date Booked and untick cancelled + no-show,
so the counts must line up exactly.

Rows are faked rather than read from a DB: the arithmetic and the row/period
alignment are the whole of what must not regress, and neither needs Postgres.
"""
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.services.metrics_engine import get_rates_trend


def _week_start(weeks_ago: int) -> date:
    today = date.today()
    return today - timedelta(days=today.weekday()) - timedelta(weeks=weeks_ago)


def _row(period, source, category, total, cancelled, no_show, checked_in):
    return SimpleNamespace(
        period=period, source=source, source_category=category,
        total=total, cancelled=cancelled, no_show=no_show, checked_in=checked_in,
    )


def _trend(rows, date_type="booked"):
    """Run the pivot over fixed rows. The db is a mock, so the query chain is
    inert and q.all() hands back `rows` verbatim."""
    db = MagicMock()
    db.query.return_value.filter.return_value.group_by.return_value.all.return_value = rows
    return get_rates_trend(db, None, mode="weekly", date_type=date_type)


def _channel(result, name):
    return next(c for c in result["channels"] if c["channel"] == name)


# The reference week: Cloudbeds Date Booked 10–16 Aug with cancelled + no-show
# unticked returned Ctrip 65, Booking.com 36, Agoda 28.
_LAST_WEEK = _week_start(1)
_CLOUDBEDS_WEEK = [
    _row(_LAST_WEEK, "Ctrip",        "OTA", 67, 2, 0, 33),
    _row(_LAST_WEEK, "Booking.com",  "OTA", 38, 2, 0,  4),
    _row(_LAST_WEEK, "Agoda",        "OTA", 33, 4, 1, 14),
]


def test_valid_count_matches_cloudbeds_status_filter():
    result = _trend(_CLOUDBEDS_WEEK)
    pi = result["periods"].index(f"W{_LAST_WEEK.isocalendar()[1]:02d} ({_LAST_WEEK.strftime('%m/%d')})")

    assert _channel(result, "Ctrip")["valid_cells"][pi]["valid"] == 65
    assert _channel(result, "Booking.com")["valid_cells"][pi]["valid"] == 36
    # 33 booked − 4 cancelled − 1 no-show. Dropping the no-show would give 29.
    assert _channel(result, "Agoda")["valid_cells"][pi]["valid"] == 28


def test_valid_rate_is_over_bookings_made_not_over_check_ins():
    result = _trend(_CLOUDBEDS_WEEK)
    pi = result["periods"].index(f"W{_LAST_WEEK.isocalendar()[1]:02d} ({_LAST_WEEK.strftime('%m/%d')})")
    cell = _channel(result, "Agoda")["valid_cells"][pi]

    assert cell["total"] == 33                      # the channel's own bookings
    assert cell["rate"] == round(28 / 33, 4)


def test_valid_rate_is_the_complement_of_cancel_and_no_show():
    result = _trend(_CLOUDBEDS_WEEK)
    pi = result["periods"].index(f"W{_LAST_WEEK.isocalendar()[1]:02d} ({_LAST_WEEK.strftime('%m/%d')})")
    ch = _channel(result, "Ctrip")

    assert ch["valid_cells"][pi]["rate"] + ch["cancel_cells"][pi]["rate"] == 1.0


def test_channels_do_not_share_a_denominator():
    """checkin_cells divides every channel by the period's total check-ins, so
    the column sums to 100%. valid_cells must not: each channel is judged
    against its own bookings, and all three can sit near 95%."""
    result = _trend(_CLOUDBEDS_WEEK)
    pi = result["periods"].index(f"W{_LAST_WEEK.isocalendar()[1]:02d} ({_LAST_WEEK.strftime('%m/%d')})")

    totals = {_channel(result, c)["valid_cells"][pi]["total"] for c in ("Ctrip", "Booking.com", "Agoda")}
    assert totals == {67, 38, 33}
    assert sum(_channel(result, c)["valid_cells"][pi]["rate"] for c in ("Ctrip", "Booking.com", "Agoda")) > 1.0


def test_period_with_no_bookings_reports_no_rate():
    """An empty cell must render as '—', not as a 0% valid rate that reads like
    every booking died."""
    result = _trend(_CLOUDBEDS_WEEK)
    empty = _week_start(5)
    pi = result["periods"].index(f"W{empty.isocalendar()[1]:02d} ({empty.strftime('%m/%d')})")
    cell = _channel(result, "Ctrip")["valid_cells"][pi]

    assert cell["total"] == 0
    assert cell["rate"] is None


def test_all_cell_series_stay_aligned_with_the_period_axis():
    result = _trend(_CLOUDBEDS_WEEK)
    n = len(result["periods"])
    assert n == 7
    for ch in result["channels"]:
        assert len(ch["valid_cells"]) == n
        assert len(ch["cancel_cells"]) == n
        assert len(ch["checkin_cells"]) == n


def test_check_in_basis_still_gets_the_check_in_share():
    """Only the booked tab swaps metrics — the check-in tab is untouched, and
    both series ship on both bases so the caller picks."""
    result = _trend(_CLOUDBEDS_WEEK, date_type="check_in")
    pi = result["periods"].index(f"W{_LAST_WEEK.isocalendar()[1]:02d} ({_LAST_WEEK.strftime('%m/%d')})")

    assert result["date_type"] == "check_in"
    shares = [_channel(result, c)["checkin_cells"][pi]["rate"] for c in ("Ctrip", "Booking.com", "Agoda")]
    assert abs(sum(shares) - 1.0) < 0.0001          # 33 + 4 + 14 check-ins
    assert _channel(result, "Ctrip")["valid_cells"][pi]["valid"] == 65


def test_direct_is_rolled_up_and_carries_a_valid_rate():
    rows = _CLOUDBEDS_WEEK + [
        _row(_LAST_WEEK, "Website",  "Direct", 60, 18, 2, 20),
        _row(_LAST_WEEK, "Walk-in",  "Direct", 40, 12, 0, 15),
    ]
    result = _trend(rows)
    pi = result["periods"].index(f"W{_LAST_WEEK.isocalendar()[1]:02d} ({_LAST_WEEK.strftime('%m/%d')})")
    cell = _channel(result, "Direct")["valid_cells"][pi]

    assert cell["total"] == 100
    assert cell["valid"] == 100 - 30 - 2
    assert cell["rate"] == round(68 / 100, 4)


def test_no_show_spellings_are_fully_covered():
    """The valid rate is 100% minus these two buckets, so a status spelling that
    matches neither list silently counts as a booking that still stands. The
    query used to look for "no_show"/"noshow" only, missing the spaced and
    hyphenated forms the rest of the engine already excludes."""
    from app.services.metrics_engine import (
        CANCELLED_STATUSES, EXCLUDED_STATUSES, NO_SHOW_STATUSES,
    )

    assert set(CANCELLED_STATUSES) | set(NO_SHOW_STATUSES) == EXCLUDED_STATUSES
    assert not set(CANCELLED_STATUSES) & set(NO_SHOW_STATUSES)
