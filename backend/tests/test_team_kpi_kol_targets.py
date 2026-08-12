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


class _Row:
    """Minimal stand-in for a TeamKPITarget row."""

    def __init__(self, kpi_key, month, target_value, branch_id=None):
        self.kpi_key = kpi_key
        self.month = month
        self.target_value = target_value
        self.branch_id = branch_id


class _StoredTargets(_FakeSession):
    """Session whose target query returns hand-entered rows."""

    def __init__(self, rows):
        self._rows = rows

    def query(self, *a, **kw):
        rows = self._rows

        class _Q(_FakeQuery):
            def all(self):
                return rows

        return _Q()


SAIGON_UUID = svc.BRANCH_KEY_TO_UUID["saigon"]

# June: the first month every KOL KPI has a target (see target_starts in
# KPI_DEFS — Posted from May, Ads Collab from June).
TGT_MONTH = 6
TGT_IDX = TGT_MONTH - 1

# Two branches with targets from the engine; one month only.
FAKE_ACTUALS = {
    TGT_MONTH: {
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
    # 2026 is the year the KOL targets start in, and TGT_MONTH is already
    # behind us — so every month asserted here stays non-future forever.
    return 2026


def _kpi(summary, key):
    return next(k for k in summary["kpis"] if k["key"] == key)


def test_branch_view_uses_engine_target(patched):
    out = svc.build_monthly_summary(_FakeSession(), "kol", patched, SAIGON_UUID)

    posted = _kpi(out, "kol_posted")
    jun = posted["monthly"][TGT_IDX]
    assert posted["computed_target"] is True
    assert posted["computed_target_note"] == "= 90% of prev-month collab (KOL Engine)"
    assert jun["target"] == 9
    assert jun["actual"] == 7.0
    assert jun["pct"] == pytest.approx(77.8)

    ads = _kpi(out, "kol_ads_collab")["monthly"][TGT_IDX]
    assert ads["target"] == 5
    assert ads["actual"] == 3.0


def test_all_branches_view_sums_engine_targets(patched):
    out = svc.build_monthly_summary(_FakeSession(), "kol", patched, None)

    assert _kpi(out, "kol_posted")["monthly"][TGT_IDX]["target"] == 13      # 9 + 4
    assert _kpi(out, "kol_ads_collab")["monthly"][TGT_IDX]["target"] == 7   # 5 + 2
    # Both branches share the same rule → a single % in the label.
    assert _kpi(out, "kol_ads_collab")["computed_target_note"] == "= 50% of Posted target (KOL Engine)"


def test_label_shows_a_range_when_branches_differ(monkeypatch, patched):
    mixed = {TGT_MONTH: {k: dict(v) for k, v in FAKE_ACTUALS[TGT_MONTH].items()}}
    mixed[TGT_MONTH]["taipei"]["kol_posted__pct"] = 75.5
    monkeypatch.setattr(svc, "get_kol_actuals_yearly_db", lambda db, year: mixed)

    out = svc.build_monthly_summary(_FakeSession(), "kol", patched, None)
    assert _kpi(out, "kol_posted")["computed_target_note"] == "= 75.5–90% of prev-month collab (KOL Engine)"


def test_label_falls_back_when_engine_omits_the_pct(monkeypatch, patched):
    """Older KOL Engine deploys return targets but no posted_pct."""
    without = {TGT_MONTH: {k: {kk: vv for kk, vv in v.items() if not kk.endswith("__pct")}
                           for k, v in FAKE_ACTUALS[TGT_MONTH].items()}}
    monkeypatch.setattr(svc, "get_kol_actuals_yearly_db", lambda db, year: without)

    out = svc.build_monthly_summary(_FakeSession(), "kol", patched, SAIGON_UUID)
    assert _kpi(out, "kol_posted")["computed_target_note"] == "= % of prev-month collab (KOL Engine)"
    assert _kpi(out, "kol_posted")["monthly"][TGT_IDX]["target"] == 9  # target still works


def test_month_without_engine_target_has_no_target(patched):
    out = svc.build_monthly_summary(_FakeSession(), "kol", patched, SAIGON_UUID)
    jul = _kpi(out, "kol_posted")["monthly"][TGT_IDX + 1]
    assert jul["target"] is None
    assert jul["has_target"] is False


def test_collaborated_still_uses_stored_hid_target(patched):
    """Only Posted + Ads-Allowed are engine-owned; Collaborated stays editable."""
    out = svc.build_monthly_summary(_FakeSession(), "kol", patched, SAIGON_UUID)
    collab = _kpi(out, "kol_collaborated")
    assert collab["computed_target"] is False
    assert collab["monthly"][TGT_IDX]["target"] is None  # no stored row in this fixture


# ── target_starts: goals only exist from the month the team set them ────────

def test_engine_target_is_suppressed_before_target_starts(monkeypatch, patched):
    """Jan–Apr keep their actuals but show no goal.

    The KOL Engine derives Posted off one year-wide percentage, so it returns
    a target for every month — including months planned by nobody. Without the
    gate those back-computed numbers would score the team on Jan–Apr.
    """
    early = {m: {k: dict(v) for k, v in FAKE_ACTUALS[TGT_MONTH].items()}
             for m in (1, 2, 3, 4)}
    monkeypatch.setattr(svc, "get_kol_actuals_yearly_db", lambda db, year: early)

    out = svc.build_monthly_summary(_FakeSession(), "kol", patched, SAIGON_UUID)
    for cell in _kpi(out, "kol_posted")["monthly"][:4]:
        assert cell["target"] is None, cell["month"]
        assert cell["has_target"] is False
        assert cell["not_started"] is False   # the KPI itself was live
        assert cell["actual"] == 7.0          # …and its actual still shows
        assert cell["pct"] is None            # so it can't drag Avg %


def test_ads_collab_starts_a_month_after_posted(monkeypatch, patched):
    """Posted's goal starts in May, Ads Collab's only in June."""
    may = {5: {k: dict(v) for k, v in FAKE_ACTUALS[TGT_MONTH].items()}}
    monkeypatch.setattr(svc, "get_kol_actuals_yearly_db", lambda db, year: may)

    out = svc.build_monthly_summary(_FakeSession(), "kol", patched, SAIGON_UUID)
    assert _kpi(out, "kol_posted")["monthly"][4]["target"] == 9
    assert _kpi(out, "kol_ads_collab")["monthly"][4]["target"] is None
    assert _kpi(out, "kol_ads_collab")["monthly"][4]["actual"] == 3.0


def test_stored_hid_target_is_suppressed_before_target_starts(monkeypatch, patched):
    """The gate covers hand-entered targets too, not just engine-computed ones."""
    monkeypatch.setattr(
        svc, "get_kol_actuals_yearly_db",
        lambda db, year: {m: {k: dict(v) for k, v in FAKE_ACTUALS[TGT_MONTH].items()}
                          for m in (1, TGT_MONTH)},
    )
    # Stored Collaborated targets for Jan (before the start) and June (after).
    stored = _StoredTargets([
        _Row("kol_collaborated", 1, 20.0),
        _Row("kol_collaborated", TGT_MONTH, 15.0),
    ])

    out = svc.build_monthly_summary(stored, "kol", patched, SAIGON_UUID)
    collab = _kpi(out, "kol_collaborated")["monthly"]
    assert collab[0]["target"] is None       # Jan goal hidden…
    assert collab[0]["actual"] == 10.0       # …actual untouched
    assert collab[TGT_IDX]["target"] == 15.0  # June goal still honoured


def test_target_starts_only_gates_its_own_year():
    """A one-off historical marker, not a rule that repeats every year.

    2027 must come back with all twelve months targetable — by then the goals
    (and, for Posted/Ads Collab, the percentage rule behind them) genuinely
    exist from January.
    """
    assert svc.kpi_target_start_month({"target_starts": "2026-05"}, 2025) == 13
    assert svc.kpi_target_start_month({"target_starts": "2026-05"}, 2026) == 5
    assert svc.kpi_target_start_month({"target_starts": "2026-05"}, 2027) is None
    assert svc.kpi_target_start_month({}, 2026) is None


def test_next_year_targets_are_untouched(monkeypatch):
    """End-to-end: January 2027 still gets its engine target."""
    monkeypatch.setattr(
        svc, "get_kol_actuals_yearly_db",
        lambda db, year: {1: {k: dict(v) for k, v in FAKE_ACTUALS[TGT_MONTH].items()}},
    )
    out = svc.build_monthly_summary(_FakeSession(), "kol", 2027, SAIGON_UUID)
    assert _kpi(out, "kol_posted")["monthly"][0]["target"] == 9
    assert _kpi(out, "kol_ads_collab")["monthly"][0]["target"] == 5


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
