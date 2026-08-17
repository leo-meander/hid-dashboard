"""
Bi-Weekly reporting period — calendar halves of a month.

A period is one half of a calendar month:

  * **H1** — the 1st through the 14th. Always 14 days.
  * **H2** — the 15th through the last day of the month. 14 to 17 days,
    detected from the calendar; February follows the same rule as every other
    month and ends on its actual last day, 28 or 29.

The two halves tile the month exactly, and that is the whole point. Revenue
targets, CRM spend and the accounting close are all monthly, so a reporting
period that does not sum to a month forces every comparison against those
figures through a proration. ISO week pairs — what this module used to
produce — never lined up with a month at all.

Each period carries two comparisons, and they are built on different rules:

    current   15–31 Aug 2026 (17 days)
    preceding 29 Jul – 14 Aug 2026  — the 17 days immediately before
    YoY       15–31 Aug 2025        — the same calendar dates, a year back

`preceding_window` is the headline: "how are we doing versus the run-up to
this". It takes the period's own LENGTH backwards, so it is the same number
of days every time and its totals always compare directly. The trade is that
its boundaries are not a reporting period anybody has read — for an
end-of-month report it reaches back across the 15th into the first half of
the same month.

`yoy_window` is built the other way, on calendar dates, because the thing it
answers is seasonal: was this August better than last August. Carrying the
period's own day NUMBERS across is what makes 15–28 Feb land on 15–28 Feb,
and it is why the spec writes the year-ago window as dates rather than as a
day count.

What this costs, and what callers have to do about it:

  1. **A leap February breaks the YoY length**, in either direction — 15–29
     Feb 2028 can only reach a 14-day 15–28 Feb 2027. Callers check
     `comparable_as_totals()` and fall back to per-day figures when it is
     False. The preceding window is equal-length by construction, so it never
     trips this.
  2. **Weekday composition drifts.** 1–14 always holds exactly two of each
     weekday, but 15–31 Aug can hold three Saturdays where 15–31 Aug 2025
     holds two. Rate metrics (ADR, OCC, RevPAR) absorb that; totals see it as
     noise. This one is accepted — calendar alignment won the trade
     because the demand drivers a manager reads this report for (Tet, Golden
     Week, Obon, payday weekends, OTA campaign calendars) are anchored to
     calendar dates, not to ISO week numbers.

Kept in its own module — no DB, no ORM, no app config — so the arithmetic is
trivially unit-testable.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta

# En-dash in labels, matching the report mockup ("Aug 1–14, 2026").
_EN_DASH = "–"

#: H2 always starts here; H1 always ends the day before. Fixed by the
#: reporting spec, not derived from the month's length.
SPLIT_DAY = 15

_HALF_NAME = {1: "1st half", 2: "2nd half"}


def days_in_month(year: int, month: int) -> int:
    """Last day of `month` — 28, 29, 30 or 31, never hard-coded."""
    return calendar.monthrange(year, month)[1]


def _bounds(year: int, month: int, half: int) -> tuple[date, date]:
    if half == 1:
        return date(year, month, 1), date(year, month, SPLIT_DAY - 1)
    return (
        date(year, month, SPLIT_DAY),
        date(year, month, days_in_month(year, month)),
    )


def shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    """`(year, month)` moved `delta` months, wrapping the year."""
    idx = (year * 12 + (month - 1)) + delta
    return idx // 12, idx % 12 + 1


@dataclass(frozen=True)
class Period:
    """One bi-weekly reporting period — half of a calendar month."""

    year: int
    month: int
    half: int            # 1 → 1st–14th, 2 → 15th–last day
    start: date
    end: date

    @property
    def key(self) -> str:
        """Stable identifier used as the cache key and URL parameter."""
        return f"{self.year:04d}-{self.month:02d}-H{self.half}"

    @property
    def label(self) -> str:
        return f"{self.start:%b} {self.year} · {_HALF_NAME[self.half]}"

    @property
    def date_label(self) -> str:
        """e.g. 'Aug 1–14, 2026' — how the period reads to a manager."""
        return (
            f"{self.start:%b} {self.start.day}{_EN_DASH}{self.end.day}, {self.year}"
        )

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def is_extended(self) -> bool:
        """True when the period runs longer than the plain 14 days — any H2 of
        a 30- or 31-day month. The renderer labels the length when this is set,
        because a 17-day total is not comparable head-on with a 14-day one.
        """
        return self.days > 14

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "date_label": self.date_label,
            "year": self.year,
            "month": self.month,
            "half": self.half,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "days": self.days,
            "is_extended": self.is_extended,
        }


def period_for(year: int, month: int, half: int) -> Period:
    """Build the `half` (1 or 2) of `month`."""
    if month < 1 or month > 12:
        raise ValueError(f"month must be 1–12, got {month}")
    if half not in (1, 2):
        raise ValueError(f"half must be 1 or 2, got {half}")
    start, end = _bounds(year, month, half)
    return Period(year=year, month=month, half=half, start=start, end=end)


def period_containing(d: date) -> Period:
    """The period that `d` falls inside."""
    return period_for(d.year, d.month, 1 if d.day < SPLIT_DAY else 2)


def previous_period(p: Period) -> Period:
    """The period immediately before `p`, crossing months and years.

    Used to walk the period list backwards for the picker. It is deliberately
    NOT the report's comparison window: `preceding_window` takes the period's
    LENGTH backwards, which for an end-of-month report is not this at all.
    """
    if p.half == 2:
        return period_for(p.year, p.month, 1)
    year, month = shift_month(p.year, p.month, -1)
    return period_for(year, month, 2)


def next_period(p: Period) -> Period:
    """The period immediately after `p`, crossing months and years."""
    if p.half == 1:
        return period_for(p.year, p.month, 2)
    year, month = shift_month(p.year, p.month, 1)
    return period_for(year, month, 1)


def current_period(today: date) -> Period:
    """The period a report sent today would cover.

    The reporting spec sends on the 15th (covering 1–14) and on the last
    calendar day of the month (covering 15–EOM). So a period becomes the
    default on its own final day, not the morning after — otherwise the
    end-of-month report would not exist yet on the day it is due to go out.

    That final day is still in progress when the report is built. `is_complete`
    is what says so, and the renderer prints it; nothing here pretends the day
    has closed.
    """
    p = period_containing(today)
    if p.end <= today:
        return p
    return previous_period(p)


def is_complete(p: Period, today: date) -> bool:
    """True once every day of `p` has fully passed."""
    return p.end < today


def preceding_window(p: Period) -> tuple[date, date]:
    """The same number of days, ending the day before `p` starts.

    The report's headline comparison: "versus the run-up to this". Aug 1–14
    meets Jul 18–31; Aug 15–31, being 17 days, meets Jul 29 – Aug 14.

    Two consequences worth being explicit about.

    It is the period's own LENGTH taken backwards, not the previous reporting
    period, so it is always exactly as long as `p` and its totals always
    compare directly — no per-day fallback ever applies to this window.

    It is also not a window anyone has read a report for. For an end-of-month
    period it reaches back across the 15th and is mostly the first half of the
    same month, which is why the header names its dates rather than calling it
    "the last report".
    """
    end = p.start - timedelta(days=1)
    return end - timedelta(days=p.days - 1), end


def yoy_window(p: Period) -> tuple[date, date]:
    """Same calendar DATES, previous year.

    Built on dates rather than on a day count, because the question it answers
    is seasonal — was this August better than last August. `p`'s own day
    numbers are carried across, so 15–28 Feb lands on 15–28 Feb.

    The end is capped at the reference month's real last day, the one
    direction the calendar cannot be argued with. That makes the window the
    same length as `p` everywhere except across a leap February, in either
    direction: 15–29 Feb 2028 can only reach a 14-day 15–28 Feb 2027, and
    15–28 Feb 2029 stops at the 28th rather than being handed 2028's extra
    day to lose against. `comparable_as_totals` catches what remains.
    """
    year = p.year - 1
    if p.half == 1:
        return date(year, p.month, 1), date(year, p.month, SPLIT_DAY - 1)
    return (
        date(year, p.month, SPLIT_DAY),
        date(year, p.month, min(p.end.day, days_in_month(year, p.month))),
    )


def window_days(window: tuple[date, date]) -> int:
    return (window[1] - window[0]).days + 1


def comparable_as_totals(p: Period, window: tuple[date, date]) -> bool:
    """True when `window` is the same length as `p`, so totals can be
    compared directly. When False, compare per-day averages instead.
    """
    return window_days(window) == p.days


def parse_period_key(key: str) -> Period:
    """Parse '2026-08-H2' back into a Period."""
    try:
        year_str, month_str, half_str = key.strip().upper().split("-")
        if not half_str.startswith("H"):
            raise ValueError(half_str)
        return period_for(int(year_str), int(month_str), int(half_str[1:]))
    except (ValueError, AttributeError) as e:
        raise ValueError(
            f"Invalid period key {key!r} — expected e.g. '2026-08-H2'"
        ) from e


def list_periods(today: date, back: int = 12) -> list[Period]:
    """The `back` most recent reportable periods, newest first."""
    out = []
    p = current_period(today)
    for _ in range(max(1, back)):
        out.append(p)
        p = previous_period(p)
    return out
