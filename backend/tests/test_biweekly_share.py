"""Emailing a branch's report, and the no-login page the email opens.

Two things make this worth pinning harder than an ordinary renderer.

The first is that the shared page is served WITHOUT a session, so anything it
fails to print is unreachable — the reader has no dashboard to go and look in.
The comment appendix is the part that has to survive: threads, replies, who
wrote them, and replies whose parent was deleted.

The second is that the page prints text people typed. On the authenticated
report that text goes in raw and always has; on a page handed to someone
outside the app it is escaped, and these tests are what stops that quietly
regressing.
"""
from datetime import date, datetime, timezone

import pytest

from app.services.biweekly_period import period_for
from app.services.biweekly_share import (
    _flag_texts,
    _labels_from_html,
    build_share_page_html,
    build_summary_email_html,
    render_comment_appendix,
)
from tests.test_biweekly_render import _payload

P = period_for(2026, 8, 2)          # Aug 15–31, 17 days
COMPUTED = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _comment(cid, body, key="bw._general", parent=None, author="Mason",
             resolved=False, when="2026-08-20T03:00:00+00:00"):
    return {
        "id": cid, "metric_key": key, "body": body, "author_name": author,
        "parent_comment_id": parent, "is_resolved": resolved,
        "created_at": when,
    }


def _with_targets(b=None):
    b = b or _payload()
    b["target"] = {
        "period": {"actual_revenue": 32_000_000, "target_revenue": 15_500_000},
        "period_pct": 206.5,
        "months": [
            {"label": "August 2026", "status": "in_progress", "pct": 70.0,
             "closed": False, "is_override": False,
             "achievement": {"actual_revenue": 2_900_000,
                             "target_revenue": 4_200_000}},
            {"label": "September 2026", "status": "upcoming", "pct": 31.0,
             "closed": False, "is_override": False,
             "achievement": {"actual_revenue": 1_200_000,
                             "target_revenue": 3_900_000}},
        ],
    }
    return b


class TestSummaryEmail:
    URL = "https://api.example.com/api/biweekly/shared/TOKEN123"

    def _html(self, **kw):
        return build_summary_email_html(_with_targets(), P, self.URL, **kw)

    def test_carries_the_headline_numbers_and_the_button(self):
        html = self._html(recipient_name="Gary")
        assert "Gary" in html
        assert P.date_label in html
        assert self.URL in html
        assert "View the full report" in html
        for label in ("REVENUE", "AVG ROOM RATE (ADR)", "OCCUPANCY", "REVPAR"):
            assert label in html

    def test_names_both_comparison_windows(self):
        """The headline delta says how long its window is. The two comparisons
        on this page are built on different rules — a day count backwards and
        a calendar range — so an unlabelled arrow would be ambiguous."""
        html = self._html()
        assert "vs prev 17d" in html          # P is Aug 15–31, 17 days
        assert "vs last year" in html
        assert "the 17 days before it and the same dates last year" in html

    def test_shows_the_months_ahead_as_booked_not_as_achievement(self):
        """The same distinction the full report's gauges make. An upcoming
        month at 31% is pickup, and an email that presents it as a miss is
        the fastest way to make the number ignored."""
        html = self._html()
        assert "August 2026" in html
        assert "September 2026" in html
        assert "booked so far" in html

    def test_greets_without_a_name_when_there_isnt_one(self):
        assert "Hi," in build_summary_email_html(_with_targets(), P, self.URL)

    def test_states_the_link_needs_no_login_and_should_not_be_forwarded(self):
        """The link IS the credential. A recipient who does not know that
        forwards it to a group chat."""
        html = self._html(expires_on=date(2026, 12, 15))
        assert "no HiD login needed" in html
        assert "don't forward" in html
        assert "15 Dec 2026" in html

    def test_the_url_is_attribute_escaped(self):
        html = build_summary_email_html(
            _with_targets(), P, 'https://x.test/a"onmouseover="evil()')
        assert 'onmouseover="evil()' not in html
        assert "&quot;" in html

    def test_survives_a_payload_with_nothing_in_it(self):
        """Cached periods predate half these keys. An email that raises on an
        old cache is worse than one with empty panels."""
        html = build_summary_email_html({"branch_name": "Meander Osaka"},
                                        P, self.URL)
        assert "Meander Osaka" in html
        assert "View the full report" in html


class TestFlagTexts:
    def test_generated_markup_survives_and_typed_text_is_escaped(self):
        b = {"highlights": [
            {"key": "flag.direct", "text": "<b>Direct is 30%</b> of revenue"},
            {"key": "flag.adr", "text": "<b>oops</b>", "edited": True},
        ]}
        assert _flag_texts(b, "highlights") == [
            ("<b>Direct is 30%</b> of revenue", False),
            ("<b>oops</b>", True),
        ]

    def test_a_legacy_list_of_plain_strings_still_reads(self):
        assert _flag_texts({"highlights": ["just a line"]}, "highlights") == [
            ("just a line", False)
        ]

    def test_missing_or_wrongly_shaped_keys_yield_nothing(self):
        assert _flag_texts({}, "highlights") == []
        assert _flag_texts({"highlights": {"a": 1}}, "highlights") == []

    def test_an_operator_edited_line_cannot_inject_markup(self):
        b = _with_targets()
        b["watchouts"] = [{"key": "flag.x", "edited": True,
                           "text": "<img src=x onerror=alert(1)>"}]
        html = build_summary_email_html(b, P, "https://x.test/t")
        assert "<img src=x" not in html
        assert "&lt;img src=x" in html


class TestLabelsFromHtml:
    def test_reads_key_and_label_off_rendered_cells(self):
        markup = build_share_page_html(_payload(), P, COMPUTED, [])
        labels = _labels_from_html(markup)
        assert labels, "the report should carry data-metric-label attributes"
        assert all(isinstance(k, str) and isinstance(v, str)
                   for k, v in labels.items())


class TestCommentAppendix:
    def test_nothing_to_show_renders_nothing(self):
        """An empty "Notes" heading claims the team said nothing, which is a
        different statement from "nobody has written here yet"."""
        assert render_comment_appendix([], "<html></html>") == ""

    def test_groups_threads_under_a_human_label(self):
        html = render_comment_appendix(
            [_comment("1", "Lift out of service"),
             _comment("2", "Need photos", key="bw._support")],
            "<html></html>",
        )
        assert "Branch Manager's Notes" in html
        assert "Support Needed From The Branch" in html
        # The apostrophe is printed, not entity-escaped into the reader's face.
        assert "Manager&#x27;s" not in html

    def test_replies_are_nested_under_their_parent(self):
        html = render_comment_appendix(
            [_comment("1", "parent line"), _comment("2", "reply line", parent="1")],
            "<html></html>",
        )
        assert html.index("parent line") < html.index("reply line")
        assert "margin:0 0 8px 26px" in html          # the reply indent

    def test_a_reply_whose_parent_was_deleted_still_appears(self):
        """Soft-deleted parents are filtered out by the query, so without a
        fallback the reply would vanish from the page entirely — and the
        reader has no other place to find it."""
        html = render_comment_appendix(
            [_comment("2", "orphaned reply", parent="gone")], "<html></html>")
        assert "orphaned reply" in html

    def test_a_resolved_support_item_is_marked_done(self):
        html = render_comment_appendix(
            [_comment("1", "handled", key="bw._support", resolved=True)],
            "<html></html>")
        assert "Done" in html

    def test_counts_the_notes_in_each_group(self):
        html = render_comment_appendix(
            [_comment("1", "a"), _comment("2", "b"), _comment("3", "c")],
            "<html></html>")
        assert "3 notes" in html
        assert "1 note<" in render_comment_appendix(
            [_comment("1", "a")], "<html></html>")

    @pytest.mark.parametrize("body,leaked,escaped", [
        ("<script>alert(1)</script>", "<script>alert(1)</script>", "&lt;script&gt;"),
        ("<img src=x onerror=alert(1)>", "<img src=x", "&lt;img src=x"),
    ])
    def test_comment_bodies_are_escaped(self, body, leaked, escaped):
        html = render_comment_appendix([_comment("1", body)], "<html></html>")
        assert leaked not in html
        assert escaped in html

    def test_author_names_are_escaped_too(self):
        html = render_comment_appendix(
            [_comment("1", "hi", author="<b>Boss</b>")], "<html></html>")
        assert "&lt;b&gt;Boss&lt;/b&gt;" in html

    def test_newlines_become_line_breaks(self):
        html = render_comment_appendix(
            [_comment("1", "line one\nline two")], "<html></html>")
        assert "line one<br>line two" in html


class TestSharePage:
    def test_is_the_full_report_plus_the_notes(self):
        html = build_share_page_html(
            _payload(), P, COMPUTED,
            [_comment("1", "Lift out of service Aug 18–22")])
        assert html.lstrip().startswith("<!DOCTYPE html>")
        assert html.rstrip().endswith("</html>")
        assert "Executive Summary" in html          # the real report
        assert "Notes &amp; discussion" in html     # and its threads
        assert "Lift out of service" in html

    def test_the_appendix_sits_inside_the_page_not_after_the_footer(self):
        html = build_share_page_html(_payload(), P, COMPUTED,
                                     [_comment("1", "a note")])
        assert html.index("Notes &amp; discussion") < html.index(
            "Bi-weekly Branch Manager Report ·")

    def test_a_report_with_no_notes_is_left_exactly_as_rendered(self):
        html = build_share_page_html(_payload(), P, COMPUTED, [])
        assert "Notes &amp; discussion" not in html
        assert html.rstrip().endswith("</html>")

    def test_says_the_page_is_read_only(self):
        """There is no way to reply from here — the reader has no session.
        Saying so beats them typing into a page that cannot save."""
        html = build_share_page_html(_payload(), P, COMPUTED,
                                     [_comment("1", "a note")])
        assert "read-only" in html
