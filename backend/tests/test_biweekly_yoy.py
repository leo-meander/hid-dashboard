"""Bi-Weekly report — the comparison arithmetic behind both reference windows.

Every section carries two comparisons on the same calendar dates: one month
back (MoM) and one year back (YoY). NEITHER is guaranteed to be the same
length as the reporting period:

  * a second half runs 15–EOM, so 15–31 Mar (17 days) meets 15–28 Feb (14)
    one month back;
  * a leap February shifts the year-ago window by a day.

Comparing those totals head-on manufactures a decline out of the calendar —
~18% for the March/February case — which is the specific mistake these tests
exist to prevent. The old ISO-week scheme had the same hazard in one place
(a 21-day week-triple against 14 days); the calendar scheme has it in two, so
both `_yoy_days` and `_mom_days` are pinned here.
"""
from app.services.biweekly_period import period_for
from app.services.biweekly_report_builder import (
    _crm_rate_plan_totals,
    _mom_days,
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


class TestMomDays:
    def test_equal_length_months_need_no_per_day_framing(self):
        # Aug and Jul are both 31 days, so 15–31 meets 15–31.
        _, days, per_day = _mom_days(period_for(2026, 8, 2))
        assert (days, per_day) == (17, False)

    def test_first_halves_are_always_directly_comparable(self):
        for month in range(1, 13):
            _, days, per_day = _mom_days(period_for(2026, month, 1))
            assert (days, per_day) == (14, False)

    def test_a_short_previous_february_forces_per_day(self):
        """15–31 Mar (17 days) against 15–28 Feb (14). Left as totals this
        reads as an ~18% collapse that never happened."""
        p = period_for(2027, 3, 2)
        assert p.days == 17
        _, days, per_day = _mom_days(p)
        assert (days, per_day) == (14, True)

    def test_february_compared_against_a_longer_january(self):
        p = period_for(2027, 2, 2)
        assert p.days == 14
        _, days, per_day = _mom_days(p)
        assert (days, per_day) == (17, True)


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
        assert t["mom_per_day"] is False

    def test_a_short_month_back_window_normalises_the_total_per_day(self):
        """This is why the totals are computed in the builder rather than
        summed in the renderer — the renderer has no way to know February was
        three days shorter."""
        t = _crm_rate_plan_totals(period_for(2027, 3, 2), self.ROWS)
        assert t["mom_per_day"] is True
        # 70000/17 = 4117.65/day against 30000/14 = 2142.86/day → +92.16%
        assert t["revenue_vs_prior_pct"] == 92.16
        # The year-ago window IS the same length (15–31 Mar 2026 is also 17
        # days), so that one stays a straight total.
        assert t["yoy_per_day"] is False
        assert t["revenue_vs_yoy_pct"] == 250.0

    def test_no_rows_yields_zero_totals_and_no_deltas(self):
        t = _crm_rate_plan_totals(period_for(2026, 8, 1), [])
        assert t["bookings"] == 0
        assert t["revenue"] == 0
        assert t["revenue_vs_prior_pct"] is None
        assert t["revenue_vs_yoy_pct"] is None
