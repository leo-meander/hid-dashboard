"""KOL Engine reach aggregation.

The first deploy of this returned "unavailable" for all five branches and
gave no way to tell why. These tests pin each distinct failure mode to its
own reason string, so the next time it comes back empty the payload says
which of them it was.
"""
from datetime import date
from unittest.mock import patch

from app.services import kol_engine
from app.services.kol_engine import fetch_kol_insights


def _collab(branch_key="saigon", published_at=None, reach=None,
            engagements=None, posts=None):
    return {
        "branch_key": branch_key,
        "published_at": published_at,
        "total_reach": reach,
        "total_engagements": engagements,
        "posts": posts or [],
    }


def _run(records, branch_key="saigon",
         d_from=date(2026, 7, 13), d_to=date(2026, 7, 26)):
    kol_engine._kol_insights_cache.clear()
    with patch.object(kol_engine, "fetch_kol_data", return_value=records):
        return fetch_kol_insights(
            base_url="https://kol.test", org_id="org", api_key="key",
            branch_key=branch_key, date_from=d_from, date_to=d_to,
        )


class TestFailureModesAreDistinguishable:
    def test_missing_api_key(self):
        kol_engine._kol_insights_cache.clear()
        out = fetch_kol_insights("https://kol.test", "org", "", "saigon",
                                 date(2026, 7, 13), date(2026, 7, 26))
        assert out["available"] is False
        assert out["reason"] == "no_api_key"

    def test_upstream_failure_names_the_exception(self):
        kol_engine._kol_insights_cache.clear()
        with patch.object(kol_engine, "fetch_kol_data",
                          side_effect=TimeoutError("upstream")):
            out = fetch_kol_insights("https://kol.test", "org", "key", "saigon",
                                     date(2026, 7, 13), date(2026, 7, 26))
        assert out["available"] is False
        assert out["reason"] == "fetch_failed:TimeoutError"

    def test_branch_has_no_collaborations(self):
        out = _run([_collab(branch_key="taipei")], branch_key="saigon")
        assert out["reason"] == "no_collaborations_for_branch"

    def test_collaborations_exist_but_none_dated_in_window(self):
        out = _run([
            _collab(published_at=None),
            _collab(published_at="2026-06-01T00:00:00+00:00"),
        ])
        assert out["reason"] == "no_publish_dates_in_window"
        assert out["collaborations"] == 2

    def test_dated_but_carrying_no_numbers(self):
        out = _run([_collab(published_at="2026-07-20", reach=None, engagements=None)])
        assert out["available"] is False
        assert out["reason"] == "published_but_unscored"

    def test_never_reports_missing_data_as_zero_views(self):
        """A zero here reads as 'the posts got no views', which is a
        performance claim we have no basis for."""
        for out in [
            _run([]),
            _run([_collab(published_at=None)]),
            _run([_collab(published_at="2026-07-20")]),
        ]:
            assert out["available"] is False


class TestAggregation:
    def test_sums_collaboration_totals_in_the_window(self):
        out = _run([
            _collab(published_at="2026-07-15", reach=4000, engagements=1000),
            _collab(published_at="2026-07-20", reach=916, engagements=348),
            _collab(published_at="2026-08-01", reach=9999, engagements=9999),  # outside
        ])
        assert out["available"] is True
        assert out["reach"] == 4916
        assert out["engagements"] == 1348
        assert out["engagement_rate_pct"] == 27.42

    def test_window_boundaries_are_inclusive(self):
        out = _run([
            _collab(published_at="2026-07-13", reach=1, engagements=1),
            _collab(published_at="2026-07-26", reach=1, engagements=1),
        ])
        assert out["posts"] == 2

    def test_timestamps_are_truncated_to_a_date(self):
        out = _run([_collab(published_at="2026-07-26T23:59:59+07:00",
                            reach=100, engagements=10)])
        assert out["available"] is True

    def test_falls_back_to_post_dates_when_the_collaboration_has_none(self):
        """Publish date and score are populated by different Engine jobs, so
        a scored collaboration can still be undated at the top level."""
        out = _run([_collab(
            published_at=None, reach=None, engagements=None,
            posts=[{"posted_at": "2026-07-18", "reach": 500, "engagements": 60},
                   {"posted_at": "2026-07-19", "views": 300, "likes": 40}],
        )])
        assert out["available"] is True
        assert out["reach"] == 800
        assert out["engagements"] == 100
        assert out["posts"] == 2

    def test_engagement_rate_is_none_rather_than_a_division_error(self):
        out = _run([_collab(published_at="2026-07-15", reach=0, engagements=5)])
        assert out["available"] is True
        assert out["engagement_rate_pct"] is None

    def test_one_fetch_serves_repeated_branch_lookups(self):
        """The cache is what keeps a five-branch build to a single HTTP call."""
        kol_engine._kol_insights_cache.clear()
        records = [_collab(published_at="2026-07-15", reach=10, engagements=1)]
        with patch.object(kol_engine, "fetch_kol_data",
                          return_value=records) as fetch:
            for _ in range(5):
                fetch_kol_insights("https://kol.test", "org", "key", "saigon",
                                   date(2026, 7, 13), date(2026, 7, 26))
        assert fetch.call_count == 1
