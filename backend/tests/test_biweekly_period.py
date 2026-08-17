"""Bi-weekly reporting period arithmetic.

The period math is pure and easy to get subtly wrong — month lengths vary,
February moves, and the two comparison windows have to land on the SAME
calendar dates one month and one year back rather than on "the window before
this one". A silent off-by-one here would mean a branch manager reads a
report labelled "Aug 15–31" that actually holds different dates, or a
month-over-month figure quietly computed against the wrong fortnight, so
these cases are pinned rather than left to inspection.
"""
from datetime import date, timedelta

import pytest

from app.services.biweekly_period import (
    Period,
    comparable_as_totals,
    current_period,
    days_in_month,
    is_complete,
    list_periods,
    mom_window,
    next_period,
    parse_period_key,
    period_containing,
    period_for,
    previous_period,
    shift_month,
    window_days,
    yoy_window,
)


class TestDaysInMonth:
    def test_month_lengths_are_read_from_the_calendar(self):
        assert days_in_month(2026, 8) == 31
        assert days_in_month(2026, 9) == 30
        assert days_in_month(2027, 2) == 28
        assert days_in_month(2028, 2) == 29      # leap year


class TestShiftMonth:
    def test_wraps_the_year_in_both_directions(self):
        assert shift_month(2026, 1, -1) == (2025, 12)
        assert shift_month(2026, 12, 1) == (2027, 1)
        assert shift_month(2026, 8, 0) == (2026, 8)

    def test_multi_month_jumps(self):
        assert shift_month(2026, 11, 3) == (2027, 2)
        assert shift_month(2026, 2, -14) == (2024, 12)


class TestPeriodFor:
    def test_first_half_is_always_the_1st_to_the_14th(self):
        for month in range(1, 13):
            p = period_for(2026, month, 1)
            assert p.start.day == 1
            assert p.end.day == 14
            assert p.days == 14
            assert not p.is_extended

    def test_second_half_runs_to_the_real_last_day(self):
        assert period_for(2026, 8, 2).end == date(2026, 8, 31)   # 31-day month
        assert period_for(2026, 9, 2).end == date(2026, 9, 30)   # 30-day month
        assert period_for(2027, 2, 2).end == date(2027, 2, 28)   # short Feb
        assert period_for(2028, 2, 2).end == date(2028, 2, 29)   # leap Feb

    def test_february_second_half_is_a_plain_14_days(self):
        p = period_for(2027, 2, 2)
        assert p.days == 14
        assert not p.is_extended

    def test_key_and_labels(self):
        p = period_for(2026, 8, 2)
        assert p.key == "2026-08-H2"
        assert p.days == 17
        assert p.is_extended
        assert p.date_label == "Aug 15–31, 2026"
        assert "Aug" in p.label and "2026" in p.label

    def test_the_two_halves_tile_the_month_exactly(self):
        # The whole reason for this framing: H1 + H2 is the month, so a
        # monthly revenue target needs no proration to reconcile.
        for year, month in ((2026, 8), (2026, 9), (2027, 2), (2028, 2)):
            h1, h2 = period_for(year, month, 1), period_for(year, month, 2)
            assert h1.start == date(year, month, 1)
            assert h1.end + timedelta(days=1) == h2.start
            assert h2.end == date(year, month, days_in_month(year, month))
            assert h1.days + h2.days == days_in_month(year, month)

    def test_rejects_bad_months_and_halves(self):
        for bad_month in (0, 13, -1):
            with pytest.raises(ValueError):
                period_for(2026, bad_month, 1)
        for bad_half in (0, 3, -1):
            with pytest.raises(ValueError):
                period_for(2026, 8, bad_half)


class TestPeriodContaining:
    def test_the_14th_and_the_15th_are_the_split(self):
        assert period_containing(date(2026, 8, 14)).key == "2026-08-H1"
        assert period_containing(date(2026, 8, 15)).key == "2026-08-H2"

    def test_every_day_of_a_year_lands_in_exactly_one_period(self):
        d = date(2026, 1, 1)
        while d < date(2027, 1, 1):
            p = period_containing(d)
            assert p.start <= d <= p.end
            d += timedelta(days=1)


class TestCurrentPeriod:
    def test_the_15th_reports_on_the_first_half(self):
        # Report 1 is sent on the 15th and covers 1–14 of the same month.
        p = current_period(date(2026, 8, 15))
        assert p.key == "2026-08-H1"
        assert (p.start, p.end) == (date(2026, 8, 1), date(2026, 8, 14))

    def test_the_last_day_reports_on_the_second_half(self):
        # Report 2 is sent on the last calendar day and covers 15–EOM. It has
        # to exist ON that day, even though the day is still running.
        p = current_period(date(2026, 8, 31))
        assert p.key == "2026-08-H2"
        assert (p.start, p.end) == (date(2026, 8, 15), date(2026, 8, 31))

    def test_february_sends_on_its_own_last_day(self):
        assert current_period(date(2027, 2, 28)).key == "2027-02-H2"
        assert current_period(date(2028, 2, 29)).key == "2028-02-H2"

    def test_mid_period_falls_back_to_the_completed_one(self):
        # On the 20th, 15–31 is only a third done — reporting it would read
        # as a collapse. The last finished period is 1–14.
        assert current_period(date(2026, 8, 20)).key == "2026-08-H1"
        assert current_period(date(2026, 8, 5)).key == "2026-07-H2"

    def test_never_returns_a_period_that_has_not_started(self):
        d = date(2026, 1, 1)
        while d < date(2028, 1, 1):
            assert current_period(d).end <= d
            d += timedelta(days=1)


class TestIsComplete:
    def test_only_true_once_the_final_day_has_passed(self):
        p = period_for(2026, 8, 2)
        assert not is_complete(p, date(2026, 8, 31))    # send day, still running
        assert is_complete(p, date(2026, 9, 1))


class TestNeighbours:
    def test_previous_within_and_across_months(self):
        assert previous_period(period_for(2026, 8, 2)).key == "2026-08-H1"
        assert previous_period(period_for(2026, 8, 1)).key == "2026-07-H2"
        assert previous_period(period_for(2026, 1, 1)).key == "2025-12-H2"

    def test_next_crosses_the_year(self):
        assert next_period(period_for(2026, 12, 2)).key == "2027-01-H1"

    def test_previous_and_next_are_inverse(self):
        for month in range(1, 13):
            for half in (1, 2):
                p = period_for(2026, month, half)
                assert previous_period(next_period(p)).key == p.key

    def test_neighbours_are_contiguous(self):
        p = period_for(2026, 3, 1)
        assert previous_period(p).end + timedelta(days=1) == p.start
        assert next_period(p).start == p.end + timedelta(days=1)


class TestMoMWindow:
    def test_first_half_lands_on_the_same_dates_last_month(self):
        # 1–14 Aug 2026 → 1–14 Jul 2026, per the reporting spec.
        p = period_for(2026, 8, 1)
        assert mom_window(p) == (date(2026, 7, 1), date(2026, 7, 14))

    def test_second_half_lands_on_the_same_dates_last_month(self):
        # 15–31 Aug 2026 → 15–31 Jul 2026. Both months are 31 days, so this
        # is a straight 17-vs-17 comparison.
        p = period_for(2026, 8, 2)
        w = mom_window(p)
        assert w == (date(2026, 7, 15), date(2026, 7, 31))
        assert comparable_as_totals(p, w)

    def test_never_the_immediately_preceding_period(self):
        # The one thing the spec explicitly forbids: 15–31 Aug must NOT be
        # compared against 1–14 Aug.
        p = period_for(2026, 8, 2)
        assert mom_window(p) != (previous_period(p).start, previous_period(p).end)

    def test_crosses_the_year_boundary(self):
        p = period_for(2026, 1, 2)
        assert mom_window(p) == (date(2025, 12, 15), date(2025, 12, 31))

    def test_february_example_from_the_spec(self):
        # 15–28 Feb 2027 → 15–28 Jan 2027. January is longer, so the window
        # is NOT clamped to Feb's length — it is January's own second half.
        p = period_for(2027, 2, 2)
        w = mom_window(p)
        assert w == (date(2027, 1, 15), date(2027, 1, 31))
        assert window_days(w) == 17
        assert p.days == 14
        assert not comparable_as_totals(p, w)

    def test_short_previous_month_shortens_the_window(self):
        # 15–31 Mar (17 days) against 15–28 Feb (14). Comparing these as
        # totals would invent an ~18% decline out of the calendar.
        p = period_for(2027, 3, 2)
        w = mom_window(p)
        assert w == (date(2027, 2, 15), date(2027, 2, 28))
        assert not comparable_as_totals(p, w)


class TestYoYWindow:
    def test_same_calendar_dates_previous_year(self):
        assert yoy_window(period_for(2026, 8, 1)) == (
            date(2025, 8, 1), date(2025, 8, 14))
        assert yoy_window(period_for(2026, 8, 2)) == (
            date(2025, 8, 15), date(2025, 8, 31))

    def test_same_length_for_every_ordinary_period(self):
        for month in range(1, 13):
            for half in (1, 2):
                p = period_for(2027, month, half)      # 2027: no leap Feb
                assert comparable_as_totals(p, yoy_window(p))

    def test_leap_february_is_the_one_mismatch(self):
        # 15–29 Feb 2028 meets 15–28 Feb 2027: 15 days against 14.
        p = period_for(2028, 2, 2)
        w = yoy_window(p)
        assert p.days == 15
        assert window_days(w) == 14
        assert not comparable_as_totals(p, w)


class TestSpecExamples:
    """The worked examples in the reporting spec, verbatim."""

    @pytest.mark.parametrize("send,key,window", [
        (date(2026, 8, 15), "2026-08-H1", (date(2026, 8, 1), date(2026, 8, 14))),
        (date(2026, 8, 31), "2026-08-H2", (date(2026, 8, 15), date(2026, 8, 31))),
        (date(2026, 9, 15), "2026-09-H1", (date(2026, 9, 1), date(2026, 9, 14))),
        (date(2026, 9, 30), "2026-09-H2", (date(2026, 9, 15), date(2026, 9, 30))),
        (date(2027, 2, 15), "2027-02-H1", (date(2027, 2, 1), date(2027, 2, 14))),
        (date(2027, 2, 28), "2027-02-H2", (date(2027, 2, 15), date(2027, 2, 28))),
    ])
    def test_send_date_maps_to_the_documented_period(self, send, key, window):
        p = current_period(send)
        assert p.key == key
        assert (p.start, p.end) == window

    def test_documented_comparison_windows(self):
        aug_h1 = period_for(2026, 8, 1)
        assert mom_window(aug_h1) == (date(2026, 7, 1), date(2026, 7, 14))
        assert yoy_window(aug_h1) == (date(2025, 8, 1), date(2025, 8, 14))

        aug_h2 = period_for(2026, 8, 2)
        assert mom_window(aug_h2) == (date(2026, 7, 15), date(2026, 7, 31))
        assert yoy_window(aug_h2) == (date(2025, 8, 15), date(2025, 8, 31))

        feb_h2 = period_for(2027, 2, 2)
        assert mom_window(feb_h2)[0] == date(2027, 1, 15)
        assert yoy_window(feb_h2) == (date(2026, 2, 15), date(2026, 2, 28))


class TestPeriodKey:
    def test_round_trip(self):
        for year in (2025, 2026, 2027):
            for month in range(1, 13):
                for half in (1, 2):
                    p = period_for(year, month, half)
                    assert parse_period_key(p.key).key == p.key

    def test_case_insensitive(self):
        assert parse_period_key("2026-08-h1").key == "2026-08-H1"

    def test_rejects_garbage(self):
        for bad in ("", "2026", "2026-08", "2026-W29", "nonsense",
                    "2026-13-H1", "2026-08-H3", "2026-08-X1"):
            with pytest.raises(ValueError):
                parse_period_key(bad)


class TestListPeriods:
    def test_newest_first_and_contiguous(self):
        periods = list_periods(date(2026, 8, 20), back=5)
        assert [p.key for p in periods] == [
            "2026-08-H1", "2026-07-H2", "2026-07-H1",
            "2026-06-H2", "2026-06-H1",
        ]
        for newer, older in zip(periods, periods[1:]):
            assert older.end + timedelta(days=1) == newer.start

    def test_walks_back_across_the_year_boundary(self):
        keys = [p.key for p in list_periods(date(2026, 1, 20), back=3)]
        assert keys == ["2026-01-H1", "2025-12-H2", "2025-12-H1"]


class TestSerialisation:
    def test_to_dict_shape(self):
        d = period_for(2026, 8, 2).to_dict()
        assert d["key"] == "2026-08-H2"
        assert d["start"] == "2026-08-15"
        assert d["end"] == "2026-08-31"
        assert d["days"] == 17
        assert (d["year"], d["month"], d["half"]) == (2026, 8, 2)
        assert d["is_extended"] is True

    def test_period_is_hashable_and_immutable(self):
        p = period_for(2026, 8, 1)
        assert isinstance(p, Period)
        assert {p, period_for(2026, 8, 1)} == {p}
        with pytest.raises(Exception):
            p.half = 2
