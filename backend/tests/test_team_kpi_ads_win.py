"""Designer KPIs: the 2026-08 start gate and "% Ads Win" from the Ads Platform.

The win rate arrives as a 0–1 fraction with real nulls in it, so most of what
is worth testing here is the difference between "no ratio" (blank) and a
genuine 0%.
"""
import pytest

from app.services import team_kpi_service as svc
from app.services import upstream_actuals


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
YEAR = 2026


def _payload(**month_overrides):
    """One by_month entry for 2026-08, overridable per test."""
    entry = {
        "month": "2026-08",
        "wins": 1, "losses": 3, "tested": 4, "win_rate": 0.25,
        "in_progress": False,
        "by_branch": [
            {"branch": "Saigon", "wins": 1, "losses": 2, "tested": 3, "win_rate": 0.3333},
            {"branch": "Bread",  "wins": 0, "losses": 1, "tested": 1, "win_rate": 0.0},
        ],
    }
    entry.update(month_overrides)
    return {"by_month": [entry], "total_wins": 1, "total_losses": 3, "overall_win_rate": 0.25}


@pytest.fixture(autouse=True)
def _clear_cache():
    svc._ads_win_cache.clear()
    yield
    svc._ads_win_cache.clear()


@pytest.fixture
def upstream(monkeypatch):
    """Install a winning-ads payload; returns a setter for per-test payloads."""
    def install(payload):
        monkeypatch.setattr(
            upstream_actuals, "fetch_winning_ads_monthly",
            lambda year, branch=None, month=None: payload,
        )
        svc._ads_win_cache.clear()
    install(_payload())
    return install


def _kpi(summary, key):
    return next(k for k in summary["kpis"] if k["key"] == key)


# ── Payload → actuals map ────────────────────────────────────────────────────

def test_fraction_becomes_a_percentage(upstream):
    out = svc.get_ads_win_actuals_yearly(YEAR)
    assert out[8]["all"]["ads_win_rate"] == 25.0
    assert out[8]["saigon"]["ads_win_rate"] == 33.33


def test_bread_has_no_hid_branch(upstream):
    assert "bread" not in svc.get_ads_win_actuals_yearly(YEAR)[8]


def test_null_win_rate_is_blank_not_zero(upstream):
    """tested == 0 → nothing was decided that month."""
    upstream(_payload(
        wins=0, losses=0, tested=0, win_rate=None,
        by_branch=[{"branch": "Saigon", "wins": 0, "losses": 0, "tested": 0, "win_rate": None}],
    ))
    assert svc.get_ads_win_actuals_yearly(YEAR) == {}


def test_every_tested_ad_lost_is_a_real_zero(upstream):
    upstream(_payload(
        wins=0, losses=2, tested=2, win_rate=0.0,
        by_branch=[{"branch": "Saigon", "wins": 0, "losses": 2, "tested": 2, "win_rate": 0.0}],
    ))
    out = svc.get_ads_win_actuals_yearly(YEAR)
    assert out[8]["all"]["ads_win_rate"] == 0.0
    assert out[8]["saigon"]["ads_win_rate"] == 0.0


def test_payload_without_the_win_fields_is_blank(upstream):
    """Ads Platform before the win/loss deploy — winners only, no denominator."""
    upstream({"by_month": [{"month": "2026-08", "wins": 1, "by_branch": [{"branch": "Saigon", "wins": 1}]}]})
    assert svc.get_ads_win_actuals_yearly(YEAR) == {}


def test_other_years_are_ignored(upstream):
    upstream({"by_month": [dict(_payload()["by_month"][0], month="2025-08")]})
    assert svc.get_ads_win_actuals_yearly(YEAR) == {}


def test_upstream_failure_is_not_fatal(upstream):
    upstream({})
    assert svc.get_ads_win_actuals_yearly(YEAR) == {}


# ── Into the KPI grid ────────────────────────────────────────────────────────

def test_branch_cell_uses_the_branch_ratio(upstream):
    out = svc.build_monthly_summary(_FakeSession(), "designer", YEAR, SAIGON_UUID)
    aug = _kpi(out, "ads_win_rate")["monthly"][7]
    assert aug["actual"] == 33.33
    assert aug["provisional"] is False


def test_all_branches_uses_the_org_ratio_not_a_branch_average(upstream):
    """Averaging 33.33% would be wrong — the denominators differ."""
    out = svc.build_monthly_summary(_FakeSession(), "designer", YEAR, None)
    assert _kpi(out, "ads_win_rate")["monthly"][7]["actual"] == 25.0


def test_in_progress_month_is_flagged_provisional(upstream):
    upstream(_payload(in_progress=True))
    out = svc.build_monthly_summary(_FakeSession(), "designer", YEAR, SAIGON_UUID)
    assert _kpi(out, "ads_win_rate")["monthly"][7]["provisional"] is True


def test_a_month_upstream_never_reported_stays_blank(upstream):
    out = svc.build_monthly_summary(_FakeSession(), "designer", YEAR, SAIGON_UUID)
    sep = _kpi(out, "ads_win_rate")["monthly"][8]
    assert sep["actual"] is None
    assert sep["not_started"] is False


# ── The 2026-08 start gate ───────────────────────────────────────────────────

@pytest.mark.parametrize("key", ["design_ideas", "ads_win_rate"])
def test_months_before_the_start_are_locked_blank(upstream, key):
    out = svc.build_monthly_summary(_FakeSession(), "designer", YEAR, SAIGON_UUID)
    months = _kpi(out, key)["monthly"]
    for m in months[:7]:  # Jan–Jul
        assert m["not_started"] is True
        assert m["target"] is None and m["actual"] is None and m["pct"] is None
        assert m["has_target"] is False
    assert months[7]["not_started"] is False  # August is live


def test_whole_year_is_locked_before_the_start_year(upstream):
    out = svc.build_monthly_summary(_FakeSession(), "designer", 2025, SAIGON_UUID)
    assert all(m["not_started"] for m in _kpi(out, "ads_win_rate")["monthly"])


def test_start_month_helper():
    defn = {"key": "x", "starts": "2026-08"}
    assert svc.kpi_start_month(defn, 2025) == 13   # nothing in that year
    assert svc.kpi_start_month(defn, 2026) == 8
    assert svc.kpi_start_month(defn, 2027) is None  # always live by then
    assert svc.kpi_start_month({"key": "x"}, 2026) is None


# ── Retired designer rows ────────────────────────────────────────────────────

def test_design_assets_and_videos_are_hidden():
    keys = [d["key"] for d in svc.visible_kpi_defs("designer")]
    assert keys == ["design_ideas", "ads_win_rate"]
    # …but their defs (and DB history) are still there.
    all_keys = [d["key"] for d in svc.KPI_DEFS["designer"]]
    assert "design_assets" in all_keys and "videos_delivered" in all_keys


def test_lark_is_not_called_while_both_lark_kpis_are_hidden(upstream, monkeypatch):
    import app.services.lark_service as lark

    def boom(*a, **kw):
        raise AssertionError("Lark should not be called for the designer role")

    monkeypatch.setattr(lark, "get_designer_actuals_yearly", boom)
    svc.build_monthly_summary(_FakeSession(), "designer", YEAR, SAIGON_UUID)
