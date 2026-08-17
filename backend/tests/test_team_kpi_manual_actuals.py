"""Hand-typed actuals: one number per branch, not one per month.

Manual actuals share the targets table under a `<kpi>__actual` key. They used to
be collapsed into a single value per (kpi, month), which meant the All tab
showed whichever branch's row the DB returned last as if it were the group's —
a number nobody had entered for "All", sitting in a month that on a branch tab
was still empty. They are branch-scoped now and combined the way the auto path
combines them: average a rate, sum a count.

The entry deadline itself (a month stays open until the 20th of the month after
it) is drawn by the grid — isLockedActualMonth in TeamKPI.jsx.
"""
from datetime import date

import pytest

from app.services import team_kpi_service as svc


YEAR = date.today().year
MONTH = 1  # a settled month, so nothing upstream competes with the manual value
SAIGON = svc.BRANCH_KEY_TO_UUID["saigon"]
TAIPEI = svc.BRANCH_KEY_TO_UUID["taipei"]


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
    """The query() filters are no-ops here, so rows are handed back whole —
    which is the point: the branch split has to happen in the mapping, not be
    an accident of what the DB filtered out."""

    def __init__(self, rows=()):
        self._rows = list(rows)

    def query(self, *models, **kw):
        if models and models[0] is svc.TeamKPITarget:
            return _FakeQuery(self._rows)
        return _FakeQuery([])


def _cell(rows, kpi_key, branch_id, role="crm"):
    out = svc.build_monthly_summary(_FakeSession(rows), role, YEAR, branch_id)
    kpi = next(k for k in out["kpis"] if k["key"] == kpi_key)
    return kpi["monthly"][MONTH - 1]


# ── A branch tab shows that branch's number ──────────────────────────────────

def test_a_branch_sees_its_own_hand_typed_rate():
    rows = [
        _FakeRow("data_fill_rate__actual", MONTH, 90.0, SAIGON),
        _FakeRow("data_fill_rate__actual", MONTH, 70.0, TAIPEI),
    ]
    assert _cell(rows, "data_fill_rate", SAIGON)["actual"] == 90.0
    assert _cell(rows, "data_fill_rate", TAIPEI)["actual"] == 70.0


def test_a_branch_with_no_number_stays_empty_while_its_sibling_has_one():
    """A branch tab was already safe in production — the SQL filter never let a
    sibling's row load. This holds the mapping to the same promise, so the
    guarantee doesn't rest on a filter two hundred lines away."""
    rows = [_FakeRow("data_fill_rate__actual", MONTH, 70.0, TAIPEI)]
    assert _cell(rows, "data_fill_rate", SAIGON)["actual"] is None


# ── The All tab combines them, it does not pick one ──────────────────────────

def test_the_group_rate_is_the_average_of_the_branches_that_reported():
    """Not 90, not 70 — and not whichever row came back last."""
    rows = [
        _FakeRow("data_fill_rate__actual", MONTH, 90.0, SAIGON),
        _FakeRow("data_fill_rate__actual", MONTH, 70.0, TAIPEI),
    ]
    assert _cell(rows, "data_fill_rate", None)["actual"] == 80.0


def test_a_group_count_is_summed_not_averaged():
    """Campaigns are a count: two branches sending 2 and 3 sent 5, not 2.5."""
    rows = [
        _FakeRow("crm_campaigns__actual", MONTH, 2.0, SAIGON),
        _FakeRow("crm_campaigns__actual", MONTH, 3.0, TAIPEI),
    ]
    assert _cell(rows, "crm_campaigns", None)["actual"] == 5.0


def test_a_month_nobody_entered_stays_empty_on_the_group_tab():
    rows = [_FakeRow("data_fill_rate__actual", MONTH + 1, 90.0, SAIGON)]
    assert _cell(rows, "data_fill_rate", None)["actual"] is None


# ── Org-wide rows carry no branch ────────────────────────────────────────────

def test_an_org_wide_row_is_read_from_the_branchless_bucket():
    """PM's Team Activities is entered once for the group, branch_id NULL."""
    rows = [_FakeRow("team_activities__actual", MONTH, 12.0, None)]
    assert _cell(rows, "team_activities", None, role="pm")["actual"] == 12.0


def test_a_branchless_row_does_not_leak_into_every_branch():
    """The Jul 2026 mess: a number typed on the All tab saves with branch_id
    NULL, and the branch query matches NULL as well as the branch — so one
    entry read back as Saigon's, 1948's, Oani's and Osaka's all at once."""
    rows = [_FakeRow("data_fill_rate__actual", MONTH, 96.64, None)]
    assert _cell(rows, "data_fill_rate", SAIGON)["actual"] is None


def test_the_group_still_shows_a_number_only_ever_entered_for_the_group():
    """CRM Campaigns Sent has always been typed on the All tab. Refusing the
    branchless bucket outright would erase its whole history."""
    rows = [_FakeRow("crm_campaigns__actual", MONTH, 3.0, None)]
    assert _cell(rows, "crm_campaigns", None)["actual"] == 3.0
    assert _cell(rows, "crm_campaigns", SAIGON)["actual"] is None


def test_branch_rows_win_over_a_stray_branchless_one():
    rows = [
        _FakeRow("data_fill_rate__actual", MONTH, 96.64, None),
        _FakeRow("data_fill_rate__actual", MONTH, 99.25, TAIPEI),
    ]
    assert _cell(rows, "data_fill_rate", TAIPEI)["actual"] == 99.25
    assert _cell(rows, "data_fill_rate", None)["actual"] == 99.25
