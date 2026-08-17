"""CRM: Data Fill-Rate is entered after the month closes, not during it.

Front-desk data-entry accuracy is counted over a finished month — read while the
month is still running it scores an incomplete set of check-ins. So this row's
entry window is the mirror image of every other row's: the current month is the
locked side and the months that have ended are the open one. The grid draws that
window (isLockedActualMonth in TeamKPI.jsx); what the backend owes it is the
`measured_at_month_end` flag, and only on the row that earns it.
"""
from datetime import date

from app.services import team_kpi_service as svc


YEAR = date.today().year
SAIGON_UUID = svc.BRANCH_KEY_TO_UUID["saigon"]


class _FakeQuery:
    def filter(self, *a, **kw):
        return self

    join = group_by = filter

    def all(self):
        return []


class _FakeSession:
    def query(self, *models, **kw):
        return _FakeQuery()


def _kpis(role_key):
    out = svc.build_monthly_summary(_FakeSession(), role_key, YEAR, SAIGON_UUID)
    return {k["key"]: k for k in out["kpis"]}


def test_data_fill_rate_is_flagged_as_a_month_end_reading():
    assert _kpis("crm")["data_fill_rate"]["measured_at_month_end"] is True


def test_the_flag_only_makes_sense_on_a_hand_entered_row():
    """An auto row is fetched, not typed — there is no entry window to move.
    Anything carrying the flag has to be manual, or the grid would be locking
    cells nobody types into anyway."""
    for role_key in svc.KPI_DEFS:
        for kpi in _kpis(role_key).values():
            if kpi["measured_at_month_end"]:
                assert kpi["auto_actuals"] is False, kpi["key"]


def test_no_other_row_moves_its_entry_window():
    """The rest of the grid keeps the ordinary rule — this month and ahead."""
    flagged = [k["key"] for role_key in svc.KPI_DEFS
               for k in _kpis(role_key).values() if k["measured_at_month_end"]]
    assert flagged == ["data_fill_rate"]
