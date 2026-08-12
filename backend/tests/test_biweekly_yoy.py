"""Bi-Weekly report — the year-over-year comparison arithmetic.

The report carries two comparisons in every section: the prior period and the
same ISO weeks last year. The prior window is always the same length as the
reporting period, so its totals compare directly. The year-ago window is not:
the 21-day W51–W53 period of a 53-week ISO year meets a 14-day week pair in a
52-week prior year. Comparing those totals directly invents a ~33% decline,
which is the specific mistake these tests exist to prevent.
"""
from app.services.biweekly_period import period_for
from app.services.biweekly_report_builder import (
    _crm_rate_plan_totals,
    _pct_norm,
    _yoy_days,
)


class TestPctNorm:
    def test_equal_length_windows_compare_as_totals(self):
        assert _pct_norm(120, 100, 14, 14) == 20.0

    def test_unequal_windows_compare_per_day_not_as_totals(self):
        """21 days of 300 against 14 days of 200 is +50% per day, not +50%
        of the totals — and emphatically not the −33% a raw total comparison
        of the same daily rate would report."""
        assert _pct_norm(300, 200, 21, 14) == 0.0        # 14.29/day vs 14.29/day
        assert _pct_norm(450, 200, 21, 14) == 50.0       # 21.43/day vs 14.29/day

    def test_a_zero_base_yields_none_rather_than_a_fake_gain(self):
        """A channel, market or campaign that did not exist a year ago has a
        zero base. The renderer draws no arrow for None; a number here would
        read as performance."""
        assert _pct_norm(50, 0, 14, 14) is None
        assert _pct_norm(50, 0, 21, 14) is None

    def test_missing_data_yields_none(self):
        assert _pct_norm(None, 100, 14, 14) is None
        assert _pct_norm(100, None, 14, 14) is None

    def test_a_degenerate_window_length_yields_none(self):
        assert _pct_norm(100, 100, 0, 14) is None
        assert _pct_norm(100, 100, 14, 0) is None


class TestYoyDays:
    def test_a_normal_period_needs_no_per_day_framing(self):
        _, days, per_day = _yoy_days(period_for(2026, 29))
        assert (days, per_day) == (14, False)

    def test_the_extended_period_meets_a_shorter_year_ago_window(self):
        """2026 is a 53-week ISO year, so P26 is W51–W53 = 21 days. 2025 had
        52 weeks, so the year-ago window clamps to W51–W52 = 14 days."""
        p = period_for(2026, 51)
        assert p.days == 21
        _, days, per_day = _yoy_days(p)
        assert (days, per_day) == (14, True)


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
        t = _crm_rate_plan_totals(period_for(2026, 29), self.ROWS)
        assert t["bookings"] == 54
        assert t["revenue"] == 70_000.0
        assert t["bookings_vs_prior_pct"] == 350.0        # 54 vs 12
        assert t["revenue_vs_prior_pct"] == 133.33        # 70k vs 30k
        assert t["bookings_vs_yoy_pct"] == 500.0          # 54 vs 9
        assert t["revenue_vs_yoy_pct"] == 250.0           # 70k vs 20k
        assert t["yoy_per_day"] is False

    def test_the_extended_period_normalises_the_year_ago_total_per_day(self):
        """This is why the totals are computed in the builder rather than
        summed in the renderer — the renderer has no way to know the year-ago
        window was seven days shorter."""
        t = _crm_rate_plan_totals(period_for(2026, 51), self.ROWS)
        assert t["yoy_per_day"] is True
        # 70000/21 = 3333.33/day against 20000/14 = 1428.57/day → +133.33%
        assert t["revenue_vs_yoy_pct"] == 133.33
        # The prior window is always the same length, so it stays a total.
        assert t["revenue_vs_prior_pct"] == 133.33

    def test_no_rows_yields_zero_totals_and_no_deltas(self):
        t = _crm_rate_plan_totals(period_for(2026, 29), [])
        assert t["bookings"] == 0
        assert t["revenue"] == 0
        assert t["revenue_vs_prior_pct"] is None
        assert t["revenue_vs_yoy_pct"] is None
