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
    """One (branch, booking date, channel) bucket as the query returns it."""

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
        FakeDB(branches), branch_id=None, year=2025, month=3,
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
        FakeDB(branches), branch_id=None, year=2025, month=2,
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
        FakeDB(branches), branch_id=None, year=2026, month=12,
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
        FakeDB(branches), branch_id=None, year=2026, month=12,
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
        FakeDB(branches), branch_id=None, year=2026, month=12,
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
        FakeDB(branches), branch_id=None, year=2026, month=12,
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
        FakeDB(branches), branch_id=None, year=2026, month=12,
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
    group = get_fill_pace(FakeDB(branches), None, 2026, 12, 30, date(2026, 9, 8))
    assert group["scope"]["currency"] is None

    one = get_fill_pace(FakeDB(branches), "b-taipei", 2026, 12, 30, date(2026, 9, 8))
    assert one["scope"]["currency"] == "TWD"
    assert one["scope"]["units_in_scope"] == 30
    assert "branches" not in one


# ── the source filter ────────────────────────────────────────────────────────

def test_channel_filter_narrows_the_headline_but_not_the_breakdown(branches, stub_rows):
    """Selecting Agoda answers "how fast is Agoda filling December". The
    per-channel table must stay whole regardless, because the question it
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
        FakeDB(branches), branch_id=None, year=2026, month=12,
        days=60, as_of=date(2026, 9, 8), channel="Agoda",
    )

    assert result["current"]["otb_room_nights"] == 100
    assert result["vs_last_year"]["pace_index"] == 2.0

    channels = {c["channel"]: c for c in result["by_channel"]}
    assert set(channels) == {"Agoda", "Booking.com", "Direct"}
    assert channels["Booking.com"]["otb_room_nights"] == 60
    # Direct rolls every own-channel source under one row, as the mix pages do.
    assert channels["Direct"]["otb_room_nights"] == 40
    assert channels["Direct"]["category"] == "Direct"
    # Sorted with the biggest channel first.
    assert result["by_channel"][0]["channel"] == "Agoda"


def test_direct_selects_by_category_not_by_source_name(branches, stub_rows):
    """"Direct" is a category covering website, walk-in, phone, email and the
    rest. Matching it against the raw source string would return nothing."""
    stub_rows({
        (2026, 12): [
            Row("b-saigon", date(2026, 8, 1), 30, source="Website/Booking Engine",
                source_category="Direct"),
            Row("b-saigon", date(2026, 8, 2), 12, source="Walk-in",
                source_category="Direct"),
            Row("b-saigon", date(2026, 8, 1), 99, source="Agoda"),
        ],
        (2025, 12): [],
    })
    result = get_fill_pace(
        FakeDB(branches), branch_id=None, year=2026, month=12,
        days=60, as_of=date(2026, 9, 8), channel="Direct",
    )
    assert result["current"]["otb_room_nights"] == 42


def test_unknown_channel_returns_an_empty_line_not_the_whole_month(branches, stub_rows):
    stub_rows({(2026, 12): [Row("b-saigon", date(2026, 8, 1), 99, source="Agoda")],
               (2025, 12): []})
    result = get_fill_pace(
        FakeDB(branches), branch_id=None, year=2026, month=12,
        days=60, as_of=date(2026, 9, 8), channel="Expedia",
    )
    assert result["current"]["otb_room_nights"] == 0
    assert result["curve"][-1]["otb_room_nights"] == 0


# ── the window itself ────────────────────────────────────────────────────────

def test_window_is_inclusive_of_both_ends(branches, stub_rows):
    stub_rows({})
    result = get_fill_pace(
        FakeDB(branches), branch_id=None, year=2026, month=12,
        days=60, as_of=date(2026, 9, 8),
    )
    assert result["window"] == {"from": "2026-07-11", "to": "2026-09-08"}
    assert len(result["curve"]) == 60
    assert result["days_out"] == {"from": 143, "to": 84}


def test_window_is_clamped_to_something_queryable(branches, stub_rows):
    stub_rows({})
    tiny = get_fill_pace(FakeDB(branches), None, 2026, 12, 0, date(2026, 9, 8))
    assert tiny["days"] == 1 and len(tiny["curve"]) == 1

    huge = get_fill_pace(FakeDB(branches), None, 2026, 12, 5000, date(2026, 9, 8))
    assert huge["days"] == fill_pace.MAX_WINDOW_DAYS


def test_comparison_can_be_switched_off(branches, stub_rows):
    stub_rows({(2026, 12): [Row("b-saigon", date(2026, 8, 1), 10)]})
    result = get_fill_pace(
        FakeDB(branches), branch_id=None, year=2026, month=12,
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
