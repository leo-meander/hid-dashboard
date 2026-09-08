"""Fill Pace — how fast a stay month is filling, versus the same run-up a year ago.

The page exists to answer one question: "over the last 60 days, did December
fill faster than it did last year". Everything that can quietly turn that answer
into a lie is asserted here.

  · Nights are clipped to the stay month. A stay straddling 30 Nov → 2 Dec must
    put one night in December, not five, and not five in both months.
  · The two years are aligned by DISTANCE FROM THE MONTH, never by calendar
    date. Leap years make those two different, and the leap case is the test.
  · The opening balance is what was already on the books when the window
    opened. Pickup is what the window itself added — the slope, which is the
    actual speed. Conflating them makes a mature month look fast.
  · A zero year-ago base has no growth rate. Taipei and Oani were not open in
    some of these months; printing a percentage there would invent a number.
  · The group roll-up sums room-nights. Averaging branch percentages would let
    a 12-room property outvote a 60-room one.

No database: the row set is crafted and the arithmetic is the whole of what is
checked. The one query that does touch SQL is asserted by compiling it.
"""
from datetime import date

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from app.services import fill_pace
from app.services.fill_pace import (
    build_curve,
    change_pct,
    fetch_month_rows,
    get_fill_pace,
    month_bounds,
    pct,
    pts,
)


# ── fakes ────────────────────────────────────────────────────────────────────

class Row:
    """One (branch, booking date, source) bucket as the query returns it."""

    def __init__(self, branch_id, reservation_date, room_nights, bookings=1,
                 source="Agoda", source_category="OTA", revenue_native=0.0,
                 revenue_vnd=0.0):
        self.branch_id = branch_id
        self.reservation_date = reservation_date
        self.room_nights = room_nights
        self.bookings = bookings
        self.source = source
        self.source_category = source_category
        self.revenue_native = revenue_native
        self.revenue_vnd = revenue_vnd


class FakeBranch:
    def __init__(self, bid, name, rooms, currency="TWD", room_count=None, dorm_count=None):
        self.id, self.name, self.total_rooms, self.currency = bid, name, rooms, currency
        self.total_room_count, self.total_dorm_count = room_count, dorm_count


class FakeBranchQuery:
    def __init__(self, branches):
        self._branches = branches

    def filter_by(self, **kwargs):
        assert kwargs == {"is_active": True}
        return self

    def all(self):
        return self._branches


class FakeDB:
    def __init__(self, branches):
        self._branches = branches

    def query(self, *args):
        return FakeBranchQuery(self._branches)


@pytest.fixture
def branches():
    return [
        FakeBranch("b-saigon", "Saigon", 20, "VND", room_count=12, dorm_count=8),
        FakeBranch("b-taipei", "Taipei", 30, "TWD", room_count=30, dorm_count=0),
    ]


@pytest.fixture
def stub_rows(monkeypatch):
    """Serve a crafted row set per (year, month); record every call made."""
    def install(by_month, record=None):
        def fake(db, branch_id, year, month, room_category=None):
            if record is not None:
                record.append((year, month, branch_id, room_category))
            return list(by_month.get((year, month), []))
        monkeypatch.setattr(fill_pace, "fetch_month_rows", fake)
    return install


# ── the arithmetic primitives ────────────────────────────────────────────────

def test_rates_need_a_denominator():
    assert pct(50, 200) == 25.0
    assert pct(50, 0) is None
    assert pts(50, 200, 40, 200) == 5.0
    assert pts(50, 200, 40, 0) is None


def test_zero_year_ago_base_has_no_growth_rate():
    """Taipei and Oani have months with no year-ago rows at all. A branch that
    booked 40 room-nights against a base of nothing did not grow by any
    percentage — the honest answer is that there is no base."""
    assert change_pct(40, 0) is None
    assert change_pct(0, 0) is None
    assert change_pct(150, 100) == 50.0
    assert change_pct(50, 100) == -50.0


def test_month_bounds_end_is_exclusive():
    start, end_excl, dim = month_bounds(2026, 12)
    assert (start, end_excl, dim) == (date(2026, 12, 1), date(2027, 1, 1), 31)
    assert month_bounds(2024, 2)[2] == 29


# ── the curve ────────────────────────────────────────────────────────────────

def _window(start: date, n: int) -> list[date]:
    from datetime import timedelta
    return [start + timedelta(days=i) for i in range(n)]


def test_opening_balance_is_separate_from_pickup():
    """What was already sold before the window opened is not speed. It sets the
    line's starting height; only what lands inside the window is pickup."""
    dates = _window(date(2026, 7, 11), 5)
    rows = [
        Row("b", date(2026, 3, 1), 100),   # long before the window
        Row("b", date(2026, 7, 12), 10),
        Row("b", date(2026, 7, 14), 5),
    ]
    curve, summary = build_curve(rows, dates)

    assert summary["opening_room_nights"] == 100
    assert summary["otb_room_nights"] == 115
    assert summary["pickup_room_nights"] == 15
    # The line starts at the opening balance, not at zero.
    assert curve[0]["otb_room_nights"] == 100
    assert [p["otb_room_nights"] for p in curve] == [100, 110, 110, 115, 115]
    assert [p["day_room_nights"] for p in curve] == [0, 10, 0, 5, 0]


def test_bookings_after_the_snapshot_stay_off_the_curve_but_count_as_final():
    """`final` is where the month ended up — the number that says how much more
    came in after this point last year. It must not leak into the curve, which
    is a reconstruction of what was known at each as-of date."""
    dates = _window(date(2025, 7, 11), 3)
    rows = [
        Row("b", date(2025, 7, 12), 10),
        Row("b", date(2025, 11, 20), 90),   # booked long after the window closed
    ]
    curve, summary = build_curve(rows, dates)

    assert curve[-1]["otb_room_nights"] == 10
    assert summary["otb_room_nights"] == 10
    assert summary["final_room_nights"] == 100


def test_undated_bookings_join_the_opening_balance_and_are_reported():
    """A handful of legacy rows carry no reservation_date. They are on the books
    but cannot be placed in time, so they are visible rather than invented."""
    dates = _window(date(2026, 7, 11), 3)
    curve, summary = build_curve([Row("b", None, 7), Row("b", date(2026, 7, 12), 3)], dates)

    assert summary["undated_room_nights"] == 7
    assert summary["opening_room_nights"] == 7
    assert curve[0]["otb_room_nights"] == 7
    assert summary["pickup_room_nights"] == 3


def test_empty_month_still_returns_a_flat_line():
    dates = _window(date(2026, 7, 11), 4)
    curve, summary = build_curve([], dates)
    assert [p["otb_room_nights"] for p in curve] == [0, 0, 0, 0]
    assert summary["otb_room_nights"] == 0
    assert summary["pickup_room_nights"] == 0


# ── year-over-year alignment ─────────────────────────────────────────────────

def test_last_year_is_read_at_the_same_distance_from_the_month(branches, stub_rows):
    """The whole comparison rests on this. 45 days before 1 Mar 2025 is 15 Jan
    2025; 45 days before 1 Mar 2024 is 16 Jan 2024, because 2024 had a 29th of
    February in between. Shifting the calendar date by a year instead would
    compare two different distances from check-in and quietly bias the answer."""
    stub_rows({})
    result = get_fill_pace(
        FakeDB(branches), branch_id=None, months=[(2025, 3)],
        days=46, as_of=date(2025, 1, 15),
    )

    assert result["days_out"]["to"] == 45
    assert result["last_year"]["as_of"] == "2024-01-16"
    assert result["last_year"]["window"]["to"] == "2024-01-16"
    # Same distance at both ends of the window.
    assert result["curve"][-1]["days_out"] == 45
    assert result["curve"][-1]["ly_date"] == "2024-01-16"


def test_february_denominators_use_each_year_own_length(branches, stub_rows):
    """Feb 2024 had 29 days and Feb 2025 had 28. Comparing room-night counts
    without that would hand last year a 3.5% head start on occupancy."""
    stub_rows({})
    result = get_fill_pace(
        FakeDB(branches), branch_id=None, months=[(2025, 2)],
        days=30, as_of=date(2025, 1, 1),
    )
    units = 20 + 30
    assert result["scope"]["available_room_nights"] == units * 28
    assert result["last_year"]["available_room_nights"] == units * 29


def test_pace_index_and_gaps_answer_faster_or_slower(branches, stub_rows):
    stub_rows({
        (2026, 12): [Row("b-saigon", date(2026, 8, 1), 90)],
        (2025, 12): [Row("b-saigon", date(2025, 8, 1), 60)],
    })
    result = get_fill_pace(
        FakeDB(branches), branch_id=None, months=[(2026, 12)],
        days=60, as_of=date(2026, 9, 8),
    )

    vs = result["vs_last_year"]
    assert vs["pace_index"] == 1.5                    # 90 picked up against 60
    assert vs["pickup_room_nights_pct"] == 50.0
    assert result["current"]["pickup_room_nights"] == 90
    assert result["last_year"]["pickup_room_nights"] == 60


def test_no_year_ago_bookings_leaves_the_comparison_blank(branches, stub_rows):
    """A branch that had not opened yet has no base. The page must say so
    instead of printing an infinity."""
    stub_rows({(2026, 12): [Row("b-taipei", date(2026, 8, 1), 40)], (2025, 12): []})
    result = get_fill_pace(
        FakeDB(branches), branch_id=None, months=[(2026, 12)],
        days=60, as_of=date(2026, 9, 8),
    )

    assert result["current"]["pickup_room_nights"] == 40
    assert result["vs_last_year"]["pace_index"] is None
    assert result["vs_last_year"]["pickup_room_nights_pct"] is None
    # The occupancy gap is still real — a zero base is a valid rate, not a
    # missing one — so points are reported even where the ratio is not.
    assert result["vs_last_year"]["pickup_occ_pts"] is not None


def test_last_year_shows_what_it_ended_at_and_what_was_still_to_come(branches, stub_rows):
    """The lead you hold at 84 days out only matters against how much last year
    still had left to sell after that point."""
    stub_rows({
        (2026, 12): [Row("b-saigon", date(2026, 8, 1), 100)],
        (2025, 12): [
            Row("b-saigon", date(2025, 8, 1), 100),     # inside the window
            Row("b-saigon", date(2025, 11, 15), 400),   # the late Q4 rush
        ],
    })
    result = get_fill_pace(
        FakeDB(branches), branch_id=None, months=[(2026, 12)],
        days=60, as_of=date(2026, 9, 8),
    )

    ly = result["last_year"]
    assert ly["otb_room_nights"] == 100
    assert ly["final_room_nights"] == 500
    assert ly["remaining_after_window_room_nights"] == 400
    # Level on pickup, but last year booked four fifths of December afterwards.
    assert result["vs_last_year"]["pace_index"] == 1.0


# ── scope, denominators and roll-up ──────────────────────────────────────────

def test_group_rollup_sums_room_nights_rather_than_averaging_branches(branches, stub_rows):
    """Saigon has 20 units and Taipei 30. A group occupancy figure is the sum of
    both numerators over the sum of both denominators — never the mean of two
    percentages, which would weigh the small property as heavily as the big one."""
    stub_rows({
        (2026, 12): [
            Row("b-saigon", date(2026, 8, 1), 310),   # 50% of 20 × 31
            Row("b-taipei", date(2026, 8, 1), 93),    # 10% of 30 × 31
        ],
        (2025, 12): [],
    })
    result = get_fill_pace(
        FakeDB(branches), branch_id=None, months=[(2026, 12)],
        days=60, as_of=date(2026, 9, 8),
    )

    assert result["scope"]["available_room_nights"] == 50 * 31
    # 403 / 1550 = 26.0% — not the 30% a mean of 50% and 10% would give.
    assert result["current"]["otb_occ_pct"] == 26.0

    by_branch = {b["branch_name"]: b for b in result["branches"]}
    assert by_branch["Saigon"]["otb_occ_pct"] == 50.0
    assert by_branch["Taipei"]["otb_occ_pct"] == 10.0


def test_room_filter_moves_the_denominator_to_match(branches, stub_rows):
    """Counting private-room nights against the whole-branch inventory (rooms
    plus dorm beds) would understate room occupancy badly."""
    calls = []
    stub_rows({}, record=calls)
    result = get_fill_pace(
        FakeDB(branches), branch_id=None, months=[(2026, 12)],
        days=30, as_of=date(2026, 9, 8), room_category="room",
    )

    assert result["room_category"] == "Room"
    assert result["scope"]["inventory_basis"] == "total_room_count"
    assert result["scope"]["units_in_scope"] == 12 + 30
    # And the filter reaches the query, normalised to what ingestion stores.
    assert all(c[3] == "Room" for c in calls)


def test_mixed_currency_scope_gets_no_currency_label(branches, stub_rows):
    """Saigon books VND and Taipei TWD. One symbol over the sum would lie."""
    stub_rows({})
    group = get_fill_pace(FakeDB(branches), None, [(2026, 12)], 30, date(2026, 9, 8))
    assert group["scope"]["currency"] is None

    one = get_fill_pace(FakeDB(branches), "b-taipei", [(2026, 12)], 30, date(2026, 9, 8))
    assert one["scope"]["currency"] == "TWD"
    assert one["scope"]["units_in_scope"] == 30
    assert "branches" not in one


# ── the source filter ────────────────────────────────────────────────────────

def test_selection_narrows_the_headline_but_not_the_breakdown(branches, stub_rows):
    """Selecting Agoda answers "how fast is Agoda filling December". The
    per-source table must stay whole regardless, because the question it
    answers — which source is pacing ahead — dies if it collapses to one row."""
    stub_rows({
        (2026, 12): [
            Row("b-saigon", date(2026, 8, 1), 100, source="Agoda"),
            Row("b-saigon", date(2026, 8, 1), 60, source="Booking.com"),
            Row("b-saigon", date(2026, 8, 1), 40, source="Website/Booking Engine",
                source_category="Direct"),
        ],
        (2025, 12): [Row("b-saigon", date(2025, 8, 1), 50, source="Agoda")],
    })
    result = get_fill_pace(
        FakeDB(branches), branch_id=None, months=[(2026, 12)],
        days=60, as_of=date(2026, 9, 8), sources=["Agoda"],
    )

    assert result["current"]["otb_room_nights"] == 100
    assert result["vs_last_year"]["pace_index"] == 2.0
    assert result["sources"] == ["Agoda"]

    by_source = {c["source"]: c for c in result["by_source"]}
    assert set(by_source) == {"Agoda", "Booking.com", "Website/Booking Engine"}
    assert by_source["Booking.com"]["otb_room_nights"] == 60
    # Sorted with the biggest source first.
    assert result["by_source"][0]["source"] == "Agoda"


def test_every_direct_source_gets_its_own_row(branches, stub_rows):
    """The mix pages roll website, walk-in and phone into one "Direct" row,
    because the question there is how much we booked ourselves. Here the
    question is which individual channel to push, so each stands alone — and
    the rows still partition the month."""
    stub_rows({
        (2026, 12): [
            Row("b-saigon", date(2026, 8, 1), 30, source="Website/Booking Engine",
                source_category="Direct"),
            Row("b-saigon", date(2026, 8, 2), 12, source="Walk-in", source_category="Direct"),
            Row("b-saigon", date(2026, 8, 1), 99, source="Agoda"),
        ],
        (2025, 12): [],
    })
    result = get_fill_pace(
        FakeDB(branches), branch_id=None, months=[(2026, 12)],
        days=60, as_of=date(2026, 9, 8),
    )

    by_source = {c["source"]: c for c in result["by_source"]}
    assert set(by_source) == {"Website/Booking Engine", "Walk-in", "Agoda"}
    assert by_source["Website/Booking Engine"]["otb_room_nights"] == 30
    assert by_source["Walk-in"]["otb_room_nights"] == 12
    assert by_source["Website/Booking Engine"]["category"] == "Direct"
    # Rows partition the month: they sum back to the unfiltered total.
    assert sum(c["otb_room_nights"] for c in result["by_source"]) == \
        result["current"]["otb_room_nights"] == 141


def test_the_website_alone_can_be_selected(branches, stub_rows):
    """The reason the roll-up had to go: "how fast is our own website filling
    December" was unanswerable while website sat inside a Direct bucket."""
    stub_rows({
        (2026, 12): [
            Row("b-saigon", date(2026, 8, 1), 30, source="Website/Booking Engine",
                source_category="Direct"),
            Row("b-saigon", date(2026, 8, 2), 12, source="Walk-in", source_category="Direct"),
            Row("b-saigon", date(2026, 8, 1), 99, source="Agoda"),
        ],
        (2025, 12): [
            Row("b-saigon", date(2025, 8, 1), 20, source="Website/Booking Engine",
                source_category="Direct"),
        ],
    })
    result = get_fill_pace(
        FakeDB(branches), branch_id=None, months=[(2026, 12)],
        days=60, as_of=date(2026, 9, 8), sources=["Website/Booking Engine"],
    )
    assert result["current"]["otb_room_nights"] == 30
    assert result["vs_last_year"]["pace_index"] == 1.5


def test_several_sources_can_be_selected_at_once(branches, stub_rows):
    stub_rows({
        (2026, 12): [
            Row("b-saigon", date(2026, 8, 1), 30, source="Website/Booking Engine",
                source_category="Direct"),
            Row("b-saigon", date(2026, 8, 2), 12, source="Walk-in", source_category="Direct"),
            Row("b-saigon", date(2026, 8, 1), 99, source="Agoda"),
        ],
        (2025, 12): [],
    })
    result = get_fill_pace(
        FakeDB(branches), branch_id=None, months=[(2026, 12)],
        days=60, as_of=date(2026, 9, 8),
        sources=["Website/Booking Engine", "Agoda"],
    )
    assert result["current"]["otb_room_nights"] == 129
    assert result["sources"] == ["Agoda", "Website/Booking Engine"]


def test_a_category_still_selects_everything_under_it(branches, stub_rows):
    """"Direct" is no longer a row, but it stays a useful shorthand: one value
    that means website, walk-in, phone, email and the rest."""
    stub_rows({
        (2026, 12): [
            Row("b-saigon", date(2026, 8, 1), 30, source="Website/Booking Engine",
                source_category="Direct"),
            Row("b-saigon", date(2026, 8, 2), 12, source="Walk-in", source_category="Direct"),
            Row("b-saigon", date(2026, 8, 1), 99, source="Agoda"),
        ],
        (2025, 12): [],
    })
    result = get_fill_pace(
        FakeDB(branches), branch_id=None, months=[(2026, 12)],
        days=60, as_of=date(2026, 9, 8), sources=["Direct"],
    )
    assert result["current"]["otb_room_nights"] == 42


def test_overlapping_selections_do_not_double_count(branches, stub_rows):
    """A set membership test, not a sum: picking both a category and a source
    inside it still counts every booking exactly once."""
    stub_rows({
        (2026, 12): [
            Row("b-saigon", date(2026, 8, 1), 30, source="Website/Booking Engine",
                source_category="Direct"),
            Row("b-saigon", date(2026, 8, 2), 12, source="Walk-in", source_category="Direct"),
        ],
        (2025, 12): [],
    })
    result = get_fill_pace(
        FakeDB(branches), branch_id=None, months=[(2026, 12)],
        days=60, as_of=date(2026, 9, 8),
        sources=["Direct", "Website/Booking Engine"],
    )
    assert result["current"]["otb_room_nights"] == 42


def test_a_booking_with_no_source_still_lands_in_the_table(branches, stub_rows):
    """The rows have to sum back to the month, so an empty source gets a label
    rather than dropping out of the breakdown."""
    stub_rows({
        (2026, 12): [Row("b-saigon", date(2026, 8, 1), 25, source=None, source_category=None)],
        (2025, 12): [],
    })
    result = get_fill_pace(
        FakeDB(branches), branch_id=None, months=[(2026, 12)],
        days=60, as_of=date(2026, 9, 8),
    )
    assert result["by_source"][0]["source"] == "Unknown"
    assert result["by_source"][0]["otb_room_nights"] == 25


def test_unknown_source_returns_an_empty_line_not_the_whole_month(branches, stub_rows):
    stub_rows({(2026, 12): [Row("b-saigon", date(2026, 8, 1), 99, source="Agoda")],
               (2025, 12): []})
    result = get_fill_pace(
        FakeDB(branches), branch_id=None, months=[(2026, 12)],
        days=60, as_of=date(2026, 9, 8), sources=["Expedia"],
    )
    assert result["current"]["otb_room_nights"] == 0
    assert result["curve"][-1]["otb_room_nights"] == 0


def test_an_empty_selection_means_every_source(branches, stub_rows):
    stub_rows({(2026, 12): [Row("b-saigon", date(2026, 8, 1), 99, source="Agoda")],
               (2025, 12): []})
    result = get_fill_pace(
        FakeDB(branches), branch_id=None, months=[(2026, 12)],
        days=60, as_of=date(2026, 9, 8), sources=[],
    )
    assert result["current"]["otb_room_nights"] == 99
    assert result["sources"] == []


# ── the window itself ────────────────────────────────────────────────────────

def test_window_is_inclusive_of_both_ends(branches, stub_rows):
    stub_rows({})
    result = get_fill_pace(
        FakeDB(branches), branch_id=None, months=[(2026, 12)],
        days=60, as_of=date(2026, 9, 8),
    )
    assert result["window"] == {"from": "2026-07-11", "to": "2026-09-08"}
    assert len(result["curve"]) == 60
    assert result["days_out"] == {"from": 143, "to": 84}


def test_window_is_clamped_to_something_queryable(branches, stub_rows):
    stub_rows({})
    tiny = get_fill_pace(FakeDB(branches), None, [(2026, 12)], 0, date(2026, 9, 8))
    assert tiny["days"] == 1 and len(tiny["curve"]) == 1

    huge = get_fill_pace(FakeDB(branches), None, [(2026, 12)], 5000, date(2026, 9, 8))
    assert huge["days"] == fill_pace.MAX_WINDOW_DAYS


def test_comparison_can_be_switched_off(branches, stub_rows):
    stub_rows({(2026, 12): [Row("b-saigon", date(2026, 8, 1), 10)]})
    result = get_fill_pace(
        FakeDB(branches), branch_id=None, months=[(2026, 12)],
        days=30, as_of=date(2026, 9, 8), compare_last_year=False,
    )
    assert "last_year" not in result
    assert "vs_last_year" not in result
    assert "ly_otb_room_nights" not in result["curve"][0]


# ── the one query that touches SQL ───────────────────────────────────────────

def _compiled_month_query(year, month, room_category=None):
    captured = {}

    class Q:
        def __init__(self, *cols):
            captured["cols"] = cols

        def filter(self, *a, **k):
            captured.setdefault("filters", []).extend(a)
            return self

        def group_by(self, *a):
            captured["group"] = a
            return self

        def all(self):
            return []

    class DB:
        def query(self, *cols):
            return Q(*cols)

    fetch_month_rows(DB(), None, year, month, room_category)
    stmt = (
        sa.select(*captured["cols"])
        .where(sa.and_(*captured["filters"]))
        .group_by(*captured["group"])
    )
    return str(stmt.compile(dialect=postgresql.dialect(),
                            compile_kwargs={"literal_binds": True}))


def test_nights_are_clipped_to_the_stay_month():
    """A stay running 28 Nov → 3 Dec owns two December nights. Counting its full
    five in December — or its full five in both months — is the single easiest
    way to make this page wrong."""
    sql = _compiled_month_query(2026, 12)

    assert "least(reservations.check_out_date, '2027-01-01')" in sql
    assert "greatest(reservations.check_in_date, '2026-12-01')" in sql
    # Only stays that actually overlap the month are read at all.
    assert "reservations.check_in_date < '2027-01-01'" in sql
    assert "reservations.check_out_date > '2026-12-01'" in sql


def test_revenue_is_prorated_to_the_nights_inside_the_month():
    """grand_total covers the whole stay. Charging all of it to one month would
    double-count every booking that crosses the boundary."""
    sql = _compiled_month_query(2026, 12)
    assert "nullif(reservations.check_out_date - reservations.check_in_date, 0)" in sql


def test_occupancy_keeps_the_non_paying_stays_that_revenue_drops():
    """Blogger, KOL and house-use guests occupy a bed — they belong in the fill
    rate. They pay nothing, so they stay out of the revenue column. That split
    is the engine's standing rule and this page follows it."""
    sql = _compiled_month_query(2026, 12)

    # The revenue columns carry their own FILTER, so split on the FROM to tell
    # the aggregate's exclusions apart from the row-level ones.
    select_list, _, where = sql.partition("FROM reservations")

    # Which rows are read at all: cancelled / no-show and maintenance out,
    # blogger and house use still in — those bodies are in the beds.
    assert "'blogger'" not in where
    assert "'maintenance'" in where
    assert "'canceled'" in where
    # What the money column counts: the non-paying sources additionally dropped.
    assert "FILTER (WHERE" in select_list
    assert "'blogger'" in select_list


def test_day_use_rows_carry_no_nights_and_are_dropped():
    sql = _compiled_month_query(2026, 12)
    assert "reservations.check_out_date > reservations.check_in_date" in sql


def test_room_category_filter_is_case_insensitive_in_sql():
    sql = _compiled_month_query(2026, 12, "dorm")
    assert "lower(coalesce(reservations.room_type_category, '')) = 'dorm'" in sql


# ── the endpoint wiring ──────────────────────────────────────────────────────
# The service is exercised above with plain Python arguments. What these add is
# the HTTP surface: a source name reaches the service intact after a round trip
# through the query string, and repeating the parameter builds a set rather than
# overwriting itself.

@pytest.fixture
def client(monkeypatch):
    from unittest.mock import MagicMock

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.routers import metrics

    captured = {}

    def spy(db, **kwargs):
        captured.update(kwargs)
        return {"stay_month": "x", "current": {}, "curve": []}

    monkeypatch.setattr(metrics, "get_fill_pace", spy)
    monkeypatch.setattr(metrics, "_last_reservations_synced_at", lambda db, b: None)

    api = FastAPI()
    api.include_router(metrics.router, prefix="/api/metrics")
    api.dependency_overrides[get_db] = lambda: MagicMock()
    return TestClient(api), captured


def test_one_source_survives_the_query_string(client):
    """"Website/Booking Engine" carries a slash and a space. Both have to come
    back out of the URL exactly as stored, or the filter matches nothing."""
    http, captured = client
    r = http.get("/api/metrics/fill-pace",
                 params={"stay_month": "2026-12", "source": "Website/Booking Engine"})

    assert r.status_code == 200
    assert captured["sources"] == ["Website/Booking Engine"]


def test_repeating_the_parameter_builds_a_set(client):
    http, captured = client
    r = http.get("/api/metrics/fill-pace?stay_month=2026-12"
                 "&source=Website%2FBooking+Engine&source=Agoda")

    assert r.status_code == 200
    assert captured["sources"] == ["Website/Booking Engine", "Agoda"]


def test_no_source_parameter_means_every_source(client):
    http, captured = client
    http.get("/api/metrics/fill-pace", params={"stay_month": "2026-12"})
    assert captured["sources"] is None


def test_the_window_defaults_and_bounds_are_enforced(client):
    http, captured = client

    http.get("/api/metrics/fill-pace", params={"stay_month": "2026-12"})
    assert captured["days"] == 60
    assert captured["as_of"] is None

    http.get("/api/metrics/fill-pace",
             params={"stay_month": "2026-12", "days": 20, "as_of": "2026-09-08"})
    assert captured["days"] == 20
    assert captured["as_of"] == date(2026, 9, 8)

    over = http.get("/api/metrics/fill-pace", params={"stay_month": "2026-12", "days": 400})
    assert over.status_code == 422


def test_a_bad_stay_month_is_refused_rather_than_guessed(client):
    """Named in the error, so a typo in one of several months is findable."""
    http, _ = client
    for bad in ("2026-13", "December", "2026-1", "2026"):
        body = http.get("/api/metrics/fill-pace", params={"stay_month": bad}).json()
        assert body["success"] is False
        assert bad in body["error"]


def test_months_reach_the_service_as_year_month_pairs(client):
    http, captured = client

    http.get("/api/metrics/fill-pace?stay_month=2026-10&stay_month=2026-12")
    assert captured["months"] == [(2026, 10), (2026, 12)]

    http.get("/api/metrics/fill-pace")
    assert len(captured["months"]) == 1


def test_too_many_months_is_refused_rather_than_silently_trimmed(client):
    """Every month costs two grouped queries. Quietly dropping the ones past
    the cap would answer a different question than the one asked."""
    http, _ = client
    many = "&".join(f"stay_month=2026-{m:02d}" for m in range(1, 13))
    assert http.get(f"/api/metrics/fill-pace?{many}").json()["success"] is True

    too_many = many + "&stay_month=2027-01"
    body = http.get(f"/api/metrics/fill-pace?{too_many}").json()
    assert body["success"] is False
    assert "13 given" in body["error"]


# ── several stay months at once ──────────────────────────────────────────────

def test_months_are_summed_not_averaged(branches, stub_rows):
    """A quarter's fill is total room-nights over total inventory-nights. A
    31-night month and a 30-night one do not carry equal weight, so a mean of
    three monthly percentages is the wrong number."""
    stub_rows({
        (2026, 10): [Row("b-saigon", date(2026, 8, 1), 155)],
        (2026, 11): [Row("b-saigon", date(2026, 8, 1), 300)],
        (2026, 12): [Row("b-saigon", date(2026, 8, 1), 100)],
        (2025, 10): [], (2025, 11): [], (2025, 12): [],
    })
    result = get_fill_pace(
        FakeDB(branches), branch_id=None,
        months=[(2026, 10), (2026, 11), (2026, 12)],
        days=60, as_of=date(2026, 9, 8),
    )

    units = 20 + 30
    assert result["stay_days"] == 31 + 30 + 31
    assert result["scope"]["available_room_nights"] == units * 92
    assert result["current"]["otb_room_nights"] == 555
    # 555 / 4600, not the mean of 10.0%, 20.0% and 6.45%.
    assert result["current"]["otb_occ_pct"] == 12.07


def test_each_month_keeps_its_own_countdown(branches, stub_rows):
    """The heart of reading several months together. On 8 Sep, October is 23
    days out and December is 84. October must be compared with the point last
    year that was 23 days from October — not with 8 Sep 2025 flat, and above all
    not with December's 84-day mark."""
    stub_rows({})
    result = get_fill_pace(
        FakeDB(branches), branch_id=None,
        months=[(2026, 10), (2026, 12)],
        days=60, as_of=date(2026, 9, 8),
    )

    by_month = {m["stay_month"]: m for m in result["months"]}
    assert by_month["2026-10"]["days_out"]["to"] == 23
    assert by_month["2026-12"]["days_out"]["to"] == 84
    # Each lands on its own year-ago date, both 23 and 84 days out respectively.
    assert by_month["2026-10"]["last_year"]["as_of"] == "2025-09-08"
    assert by_month["2026-12"]["last_year"]["as_of"] == "2025-09-08"
    assert by_month["2026-10"]["last_year"]["stay_month"] == "2025-10"


def test_a_leap_year_moves_the_months_apart(branches, stub_rows):
    """The two year-ago dates coincide only because 365 days separate the two
    Octobers. Put a 29 February in the way and they do not — which is exactly
    why each month carries its own countdown instead of one shared offset."""
    stub_rows({})
    result = get_fill_pace(
        FakeDB(branches), branch_id=None,
        months=[(2025, 2), (2025, 3)],
        days=30, as_of=date(2025, 1, 15),
    )

    by_month = {m["stay_month"]: m for m in result["months"]}
    # 17 days before 1 Feb 2025 is 15 Jan; 17 days before 1 Feb 2024 is 15 Jan.
    assert by_month["2025-02"]["last_year"]["as_of"] == "2024-01-15"
    # 45 days before 1 Mar 2025 is 15 Jan; before 1 Mar 2024 it is 16 Jan.
    assert by_month["2025-03"]["last_year"]["as_of"] == "2024-01-16"
    # Denominators follow each year's own calendar: Feb 2024 had 29 days.
    assert result["stay_days"] == 28 + 31
    assert result["last_year"]["stay_days"] == 29 + 31


def test_the_total_can_be_on_pace_while_a_month_inside_it_is_not(branches, stub_rows):
    """The reason the per-month rows exist at all."""
    stub_rows({
        (2026, 11): [Row("b-saigon", date(2026, 8, 1), 200)],
        (2026, 12): [Row("b-saigon", date(2026, 8, 1), 20)],
        (2025, 11): [Row("b-saigon", date(2025, 8, 1), 100)],
        (2025, 12): [Row("b-saigon", date(2025, 8, 1), 120)],
    })
    result = get_fill_pace(
        FakeDB(branches), branch_id=None,
        months=[(2026, 11), (2026, 12)],
        days=60, as_of=date(2026, 9, 8),
    )

    assert result["vs_last_year"]["pace_index"] == 1.0        # 220 vs 220
    by_month = {m["stay_month"]: m for m in result["months"]}
    assert by_month["2026-11"]["vs_last_year"]["pace_index"] == 2.0
    assert by_month["2026-12"]["vs_last_year"]["pace_index"] == round(20 / 120, 3)


def test_the_curve_adds_the_months_point_by_point(branches, stub_rows):
    stub_rows({
        (2026, 11): [Row("b-saigon", date(2026, 9, 1), 30)],
        (2026, 12): [Row("b-saigon", date(2026, 9, 1), 12)],
        (2025, 11): [], (2025, 12): [],
    })
    result = get_fill_pace(
        FakeDB(branches), branch_id=None,
        months=[(2026, 11), (2026, 12)],
        days=10, as_of=date(2026, 9, 8),
    )

    curve = result["curve"]
    assert len(curve) == 10
    assert curve[0]["otb_room_nights"] == 0       # before 1 Sep, nothing booked
    assert curve[-1]["otb_room_nights"] == 42     # both months, together
    # No single countdown to plot against, so the axis is the booking date.
    assert "days_out" not in curve[0]
    assert "days_out" not in result


def test_one_month_still_reads_as_a_countdown(branches, stub_rows):
    """The single-month view must not change shape now that many are allowed."""
    stub_rows({(2026, 12): [], (2025, 12): []})
    result = get_fill_pace(
        FakeDB(branches), branch_id=None, months=[(2026, 12)],
        days=60, as_of=date(2026, 9, 8),
    )

    assert result["stay_months"] == ["2026-12"]
    assert result["days_out"] == {"from": 143, "to": 84}
    assert result["curve"][0]["days_out"] == 143
    assert result["curve"][-1]["days_out"] == 84
    assert result["curve"][-1]["ly_date"] == "2025-09-08"
    assert result["last_year"]["as_of"] == "2025-09-08"


def test_sources_and_branches_sum_across_the_months(branches, stub_rows):
    stub_rows({
        (2026, 11): [
            Row("b-saigon", date(2026, 8, 1), 30, source="Agoda"),
            Row("b-taipei", date(2026, 8, 1), 10, source="Website/Booking Engine",
                source_category="Direct"),
        ],
        (2026, 12): [
            Row("b-saigon", date(2026, 8, 1), 12, source="Agoda"),
            Row("b-taipei", date(2026, 8, 1), 5, source="Website/Booking Engine",
                source_category="Direct"),
        ],
        (2025, 11): [], (2025, 12): [],
    })
    result = get_fill_pace(
        FakeDB(branches), branch_id=None,
        months=[(2026, 11), (2026, 12)],
        days=60, as_of=date(2026, 9, 8),
    )

    by_source = {c["source"]: c for c in result["by_source"]}
    assert by_source["Agoda"]["otb_room_nights"] == 42
    assert by_source["Website/Booking Engine"]["otb_room_nights"] == 15

    by_branch = {b["branch_name"]: b for b in result["branches"]}
    assert by_branch["Saigon"]["otb_room_nights"] == 42
    # Each branch against its own inventory across the whole span.
    assert by_branch["Saigon"]["available_room_nights"] == 20 * 61
    assert by_branch["Taipei"]["available_room_nights"] == 30 * 61


def test_repeated_months_are_read_once(branches, stub_rows):
    """Asking for December twice must not sell December twice."""
    stub_rows({(2026, 12): [Row("b-saigon", date(2026, 8, 1), 50)], (2025, 12): []})
    result = get_fill_pace(
        FakeDB(branches), branch_id=None,
        months=[(2026, 12), (2026, 12)],
        days=60, as_of=date(2026, 9, 8),
    )
    assert result["stay_months"] == ["2026-12"]
    assert result["current"]["otb_room_nights"] == 50


def test_months_come_back_in_calendar_order(branches, stub_rows):
    stub_rows({})
    result = get_fill_pace(
        FakeDB(branches), branch_id=None,
        months=[(2027, 1), (2026, 11), (2026, 12)],
        days=30, as_of=date(2026, 9, 8),
    )
    assert result["stay_months"] == ["2026-11", "2026-12", "2027-01"]


def test_the_month_count_is_capped_in_the_service_too(branches, stub_rows):
    """The endpoint refuses an over-long list; the service is also called from
    tests and any future caller, so it holds its own bound."""
    stub_rows({})
    result = get_fill_pace(
        FakeDB(branches), branch_id=None,
        months=[(2026, m) for m in range(1, 13)] + [(2027, 1)],
        days=30, as_of=date(2026, 9, 8),
    )
    assert len(result["stay_months"]) == fill_pace.MAX_STAY_MONTHS
