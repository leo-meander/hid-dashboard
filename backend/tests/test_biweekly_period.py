"""Bi-weekly reporting period arithmetic.

The period math is pure and easy to get subtly wrong — ISO years do not
line up with calendar years, and roughly one year in seven has 53 weeks.
A silent off-by-one here would mean a branch manager reads a report labelled
"Week 29–30" that actually contains different dates, so these cases are
pinned rather than left to inspection.
"""
from datetime import date, timedelta

import pytest

from app.services.biweekly_period import (
    Period,
    comparable_as_totals,
    current_period,
    iso_weeks_in_year,
    list_periods,
    next_period,
    parse_period_key,
    period_containing,
    period_for,
    previous_period,
    window_days,
    yoy_window,
)


class TestIsoWeeksInYear:
    def test_53_week_years(self):
        # An ISO year has 53 weeks when 1 Jan is a Thursday, or a Wednesday
        # in a leap year. 2026 is the one that matters right now.
        assert iso_weeks_in_year(2026) == 53
        assert iso_weeks_in_year(2020) == 53
        assert iso_weeks_in_year(2015) == 53

    def test_52_week_years(self):
        assert iso_weeks_in_year(2025) == 52
        assert iso_weeks_in_year(2027) == 52


class TestPeriodFor:
    def test_weeks_and_dates(self):
        p = period_for(2026, 29)
        assert (p.week_a, p.week_b) == (29, 30)
        assert p.start == date(2026, 7, 13)      # Monday of W29
        assert p.end == date(2026, 7, 26)        # Sunday of W30
        assert p.days == 14
        assert p.key == "2026-W29"
        assert not p.is_extended

    def test_starts_on_monday_ends_on_sunday(self):
        for week in range(1, 52, 2):
            p = period_for(2026, week)
            assert p.start.weekday() == 0
            assert p.end.weekday() == 6

    def test_rejects_even_and_out_of_range_weeks(self):
        for bad in (2, 30, 0, 52, 53, -1):
            with pytest.raises(ValueError):
                period_for(2026, bad)


class TestFiftyThreeWeekYear:
    """W53 has no partner, so it folds into the final period of the year."""

    def test_final_period_absorbs_week_53(self):
        p = period_for(2026, 51)
        assert (p.week_a, p.week_b) == (51, 53)
        assert p.days == 21
        assert p.is_extended
        assert p.end == date(2027, 1, 3)         # Sunday of ISO 2026-W53

    def test_final_period_of_52_week_year_is_normal(self):
        p = period_for(2025, 51)
        assert (p.week_a, p.week_b) == (51, 52)
        assert p.days == 14
        assert not p.is_extended

    def test_week_53_maps_into_the_final_period(self):
        # 31 Dec 2026 is ISO 2026-W53 — it must not start a period of its own.
        assert period_containing(date(2026, 12, 31)).key == "2026-W51"

    def test_periods_tile_the_year_without_gaps(self):
        # Walk every period of a 53-week year: no gaps, no overlaps, and the
        # last one ends the ISO year.
        p = period_for(2026, 1)
        seen_end = None
        count = 0
        while True:
            count += 1
            if seen_end is not None:
                assert p.start == seen_end + timedelta(days=1)
            seen_end = p.end
            if p.week_a == 51:
                break
            p = next_period(p)
        assert count == 26
        assert seen_end == date(2027, 1, 3)


class TestCurrentPeriod:
    def test_only_returns_a_fully_completed_period(self):
        # 2026-08-07 is a Friday inside W32; W31–W32 is still running, so the
        # latest reportable period is W29–W30.
        p = current_period(date(2026, 8, 7))
        assert p.key == "2026-W29"
        assert p.start == date(2026, 7, 13)
        assert p.end == date(2026, 7, 26)

    def test_does_not_roll_on_the_final_sunday(self):
        # The period ends this very day — the day is not over, so it is not
        # yet reportable. Same rule the weekly report uses.
        assert current_period(date(2026, 7, 26)).key == "2026-W27"

    def test_rolls_the_next_morning(self):
        assert current_period(date(2026, 7, 27)).key == "2026-W29"

    def test_never_returns_a_period_containing_today(self):
        d = date(2026, 1, 1)
        while d < date(2028, 1, 1):
            assert current_period(d).end < d
            d += timedelta(days=1)


class TestNeighbours:
    def test_previous_within_year(self):
        assert previous_period(period_for(2026, 29)).key == "2026-W27"

    def test_previous_crosses_into_prior_iso_year(self):
        # Every ISO year's last period starts at W51, whether it holds two
        # weeks or three.
        assert previous_period(period_for(2026, 1)).key == "2025-W51"
        assert previous_period(period_for(2027, 1)).key == "2026-W51"

    def test_next_crosses_iso_year(self):
        assert next_period(period_for(2026, 51)).key == "2027-W01"

    def test_previous_and_next_are_inverse(self):
        for year in (2025, 2026, 2027):
            for week in range(1, 52, 2):
                p = period_for(year, week)
                assert previous_period(next_period(p)).key == p.key

    def test_previous_period_is_contiguous(self):
        p = period_for(2026, 1)
        prev = previous_period(p)
        assert prev.end + timedelta(days=1) == p.start


class TestYoYWindow:
    def test_same_week_numbers_previous_year(self):
        p = period_for(2026, 29)
        assert yoy_window(p) == (date(2025, 7, 14), date(2025, 7, 27))

    def test_yoy_window_starts_monday_ends_sunday(self):
        start, end = yoy_window(period_for(2026, 29))
        assert start.weekday() == 0 and end.weekday() == 6

    def test_yoy_is_same_length_for_normal_periods(self):
        p = period_for(2026, 29)
        assert window_days(yoy_window(p)) == p.days == 14
        assert comparable_as_totals(p, yoy_window(p))

    def test_extended_period_clamps_against_a_52_week_prior_year(self):
        # 21-day W51–W53 of 2026 vs a 2025 that only has 52 weeks: the window
        # clamps to 14 days, and callers must NOT compare the totals.
        p = period_for(2026, 51)
        w = yoy_window(p)
        assert w == (date(2025, 12, 15), date(2025, 12, 28))
        assert window_days(w) == 14
        assert p.days == 21
        assert not comparable_as_totals(p, w)


class TestPeriodKey:
    def test_round_trip(self):
        for year in (2025, 2026, 2027):
            for week in range(1, 52, 2):
                p = period_for(year, week)
                assert parse_period_key(p.key).key == p.key

    def test_zero_padded_and_case_insensitive(self):
        assert parse_period_key("2026-W01").week_a == 1
        assert parse_period_key("2026-w29").key == "2026-W29"

    def test_rejects_garbage(self):
        for bad in ("", "2026", "2026-W", "nonsense", "2026-W30", "2026-W99"):
            with pytest.raises(ValueError):
                parse_period_key(bad)


class TestListPeriods:
    def test_newest_first_and_contiguous(self):
        periods = list_periods(date(2026, 8, 7), back=6)
        assert [p.key for p in periods] == [
            "2026-W29", "2026-W27", "2026-W25",
            "2026-W23", "2026-W21", "2026-W19",
        ]
        for newer, older in zip(periods, periods[1:]):
            assert older.end + timedelta(days=1) == newer.start

    def test_walks_back_across_the_year_boundary(self):
        keys = [p.key for p in list_periods(date(2026, 1, 20), back=3)]
        assert keys == ["2026-W01", "2025-W51", "2025-W49"]


class TestSerialisation:
    def test_to_dict_shape(self):
        d = period_for(2026, 29).to_dict()
        assert d["key"] == "2026-W29"
        assert d["start"] == "2026-07-13"
        assert d["end"] == "2026-07-26"
        assert d["days"] == 14
        assert d["is_extended"] is False
        assert "Week 29" in d["label"] and "2026" in d["label"]

    def test_period_is_hashable_and_immutable(self):
        p = period_for(2026, 29)
        assert isinstance(p, Period)
        assert {p, period_for(2026, 29)} == {p}
        with pytest.raises(Exception):
            p.week_a = 31
