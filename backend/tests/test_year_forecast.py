"""Will the year's revenue target be hit — months that happened, plus pace.

The arithmetic is a sum; what has to hold is where each half comes from.

  · A month that has finished is worth exactly what the KPI grid says it is
    worth. A hand-typed accounting override is used as typed, and the
    deduct%/other-revenue adjustment lands on the Cloudbeds sum and nowhere
    else. Two pages disagreeing about a settled month would void every
    comparison built on top of it.
  · The month underway is projected, not read. In September, September is
    mostly still ahead of itself.
  · The forecast is adjusted the same way a Cloudbeds month is, or it is being
    compared with a target set on a different basis.
  · A month that cannot be projected leaves the projection AND the target it
    would have been measured against.

No database: the three lookups that need one are faked.
"""
from datetime import date

import pytest

from app.services import year_forecast
from app.services.year_forecast import forecast_year

AS_OF = date(2026, 9, 15)          # Jan-Aug settled, Sep-Dec still open


class FakeBranch:
    def __init__(self, bid, name, currency="TWD", deduction_pct=0, other_revenue=0):
        self.id, self.name, self.currency = bid, name, currency
        self.deduction_pct = deduction_pct
        self.other_revenue_native = other_revenue


class FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter_by(self, **kw):
        return self

    def filter(self, *a):
        return self

    def order_by(self, *a):
        return self

    def all(self):
        return self._rows


class FakeDB:
    def __init__(self, branches):
        self._branches = branches

    def query(self, *args):
        return FakeQuery(self._branches)


def cell(bid, month, revenue, *, low=None, high=None, target=0.0):
    return {
        "branch_id": bid, "branch_name": bid, "currency": "TWD",
        "year": 2026, "month": month, "stay_month": f"2026-{month:02d}",
        "days_out": 16, "basis": "ly_pickup",
        "room_nights": 1000.0, "room_nights_low": 900.0, "room_nights_high": 1100.0,
        "capacity_capped": False,
        "revenue_native": revenue,
        "revenue_low_native": low if low is not None else (
            revenue * 0.9 if revenue is not None else None),
        "revenue_high_native": high if high is not None else (
            revenue * 1.1 if revenue is not None else None),
        "target_native": target,
        "otb_room_nights": 400.0,
        "ly_otb_room_nights": 300.0,
        "ly_final_room_nights": 900.0,
        "available_room_nights": 1200.0,
        "run_rate_occ_pct": None,
        "adr_remaining": 1000.0,
        "adr_yoy": 1.0,
        "booked_revenue_native": 400_000.0,
        "booked_room_nights": 400,
        # The run-rate reading is the half the year projection is built from.
        "run_rate": {
            "revenue_native": revenue,
            "room_nights": 1000.0,
            "otb_room_nights": 400.0,
            "room_nights_per_day": 10.0,
            "days_left": 46,
            "adr": 1000.0,
        },
    }


@pytest.fixture
def scenario(monkeypatch):
    """One branch, 1m/month of target all year, 1m earned every settled month."""
    def build(branches, targets=None, cloudbeds=None, cells=None, as_of=AS_OF):
        monkeypatch.setattr(year_forecast, "_targets", lambda *a, **k: targets or {})
        monkeypatch.setattr(year_forecast, "_settled_revenue",
                            lambda *a, **k: cloudbeds or {})
        monkeypatch.setattr(
            year_forecast.fill_pace, "get_fill_pace",
            lambda *a, **k: {"forecast": {"cells": cells or []}},
        )
        monkeypatch.setattr(year_forecast, "get_cached_rate",
                            lambda c, t="VND": {"TWD": 830.0, "JPY": 165.0}.get(c, 1.0))
        return forecast_year(FakeDB(branches), year=2026, as_of=as_of)
    return build


def flat(value, months=range(1, 13)):
    return {("b1", m): {"target": value, "override": None} for m in months}


# ── the split ────────────────────────────────────────────────────────────────

def test_the_year_splits_into_what_happened_and_what_is_projected(scenario):
    out = scenario(
        [FakeBranch("b1", "1948")],
        targets=flat(1_000_000.0),
        cloudbeds={("b1", m): 1_000_000.0 for m in range(1, 9)},
        cells=[cell("b1", m, 900_000.0) for m in (9, 10, 11, 12)],
    )
    row = out["branches"][0]

    assert out["settled_months"] == list(range(1, 9))
    assert out["projected_months"] == [9, 10, 11, 12]
    assert row["actual_to_date_native"] == 8_000_000.0
    assert row["forecast_remaining_native"] == 3_600_000.0
    assert row["projection_native"] == 11_600_000.0
    assert row["target_native"] == 12_000_000.0
    assert row["gap_native"] == -400_000.0
    assert row["achievement_pct"] == pytest.approx(96.7, abs=0.1)


def test_the_month_underway_is_projected_not_read(scenario):
    """September is not a settled month on 15 September, and counting only what
    it has banked so far would report a year that is short by half a month."""
    out = scenario(
        [FakeBranch("b1", "1948")],
        targets=flat(1_000_000.0),
        cloudbeds={("b1", m): 1_000_000.0 for m in range(1, 10)},   # Sep present
        cells=[cell("b1", m, 900_000.0) for m in (9, 10, 11, 12)],
    )
    # September's daily_metrics row exists but is ignored: it is in the
    # projected half, and its 900k comes from the forecast, not the 1m above.
    assert out["branches"][0]["actual_to_date_native"] == 8_000_000.0
    assert 9 in out["projected_months"]


def test_an_accounting_override_is_used_exactly_as_typed(scenario):
    """Accounting has already netted whatever the override represents, so no
    deduction and no other-revenue go on top of it."""
    targets = flat(1_000_000.0)
    targets[("b1", 3)] = {"target": 1_000_000.0, "override": 500_000.0}
    out = scenario(
        [FakeBranch("b1", "1948", deduction_pct=10, other_revenue=50_000)],
        targets=targets,
        cloudbeds={("b1", m): 1_000_000.0 for m in range(1, 9)},
        cells=[],
    )
    # Seven Cloudbeds months at 1m × 0.9 + 50k, plus March as typed.
    assert out["branches"][0]["actual_to_date_native"] == pytest.approx(
        7 * (1_000_000 * 0.9 + 50_000) + 500_000)


def test_the_forecast_carries_the_same_adjustment_a_cloudbeds_month_does(scenario):
    """The target was set against revenue net of the deduction and gross of
    other revenue. A forecast compared with it un-adjusted measures something
    else."""
    out = scenario(
        [FakeBranch("b1", "1948", deduction_pct=10, other_revenue=50_000)],
        targets=flat(1_000_000.0),
        cloudbeds={},
        cells=[cell("b1", 12, 1_000_000.0)],
    )
    assert out["branches"][0]["forecast_remaining_native"] == pytest.approx(
        1_000_000 * 0.9 + 50_000)


# ── a month that cannot be projected ─────────────────────────────────────────

def test_a_month_with_no_projection_leaves_its_target_behind_too(scenario):
    """Nine months of projection read against twelve months of target is not
    an achievement percentage."""
    out = scenario(
        [FakeBranch("b1", "1948")],
        targets=flat(1_000_000.0),
        cloudbeds={("b1", m): 1_000_000.0 for m in range(1, 9)},
        cells=[cell("b1", m, 1_000_000.0) for m in (9, 10, 11)],   # December missing
    )
    row = out["branches"][0]

    assert row["months_not_projected"] == [12]
    assert row["target_native"] == 11_000_000.0        # December's target is out
    assert row["target_full_year_native"] == 12_000_000.0
    assert row["achievement_pct"] == pytest.approx(100.0)
    # And the quarter, which is now incomplete, refuses to publish a number.
    assert row["q4_projection_native"] is None
    assert row["q4_achievement_pct"] is None


def test_an_unpriced_month_counts_as_not_projected(scenario):
    out = scenario(
        [FakeBranch("b1", "1948")],
        targets=flat(1_000_000.0),
        cloudbeds={},
        cells=[cell("b1", 9, 1_000_000.0), cell("b1", 10, None)],
    )
    assert out["branches"][0]["months_not_projected"] == [10, 11, 12]


# ── the quarter ──────────────────────────────────────────────────────────────

def test_q4_is_the_same_arithmetic_over_three_months(scenario):
    out = scenario(
        [FakeBranch("b1", "1948")],
        targets=flat(1_000_000.0),
        cloudbeds={("b1", m): 1_000_000.0 for m in range(1, 9)},
        cells=[cell("b1", m, 1_200_000.0) for m in (9, 10, 11, 12)],
    )
    row = out["branches"][0]
    assert row["q4_projection_native"] == pytest.approx(3_600_000.0)
    assert row["q4_target_native"] == 3_000_000.0
    assert row["q4_achievement_pct"] == pytest.approx(120.0)


def test_a_settled_q4_month_is_counted_as_it_happened(scenario):
    """Read in December, October and November are history, not pace: the
    quarter is then two settled months plus one forecast."""
    out = scenario(
        [FakeBranch("b1", "1948")],
        targets=flat(1_000_000.0),
        cloudbeds={("b1", m): 1_000_000.0 for m in range(1, 12)},
        cells=[cell("b1", 12, 500_000.0)],
        as_of=date(2026, 12, 10),
    )
    row = out["branches"][0]
    assert out["settled_months"] == list(range(1, 12))
    assert out["projected_months"] == [12]
    assert row["q4_projection_native"] == pytest.approx(2_000_000 + 500_000)


# ── the group ────────────────────────────────────────────────────────────────

def test_branches_are_added_in_vnd_because_nothing_else_adds(scenario):
    """TWD and JPY do not sum. The group plans in VND, so the roll-up is VND."""
    out = scenario(
        [FakeBranch("b1", "1948", currency="TWD"),
         FakeBranch("b2", "Osaka", currency="JPY")],
        targets={**flat(1_000_000.0),
                 **{("b2", m): {"target": 10_000_000.0, "override": None}
                    for m in range(1, 13)}},
        cloudbeds={},
        cells=[cell("b1", m, 1_000_000.0) for m in (9, 10, 11, 12)]
              + [{**cell("b2", m, 10_000_000.0), "currency": "JPY"}
                 for m in (9, 10, 11, 12)],
    )
    total = out["total"]
    assert total["currency"] == "VND"
    assert total["projection_vnd"] == pytest.approx(
        4_000_000 * 830 + 40_000_000 * 165)
    assert total["target_vnd"] == pytest.approx(
        12_000_000 * 830 + 120_000_000 * 165)
    assert total["achievement_pct"] == pytest.approx(
        total["projection_vnd"] / total["target_vnd"] * 100, abs=0.1)


def test_the_projection_carries_no_error_band(scenario):
    """The run-rate half is arithmetic — today's speed times days left — so
    there is no measured error to put a range around. An earlier version
    projected by the shape of the year before and did carry one; the band went
    when that reading did."""
    out = scenario(
        [FakeBranch("b1", "1948")],
        targets=flat(1_000_000.0),
        cloudbeds={("b1", m): 1_000_000.0 for m in range(1, 9)},
        cells=[cell("b1", m, 1_000_000.0) for m in (9, 10, 11, 12)],
    )
    row = out["branches"][0]
    assert row["projection_low_native"] == row["projection_native"]
    assert row["projection_high_native"] == row["projection_native"]
