"""Bi-Weekly report — the comparison arithmetic behind both reference windows.

Every section carries two comparisons, built on different rules: the
preceding window (the period's own length, immediately before it) and the
same calendar dates a year back.

The preceding window is equal-length by construction. The year-ago one is
not — a 15-day 15–29 Feb 2028 meets a 14-day 15–28 Feb 2027 — and comparing
those totals head-on manufactures a decline out of the calendar, which is the
specific mistake these tests exist to prevent.

Both go through `_pct_norm` even though only one can currently trip it, so
these pin the invariant as well as the arithmetic.
"""
from app.services.biweekly_period import period_for
from app.services.biweekly_report_builder import (
    _crm_rate_plan_totals,
    _prior_days,
    _pct_norm,
    _yoy_days,
)


class TestPctNorm:
    def test_equal_length_windows_compare_as_totals(self):
        assert _pct_norm(120, 100, 14, 14) == 20.0

    def test_unequal_windows_compare_per_day_not_as_totals(self):
        """17 days of 170 against 14 days of 140 is flat per day — and
        emphatically not the +21% a raw total comparison would report."""
        assert _pct_norm(170, 140, 17, 14) == 0.0        # 10/day vs 10/day
        assert _pct_norm(255, 140, 17, 14) == 50.0       # 15/day vs 10/day

    def test_the_shorter_direction_too(self):
        """14 days against a 17-day month-back window: the period is the
        SHORT side here, which the old scheme could never produce."""
        assert _pct_norm(140, 170, 14, 17) == 0.0
        assert _pct_norm(210, 170, 14, 17) == 50.0

    def test_a_zero_base_yields_none_rather_than_a_fake_gain(self):
        """A channel, market or campaign that did not exist a year ago has a
        zero base. The renderer draws no arrow for None; a number here would
        read as performance."""
        assert _pct_norm(50, 0, 14, 14) is None
        assert _pct_norm(50, 0, 17, 14) is None

    def test_missing_data_yields_none(self):
        assert _pct_norm(None, 100, 14, 14) is None
        assert _pct_norm(100, None, 14, 14) is None

    def test_a_degenerate_window_length_yields_none(self):
        assert _pct_norm(100, 100, 0, 14) is None
        assert _pct_norm(100, 100, 14, 0) is None


class TestYoyDays:
    def test_an_ordinary_period_needs_no_per_day_framing(self):
        _, days, per_day = _yoy_days(period_for(2026, 8, 2))
        assert (days, per_day) == (17, False)

    def test_a_leap_february_meets_a_shorter_year_ago_window(self):
        """15–29 Feb 2028 is 15 days; 15–28 Feb 2027 is 14."""
        p = period_for(2028, 2, 2)
        assert p.days == 15
        _, days, per_day = _yoy_days(p)
        assert (days, per_day) == (14, True)


class TestPriorDays:
    def test_the_preceding_window_never_needs_per_day_framing(self):
        """It is the period's own length taken backwards, so it cannot come
        out a different size — in any month, in any year. The helper still
        computes the flag rather than hard-coding False, so redefining the
        window cannot silently start comparing unequal totals."""
        for year in (2027, 2028):          # includes a leap February
            for month in range(1, 13):
                for half in (1, 2):
                    p = period_for(year, month, half)
                    _, days, per_day = _prior_days(p)
                    assert (days, per_day) == (p.days, False)


class TestCrmRatePlanTotals:
    ROWS = [
        {"bookings": 17, "revenue": 40_000.0, "prior_bookings": 12,
         "prior_revenue": 30_000.0, "yoy_bookings": 9, "yoy_revenue": 20_000.0},
        # A campaign that ran in neither comparison window — it contributes 0
        # to both comparison totals, because it is genuinely new.
        {"bookings": 37, "revenue": 30_000.0, "prior_bookings": None,
         "prior_revenue": None, "yoy_bookings": None, "yoy_revenue": None},
    ]

    def test_sums_rows_and_computes_both_comparisons(self):
        # Aug 1–14: both reference windows are 14 days, so everything is a
        # straight total-vs-total.
        t = _crm_rate_plan_totals(period_for(2026, 8, 1), self.ROWS)
        assert t["bookings"] == 54
        assert t["revenue"] == 70_000.0
        assert t["bookings_vs_prior_pct"] == 350.0        # 54 vs 12
        assert t["revenue_vs_prior_pct"] == 133.33        # 70k vs 30k
        assert t["bookings_vs_yoy_pct"] == 500.0          # 54 vs 9
        assert t["revenue_vs_yoy_pct"] == 250.0           # 70k vs 20k
        assert t["yoy_per_day"] is False
        assert t["prior_per_day"] is False

    def test_a_leap_february_normalises_the_year_ago_total_per_day(self):
        """This is why the totals are computed in the builder rather than
        summed in the renderer — the renderer has no way to know last February
        was a day shorter."""
        t = _crm_rate_plan_totals(period_for(2028, 2, 2), self.ROWS)   # 15 days
        assert t["yoy_per_day"] is True
        # 70000/15 = 4666.67/day against 20000/14 = 1428.57/day → +226.67%
        assert t["revenue_vs_yoy_pct"] == 226.67
        # The preceding window is always equal-length, so it stays a total.
        assert t["prior_per_day"] is False
        assert t["revenue_vs_prior_pct"] == 133.33

    def test_no_rows_yields_zero_totals_and_no_deltas(self):
        t = _crm_rate_plan_totals(period_for(2026, 8, 1), [])
        assert t["bookings"] == 0
        assert t["revenue"] == 0
        assert t["revenue_vs_prior_pct"] is None
        assert t["revenue_vs_yoy_pct"] is None
