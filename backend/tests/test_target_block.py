"""`target_block` assembly.

Two things are pinned here.

The first is the shape of the payload. `target_block` is the one section that
mirrors part of itself at the top level (`month_pct`, `month_label`, …) for
consumers written before `months` existed, and a cached report is never
rebuilt on its own — so a shape change here goes unnoticed until a manager
opens a period from last month and finds the block blank.

The second is which months appear. The block now looks AHEAD as well as back:
the reporting month, then the months after it, so the team can see a soft
month while there is still time to sell into it. A look-ahead month with no
target and nothing booked is dropped rather than drawn as an empty gauge.
"""
from unittest.mock import patch

from app.services.biweekly_period import period_for
from app.services.biweekly_report_builder import TARGET_LOOKAHEAD_MONTHS, target_block


def _month(label, pct, status="closed", year=2026, month=7,
           actual=100.0, target=90.0, has_target=True):
    return {
        "year": year, "month": month, "label": label,
        "achievement": {"actual_revenue": actual, "target_revenue": target},
        "pct": pct, "closed": status == "closed", "status": status,
        "has_target": has_target, "is_override": False,
    }


def _run(p, side_effect, period_pct=0.90):
    with patch("app.services.biweekly_report_builder.compute_period_achievement",
               return_value={"achievement_pct": period_pct}), \
         patch("app.services.biweekly_report_builder._month_achievement",
               side_effect=side_effect):
        return target_block(db=None, branch=None, p=p)


class TestTargetBlockAssembly:
    def test_builds_a_complete_dict_for_the_reporting_month(self):
        p = period_for(2026, 7, 2)          # Jul 15–31, entirely within July
        assert p.start.month == p.end.month == 7

        result = _run(p, lambda db, branch, year, month, as_of: (
            _month("July 2026", 106.0) if month == 7
            else _month("later", None, "upcoming", month=month,
                        actual=0.0, target=0.0, has_target=False)
        ))

        assert [m["label"] for m in result["months"]] == ["July 2026"]
        assert result["month_label"] == "July 2026"
        assert result["month_pct"] == 106.0
        assert result["month_closed"] is True
        assert result["period_pct"] == 90.0
        assert result["light"] in ("g", "w", "b")
        # Dropped along with the "through {date}" wording it once fed — a
        # month's Actual is no longer capped to a date, so there's nothing
        # honest left to report here.
        assert "month_through" not in result

    def test_a_half_month_period_never_spans_two_months(self):
        for month in range(1, 13):
            for half in (1, 2):
                p = period_for(2026, month, half)
                assert p.start.month == p.end.month == p.month


class TestLookAhead:
    def test_shows_the_reporting_month_then_the_month_ahead(self):
        """Two gauges, not three. The second month out was cut (2026-08-17):
        its pickup is too thin to act on within this fortnight, and it pushed
        the month that IS actionable into a crowded row."""
        p = period_for(2026, 8, 1)          # Aug 1–14

        def fake(db, branch, year, month, as_of):
            return {
                8: _month("August 2026", 70.0, "in_progress", month=8),
                9: _month("September 2026", 41.0, "upcoming", month=9),
                10: _month("October 2026", 12.0, "upcoming", month=10),
            }[month]

        result = _run(p, fake)

        assert [m["label"] for m in result["months"]] == [
            "August 2026", "September 2026",
        ]
        assert len(result["months"]) == 1 + TARGET_LOOKAHEAD_MONTHS
        assert [m["status"] for m in result["months"][1:]] == ["upcoming"]

    def test_never_reaches_past_the_look_ahead_horizon(self):
        """The horizon is a query bound, not just a display filter — a month
        outside it is never asked for at all."""
        p = period_for(2026, 8, 1)
        asked = []

        def fake(db, branch, year, month, as_of):
            asked.append((year, month))
            return _month(f"m{month}", 50.0, "upcoming", month=month)

        _run(p, fake)
        assert asked == [(2026, 8), (2026, 9)]

    def test_top_level_mirrors_the_reporting_month_not_the_last_gauge(self):
        """The look-ahead months are forecast. `month_pct` has always meant
        "the month this report covers", and a consumer reading it must not
        silently start getting October's pickup instead."""
        p = period_for(2026, 8, 1)

        def fake(db, branch, year, month, as_of):
            return _month(f"m{month}", 70.0 if month == 8 else 5.0,
                          "in_progress" if month == 8 else "upcoming",
                          month=month)

        result = _run(p, fake)
        assert result["month_label"] == "m8"
        assert result["month_pct"] == 70.0

    def test_a_future_month_with_no_target_and_no_bookings_is_dropped(self):
        """Nothing planned, nothing sold — an empty 0/0 gauge is noise, so the
        block falls back to the reporting month alone."""
        p = period_for(2026, 8, 1)

        def fake(db, branch, year, month, as_of):
            if month == 8:
                return _month("August 2026", 70.0, "in_progress", month=8)
            return _month("September 2026", None, "upcoming", month=9,
                          actual=0.0, target=0.0, has_target=False)

        result = _run(p, fake)
        assert [m["label"] for m in result["months"]] == ["August 2026"]

    def test_a_future_month_with_bookings_but_no_target_is_kept(self):
        """No target set, but rooms are already on the books. That gap is
        exactly the thing this block exists to surface — dropping it would
        hide a month nobody has planned."""
        p = period_for(2026, 8, 1)

        def fake(db, branch, year, month, as_of):
            if month == 8:
                return _month("August 2026", 70.0, "in_progress", month=8)
            return _month("September 2026", None, "upcoming", month=9,
                          actual=250_000.0, target=0.0, has_target=False)

        result = _run(p, fake)
        assert [m["label"] for m in result["months"]] == [
            "August 2026", "September 2026",
        ]

    def test_crosses_the_year_boundary(self):
        p = period_for(2026, 12, 2)
        seen = []

        def fake(db, branch, year, month, as_of):
            seen.append((year, month))
            return _month(f"{year}-{month}", 50.0, "upcoming", year=year, month=month)

        _run(p, fake)
        assert seen == [(2026, 12), (2027, 1)]
