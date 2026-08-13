"""The occupancy-basis booking count, read off `reservations`.

Markets and Channel Mix count "bookings with at least one night in the period"
from `reservation_daily`. That table holds no nights before 2026, which is why
their year-over-year columns were blank — but a booking COUNT does not need the
one thing that table uniquely holds (Cloudbeds' actual per-night rate), so the
same population is reachable from `reservations` via a date-overlap predicate.

These tests pin the predicate against the loop that writes reservation_daily
(`cloudbeds.populate_reservation_daily`): `current = check_in_date; while
current < check_out_date` — so nights run check_in .. check_out−1, and the
off-by-one at each edge is the whole risk. An overlap that is one day loose
counts a booking that never slept in the period; one day tight drops a
same-day check-out.
"""
from datetime import date, timedelta

import pytest

from app.services.biweekly_report_builder import _stay_overlaps


def _nights(check_in: date, check_out: date) -> list:
    """Exactly the nights populate_reservation_daily would write."""
    out, cur = [], check_in
    while cur < check_out:
        out.append(cur)
        cur += timedelta(days=1)
    return out


def _predicate_says_overlap(check_in: date, check_out: date,
                            d_from: date, d_to: date) -> bool:
    """Evaluate `_stay_overlaps` in Python.

    It returns SQLAlchemy clauses against columns; the comparisons it encodes
    are `check_in_date <= d_to`, `check_out_date > d_from` and
    `check_out_date > check_in_date`, so mirroring them here keeps the test
    honest about WHAT is asserted while staying a unit test.
    `test_the_predicate_is_the_comparisons_claimed` guards the mirror.
    """
    return check_in <= d_to and check_out > d_from and check_out > check_in


def _truth(check_in: date, check_out: date, d_from: date, d_to: date) -> bool:
    """Ground truth: does any written night fall inside the window?"""
    return any(d_from <= n <= d_to for n in _nights(check_in, check_out))


W_FROM, W_TO = date(2026, 7, 27), date(2026, 8, 9)      # a real period, W31–32


class TestPredicateMatchesReservationDaily:
    @pytest.mark.parametrize("check_in,check_out,expected", [
        # Wholly before: checks out the morning the window opens, so its last
        # night is the 26th. Must NOT count.
        (date(2026, 7, 20), date(2026, 7, 27), False),
        # Last night is the 26th, one day earlier still.
        (date(2026, 7, 20), date(2026, 7, 26), False),
        # Checks out on the 28th → nights 27th (in) — counts.
        (date(2026, 7, 26), date(2026, 7, 28), True),
        # Arrives the last day of the window, one night — counts.
        (date(2026, 8, 9), date(2026, 8, 10), True),
        # Arrives the day AFTER the window closes — must not count.
        (date(2026, 8, 10), date(2026, 8, 12), False),
        # Spans the whole window and out both sides.
        (date(2026, 7, 1), date(2026, 9, 1), True),
        # Entirely inside.
        (date(2026, 8, 1), date(2026, 8, 4), True),
    ])
    def test_edges(self, check_in, check_out, expected):
        assert _truth(check_in, check_out, W_FROM, W_TO) is expected
        assert _predicate_says_overlap(check_in, check_out, W_FROM, W_TO) is expected

    def test_agrees_with_reservation_daily_across_a_sweep(self):
        """Every stay start/length around the window, checked against the
        nights populate_reservation_daily would actually write."""
        for offset in range(-20, 20):
            for length in range(1, 12):
                ci = W_FROM + timedelta(days=offset)
                co = ci + timedelta(days=length)
                assert (
                    _predicate_says_overlap(ci, co, W_FROM, W_TO)
                    == _truth(ci, co, W_FROM, W_TO)
                ), f"disagreement for {ci}..{co}"

    @pytest.mark.parametrize("day", [
        date(2026, 8, 1),      # inside the window
        date(2026, 7, 27),     # its first day
        date(2026, 8, 9),      # its last day
    ])
    def test_a_zero_night_booking_never_counts(self, day):
        """check_out == check_in writes no nights at all, and the data does
        carry such rows — `populate_reservation_daily` skips `nights <= 0`
        outright. The two range comparisons alone are both satisfied by one
        sitting inside the window, so the predicate needs its third clause;
        this test is what caught that."""
        assert _nights(day, day) == []
        assert _truth(day, day, W_FROM, W_TO) is False
        assert _predicate_says_overlap(day, day, W_FROM, W_TO) is False

    def test_a_backwards_booking_never_counts(self):
        """check_out before check_in is corrupt rather than real, but it exists
        in booking data and must not be counted on one side of a comparison
        and not the other."""
        ci, co = date(2026, 8, 5), date(2026, 8, 2)
        assert _nights(ci, co) == []
        assert _predicate_says_overlap(ci, co, W_FROM, W_TO) is False

    def test_the_predicate_is_the_comparisons_claimed(self):
        """Guards the Python mirror above: if `_stay_overlaps` grows a clause or
        flips an operator, the mirror stops representing it and every test in
        this file quietly stops testing the real thing."""
        clauses = _stay_overlaps(W_FROM, W_TO)
        assert len(clauses) == 3
        rendered = " ".join(str(c) for c in clauses)
        assert "check_in_date <=" in rendered
        assert "check_out_date >" in rendered
        assert rendered.count("check_out_date >") == 2   # window edge + sanity
