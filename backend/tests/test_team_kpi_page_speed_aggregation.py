"""Avg Website Load Speed is a level, not an amount.

Every other Paid Ads row accumulates: two months of revenue add up, five
branches of spend add up. Seconds do not. Five branch pages at ~5.8s each are
a 5.8s website, not a 29s one, and a July at 3s followed by an August at 3s is
still a 3s year — yet the All tab was showing 29 and the YTD target row was
showing 17.9 (2.99 × six months), because both fell through to the default
"sum". The KPI declares itself averaged on both axes instead.
"""
from datetime import date

import pytest

from app.services import team_kpi_service as svc

YEAR = date.today().year
CUR_MONTH = date.today().month

SAIGON = svc.BRANCH_KEY_TO_UUID["saigon"]
TAIPEI = svc.BRANCH_KEY_TO_UUID["taipei"]

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
    def __init__(self, rows=()):
        self._rows = list(rows)

    def query(self, *models, **kw):
        if models and models[0] is svc.TeamKPITarget:
            return _FakeQuery(self._rows)
        return _FakeQuery([])


@pytest.fixture(autouse=True)
def _no_upstream(monkeypatch):
    """Only hand-typed months are in play here — the months this KPI's history
    was built from, before the PageSpeed sync existed."""
    monkeypatch.setattr(svc, "get_paid_ads_actuals_yearly", lambda db, year: {})
    svc._ads_actuals_cache.clear()
    yield
    svc._ads_actuals_cache.clear()


def _kpi(rows, branch_id=None):
    out = svc.build_monthly_summary(_FakeSession(rows), "paid_ads", YEAR, branch_id)
    return next(k for k in out["kpis"] if k["key"] == "page_load_speed")


# ── Across branches ──────────────────────────────────────────────────────────

def test_the_all_tab_averages_the_branch_pages_instead_of_adding_them():
    rows = [
        _FakeRow("page_load_speed__actual", MONTH_A, 5.8, SAIGON),
        _FakeRow("page_load_speed__actual", MONTH_A, 6.0, TAIPEI),
    ]
    cell = _kpi(rows)["monthly"][MONTH_A - 1]
    assert cell["actual"] == 5.9
    assert cell["actual"] != 11.8  # the sum the grid used to show


def test_a_branch_still_shows_its_own_seconds():
    rows = [
        _FakeRow("page_load_speed__actual", MONTH_A, 5.8, SAIGON),
        _FakeRow("page_load_speed__actual", MONTH_A, 6.0, TAIPEI),
    ]
    assert _kpi(rows, SAIGON)["monthly"][MONTH_A - 1]["actual"] == 5.8
    assert _kpi(rows, TAIPEI)["monthly"][MONTH_A - 1]["actual"] == 6.0


def test_the_all_tab_target_is_one_branch_goal_not_five_stacked():
    """<3s is the goal for every branch page. Summing the per-branch targets
    turned the group goal into <15s, which nothing could ever fail."""
    rows = [
        _FakeRow("page_load_speed", MONTH_A, 3.0, SAIGON),
        _FakeRow("page_load_speed", MONTH_A, 3.0, TAIPEI),
    ]
    assert _kpi(rows)["monthly"][MONTH_A - 1]["target"] == 3.0


# ── Across months ────────────────────────────────────────────────────────────

def test_ytd_is_the_mean_of_the_months_not_their_sum():
    rows = [
        _FakeRow("page_load_speed", MONTH_A, 3.0, SAIGON),
        _FakeRow("page_load_speed", MONTH_B, 3.0, SAIGON),
        _FakeRow("page_load_speed__actual", MONTH_A, 4.0, SAIGON),
        _FakeRow("page_load_speed__actual", MONTH_B, 6.0, SAIGON),
    ]
    kpi = _kpi(rows, SAIGON)

    assert kpi["ytd_mode"] == "avg"
    assert kpi["ytd_actual"] == 5.0   # (4 + 6) / 2, not 10
    assert kpi["ytd_target"] == 3.0   # the standing goal, not 6


def test_the_ytd_target_covers_the_same_months_as_the_ytd_actual():
    """A target row averaged over twelve months against an actual row averaged
    over the months measured so far reads two different years against each
    other. Only months that have both count."""
    rows = [
        _FakeRow("page_load_speed", MONTH_A, 2.0, SAIGON),
        _FakeRow("page_load_speed", MONTH_B, 4.0, SAIGON),
        # MONTH_B was never measured — its 4.0 target must not drag the mean.
        _FakeRow("page_load_speed__actual", MONTH_A, 5.0, SAIGON),
    ]
    kpi = _kpi(rows, SAIGON)
    assert kpi["ytd_actual"] == 5.0
    assert kpi["ytd_target"] == 2.0
