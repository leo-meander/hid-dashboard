"""`resolve_window` — the weekly report's window derivation, generalised.

The Bi-Weekly Branch Manager report drives the weekly report's section
queries over a 14-day range by passing an explicit window. Those sections
previously hard-coded `last_week_range(today)` plus a 7-day look-back:

    cutoff, end_date = last_week_range(today)
    prev_end = cutoff - timedelta(days=1)
    prev_start = prev_end - timedelta(days=6)

The whole point of this refactor is that the weekly report's behaviour is
UNCHANGED. These tests pin that: with no explicit window, `resolve_window`
must reproduce the old arithmetic exactly, for every day of the week.
"""
from datetime import date, timedelta

from app.services.biweekly_period import period_for, preceding_window
from app.services.weekly_report_builder import last_week_range, resolve_window


def _legacy(today: date):
    """The arithmetic that used to be inlined in every section."""
    cutoff, end_date = last_week_range(today)
    prev_end = cutoff - timedelta(days=1)
    prev_start = prev_end - timedelta(days=6)
    return cutoff, end_date, prev_start, prev_end


class TestWeeklyBehaviourUnchanged:
    def test_matches_legacy_arithmetic_across_four_years(self):
        d = date(2024, 1, 1)
        while d < date(2028, 1, 1):
            assert resolve_window(d) == _legacy(d), f"diverged on {d}"
            d += timedelta(days=1)

    def test_every_weekday_lands_on_the_same_completed_week(self):
        # Whichever day of the week the report is generated on, it covers the
        # same last-completed Mon–Sun. That stability is deliberate: a late or
        # re-run report must not silently report on a different week.
        monday = date(2026, 8, 3)
        for offset in range(7):           # Mon through Sun
            start, end, _, _ = resolve_window(monday + timedelta(days=offset))
            assert (start, end) == (date(2026, 7, 27), date(2026, 8, 2)), (
                f"offset {offset} moved the window"
            )

    def test_window_never_includes_today(self):
        d = date(2026, 1, 1)
        while d < date(2027, 1, 1):
            assert resolve_window(d)[1] < d
            d += timedelta(days=1)

    def test_comparison_window_is_seven_days_and_contiguous(self):
        start, end, prev_start, prev_end = resolve_window(date(2026, 8, 7))
        assert (end - start).days + 1 == 7
        assert (prev_end - prev_start).days + 1 == 7
        assert prev_end + timedelta(days=1) == start


class TestExplicitWindow:
    def test_comparison_matches_the_reporting_window_length(self):
        p = period_for(2026, 8, 1)                      # Aug 1–14
        start, end, prev_start, prev_end = resolve_window(p.end, (p.start, p.end))
        assert (start, end) == (date(2026, 8, 1), date(2026, 8, 14))
        assert (end - start).days + 1 == 14
        assert (prev_end - prev_start).days + 1 == 14
        assert prev_end + timedelta(days=1) == start
        assert (prev_start, prev_end) == (date(2026, 7, 18), date(2026, 7, 31))

    def test_a_17_day_period_gets_a_17_day_comparison(self):
        p = period_for(2026, 8, 2)                      # Aug 15–31, 17 days
        start, end, prev_start, prev_end = resolve_window(p.end, (p.start, p.end))
        assert (end - start).days + 1 == 17
        assert (prev_end - prev_start).days + 1 == 17

    def test_explicit_window_ignores_today(self):
        # `today` still drives a section's internal "as of" logic, but must not
        # move the reporting window when one is given.
        p = period_for(2026, 8, 1)
        for anchor in (date(2026, 8, 14), date(2026, 8, 31), date(2027, 1, 1)):
            assert resolve_window(anchor, (p.start, p.end))[:2] == (p.start, p.end)

    def test_single_day_window(self):
        d = date(2026, 7, 20)
        start, end, prev_start, prev_end = resolve_window(d, (d, d))
        assert (start, end) == (d, d)
        assert (prev_start, prev_end) == (d - timedelta(days=1), d - timedelta(days=1))


class TestExplicitComparison:
    """`compare` is what keeps the shared weekly sections pointed at the same
    reference as everything else on the bi-weekly page.

    The default placement now happens to agree with `preceding_window`, so the
    argument looks redundant — it is not. It is what makes the agreement a
    guarantee rather than a coincidence, and the last test here is what would
    fail if either definition drifted.
    """

    def test_compare_is_used_verbatim(self):
        p = period_for(2026, 8, 2)
        window = (date(2026, 8, 15), date(2026, 8, 31))
        explicit = (date(2025, 1, 1), date(2025, 1, 9))     # deliberately odd
        start, end, prev_start, prev_end = resolve_window(p.end, window, explicit)
        assert (start, end) == window
        assert (prev_start, prev_end) == explicit

    def test_omitting_compare_keeps_the_weekly_behaviour_byte_for_byte(self):
        for d in (date(2026, 8, 3), date(2026, 1, 5), date(2027, 3, 1)):
            assert resolve_window(d) == resolve_window(d, None, None)

    def test_the_default_placement_agrees_with_preceding_window(self):
        """If these two ever disagree, the arrows inside Ads / Channel Mix /
        CRM / KOL would silently measure against a different window than every
        other number on the same page."""
        for year in (2027, 2028):
            for month in range(1, 13):
                for half in (1, 2):
                    p = period_for(year, month, half)
                    _, _, prev_start, prev_end = resolve_window(
                        p.end, (p.start, p.end))
                    assert (prev_start, prev_end) == preceding_window(p)
