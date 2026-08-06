"""Nora's On-Time Delivery Rate on the Designer KPI grid.

The number has to be the one her Task Overview card shows — on-time ÷ scored,
with excused misses out of both sides — so the KPI row reads it off the same
aggregation instead of recounting the records its own way.
"""
import pytest

from app.services import lark_service as lark
from app.services import team_kpi_service as svc


class _FakeQuery:
    def filter(self, *a, **kw):
        return self

    def all(self):
        return []


class _FakeSession:
    def query(self, *a, **kw):
        return _FakeQuery()


SAIGON_UUID = svc.BRANCH_KEY_TO_UUID["saigon"]
YEAR = 2026

# Nora holds two Lark record IDs; July is split across both, and the merged
# rate is recomputed from the counts (13 ÷ 17), never averaged.
NORA_OVERVIEW = {
    "recN1": {
        7: {"total_tasks": 12, "completed": 10, "on_time_count": 8,
            "on_time_filled": 11, "late_count": 3, "on_time_rate": 72.7},
        8: {"total_tasks": 4, "completed": 4, "on_time_count": 4,
            "on_time_filled": 4, "late_count": 0, "on_time_rate": 100.0},
        "open_workload": 3, "no_deadline_count": 0, "excluded_status_count": 0,
    },
    "recN2": {
        7: {"total_tasks": 8, "completed": 7, "on_time_count": 5,
            "on_time_filled": 6, "late_count": 1, "on_time_rate": 83.3},
        "open_workload": 0, "no_deadline_count": 0, "excluded_status_count": 0,
    },
}


@pytest.fixture
def overview(monkeypatch):
    """Install a Task Overview payload; returns a setter for per-test data."""
    def install(data):
        monkeypatch.setattr(lark, "get_task_overview_yearly", lambda year: data)
        monkeypatch.setattr(
            lark, "PIC_NAME_MAP",
            {"recN1": "Nora", "recN2": "Nora", "recM": "Mason"},
        )
    install(NORA_OVERVIEW)
    return install


def _kpi(summary, key):
    return next(k for k in summary["kpis"] if k["key"] == key)


# ── Lark aggregation → actuals map ───────────────────────────────────────────

def test_matches_the_task_overview_card(overview):
    """13 on-time ÷ 17 scored = 76.5%, not the average of 72.7 and 83.3."""
    out = lark.get_delivery_rate_yearly(YEAR)
    assert out[7]["all"]["delivery_rate"] == 76.5
    assert out[8]["all"]["delivery_rate"] == 100.0


def test_a_month_with_nothing_scored_is_omitted(overview):
    overview({"recN1": {9: {"total_tasks": 2, "completed": 0, "on_time_count": 0,
                            "on_time_filled": 0, "on_time_rate": None}}})
    assert lark.get_delivery_rate_yearly(YEAR) == {}


def test_unknown_person_is_not_fatal(overview):
    assert lark.get_delivery_rate_yearly(YEAR, pic_name="Nobody") == {}


# ── Into the KPI grid ────────────────────────────────────────────────────────

def test_row_shows_in_the_all_view(overview):
    out = svc.build_monthly_summary(_FakeSession(), "designer", YEAR, None)
    kpi = _kpi(out, "delivery_rate")
    assert kpi["org_wide"] is True
    assert kpi["auto_actuals"] is True
    assert kpi["monthly"][6]["actual"] == 76.5   # Jul
    assert kpi["monthly"][7]["actual"] == 100.0  # Aug


def test_org_wide_value_survives_a_branch_view(overview):
    """The frontend hides org-wide rows per branch, but the value is unchanged."""
    out = svc.build_monthly_summary(_FakeSession(), "designer", YEAR, SAIGON_UUID)
    assert _kpi(out, "delivery_rate")["monthly"][6]["actual"] == 76.5


def test_months_before_the_lark_cutoff_are_locked_blank(overview):
    out = svc.build_monthly_summary(_FakeSession(), "designer", YEAR, None)
    months = _kpi(out, "delivery_rate")["monthly"]
    assert all(m["not_started"] for m in months[:6])   # Jan–Jun
    assert months[6]["not_started"] is False           # Jul, where Lark starts


def test_lark_failure_leaves_the_row_blank(monkeypatch):
    def boom(year):
        raise RuntimeError("Lark down")

    monkeypatch.setattr(lark, "get_task_overview_yearly", boom)
    out = svc.build_monthly_summary(_FakeSession(), "designer", YEAR, None)
    assert all(m["actual"] is None for m in _kpi(out, "delivery_rate")["monthly"])
