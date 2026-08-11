"""`target_block`'s dict assembly.

Its two DB-touching dependencies — `compute_period_achievement` and
`_month_achievement` — are mocked out here, because what actually broke in
production wasn't either of them: it was `target_block` itself reaching for
a `"through"` key that `_month_achievement` had stopped returning a commit
earlier in the same PR. `safe_section` in the caller swallows any exception
from a section and degrades it to `{}`, so that KeyError never surfaced as
an error anywhere — Target Achievement just went blank for every branch,
silently, until the payload was inspected by hand. These tests exercise the
assembly directly against a real DB session isn't available in this
environment) so a stale key reference here fails loudly instead.
"""
from unittest.mock import patch

from app.services.biweekly_period import period_for
from app.services.biweekly_report_builder import target_block


def _month(label, pct, closed):
    return {
        "year": 2026, "month": 7, "label": label,
        "achievement": {"actual_revenue": 100.0, "target_revenue": 90.0},
        "pct": pct, "closed": closed, "is_override": False,
    }


class TestTargetBlockAssembly:
    def test_single_month_period_builds_a_complete_dict(self):
        p = period_for(2026, 29)   # Jul 13 – 26, entirely within July
        assert p.start.month == p.end.month == 7

        with patch("app.services.biweekly_report_builder.compute_period_achievement",
                   return_value={"achievement_pct": 0.90}), \
             patch("app.services.biweekly_report_builder._month_achievement",
                   return_value=_month("July 2026", 106.0, True)):
            result = target_block(db=None, branch=None, p=p)

        assert result["months"] == [_month("July 2026", 106.0, True)]
        assert result["month_label"] == "July 2026"
        assert result["month_pct"] == 106.0
        assert result["month_closed"] is True
        assert result["period_pct"] == 90.0
        assert result["light"] in ("g", "w", "b")
        # Dropped along with the "through {date}" wording it once fed — a
        # month's Actual is no longer capped to a date, so there's nothing
        # honest left to report here.
        assert "month_through" not in result

    def test_period_spanning_two_months_builds_a_complete_dict(self):
        """The exact production case that broke: Week 31–32 2026 runs
        Jul 27 – Aug 9, so target_block calls _month_achievement once for
        July and once for August."""
        p = period_for(2026, 31)
        assert (p.start.month, p.end.month) == (7, 8)

        def fake_month_achievement(db, branch, year, month, as_of):
            return (_month("July 2026", 94.0, True) if month == 7
                   else _month("August 2026", 85.0, False))

        with patch("app.services.biweekly_report_builder.compute_period_achievement",
                   return_value={"achievement_pct": 2.065}), \
             patch("app.services.biweekly_report_builder._month_achievement",
                   side_effect=fake_month_achievement):
            result = target_block(db=None, branch=None, p=p)

        assert [m["label"] for m in result["months"]] == ["July 2026", "August 2026"]
        # Top-level fields mirror the LATEST month (the one the period ends
        # in) for backward compatibility with anything reading this payload
        # from before `months` existed.
        assert result["month_label"] == "August 2026"
        assert result["month_pct"] == 85.0
        assert result["month_closed"] is False
        assert "month_through" not in result
