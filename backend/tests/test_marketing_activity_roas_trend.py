"""Marketing Activity ROAS trend — the YTD view's month-by-month line.

The line is built one month at a time by the same overview builder /summary
uses, so each point must read exactly what picking that month in the Monthly
view shows. Two things the chart depends on and threads could break:

  * points come back in calendar order, not in the order the workers finished;
  * a month whose upstream call fails is marked unavailable, never sent as a
    zero — a zero would draw as a ROAS collapse that never happened.
"""
from datetime import date
from unittest.mock import patch

from app.routers import marketing_activity as ma


def _overview(roas):
    return {
        "paid_ads": {"bookings": 1, "revenue": 100.0, "cost": 10.0, "roas": roas},
        "kol": {"bookings": 1, "revenue": 100.0, "cost": 1.0, "roas": roas * 10},
        "crm": {"bookings": 1, "revenue": 50.0, "cost": 10.0, "roas": roas},
        "total": {"bookings": 3, "revenue": 250.0, "cost": 21.0, "roas": roas},
    }


class _Session:
    def close(self):
        pass


class _DB:
    """Stands in for the request Session — only asked for the Branch currency."""
    def __init__(self, branch=None):
        self._branch = branch

    def query(self, *_a):
        return self

    def filter(self, *_a):
        return self

    def first(self):
        return self._branch


def _months(payload):
    return payload["data"]["months"]


def _run(build_side_effect):
    """Call the endpoint with every month served by ``build_side_effect``."""
    with patch.object(ma, "SessionLocal", _Session), \
         patch.object(ma, "_cache_read", return_value=None), \
         patch.object(ma, "_build_overview", side_effect=build_side_effect):
        return ma.get_roas_trend(branch_id=None, year=2025, db=_DB())


class TestPointsCoverTheYear:
    def test_a_past_year_runs_january_to_december(self):
        payload = _run(lambda _s, _b, d_from, _d_to, _n: _overview(d_from.month))
        months = _months(payload)
        assert [m["month"] for m in months] == [f"2025-{m:02d}" for m in range(1, 13)]
        assert [m["label"] for m in months][:3] == ["Jan", "Feb", "Mar"]

    def test_calendar_order_survives_workers_finishing_out_of_order(self):
        # Later months answer first; the response must still read Jan → Dec.
        payload = _run(lambda _s, _b, d_from, _d_to, _n: _overview(13 - d_from.month))
        roas = [m["total"]["roas"] for m in _months(payload)]
        assert roas == list(range(12, 0, -1))

    def test_the_current_year_stops_at_this_month(self):
        today = date.today()
        with patch.object(ma, "SessionLocal", _Session), \
             patch.object(ma, "_cache_read", return_value=None), \
             patch.object(ma, "_build_overview", return_value=_overview(2.0)):
            payload = ma.get_roas_trend(branch_id=None, year=None, db=_DB())
        months = _months(payload)
        assert payload["data"]["year"] == today.year
        assert len(months) == today.month
        assert months[-1]["month"] == f"{today.year}-{today.month:02d}"

    def test_a_future_year_has_no_points_at_all(self):
        payload = ma.get_roas_trend(branch_id=None, year=date.today().year + 1, db=_DB())
        assert _months(payload) == []


class TestAFailedMonthIsAGapNotAZero:
    def test_the_broken_month_carries_no_numbers(self):
        def _build(_s, _b, d_from, _d_to, _n):
            if d_from.month == 6:
                raise RuntimeError("KOL Engine unreachable")
            return _overview(2.0)

        months = _months(_run(_build))
        june = months[5]
        assert june["month"] == "2025-06"
        assert june["unavailable"] is True
        assert "total" not in june

    def test_every_other_month_still_reports(self):
        def _build(_s, _b, d_from, _d_to, _n):
            if d_from.month == 6:
                raise RuntimeError("KOL Engine unreachable")
            return _overview(2.0)

        months = _months(_run(_build))
        assert all(m["total"]["roas"] == 2.0 for m in months if m["month"] != "2025-06")


class TestCurrency:
    def test_all_branches_reports_vnd(self):
        payload = _run(lambda *_a: _overview(2.0))
        assert payload["data"]["currency"] == "VND"

    def test_a_single_branch_reports_its_own_currency(self):
        class _Branch:
            currency = "TWD"

        with patch.object(ma, "SessionLocal", _Session), \
             patch.object(ma, "_cache_read", return_value=None), \
             patch.object(ma, "_build_overview", return_value=_overview(2.0)):
            payload = ma.get_roas_trend(
                branch_id="c07ddc13-524d-4600-b3d8-5cc1871a0286",
                year=2025,
                db=_DB(_Branch()),
            )
        assert payload["data"]["currency"] == "TWD"
