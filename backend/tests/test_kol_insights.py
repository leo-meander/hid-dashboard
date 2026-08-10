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
    def test_sums_collaboration_totals_when_there_is_no_post_detail(self):
        out = _run([
            _collab(published_at="2026-07-15", reach=4000, engagements=1000),
            _collab(published_at="2026-07-20", reach=916, engagements=348),
            _collab(published_at="2026-08-01", reach=9999, engagements=9999),  # outside
        ])
        assert out["available"] is True
        assert out["reach"] == 4916
        assert out["engagements"] == 1348

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

    def test_posts_are_filtered_on_their_own_date_not_the_collaboration_s(self):
        """A collaboration's `total_reach` covers its whole life. Counting it
        because the header date landed in the window pulls in posts published
        outside it — Oani read 8,182 reach against the Engine's 357 that way.
        """
        out = _run([_collab(
            published_at="2026-07-15", reach=8182, engagements=5008,
            posts=[{"posted_at": "2026-07-18", "views": 357, "engagements": 85},
                   {"posted_at": "2026-09-01", "views": 7825, "engagements": 4923}],
        )])
        assert out["reach"] == 357, "the September post must not be counted"
        assert out["engagements"] == 85
        assert out["posts"] == 1

    def test_post_level_detail_wins_over_the_collaboration_header(self):
        out = _run([_collab(
            published_at=None, reach=None, engagements=None,
            posts=[{"posted_at": "2026-07-18", "reach": 500, "engagements": 60},
                   {"posted_at": "2026-07-19", "views": 300, "likes": 40}],
        )])
        assert out["available"] is True
        assert out["reach"] == 800
        assert out["engagements"] == 100
        assert out["posts"] == 2


class TestEngagementComponents:
    """The Engine stores engagement in parts — its platform accounts carry
    total_likes / total_comments / total_saves separately. Summing likes
    alone under-reported every branch: Saigon read 906 against 1,348."""

    def test_sums_likes_comments_and_saves(self):
        out = _run([_collab(posts=[{
            "posted_at": "2026-07-18", "views": 1000,
            "likes": 300, "comments": 42, "saves": 100,
        }])])
        assert out["engagements"] == 442

    def test_saves_dominate_on_xiaohongshu(self):
        """XHS engagement is mostly saves; counting likes alone loses most
        of it, and XHS reports no view count to notice the gap against."""
        out = _run([_collab(posts=[{
            "posted_at": "2026-07-18", "views": 0,
            "likes": 76, "comments": 12, "saves": 619,
        }])])
        assert out["engagements"] == 707
        assert out["engagement_rate_pct"] is None   # no reach to divide by

    def test_an_explicit_total_wins_over_the_components(self):
        """Preferring the total avoids double counting if the Engine ever
        starts sending both."""
        out = _run([_collab(posts=[{
            "posted_at": "2026-07-18", "views": 100,
            "engagements": 500, "likes": 300, "comments": 42, "saves": 100,
        }])])
        assert out["engagements"] == 500

    def test_missing_components_are_not_an_error(self):
        out = _run([_collab(posts=[{"posted_at": "2026-07-18", "views": 100}])])
        assert out["engagements"] == 0

    def test_reach_accepts_any_of_the_names_the_engine_uses(self):
        for field in ("reach", "views", "impressions"):
            out = _run([_collab(posts=[
                {"posted_at": "2026-07-18", field: 250, "likes": 5},
            ])])
            assert out["reach"] == 250, field


class TestEngagementRate:
    def test_zero_view_platforms_do_not_inflate_the_rate(self):
        """Xiaohongshu returns engagements with views=0. Charging those
        engagements against the other platforms' reach produced Oani's
        nonsensical 61% on the first deploy."""
        out = _run([_collab(posts=[
            {"posted_at": "2026-07-18", "views": 357, "engagements": 85},   # youtube
            {"posted_at": "2026-07-19", "views": 0, "engagements": 707},    # xhs
        ])])
        assert out["reach"] == 357
        assert out["engagements"] == 792          # totals still report everything
        assert out["engagement_rate_pct"] == 23.81   # 85/357, xhs excluded
        assert out["engagement_rate_posts"] == 1

    def test_rate_is_none_when_nothing_reported_reach(self):
        out = _run([_collab(posts=[
            {"posted_at": "2026-07-18", "views": 0, "engagements": 500},
        ])])
        assert out["available"] is True
        assert out["engagements"] == 500
        assert out["engagement_rate_pct"] is None

    def test_rate_covers_every_post_when_all_report_reach(self):
        out = _run([_collab(posts=[
            {"posted_at": "2026-07-18", "views": 100, "engagements": 10},
            {"posted_at": "2026-07-19", "views": 100, "engagements": 30},
        ])])
        assert out["engagement_rate_pct"] == 20.0
        assert out["engagement_rate_posts"] == out["posts"] == 2

    def test_collaboration_level_zero_reach_is_excluded_too(self):
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
