"""Paid Ads: Purchase Conversion Rate from the GA4 Data API.

The metric counts unique users, and unique users de-duplicate across time. Most
of what is worth pinning here is that consequence: every figure is its own query
over its own date range, nothing is ever assembled by adding months up, and a
view where the number cannot be measured honestly stays blank instead of
showing a plausible one.
"""
import calendar
import json
from datetime import date

import pytest

from app.config import settings
from app.services import ga4_service
from app.services import team_kpi_service as svc


YEAR = date.today().year
CUR_MONTH = date.today().month
SAIGON_UUID = svc.BRANCH_KEY_TO_UUID["saigon"]
OANI_UUID = svc.BRANCH_KEY_TO_UUID["oani"]

SAIGON_PROPERTY = "284939713"


# ── Fake DB ──────────────────────────────────────────────────────────────────

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
    """Only the Team KPI target table matters here; everything else is empty."""

    def __init__(self, rows=()):
        self._rows = list(rows)

    def query(self, *models, **kw):
        if models and models[0] is svc.TeamKPITarget:
            return _FakeQuery(self._rows)
        return _FakeQuery([])


# ── GA4 stub ─────────────────────────────────────────────────────────────────

def _reading(rate_pct=1.63, total_users=10000.0, active_users=9800.0,
             purchasing_users=163.0, thresholded=False):
    return ga4_service.Ga4PurchaseReading(
        rate_pct=rate_pct,
        total_users=total_users,
        active_users=active_users,
        purchasing_users=purchasing_users,
        thresholded=thresholded,
    )


@pytest.fixture(autouse=True)
def _clear_cache():
    svc._ga4_cvr_cache.clear()
    ga4_service.reset_token_cache()
    yield
    svc._ga4_cvr_cache.clear()
    ga4_service.reset_token_cache()


@pytest.fixture(autouse=True)
def _only_saigon_is_mapped(monkeypatch):
    """One clean property, plus Oani deliberately unmapped (its tag is polluted)."""
    monkeypatch.setattr(settings, "GA4_PROPERTY_ID_SAIGON", SAIGON_PROPERTY)
    monkeypatch.setattr(settings, "GA4_PROPERTY_ID_1948", "")
    monkeypatch.setattr(settings, "GA4_PROPERTY_ID_TAIPEI", "")
    monkeypatch.setattr(settings, "GA4_PROPERTY_ID_OSAKA", "")
    monkeypatch.setattr(settings, "GA4_PROPERTY_ID_OANI", "")


@pytest.fixture
def ga4(monkeypatch):
    """Record every runReport window; returns (calls, install_responder)."""
    calls = []
    state = {"responder": lambda pid, start, end: _reading()}

    def fake_run(property_id, start_date, end_date):
        calls.append((property_id, start_date, end_date))
        return state["responder"](property_id, start_date, end_date)

    monkeypatch.setattr(ga4_service, "run_purchase_report", fake_run)

    def install(responder):
        state["responder"] = responder
        calls.clear()
        svc._ga4_cvr_cache.clear()

    return calls, install


def _kpi(summary, key="purchase_cvr"):
    return next(k for k in summary["kpis"] if k["key"] == key)


# ── Payload → actuals map ────────────────────────────────────────────────────

def test_decimal_rate_is_already_scaled_to_a_percentage(ga4):
    out = svc.get_purchase_cvr_actuals_yearly(YEAR)
    assert out[CUR_MONTH]["saigon"]["purchase_cvr"] == 1.63


def test_every_month_is_its_own_query_over_that_months_calendar_dates(ga4):
    """A month built from daily rows would double-count returning users."""
    calls, _ = ga4
    svc.get_purchase_cvr_actuals_yearly(YEAR)

    windows = {(s, e) for _pid, s, e in calls}
    for m in range(1, CUR_MONTH + 1):
        last = calendar.monthrange(YEAR, m)[1]
        assert (f"{YEAR}-{m:02d}-01", f"{YEAR}-{m:02d}-{last:02d}") in windows

    # …and nothing narrower than a whole month was ever requested.
    month_starts = {f"{YEAR}-{m:02d}-01" for m in range(1, CUR_MONTH + 1)}
    for _pid, start, end in calls:
        assert start in month_starts | {f"{YEAR}-01-01"}
        assert start != end


def test_the_month_selector_changes_the_queried_range(ga4):
    """Different months must produce different GA4 windows, not one re-sliced pull."""
    calls, _ = ga4
    svc.get_purchase_cvr_actuals_yearly(YEAR)
    ytd = (SAIGON_PROPERTY, f"{YEAR}-01-01", date.today().isoformat())
    monthly = [(s, e) for call in calls if call != ytd for _pid, s, e in [call]]
    assert len(set(monthly)) == len(monthly) == CUR_MONTH


def test_ytd_is_a_dedicated_jan_first_query(ga4):
    calls, _ = ga4
    out = svc.get_purchase_cvr_actuals_yearly(YEAR)
    assert (SAIGON_PROPERTY, f"{YEAR}-01-01", date.today().isoformat()) in calls
    assert svc.GA4_YTD_MONTH in out


def test_a_year_in_the_future_is_not_queried(ga4):
    calls, _ = ga4
    assert svc.get_purchase_cvr_actuals_yearly(YEAR + 1) == {}
    assert calls == []


def test_an_unreachable_property_leaves_the_month_blank(ga4):
    _calls, install = ga4
    install(lambda pid, start, end: None)
    assert svc.get_purchase_cvr_actuals_yearly(YEAR) == {}


def test_no_properties_configured_is_not_fatal(ga4, monkeypatch):
    monkeypatch.setattr(settings, "GA4_PROPERTY_ID_SAIGON", "")
    assert svc.get_purchase_cvr_actuals_yearly(YEAR) == {}


# ── Into the KPI grid ────────────────────────────────────────────────────────

def test_branch_cell_shows_the_property_rate(ga4):
    out = svc.build_monthly_summary(_FakeSession(), "paid_ads", YEAR, SAIGON_UUID)
    kpi = _kpi(out)
    assert kpi["monthly"][CUR_MONTH - 1]["actual"] == 1.63
    assert kpi["unavailable_note"] is None


def test_the_row_renders_as_two_decimals_with_a_percent_sign(ga4):
    kpi = _kpi(svc.build_monthly_summary(_FakeSession(), "paid_ads", YEAR, SAIGON_UUID))
    assert kpi["decimals"] == 2
    assert kpi["fixed_decimals"] is True
    assert kpi["value_suffix"] == "%"
    assert kpi["unit"] == "%"


def test_achievement_uses_the_same_formula_as_the_roas_row(ga4):
    rows = [_FakeRow("purchase_cvr", CUR_MONTH, 2.0, SAIGON_UUID)]
    kpi = _kpi(svc.build_monthly_summary(_FakeSession(rows), "paid_ads", YEAR, SAIGON_UUID))
    cell = kpi["monthly"][CUR_MONTH - 1]
    assert cell["target"] == 2.0
    assert cell["pct"] == round(1.63 / 2.0 * 100, 1)


def test_a_small_percentage_target_is_stored_as_typed(ga4):
    """1.8 means 1.8%, not the 180% the fraction rule would make of it."""
    rows = [_FakeRow("purchase_cvr", CUR_MONTH, 1.8, SAIGON_UUID)]
    kpi = _kpi(svc.build_monthly_summary(_FakeSession(rows), "paid_ads", YEAR, SAIGON_UUID))
    assert kpi["monthly"][CUR_MONTH - 1]["target"] == 1.8


def test_other_pct_kpis_still_normalise_fractions():
    """The opt-out is per KPI — 0.9 on a Designer rate is still 90%."""
    rows = [_FakeRow("delivery_rate", CUR_MONTH, 0.9, None)]
    out = svc.build_monthly_summary(_FakeSession(rows), "designer", YEAR, SAIGON_UUID)
    assert _kpi(out, "delivery_rate")["monthly"][CUR_MONTH - 1]["target"] == 90.0


def test_thresholded_month_is_marked(ga4):
    _calls, install = ga4
    install(lambda pid, start, end: _reading(thresholded=True))
    kpi = _kpi(svc.build_monthly_summary(_FakeSession(), "paid_ads", YEAR, SAIGON_UUID))
    assert kpi["monthly"][CUR_MONTH - 1]["provisional"] is True
    assert "thresholding" in kpi["provisional_note"]


def test_ga4_silence_is_a_blank_cell_not_a_zero(ga4):
    _calls, install = ga4
    install(lambda pid, start, end: None)
    kpi = _kpi(svc.build_monthly_summary(_FakeSession(), "paid_ads", YEAR, SAIGON_UUID))
    assert all(m["actual"] is None for m in kpi["monthly"])


# ── YTD ──────────────────────────────────────────────────────────────────────

def test_ytd_comes_from_the_year_query_not_the_monthly_cells(ga4):
    _calls, install = ga4
    install(lambda pid, start, end: _reading(rate_pct=1.71 if start.endswith("-01-01") else 1.63))
    kpi = _kpi(svc.build_monthly_summary(_FakeSession(), "paid_ads", YEAR, SAIGON_UUID))
    assert kpi["ytd_mode"] == "query"
    assert kpi["ytd_actual"] == 1.71          # the Jan-1 → today reading
    assert kpi["ytd_actual"] != 1.63 * CUR_MONTH  # never a sum of the months


def test_ytd_is_blank_when_the_year_query_has_no_answer(ga4):
    """Summing the monthly percentages instead would produce a number that means nothing."""
    _calls, install = ga4
    install(lambda pid, start, end: None if start.endswith("-01-01") else _reading())
    kpi = _kpi(svc.build_monthly_summary(_FakeSession(), "paid_ads", YEAR, SAIGON_UUID))
    assert kpi["ytd_actual"] is None
    assert any(m["actual"] == 1.63 for m in kpi["monthly"])


def test_roas_keeps_summing_its_ytd(ga4):
    assert _kpi(svc.build_monthly_summary(_FakeSession(), "paid_ads", YEAR, SAIGON_UUID),
                "roas")["ytd_mode"] == "sum"


# ── Views where the number cannot be measured ────────────────────────────────

def test_oani_is_blank_with_the_tagging_reason(ga4):
    kpi = _kpi(svc.build_monthly_summary(_FakeSession(), "paid_ads", YEAR, OANI_UUID))
    assert all(m["actual"] is None for m in kpi["monthly"])
    assert kpi["ytd_actual"] is None
    assert "1948 and Osaka" in kpi["unavailable_note"]


def test_oani_stays_blank_even_if_its_property_gets_mapped_by_accident(ga4, monkeypatch):
    """The blocker is the polluted tag, not a missing config value."""
    monkeypatch.setattr(settings, "GA4_PROPERTY_ID_OANI", "514380737")
    svc._ga4_cvr_cache.clear()
    kpi = _kpi(svc.build_monthly_summary(_FakeSession(), "paid_ads", YEAR, OANI_UUID))
    assert all(m["actual"] is None for m in kpi["monthly"])


def test_all_branches_has_no_group_wide_rate(ga4):
    """Five properties are five user namespaces — an average would be invented."""
    kpi = _kpi(svc.build_monthly_summary(_FakeSession(), "paid_ads", YEAR, None))
    assert all(m["actual"] is None for m in kpi["monthly"])
    assert "de-duplicated" in kpi["unavailable_note"]


@pytest.mark.parametrize("branch_id", [SAIGON_UUID, OANI_UUID, None])
def test_no_ga4_property_id_reaches_the_grid(ga4, branch_id):
    """The grid speaks in branches. The branch → property mapping is config,
    and must never surface in anything rendered to the reader."""
    summary = svc.build_monthly_summary(_FakeSession(), "paid_ads", YEAR, branch_id)
    rendered = json.dumps(summary)
    for property_id in ["284939713", "285135676", "295612616", "482876806", "514380737"]:
        assert property_id not in rendered


def test_blocking_the_view_does_not_touch_the_stored_target(ga4):
    rows = [_FakeRow("purchase_cvr", CUR_MONTH, 1.8, OANI_UUID)]
    kpi = _kpi(svc.build_monthly_summary(_FakeSession(rows), "paid_ads", YEAR, OANI_UUID))
    assert kpi["monthly"][CUR_MONTH - 1]["target"] == 1.8


def test_the_other_paid_ads_rows_are_unaffected(ga4):
    keys = [k["key"] for k in
            svc.build_monthly_summary(_FakeSession(), "paid_ads", YEAR, SAIGON_UUID)["kpis"]]
    assert keys == ["roas", "ads_revenue", "purchase_cvr"]


# ── Which denominator GA4 actually uses (§6) ─────────────────────────────────

def test_total_users_reproduces_the_rate():
    assert ga4_service._implied_denominator(
        _reading(rate_pct=1.63, total_users=10000, active_users=5000, purchasing_users=163)
    ) == "totalUsers"


def test_active_users_reproduces_the_rate():
    assert ga4_service._implied_denominator(
        _reading(rate_pct=1.63, total_users=5000, active_users=10000, purchasing_users=163)
    ) == "activeUsers"


def test_counts_too_close_to_tell_apart():
    assert ga4_service._implied_denominator(
        _reading(rate_pct=1.63, total_users=10000, active_users=9990, purchasing_users=163)
    ) == "both"


def test_neither_denominator_reproduces_a_thresholded_reading():
    assert ga4_service._implied_denominator(
        _reading(rate_pct=1.63, total_users=10000, active_users=9800, purchasing_users=40)
    ) is None
