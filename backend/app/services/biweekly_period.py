"""
Bi-Weekly reporting period — ISO-week pairs.

A period is two consecutive ISO weeks of an ISO year: W1–W2, W3–W4, … W51–W52.
Branch managers asked for "week of the year" framing rather than calendar
halves, and it buys a real analytical benefit: the same week numbers in the
previous ISO year land on the same weekdays, so a year-over-year comparison
is not distorted by a period that happened to catch three Saturdays.

Two edge cases drive most of the code here:

  1. **53-week ISO years.** An ISO year has 53 weeks when 1 Jan falls on a
     Thursday, or on a Wednesday in a leap year. 2026 is one of them. Pairing
     weeks (1,2), (3,4), … leaves W53 orphaned, so it folds into the final
     period: P26 = W51–W53, 21 days long.

  2. **Comparison windows of a different length.** Because of (1), a 21-day
     period can be compared against a 14-day window in a 52-week prior year.
     Comparing those totals directly would invent a ~33% decline. Callers must
     check `comparable_as_totals()` and fall back to per-day figures.

Kept in its own module — no DB, no ORM, no app config — so the arithmetic is
trivially unit-testable.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

# En-dash in labels, matching the report mockup ("Week 29–30 · 2026").
_EN_DASH = "–"


def iso_weeks_in_year(iso_year: int) -> int:
    """Number of ISO weeks in `iso_year` — 52 or 53.

    28 December is always in the last ISO week of its ISO year (it is the
    earliest date that can be), which makes this a lookup rather than a rule.
    """
    return date(iso_year, 12, 28).isocalendar()[1]


@dataclass(frozen=True)
class Period:
    """One bi-weekly reporting period."""

    iso_year: int
    week_a: int          # first ISO week (always odd)
    week_b: int          # last ISO week — week_a + 1, or + 2 when W53 folds in
    start: date          # Monday of week_a
    end: date            # Sunday of week_b

    @property
    def key(self) -> str:
        """Stable identifier used as the cache key and URL parameter."""
        return f"{self.iso_year}-W{self.week_a:02d}"

    @property
    def label(self) -> str:
        return f"Week {self.week_a}{_EN_DASH}{self.week_b} · {self.iso_year}"

    @property
    def date_label(self) -> str:
        """e.g. 'Jul 13 – Jul 26, 2026' — how the period reads to a manager."""
        return (
            f"{self.start.strftime('%b %d')} {_EN_DASH} "
            f"{self.end.strftime('%b %d')}, {self.end.year}"
        )

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def is_extended(self) -> bool:
        """True for the 21-day W51–W53 period of a 53-week ISO year."""
        return self.week_b - self.week_a > 1

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "date_label": self.date_label,
            "iso_year": self.iso_year,
            "week_a": self.week_a,
            "week_b": self.week_b,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "days": self.days,
            "is_extended": self.is_extended,
        }


def period_for(iso_year: int, week_a: int) -> Period:
    """Build the period starting at ISO week `week_a` (must be odd)."""
    if week_a < 1 or week_a > 51 or week_a % 2 == 0:
        raise ValueError(
            f"week_a must be an odd ISO week between 1 and 51, got {week_a}"
        )
    last_week = iso_weeks_in_year(iso_year)
    # W53 has no partner of its own — it joins the final pair.
    week_b = 53 if (week_a == 51 and last_week == 53) else week_a + 1
    return Period(
        iso_year=iso_year,
        week_a=week_a,
        week_b=week_b,
        start=date.fromisocalendar(iso_year, week_a, 1),
        end=date.fromisocalendar(iso_year, week_b, 7),
    )


def period_containing(d: date) -> Period:
    """The period that `d` falls inside."""
    iso_year, week, _ = d.isocalendar()
    if week == 53:
        # Folded into W51–W53 rather than starting a period of its own.
        week_a = 51
    else:
        week_a = week - ((week - 1) % 2)
    return period_for(iso_year, week_a)


def previous_period(p: Period) -> Period:
    """The period immediately before `p`, crossing ISO years when needed."""
    if p.week_a == 1:
        # Every ISO year's last period starts at W51, whether it holds 2 or 3
        # weeks — 52-week years end (51,52), 53-week years end (51,52,53).
        return period_for(p.iso_year - 1, 51)
    return period_for(p.iso_year, p.week_a - 2)


def next_period(p: Period) -> Period:
    """The period immediately after `p`, crossing ISO years when needed."""
    if p.week_a == 51:
        return period_for(p.iso_year + 1, 1)
    return period_for(p.iso_year, p.week_a + 2)


def current_period(today: date) -> Period:
    """The most recently *fully completed* period.

    A period the calendar is still inside is never reported on — half a
    period of data reads as a collapse. Matches the weekly report's rule
    that "last week" only rolls once the week has fully passed, so on the
    Monday after a period closes the new report is already available.
    """
    p = period_containing(today)
    if p.end < today:
        return p
    return previous_period(p)


def yoy_window(p: Period) -> tuple[date, date]:
    """Same ISO week numbers, previous ISO year.

    When `p` is the extended W51–W53 period and the prior ISO year only had
    52 weeks, the window is clamped to W51–W52 — 14 days against 21. That is
    intentional: inventing a 53rd week would be worse. `comparable_as_totals`
    is what stops a caller from comparing the two as raw totals.
    """
    prev_year = p.iso_year - 1
    last_week = iso_weeks_in_year(prev_year)
    week_b = min(p.week_b, last_week)
    return (
        date.fromisocalendar(prev_year, p.week_a, 1),
        date.fromisocalendar(prev_year, week_b, 7),
    )


def previous_window(p: Period) -> tuple[date, date]:
    """Date range of the period immediately before `p`."""
    prev = previous_period(p)
    return prev.start, prev.end


def window_days(window: tuple[date, date]) -> int:
    return (window[1] - window[0]).days + 1


def comparable_as_totals(p: Period, window: tuple[date, date]) -> bool:
    """True when `window` is the same length as `p`, so totals can be
    compared directly. When False, compare per-day averages instead.
    """
    return window_days(window) == p.days


def parse_period_key(key: str) -> Period:
    """Parse '2026-W29' back into a Period."""
    try:
        year_str, week_str = key.strip().upper().split("-W")
        return period_for(int(year_str), int(week_str))
    except (ValueError, AttributeError) as e:
        raise ValueError(f"Invalid period key {key!r} — expected e.g. '2026-W29'") from e


def list_periods(today: date, back: int = 12) -> list[Period]:
    """The `back` most recent completed periods, newest first."""
    out = []
    p = current_period(today)
    for _ in range(max(1, back)):
        out.append(p)
        p = previous_period(p)
    return out
