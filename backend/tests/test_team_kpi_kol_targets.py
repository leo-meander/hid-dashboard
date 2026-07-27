"""KOL Posted / Ads-Allowed targets mirror the KOL Engine (percentage-driven).

HiD never stores those two targets: the KOL Engine computes
    posted      = round(prev_month_collaborated × posted_pct / 100)
    ads_allowed = round(posted_target × ads_allowed_pct / 100)
and HiD reads the resolved number off the public targets API.
"""
from datetime import date

import pytest

from app.services import kol_engine
from app.services import team_kpi_service as svc


class _FakeQuery:
    """db.query(...).filter(...).all() → no stored HiD targets."""

    def filter(self, *a, **kw):
        return self

    def all(self):
        return []


class _FakeSession:
    def query(self, *a, **kw):
        return _FakeQuery()


SAIGON_UUID = svc.BRANCH_KEY_TO_UUID["saigon"]

# Two branches with targets from the engine; month 1 only.
FAKE_ACTUALS = {
    1: {
        "all": {"kol_invited": 12.0, "kol_revenue": 0.0},
        "saigon": {
            "kol_revenue": 0.0,
            "kol_collaborated": 10.0,
            "kol_posted": 7.0,
            "kol_ads_collab": 3.0,
            "kol_posted__target": 9.0,       # 10 collabs in Dec × 90%
            "kol_ads_collab__target": 5.0,   # 9 × 50% → 4.5 → 5
            "kol_posted__pct": 90.0,
            "kol_ads_collab__pct": 50.0,
        },
        "taipei": {
            "kol_revenue": 0.0,
            "kol_collaborated": 4.0,
            "kol_posted": 2.0,
            "kol_ads_collab": 1.0,
            "kol_posted__target": 4.0,
            "kol_ads_collab__target": 2.0,
            "kol_posted__pct": 90.0,
            "kol_ads_collab__pct": 50.0,
        },
    }
}


@pytest.fixture
def patched(monkeypatch):
    monkeypatch.setattr(svc, "get_kol_actuals_yearly_db", lambda db, year: FAKE_ACTUALS)
    # Year in the past → every month is non-future, keeps the test date-independent.
    return 2024


def _kpi(summary, key):
    return next(k for k in summary["kpis"] if k["key"] == key)


def test_branch_view_uses_engine_target(patched):
    out = svc.build_monthly_summary(_FakeSession(), "kol", patched, SAIGON_UUID)

    posted = _kpi(out, "kol_posted")
    jan = posted["monthly"][0]
    assert posted["computed_target"] is True
    assert posted["computed_target_note"] == "= 90% of prev-month collab (KOL Engine)"
    assert jan["target"] == 9
    assert jan["actual"] == 7.0
    assert jan["pct"] == pytest.approx(77.8)

    ads = _kpi(out, "kol_ads_collab")["monthly"][0]
    assert ads["target"] == 5
    assert ads["actual"] == 3.0


def test_all_branches_view_sums_engine_targets(patched):
    out = svc.build_monthly_summary(_FakeSession(), "kol", patched, None)

    assert _kpi(out, "kol_posted")["monthly"][0]["target"] == 13      # 9 + 4
    assert _kpi(out, "kol_ads_collab")["monthly"][0]["target"] == 7   # 5 + 2
    # Both branches share the same rule → a single % in the label.
    assert _kpi(out, "kol_ads_collab")["computed_target_note"] == "= 50% of Posted target (KOL Engine)"


def test_label_shows_a_range_when_branches_differ(monkeypatch, patched):
    mixed = {1: {k: dict(v) for k, v in FAKE_ACTUALS[1].items()}}
    mixed[1]["taipei"]["kol_posted__pct"] = 75.5
    monkeypatch.setattr(svc, "get_kol_actuals_yearly_db", lambda db, year: mixed)

    out = svc.build_monthly_summary(_FakeSession(), "kol", patched, None)
    assert _kpi(out, "kol_posted")["computed_target_note"] == "= 75.5–90% of prev-month collab (KOL Engine)"


def test_label_falls_back_when_engine_omits_the_pct(monkeypatch, patched):
    """Older KOL Engine deploys return targets but no posted_pct."""
    without = {1: {k: {kk: vv for kk, vv in v.items() if not kk.endswith("__pct")}
                   for k, v in FAKE_ACTUALS[1].items()}}
    monkeypatch.setattr(svc, "get_kol_actuals_yearly_db", lambda db, year: without)

    out = svc.build_monthly_summary(_FakeSession(), "kol", patched, SAIGON_UUID)
    assert _kpi(out, "kol_posted")["computed_target_note"] == "= % of prev-month collab (KOL Engine)"
    assert _kpi(out, "kol_posted")["monthly"][0]["target"] == 9  # target still works


def test_month_without_engine_target_has_no_target(patched):
    out = svc.build_monthly_summary(_FakeSession(), "kol", patched, SAIGON_UUID)
    feb = _kpi(out, "kol_posted")["monthly"][1]
    assert feb["target"] is None
    assert feb["has_target"] is False


def test_collaborated_still_uses_stored_hid_target(patched):
    """Only Posted + Ads-Allowed are engine-owned; Collaborated stays editable."""
    out = svc.build_monthly_summary(_FakeSession(), "kol", patched, SAIGON_UUID)
    collab = _kpi(out, "kol_collaborated")
    assert collab["computed_target"] is False
    assert collab["monthly"][0]["target"] is None  # no stored row in this fixture


# ── Engine payload → actuals map ────────────────────────────────────────────

class _EmptyRowsQuery:
    def join(self, *a, **kw):
        return self

    def filter(self, *a, **kw):
        return self

    def all(self):
        return []


class _NoCacheSession:
    """No marketing_activity_cache rows → no branch buckets from revenue."""

    def query(self, *a, **kw):
        return _EmptyRowsQuery()


def _engine_payload():
    return {
        "totals": {"invited_proactive": {"actual": 12, "target": 20, "pct": 60.0}},
        "branches": [{
            "hotel_id": kol_engine.BRANCH_KEY_TO_HOTEL["saigon"],
            "hotel_name": "MEANDER Saigon",
            "collaborated": {"actual": 10, "target": 12, "pct": 83.3},
            "posted": {"actual": 7, "target": 9, "pct": 77.8},
            "ads_allowed": {"actual": 3, "target": 5, "pct": 60.0},
            "posted_pct": 90,
            "ads_allowed_pct": 50,
        }],
    }


def test_engine_targets_land_without_a_revenue_row(monkeypatch):
    """Target cells must fill even for months with no cached revenue row."""
    svc._kol_actuals_cache.clear()
    calls = []

    def fake_targets(base_url, org_slug, api_key, year, month):
        calls.append(month)
        return _engine_payload()

    monkeypatch.setattr(kol_engine, "fetch_kol_targets", fake_targets)
    monkeypatch.setattr(svc, "fetch_kol_revenue", lambda **kw: None)

    year = date.today().year
    out = svc.get_kol_actuals_yearly_db(_NoCacheSession(), year)

    saigon = out[1]["saigon"]
    assert saigon["kol_posted__target"] == 9.0
    assert saigon["kol_ads_collab__target"] == 5.0
    assert saigon["kol_posted__pct"] == 90.0
    assert saigon["kol_ads_collab__pct"] == 50.0
    # No revenue row → counts stay absent (blank), not a misleading 0.
    assert "kol_posted" not in saigon

    # One month past today so the upcoming month's target is already visible.
    expected_last = min(date.today().month + 1, 12)
    assert calls[-1] == expected_last
