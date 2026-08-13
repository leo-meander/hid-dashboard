"""On-demand PageSpeed run — POST /api/team-kpi/refresh/page_load_speed.

A PageSpeed reading only exists because a Lighthouse test ran, and that happens
once a month by cron. Between runs there is no newer number to fetch anywhere,
so this endpoint asks for a fresh test. The cases worth pinning are the two ways
it could lie: leaving its own result behind the read cache it was meant to
replace, and answering 200 when no branch actually returned anything.
"""
from datetime import date

import pytest
from fastapi import HTTPException

from app.routers import team_kpi as router_mod
from app.services import pagespeed_service as psi


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
    psi.invalidate_page_speed_cache()
    yield
    psi.invalidate_page_speed_cache()


# ── Cache invalidation ───────────────────────────────────────────────────────

def test_invalidating_one_year_leaves_the_other_years_cached():
    psi._page_speed_cache[YEAR] = (1e12, {1: {"saigon": {}}})
    psi._page_speed_cache[YEAR - 1] = (1e12, {1: {"taipei": {}}})

    psi.invalidate_page_speed_cache(YEAR)

    assert YEAR not in psi._page_speed_cache
    assert YEAR - 1 in psi._page_speed_cache


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


def test_purchase_cvr_is_not_refreshable():
    """GA4 re-reads daily on its own, which is as fast as the number moves —
    there is no button for it, so the endpoint must not offer one either."""
    with pytest.raises(HTTPException) as exc:
        router_mod.refresh_auto_kpi("purchase_cvr", year=YEAR, month=None, db=_FakeDB())
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
