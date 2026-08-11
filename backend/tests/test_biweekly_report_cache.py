"""Bi-weekly report cache staleness.

`_get_report` treats a cached payload as good forever once the bi-weekly
period itself has closed — true for the period's own day-by-day numbers,
but not for the monthly Target Achievement gauges it also carries: those
mirror the KPI Targets page for as long as the month they cover is still
open (more nights land on the books, a target can be entered late). A
period that ended a few days into August must keep recomputing August's
gauge on every read until August itself is over, or it freezes at whatever
was true on the first read — see the "Actual/Target of august hiển thị
nguyên 1 tháng" report where Taipei's cached August target went stale
after the real target was entered later.
"""
from datetime import date

from app.routers import biweekly_report as router_mod
from app.routers.biweekly_report import _payload_has_open_month


def _payload_with_month(year: int, month: int) -> list:
    return [{
        "target": {
            "months": [{"year": year, "month": month}],
        },
    }]


class TestPayloadHasOpenMonth:
    def test_month_still_in_progress_is_open(self, monkeypatch):
        monkeypatch.setattr(router_mod, "ict_today", lambda: date(2026, 8, 11))
        assert _payload_has_open_month(_payload_with_month(2026, 8)) is True

    def test_month_that_already_ended_is_not_open(self, monkeypatch):
        monkeypatch.setattr(router_mod, "ict_today", lambda: date(2026, 9, 5))
        assert _payload_has_open_month(_payload_with_month(2026, 8)) is False

    def test_month_ending_today_is_still_open(self, monkeypatch):
        # August 31 hasn't fully elapsed until the day is over.
        monkeypatch.setattr(router_mod, "ict_today", lambda: date(2026, 8, 31))
        assert _payload_has_open_month(_payload_with_month(2026, 8)) is True

    def test_period_spanning_two_months_open_if_either_is(self, monkeypatch):
        monkeypatch.setattr(router_mod, "ict_today", lambda: date(2026, 8, 11))
        payload = [{
            "target": {
                "months": [{"year": 2026, "month": 7}, {"year": 2026, "month": 8}],
            },
        }]
        assert _payload_has_open_month(payload) is True

    def test_missing_target_or_months_is_not_open(self):
        assert _payload_has_open_month([{}]) is False
        assert _payload_has_open_month([{"target": {}}]) is False
        assert _payload_has_open_month([{"target": {"months": []}}]) is False
