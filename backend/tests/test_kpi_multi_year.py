"""Year selection, comparison basis, and chained deltas for /api/kpi/multi-year.

The endpoint exists so the Revenue KPI table can put several years beside each
other. Two things make that harder than repeating the single-year query:

  * a year still running holds forward bookings in `daily_metrics`, so its
    full-year total is not comparable with a closed year's, and
  * `daily_metrics` also holds rows for periods nobody was tracking, which a
    naive year-on-year % turns into a fake surge.

Both rules live here rather than in the React table so the report and the API
answer the same way.
"""
from datetime import date

import pytest
from fastapi import HTTPException

from app.routers.kpi import (
    MAX_COMPARE_YEARS,
    _cutoff_month,
    _parse_years,
    chain_year_deltas,
)


class TestParseYears:
    def test_sorts_ascending_and_dedupes(self):
        assert _parse_years("2026,2025,2026") == [2025, 2026]

    def test_tolerates_spacing_and_trailing_comma(self):
        assert _parse_years(" 2025 , 2026 ,") == [2025, 2026]

    def test_single_year_is_valid(self):
        assert _parse_years("2026") == [2026]

    def test_rejects_non_numeric(self):
        with pytest.raises(HTTPException) as exc:
            _parse_years("2025,last")
        assert exc.value.status_code == 400

    def test_rejects_absurd_year(self):
        with pytest.raises(HTTPException):
            _parse_years("12026")

    def test_rejects_empty(self):
        with pytest.raises(HTTPException):
            _parse_years(" , ")

    def test_caps_the_number_of_columns(self):
        too_many = ",".join(str(2020 + i) for i in range(MAX_COMPARE_YEARS + 1))
        with pytest.raises(HTTPException) as exc:
            _parse_years(too_many)
        assert exc.value.status_code == 400


class TestCutoffMonth:
    """`basis=ytd` clips every compared year to the same completed months."""

    @pytest.fixture
    def today(self, monkeypatch):
        def _set(d):
            monkeypatch.setattr("app.routers.kpi.ict_today", lambda: d)
        return _set

    def test_running_year_stops_at_the_last_closed_month(self, today):
        today(date(2026, 9, 15))
        assert _cutoff_month([2025, 2026]) == 8

    def test_closed_years_only_use_the_whole_year(self, today):
        today(date(2026, 9, 15))
        assert _cutoff_month([2024, 2025]) == 12

    def test_future_year_in_the_selection_does_not_clip(self, today):
        # 2027 has no closed month at all; the running year still governs.
        today(date(2026, 9, 15))
        assert _cutoff_month([2026, 2027]) == 8

    def test_january_has_no_honest_ytd_window(self, today):
        # Nothing has closed in the running year, so clipping to "month 0"
        # would compare two empty columns. Fall back to the full year.
        today(date(2027, 1, 10))
        assert _cutoff_month([2026, 2027]) == 12


def _cell(actual, has_kpi=True):
    return {"actual": actual, "has_kpi": has_kpi}


class TestChainYearDeltas:
    def test_first_year_has_no_baseline(self):
        out = chain_year_deltas([2025, 2026], {2025: _cell(100), 2026: _cell(150)})
        assert out["2025"]["vs_prev_pct"] is None
        assert out["2025"]["vs_prev_year"] is None

    def test_second_year_moves_against_the_first(self):
        out = chain_year_deltas([2025, 2026], {2025: _cell(100), 2026: _cell(150)})
        assert out["2026"]["vs_prev_pct"] == 50.0
        assert out["2026"]["vs_prev_year"] == 2025

    def test_decline_is_negative(self):
        out = chain_year_deltas([2025, 2026], {2025: _cell(200), 2026: _cell(150)})
        assert out["2026"]["vs_prev_pct"] == -25.0

    def test_each_year_chains_to_the_one_before_it(self):
        out = chain_year_deltas(
            [2024, 2025, 2026],
            {2024: _cell(100), 2025: _cell(200), 2026: _cell(300)},
        )
        assert out["2025"]["vs_prev_pct"] == 100.0
        assert out["2026"]["vs_prev_pct"] == 50.0
        assert out["2026"]["vs_prev_year"] == 2025

    def test_a_skipped_year_rebases_onto_what_is_selected(self):
        # Dropping 2025 must not leave 2026 without a baseline.
        out = chain_year_deltas([2024, 2026], {2024: _cell(100), 2026: _cell(300)})
        assert out["2026"]["vs_prev_year"] == 2024
        assert out["2026"]["vs_prev_pct"] == 200.0

    def test_untracked_baseline_yields_no_percentage(self):
        # A branch fitting out in 2025 has placeholder rows, not a baseline —
        # showing "+560%" against them would be inventing a result.
        out = chain_year_deltas(
            [2025, 2026], {2025: _cell(11.4, has_kpi=False), 2026: _cell(75.5)}
        )
        assert out["2026"]["vs_prev_pct"] is None
        assert out["2026"]["vs_prev_year"] == 2025

    def test_untracked_current_year_yields_no_percentage(self):
        out = chain_year_deltas(
            [2025, 2026], {2025: _cell(100), 2026: _cell(120, has_kpi=False)}
        )
        assert out["2026"]["vs_prev_pct"] is None

    def test_zero_baseline_does_not_divide(self):
        out = chain_year_deltas([2025, 2026], {2025: _cell(0), 2026: _cell(120)})
        assert out["2026"]["vs_prev_pct"] is None

    def test_keys_are_strings_for_json(self):
        out = chain_year_deltas([2025, 2026], {2025: _cell(100), 2026: _cell(150)})
        assert set(out) == {"2025", "2026"}
