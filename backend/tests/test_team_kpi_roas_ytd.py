"""ROAS is a ratio (revenue ÷ spend), not an amount — its months cannot be
added together the way ads_revenue's can. Summing ×1.0 + ×5.0 across two
months would claim a ×6.0 YTD ROAS that no single day of the business ever
hit. The correct YTD is Σrevenue_vnd ÷ Σspend_vnd across the same months
the grid already restricts YTD to (only months with a target set), and the
YTD *target* has to be weighted by spend the same way the monthly target
already is — see team_kpi_service.py's "ratio" ytd_mode.
"""
from datetime import date

import pytest

from app.services import team_kpi_service as svc

YEAR = date.today().year
CUR_MONTH = date.today().month

SAIGON_UUID = svc.BRANCH_KEY_TO_UUID["saigon"]
TAIPEI_UUID = svc.BRANCH_KEY_TO_UUID["taipei"]

pytestmark = pytest.mark.skipif(CUR_MONTH < 2, reason="needs two distinct past months")

MONTH_A, MONTH_B = 1, CUR_MONTH


class _FakeRow:
    def __init__(self, kpi_key, month, target_value, branch_id=None):
        self.kpi_key = kpi_key
        self.month = month
        self.target_value = target_value
        self.branch_id = branch_id


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **kw):
        return self

    join = group_by = filter

    def all(self):
        return self._rows


class _FakeSession:
    """Only the Team KPI target table matters; ads data is monkeypatched in
    directly on get_paid_ads_actuals_yearly instead of faked at the ORM
    layer."""

    def __init__(self, rows=()):
        self._rows = list(rows)

    def query(self, *models, **kw):
        if models and models[0] is svc.TeamKPITarget:
            return _FakeQuery(self._rows)
        return _FakeQuery([])


def _kpi(summary, key="roas"):
    return next(k for k in summary["kpis"] if k["key"] == key)


@pytest.fixture(autouse=True)
def _clear_cache():
    svc._ads_actuals_cache.clear()
    yield
    svc._ads_actuals_cache.clear()


def test_branch_ytd_is_total_revenue_over_total_spend(monkeypatch):
    """Month A: spend 1,000 / revenue 1,000 (×1.0, target ×2.0).
    Month B: spend 3,000 / revenue 15,000 (×5.0, target ×4.0).
    Naive sum would show YTD ×6.0 against a ×6.0 target (100%) — both
    meaningless. The real blended figures are ×4.0 actual against a
    spend-weighted ×3.5 target.
    """
    monkeypatch.setattr(svc, "get_paid_ads_actuals_yearly", lambda db, year: {
        MONTH_A: {"saigon": {"ads_spend": 1000.0, "ads_revenue": 1000.0, "roas": 1.0}},
        MONTH_B: {"saigon": {"ads_spend": 3000.0, "ads_revenue": 15000.0, "roas": 5.0}},
    })
    rows = [
        _FakeRow("roas", MONTH_A, 2.0, SAIGON_UUID),
        _FakeRow("roas", MONTH_B, 4.0, SAIGON_UUID),
    ]
    kpi = _kpi(svc.build_monthly_summary(_FakeSession(rows), "paid_ads", YEAR, SAIGON_UUID))

    assert kpi["ytd_mode"] == "ratio"
    assert kpi["ytd_actual"] == 4.0     # (1000+15000) / (1000+3000)
    assert kpi["ytd_target"] == 3.5     # (1000*2 + 3000*4) / (1000+3000)
    # the naive sums a "sum" mode would have produced — must not appear
    assert kpi["ytd_actual"] != 6.0
    assert kpi["ytd_target"] != 6.0


def test_all_view_ytd_sums_every_branchs_vnd_first(monkeypatch):
    """The All tab's YTD must blend across branches too, not just months —
    every branch's spend/revenue is already VND at write-time, so summing
    them together before dividing never mixes currencies."""
    monkeypatch.setattr(svc, "get_paid_ads_actuals_yearly", lambda db, year: {
        MONTH_A: {
            "saigon": {"ads_spend": 1000.0, "ads_revenue": 1000.0, "roas": 1.0},
            "taipei": {"ads_spend": 2000.0, "ads_revenue": 12000.0, "roas": 6.0},
        },
    })
    rows = [
        _FakeRow("roas", MONTH_A, 2.0, SAIGON_UUID),
        _FakeRow("roas", MONTH_A, 10.0, TAIPEI_UUID),
    ]
    kpi = _kpi(svc.build_monthly_summary(_FakeSession(rows), "paid_ads", YEAR, None))

    assert kpi["ytd_mode"] == "ratio"
    assert kpi["ytd_actual"] == round((1000.0 + 12000.0) / (1000.0 + 2000.0), 2)
    assert kpi["ytd_target"] == round((1000.0 * 2.0 + 2000.0 * 10.0) / (1000.0 + 2000.0), 2)


def test_months_without_a_target_are_excluded_from_ytd(monkeypatch):
    """Same rule the grid already applies to every other KPI's YTD."""
    monkeypatch.setattr(svc, "get_paid_ads_actuals_yearly", lambda db, year: {
        MONTH_A: {"saigon": {"ads_spend": 9999.0, "ads_revenue": 1.0, "roas": 0.0001}},
        MONTH_B: {"saigon": {"ads_spend": 3000.0, "ads_revenue": 15000.0, "roas": 5.0}},
    })
    rows = [_FakeRow("roas", MONTH_B, 4.0, SAIGON_UUID)]   # Month A has no target
    kpi = _kpi(svc.build_monthly_summary(_FakeSession(rows), "paid_ads", YEAR, SAIGON_UUID))

    assert kpi["ytd_actual"] == 5.0
    assert kpi["ytd_target"] == 4.0
