"""Parsing of Lark Base field values.

Lark returns the same logical value in several shapes depending on whether a
field is hand-entered or computed by a formula. Reading only one shape made
every formula field look empty, which is what these tests pin down.
"""
from datetime import date

from app.services.lark_service import (
    _created_in_scope,
    _extract_number,
    _extract_text,
    _is_excluded_status,
    _is_excused,
    _norm_reason,
    _norm_status,
    _parse_date,
    _sane_days,
)


class TestExtractNumber:
    def test_formula_shape_wraps_value_in_a_list(self):
        # {'type': 2, 'value': [8]} — how Cycle Time actually arrives
        assert _extract_number({"type": 2, "value": [8]}) == 8.0

    def test_bare_number(self):
        assert _extract_number(3) == 3.0
        assert _extract_number(2.5) == 2.5

    def test_number_dict(self):
        assert _extract_number({"number": 4}) == 4.0

    def test_numeric_string(self):
        assert _extract_number("12") == 12.0

    def test_empty_and_unparseable(self):
        assert _extract_number(None) is None
        assert _extract_number("") is None
        assert _extract_number([]) is None
        assert _extract_number("n/a") is None

    def test_bool_is_not_a_number(self):
        assert _extract_number(True) is None

    def test_zero_is_kept_distinct_from_missing(self):
        assert _extract_number({"type": 2, "value": [0]}) == 0.0


class TestSaneDays:
    def test_accepts_a_normal_duration(self):
        assert _sane_days({"type": 2, "value": [8]}) == (8.0, False)

    def test_rejects_broken_formula_output(self):
        # Both seen in the live base
        assert _sane_days({"type": 2, "value": [46112]}) == (None, True)
        assert _sane_days({"type": 2, "value": [-7]}) == (None, True)

    def test_zero_is_rejected_not_averaged(self):
        assert _sane_days(0) == (None, True)

    def test_missing_is_not_flagged_as_broken(self):
        assert _sane_days(None) == (None, False)


class TestExtractText:
    def test_formula_shape(self):
        raw = {"type": 1, "value": [{"text": "Late", "type": "text"}]}
        assert _extract_text(raw) == "Late"

    def test_list_shape(self):
        assert _extract_text([{"text": "On-time"}]) == "On-time"

    def test_plain_string(self):
        assert _extract_text("  On-time  ") == "On-time"

    def test_empty(self):
        assert _extract_text(None) == ""
        assert _extract_text({"type": 1, "value": []}) == ""


class TestLateReason:
    def test_any_reason_excuses_the_miss(self):
        for option in ("Waiting for approval", "Scope / priority changed",
                       "Tool / platform issue"):
            assert _is_excused(_norm_reason(option)), option

    def test_an_option_added_in_lark_needs_no_code_change(self):
        assert _is_excused(_norm_reason("Sick / leave"))
        assert _is_excused(_norm_reason("Waiting on external party"))

    def test_no_reason_means_the_miss_counts(self):
        # Silence is never forgiveness
        assert not _is_excused(_norm_reason(""))
        assert not _is_excused(_norm_reason(None))
        assert not _is_excused(_norm_reason("   "))

    def test_reads_the_single_select_shape(self):
        raw = {"type": 1, "value": [{"text": "Waiting for approval", "type": "text"}]}
        assert _is_excused(_norm_reason(raw))

    def test_reason_text_is_normalized_for_grouping(self):
        # Feeds the per-reason breakdown, so variants must collapse to one key
        assert _norm_reason("Scope  /  Priority   Changed") == "scope/priority changed"
        assert _norm_reason({"type": 1, "value": [{"text": "Tool / platform issue"}]}) \
            == "tool/platform issue"


class TestExcludedStatus:
    def test_backlog_and_standing_work_are_out(self):
        assert _is_excluded_status(_norm_status("Upcoming Tasks"))
        assert _is_excluded_status(_norm_status("Regular task"))

    def test_case_and_spacing_variants(self):
        assert _is_excluded_status(_norm_status("  regular   task "))
        assert _is_excluded_status(_norm_status("UPCOMING TASKS"))

    def test_scored_statuses_stay_in(self):
        for s in ("Completed", "Ongoing", "Not started", "Blocked",
                  "Review", "Do Today", "Do This Week", "Do this Month",
                  "Suspended", ""):
            assert not _is_excluded_status(_norm_status(s)), s

    def test_reads_the_single_select_shape(self):
        raw = {"type": 1, "value": [{"text": "Regular task", "type": "text"}]}
        assert _is_excluded_status(_norm_status(raw))


class TestCreatedInScope:
    """Tasks with no deadline are only chased from the July 2026 cutoff on."""

    def test_created_on_or_after_the_cutoff_month(self):
        assert _created_in_scope("2026-07-01", 2026)
        assert _created_in_scope("2026-07-20", 2026)
        assert _created_in_scope("2026-12-31", 2026)

    def test_created_before_the_cutoff_is_out(self):
        assert not _created_in_scope("2026-06-30", 2026)
        assert not _created_in_scope("2026-01-15", 2026)

    def test_an_earlier_year_is_out(self):
        assert not _created_in_scope("2025-11-01", 2026)

    def test_unreadable_creation_date_stays_visible(self):
        # Hiding a real gap is worse than surfacing a stale one
        assert _created_in_scope(None, 2026)
        assert _created_in_scope("", 2026)
        assert _created_in_scope("not a date", 2026)

    def test_reads_a_millisecond_timestamp(self):
        assert _created_in_scope(1782838800000, 2026)       # 2026-07-01 ICT
        assert not _created_in_scope(1780160400000, 2026)   # 2026-05-31 ICT


class TestParseDate:
    def test_iso_string(self):
        assert _parse_date("2026-07-05") == date(2026, 7, 5)

    def test_lark_date_dict(self):
        assert _parse_date({"date": "2026-07-05"}) == date(2026, 7, 5)

    def test_timestamp_is_read_in_ict_not_utc(self):
        # Midnight ICT on 2026-07-01 is 17:00 UTC on 2026-06-30. Reading it in
        # UTC moved the task into the previous month.
        ms = 1782838800000  # 2026-07-01T00:00:00+07:00
        assert _parse_date(ms) == date(2026, 7, 1)

    def test_missing(self):
        assert _parse_date(None) is None
        assert _parse_date("") is None
