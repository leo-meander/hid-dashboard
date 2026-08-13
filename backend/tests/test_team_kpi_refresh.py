"""On-demand refresh of the two upstream-backed Paid Ads KPIs.

Both KPIs are normally only as fresh as a schedule allows — GA4 behind a
one-hour read cache, PageSpeed Insights behind a monthly cron — so the whole
point of POST /api/team-kpi/refresh/{kpi_key} is that it bypasses that wait and
reports what actually came back. The cases worth pinning are therefore the two
ways it could lie: serving the cached number it was asked to replace, and
answering 200 when the upstream gave it nothing.
"""
from datetime import date

import pytest
from fastapi import HTTPException

from app.routers import team_kpi as router_mod
from app.services import pagespeed_service as psi
from app.services import team_kpi_service as svc


YEAR = date.today().year
CUR_MONTH = date.today().month


# ── Fake DB — enough for PageSpeedCache upserts ──────────────────────────────

class _FakeQuery:
    """Evaluates ``==`` filters against the in-memory rows.

    Real matching matters here: sync_page_speed upserts one row per branch, so
    a fake that returns the same row for every filter would let a bug that
    overwrites one branch with another's reading pass unnoticed.
    """

    def __init__(self, rows):
        self._rows = rows

    def filter(self, *criteria):
        rows = self._rows
        for crit in criteria:
            field, want = crit.left.key, crit.right.value
            rows = [r for r in rows if str(getattr(r, field, None)) == str(want)]
        return _FakeQuery(rows)

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeDB:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.commits = 0

    def query(self, *args, **kwargs):
        return _FakeQuery(self.rows)

    def add(self, row):
        self.rows.append(row)

    def commit(self):
        self.commits += 1


@pytest.fixture(autouse=True)
def _clean_caches():
    svc.invalidate_purchase_cvr_cache()
    psi.invalidate_page_speed_cache()
    yield
    svc.invalidate_purchase_cvr_cache()
    psi.invalidate_page_speed_cache()


# ── Cache invalidation ───────────────────────────────────────────────────────

def test_invalidating_one_year_leaves_the_other_years_cached():
    svc._ga4_cvr_cache[("ga4_cvr", YEAR)] = (1e12, {1: {"saigon": {}}})
    svc._ga4_cvr_cache[("ga4_cvr", YEAR - 1)] = (1e12, {1: {"taipei": {}}})

    svc.invalidate_purchase_cvr_cache(YEAR)

    assert ("ga4_cvr", YEAR) not in svc._ga4_cvr_cache
    assert ("ga4_cvr", YEAR - 1) in svc._ga4_cvr_cache


def test_a_page_speed_sync_makes_its_own_reading_readable_immediately(monkeypatch):
    """Without cache invalidation inside sync, a fresh run stays invisible for
    up to the 10-minute read TTL — which is the exact wait the button removes."""
    db = _FakeDB()
    # Pre-warm the read cache with a stale value, as a page load would.
    psi._page_speed_cache[YEAR] = (1e12, {CUR_MONTH: {"saigon": {"page_load_speed": 9.9}}})

    monkeypatch.setattr(psi, "fetch_speed_index", lambda url: 4.2)
    psi.sync_page_speed(db, year=YEAR, month=CUR_MONTH)

    fresh = psi.get_page_speed_actuals_yearly(db, YEAR)
    assert fresh[CUR_MONTH]["saigon"]["page_load_speed"] == 4.2
    # One row per branch, not one row reused.
    assert len(db.rows) == len(psi.settings.pagespeed_url_map)


# ── Router contract ──────────────────────────────────────────────────────────

def test_an_unknown_kpi_key_is_rejected():
    with pytest.raises(HTTPException) as exc:
        router_mod.refresh_auto_kpi("roas", year=YEAR, month=None, db=_FakeDB())
    assert exc.value.status_code == 400


def test_page_speed_refresh_reports_a_total_upstream_failure_as_an_error(monkeypatch):
    """fetch_speed_index swallows its own failures and returns None. A refresh
    where every branch came back None has changed nothing, so it must not
    answer 200."""
    monkeypatch.setattr(psi, "fetch_speed_index", lambda url: None)

    with pytest.raises(HTTPException) as exc:
        router_mod.refresh_auto_kpi("page_load_speed", year=YEAR, month=CUR_MONTH, db=_FakeDB())
    assert exc.value.status_code == 502


def test_page_speed_refresh_names_the_branches_that_failed(monkeypatch):
    """A partial failure still succeeds, but the branches that did not answer
    travel back in the payload rather than being silently dropped."""
    urls = psi.settings.pagespeed_url_map
    failing = psi.settings.PAGESPEED_URL_OSAKA
    monkeypatch.setattr(psi, "fetch_speed_index", lambda url: None if url == failing else 3.1)

    out = router_mod.refresh_auto_kpi("page_load_speed", year=YEAR, month=CUR_MONTH, db=_FakeDB())

    data = out["data"]
    assert [e["branch"] for e in data["errors"]] == ["osaka"]
    assert len(data["synced"]) == len(urls) - 1


def test_purchase_cvr_refresh_errors_when_ga4_returns_nothing(monkeypatch):
    """get_purchase_cvr_actuals_yearly returns {} on any failure — an empty
    payload is not a successful refresh."""
    monkeypatch.setattr(svc, "get_purchase_cvr_actuals_yearly", lambda year: {})

    with pytest.raises(HTTPException) as exc:
        router_mod.refresh_auto_kpi("purchase_cvr", year=YEAR, month=CUR_MONTH, db=_FakeDB())
    assert exc.value.status_code == 502


def test_purchase_cvr_refresh_returns_the_requested_month(monkeypatch):
    monkeypatch.setattr(svc, "get_purchase_cvr_actuals_yearly", lambda year: {
        svc.GA4_YTD_MONTH: {"saigon": {"purchase_cvr": 0.9}},
        CUR_MONTH: {"saigon": {"purchase_cvr": 1.39}, "taipei": {"purchase_cvr": 0.72}},
    })

    out = router_mod.refresh_auto_kpi("purchase_cvr", year=YEAR, month=CUR_MONTH, db=_FakeDB())

    data = out["data"]
    assert data["month"] == CUR_MONTH
    assert data["readings"] == {"saigon": 1.39, "taipei": 0.72}
    # The synthetic year-to-date bucket is not a month and must never be
    # offered as one.
    assert svc.GA4_YTD_MONTH not in data["months_refreshed"]
