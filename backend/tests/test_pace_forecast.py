"""Pace forecast — where a stay month lands, and whether that clears target.

The estimator is one line of arithmetic; everything that can make it lie lives
in the conditions around it, so that is what is asserted here.

  · The year-ago base must be real. Under 30 room-nights on the books, or a
    year-ago month that never traded normally, and the branch-month is not
    forecast from itself — Taipei opened mid-October 2025 and finished that
    month at 26%, which predicts an opening, not an October.
  · Nothing is ever borrowed from another branch. A branch without a year-ago
    month falls back to its own recent occupancy, or to no number at all.
  · Nothing sells more than the house holds.
  · A month already over is not forecast. It is reported.
  · A branch that drops out takes its inventory and its target with it, or the
    totals quietly report a partial forecast against a whole target.

No database: the two lookups that need one are faked, and the arithmetic is
the whole of what is checked.
"""
from datetime import date

import pytest

from app.services import pace_forecast
from app.services.pace_forecast import (
    MAX_FORECAST_OCC,
    MIN_LY_BASE_ROOM_NIGHTS,
    _adr_yoy,
    _settled_months,
    _usable_base,
    build_forecast,
)


# ── fakes ────────────────────────────────────────────────────────────────────

BRANCHES = {
    "b-1948": {"name": "1948", "currency": "TWD", "city": "Taipei",
               "units": 69, "total_rooms": 69},
    "b-oani": {"name": "Oani", "currency": "TWD", "city": "Taipei",
               "units": 92, "total_rooms": 92},
    "b-taipei": {"name": "Taipei", "currency": "TWD", "city": "Taipei",
                 "units": 138, "total_rooms": 138},
    "b-osaka": {"name": "Osaka", "currency": "JPY", "city": "Osaka",
                "units": 71, "total_rooms": 71},
}

# Read at 15 Sep 2026, the three settled months behind it.
REF = [(2026, 8), (2026, 7), (2026, 6)]
REF_DAYS = {6: 30, 7: 31, 8: 31}


def dm(bid, year, occ, rooms, revenue_per_night=0.0):
    """daily_metrics rows for the three reference months at a flat occupancy."""
    out = {}
    for (_, m) in REF:
        nights = round(rooms * REF_DAYS[m] * occ)
        out[(bid, year, m)] = {
            "revenue": nights * revenue_per_night,
            "nights": nights,
            "adr": revenue_per_night or None,
        }
    return out


def cell(bid="b-1948", *, year=2026, month=10, days_out=16, capacity=2139,
         otb=662, ly_otb=434, ly_final=1755, status="future", city=None):
    return {
        "branch_id": bid,
        "city": city or BRANCHES[bid]["city"],
        "year": year,
        "month": month,
        "days_out": days_out,
        "status": status,
        "capacity": capacity,
        "otb_nights": float(otb),
        "ly_otb_nights": float(ly_otb),
        "ly_final_nights": float(ly_final),
    }


def _run(monkeypatch, cells, occ=None, targets=None, scoped_sources=False):
    monkeypatch.setattr(pace_forecast, "_monthly_adr", lambda *a, **k: occ or {})
    monkeypatch.setattr(pace_forecast, "_targets", lambda *a, **k: targets or {})
    monkeypatch.setattr(pace_forecast, "get_cached_rate",
                        lambda c, t="VND": {"TWD": 830.0, "JPY": 165.0}.get(c, 1.0))
    return build_forecast(None, cells, as_of=date(2026, 9, 15),
                          branch_meta=BRANCHES, scoped_sources=scoped_sources)


@pytest.fixture
def nights_only(monkeypatch):
    """Nights only: no daily_metrics unless a test supplies some, no targets."""
    def go(cells, **kw):
        return _run(monkeypatch, cells, **kw)
    return go


@pytest.fixture
def with_money(monkeypatch):
    """A branch that sold at 2,000 last October and is running 10% up on ADR."""
    adr = {
        ("b-1948", 2025, 10): {"revenue": 3_510_000.0, "nights": 1755, "adr": 2000.0},
        ("b-1948", 2026, 10): {"revenue": 1_324_000.0, "nights": 662, "adr": 2000.0},
        ("b-1948", 2026, 8): {"revenue": 2_200_000.0, "nights": 1000, "adr": 2200.0},
        ("b-1948", 2025, 8): {"revenue": 2_000_000.0, "nights": 1000, "adr": 2000.0},
    }
    targets = {("b-1948", 2026, 10): {"native": 4_500_000.0, "vnd": 0.0}}

    def go(cells, occ=None, **kw):
        return _run(monkeypatch, cells, occ={**adr, **(occ or {})},
                    targets=targets, **kw)
    return go


def only(result, key="total"):
    return result[key]


# ── the gate ─────────────────────────────────────────────────────────────────

def test_a_handful_of_year_ago_nights_is_not_a_base():
    """Dividing by it, or adding to it, both magnify the same noise. Below the
    floor the branch-month is not forecast from its own history at all."""
    assert _usable_base(cell(ly_otb=MIN_LY_BASE_ROOM_NIGHTS))
    assert not _usable_base(cell(ly_otb=MIN_LY_BASE_ROOM_NIGHTS - 1))


def test_a_year_ago_month_that_never_traded_is_not_a_base():
    """Taipei finished October 2025 at 26% because it opened mid-month. Using
    it would forecast the opening rather than the October."""
    ramp = cell("b-taipei", capacity=4278, otb=729, ly_otb=400, ly_final=1128)
    assert not _usable_base(ramp)
    # The same branch a month later, trading normally, is fine.
    normal = cell("b-taipei", month=11, capacity=4140, otb=314,
                  ly_otb=100, ly_final=3232)
    assert _usable_base(normal)


# ── the estimator ────────────────────────────────────────────────────────────

def test_forecast_adds_last_years_remaining_pickup(nights_only):
    """The whole method: what is on the books now, plus what last year still
    had to come at this same distance from the month."""
    total = only(nights_only([cell()]))

    assert total["room_nights"] == pytest.approx(662 + (1755 - 434))
    assert total["basis"] == ["ly_pickup"]
    assert total["occ_pct"] == pytest.approx(1983 / 2139 * 100, abs=0.1)
    # Ahead of where last year finished, stated in points of the same house.
    assert total["occ_pts_vs_ly"] == pytest.approx((1983 - 1755) / 2139 * 100, abs=0.1)


def test_band_is_the_measured_error_and_never_dips_under_the_book(nights_only):
    """p10/p90 of the backtest, applied to the forecast — except that the low
    end cannot fall below what is already sold. A book does not shrink."""
    total = only(nights_only([cell()]))
    assert total["room_nights_low"] < total["room_nights"] < total["room_nights_high"]
    assert total["room_nights_low"] >= total["otb_room_nights"]

    thin = only(nights_only([cell(otb=1700, ly_otb=434, ly_final=1755)]))
    assert thin["room_nights_low"] >= 1700


def test_a_month_further_out_than_the_backtest_gets_a_wider_band(nights_only):
    near = only(nights_only([cell(days_out=60)]))
    far = only(nights_only([cell(days_out=107)]))
    assert (far["room_nights_high"] - far["room_nights_low"]
            > near["room_nights_high"] - near["room_nights_low"])


def test_nothing_sells_more_than_the_house_holds(nights_only):
    """The estimator has no idea inventory exists. Left alone it returns 104%
    occupancy; the ceiling stops it and the month says the ceiling bound."""
    total = only(nights_only([cell(otb=1500, ly_otb=434, ly_final=1755)]))
    assert total["room_nights"] == pytest.approx(2139 * MAX_FORECAST_OCC, abs=0.1)
    assert total["capacity_capped"] is True


def test_a_finished_month_is_reported_not_forecast(nights_only):
    total = only(nights_only([cell(status="finished", otb=1700)]))
    assert total["basis"] == ["actual"]
    assert total["room_nights"] == total["room_nights_low"] == 1700


# ── no year-ago month ────────────────────────────────────────────────────────

def test_a_branch_without_a_year_ago_month_holds_its_own_run_rate(nights_only):
    """Oani had not opened a year ago, so nothing about its October can be read
    from its own history. What it does have is a present: it has run 70% every
    month this year, and that is what its Q4 is projected at."""
    out = nights_only([cell("b-oani", capacity=2852, otb=1159,
                            ly_otb=0, ly_final=0)],
                      occ=dm("b-oani", 2026, 0.700, rooms=92))
    row = out["branches"][0]

    assert row["basis"] == ["own_run_rate"]
    assert row["room_nights"] == pytest.approx(0.70 * 2852, abs=15)
    used = out["total"]["run_rate_months"]
    assert len(used) == 1
    assert used[0]["branch_name"] == "Oani"
    assert used[0]["stay_month"] == "2026-10"
    assert used[0]["occ_pct"] == pytest.approx(70.0, abs=0.2)


def test_the_run_rate_never_reads_under_what_is_already_sold(nights_only):
    """A month can be ahead of the run rate the moment it is read."""
    out = nights_only([cell("b-oani", capacity=2852, otb=2400,
                            ly_otb=0, ly_final=0)],
                      occ=dm("b-oani", 2026, 0.700, rooms=92))
    assert out["total"]["room_nights"] >= 2400


def test_nothing_is_ever_borrowed_from_another_branch(nights_only):
    """A sibling in the same city, trading normally, with a full year-ago
    October — and it still lends nothing. 1948 is a 69-room hostel and Oani a
    92-room hotel; they sell to different people on different booking curves,
    and Oani is 41% booked where 1948 is 31%, so lending 1948's remaining
    run-up would count Oani's early bookings twice."""
    out = nights_only([
        cell("b-1948"),
        cell("b-oani", capacity=2852, otb=1159, ly_otb=0, ly_final=0),
    ])   # no daily_metrics at all, so Oani has no run rate of its own either

    oani = [b for b in out["branches"] if b["branch_id"] == "b-oani"][0]
    assert oani["basis"] == ["no_base"]
    assert oani["room_nights"] is None
    assert out["total"]["unforecastable"] == [
        {"branch_id": "b-oani", "branch_name": "Oani", "stay_month": "2026-10"}
    ]


# ── what a branch that drops out takes with it ───────────────────────────────

def test_an_unforecastable_branch_leaves_its_inventory_out_too(nights_only):
    """Counting Oani's 2,852 room-nights of capacity while forecasting none of
    them would report the group's occupancy short by exactly the share of the
    house that was left out."""
    out = nights_only([
        cell("b-1948"),
        cell("b-oani", capacity=2852, otb=1159, ly_otb=0, ly_final=0),
    ])
    total = out["total"]

    assert total["room_nights"] == pytest.approx(1983)
    assert total["available_room_nights"] == 2139          # 1948 only
    assert total["occ_pct"] == pytest.approx(1983 / 2139 * 100, abs=0.1)
    assert (total["months_counted"], total["months_in_scope"]) == (1, 2)


def test_the_target_comes_out_with_the_month_it_belongs_to(with_money):
    """A forecast covering one branch against a target covering two is not an
    achievement percentage, it is a smaller number wearing one."""
    out = with_money([
        cell("b-1948"),
        cell("b-osaka", capacity=2201, otb=1259, ly_otb=1082, ly_final=1892,
             city="Osaka"),
    ])
    total = out["total"]

    # Osaka is forecast, but this fixture has no Osaka rate to price it at.
    assert total["room_nights"] is not None
    assert total["unpriced"] == [
        {"branch_id": "b-osaka", "branch_name": "Osaka", "stay_month": "2026-10"}
    ]
    # So the money is 1948's alone — and so is the target it is read against.
    assert total["target_native"] == 4_500_000.0
    assert total["achievement_pct"] == pytest.approx(
        total["revenue_native"] / 4_500_000 * 100, abs=0.1)


# ── several months, several branches ─────────────────────────────────────────

def test_months_are_summed_and_the_rate_divided_once(nights_only):
    """A quarter's occupancy is not the mean of three months' percentages: a
    31-night month and a 30-night one do not carry equal weight."""
    oct_ = cell(month=10, capacity=2139, otb=662, ly_otb=434, ly_final=1755)
    nov = cell(month=11, capacity=2600, otb=308, ly_otb=101, ly_final=1811)
    out = nights_only([oct_, nov])

    total = only(out)
    nights = (662 + 1755 - 434) + (308 + 1811 - 101)
    assert total["room_nights"] == pytest.approx(nights)
    assert total["occ_pct"] == pytest.approx(nights / (2139 + 2600) * 100, abs=0.1)
    assert [m["stay_month"] for m in out["months"]] == ["2026-10", "2026-11"]


def test_a_filtered_selection_gets_no_projection_at_all(nights_only):
    """Pace narrows to one source or one room type; nothing it is measured
    against does. `daily_metrics` has no source column and no room split, so
    the book, its money and the ADR would stay whole-house beside a sliced
    pickup — and the KPI target is the branch's, not Agoda's. Every figure
    would be a slice over a whole: a wrong number that looks right."""
    for kw in ({"scoped_sources": True}, {"room_category": "Dorm"}):
        out = build_forecast(None, [cell()], as_of=date(2026, 9, 15),
                             branch_meta=BRANCHES, **{"scoped_sources": False, **kw})
        assert out["available"] is False
        assert out["reason"] == "filtered"
        assert out["filtered_by"] == (["source"] if "scoped_sources" in kw
                                      else ["room_category"])


def test_mixed_currencies_lose_the_symbol_but_keep_the_vnd_total(with_money):
    """The group runs TWD, JPY and VND. One symbol over the sum would pick one
    and be wrong about the other two."""
    mixed = with_money([
        cell("b-1948"),
        cell("b-osaka", capacity=2201, otb=1259, ly_otb=1082, ly_final=1892,
             city="Osaka"),
    ])
    assert mixed["total"]["currency"] is None
    single = with_money([cell("b-1948")])
    assert single["total"]["currency"] == "TWD"
    assert single["total"]["revenue_vnd"] == pytest.approx(
        single["total"]["revenue_native"] * 830)


# ── money ────────────────────────────────────────────────────────────────────

def test_only_the_nights_still_to_come_are_priced(with_money):
    """Nights already sold are already priced, at whatever they were sold for.
    Applying a forecast ADR to the whole month would re-price the book."""
    total = only(with_money([cell()]))

    remaining = 1983 - 662          # daily_metrics book is the same 662 nights
    assert total["revenue_native"] == pytest.approx(1_324_000 + remaining * 2000 * 1.1)
    assert total["target_native"] == 4_500_000.0
    assert total["achievement_pct"] == pytest.approx(
        total["revenue_native"] / 4_500_000 * 100, abs=0.1)


def test_adr_is_last_years_month_moved_by_this_years_trend(with_money):
    out = with_money([cell()])
    assert out["branches"][0]["adr_yoy"] == pytest.approx(1.1)


def test_adr_trend_is_pooled_across_months_not_averaged():
    """A quiet month must not carry the same weight as a busy one."""
    adr = {
        ("b", 2026, 8): {"revenue": 1000.0, "nights": 10, "adr": 100.0},
        ("b", 2025, 8): {"revenue": 1000.0, "nights": 10, "adr": 100.0},
        ("b", 2026, 7): {"revenue": 20_000.0, "nights": 100, "adr": 200.0},
        ("b", 2025, 7): {"revenue": 10_000.0, "nights": 100, "adr": 100.0},
    }
    pooled = _adr_yoy(adr, "b", [(2026, 8), (2026, 7)])
    assert pooled == pytest.approx((21_000 / 110) / (11_000 / 110))
    # The hundred-night month moved 2.0x and the ten-night month 1.0x.
    # Averaging the two ratios would call that 1.5x; pooling lets the month
    # with the volume in it carry the answer.
    assert pooled > 1.5


def test_a_month_with_no_year_ago_rate_is_left_unpriced(nights_only):
    total = only(nights_only([cell()]))
    assert total["room_nights"] is not None
    assert total["revenue_native"] is None
    assert total["achievement_pct"] is None


def test_settled_months_walks_backwards_across_the_year_boundary():
    assert _settled_months(date(2026, 2, 10), 3) == [(2026, 1), (2025, 12), (2025, 11)]


def test_a_branch_with_no_year_ago_rate_is_priced_off_its_own_settled_months(monkeypatch):
    """Not off its forward book. Oani's December reads 7,955 a night at ten per
    cent sold — whatever its first few holiday bookings happened to pay —
    against the 3,753 it has actually averaged all year."""
    occ = dm("b-oani", 2026, 0.700, rooms=92, revenue_per_night=3_753.0)
    occ[("b-oani", 2026, 12)] = {"revenue": 2_418_373.0, "nights": 304,
                                 "adr": 7_955.0}
    out = _run(monkeypatch,
               [cell("b-oani", month=12, capacity=2852, otb=304,
                     ly_otb=0, ly_final=0, days_out=77)],
               occ=occ)
    row = out["branches"][0]
    remaining = row["room_nights"] - 304
    assert row["revenue_native"] == pytest.approx(2_418_373 + remaining * 3_753, rel=1e-3)


def test_a_rejected_year_ago_month_cannot_price_the_month_either(monkeypatch):
    """The gate that throws out a ramp month's volume throws out its rate with
    it. Oani's October 2025 ran 37 room-nights; whatever those few bookings
    paid is not October's rate."""
    occ = dm("b-oani", 2026, 0.700, rooms=92, revenue_per_night=3_500.0)
    occ[("b-oani", 2025, 10)] = {"revenue": 807_800.0, "nights": 37, "adr": 21_832.0}
    occ[("b-oani", 2026, 10)] = {"revenue": 5_373_798.0, "nights": 1159, "adr": 4_636.0}
    out = _run(monkeypatch,
               [cell("b-oani", capacity=2852, otb=1159, ly_otb=0, ly_final=37)],
               occ=occ)
    row = out["branches"][0]
    assert row["basis"] == ["own_run_rate"]
    remaining = row["room_nights"] - 1159
    # Priced off its own settled months, not off a 37-night October.
    assert row["revenue_native"] == pytest.approx(5_373_798 + remaining * 3_500, rel=1e-3)


# ── two ways of counting a sold night ────────────────────────────────────────

def test_the_forecast_is_in_the_unit_the_target_is_set_in(monkeypatch):
    """`reservations` counts a booking of three dorm beds once; daily_metrics
    counts the beds. Taipei is 108 beds to 30 rooms and reads 1.28x between the
    two, so a forecast built straight out of reservations would quote occupancy
    a fifth below the KPI page it is compared against — and price a fifth too
    few nights."""
    occ = {("b-taipei", 2026, 10): {"revenue": 1_711_956.0, "nights": 936,
                                    "adr": 1_829.0}}
    out = _run(monkeypatch,
               [cell("b-taipei", capacity=4278, otb=729, ly_otb=400, ly_final=2200,
                     days_out=16)],
               occ=occ)
    row = out["branches"][0]

    # 936 / 729 — the same bookings, counted both ways.
    assert row["otb_room_nights"] == 936
    assert out["cells"][0]["bed_factor"] == pytest.approx(936 / 729, abs=0.01)
    # And the year-ago pickup is converted before it is added, never after.
    assert out["cells"][0]["room_nights"] == pytest.approx(
        936 + (2200 - 400) * (936 / 729), abs=1)


def test_the_ramp_gate_reads_on_the_converted_basis_too(monkeypatch):
    """`ly_final_nights` counts reservations and `capacity` counts units, so on
    a dorm-heavy branch the raw ratio reads a fifth low — and a year-ago month
    that traded at 44% would be thrown out as a ramp it never was."""
    occ = {("b-taipei", 2026, 10): {"revenue": 1.0, "nights": 936, "adr": 1.0}}
    # 1,450 reservation-nights of 4,278 reads 33.9%, under the 40% floor;
    # converted at 1.28 it is 43.5%, which is a month that traded.
    out = _run(monkeypatch,
               [cell("b-taipei", capacity=4278, otb=729, ly_otb=400, ly_final=1450)],
               occ=occ)
    assert out["cells"][0]["basis"] == "ly_pickup"


def test_a_thin_book_is_not_enough_to_take_the_ratio_from(monkeypatch):
    """Twelve nights sold against fourteen counted is not a 1.17x branch, it is
    two bookings. Under the floor the two counts are treated as the same."""
    occ = {("b-taipei", 2026, 12): {"revenue": 20_000.0, "nights": 14, "adr": 1_400.0}}
    out = _run(monkeypatch,
               [cell("b-taipei", month=12, capacity=4278, otb=12,
                     ly_otb=400, ly_final=3039, days_out=77)],
               occ=occ)
    assert out["cells"][0]["bed_factor"] == 1.0


def test_the_ratio_is_pooled_across_the_months_in_scope(monkeypatch):
    """A December read at six per cent sold has too little book to divide by,
    but the quarter it sits in does not."""
    occ = {
        ("b-taipei", 2026, 10): {"revenue": 1.0, "nights": 936, "adr": 1.0},
        ("b-taipei", 2026, 12): {"revenue": 1.0, "nights": 20, "adr": 1.0},
    }
    out = _run(monkeypatch, [
        cell("b-taipei", month=10, capacity=4278, otb=729, ly_otb=400, ly_final=1128),
        cell("b-taipei", month=12, capacity=4278, otb=12, ly_otb=400, ly_final=3039,
             days_out=77),
    ], occ=occ)
    factors = {c["stay_month"]: c["bed_factor"] for c in out["cells"]}
    assert factors["2026-10"] == factors["2026-12"] == pytest.approx(
        (936 + 20) / (729 + 12), abs=0.01)


# ── keeping today's speed ────────────────────────────────────────────────────

def rr_cell(bid="b-1948", **kw):
    """A cell carrying what the run-rate reading needs: the window, what it
    picked up, and what that pickup sold for."""
    base = cell(bid, **{k: v for k, v in kw.items() if k not in
                        ("pickup_nights", "pickup_revenue", "window_days")})
    base.update({
        "pickup_nights": kw.get("pickup_nights", 300.0),
        "pickup_revenue": kw.get("pickup_revenue", 600_000.0),
        "window_days": kw.get("window_days", 30),
    })
    return base


def test_todays_speed_is_carried_flat_to_the_end_of_the_month(monkeypatch):
    """300 nights in 30 days is 10 a day; 46 days left to sell is 460 more."""
    occ = {("b-1948", 2026, 10): {"revenue": 1_324_000.0, "nights": 662, "adr": 2000.0}}
    out = _run(monkeypatch, [rr_cell()], occ=occ)
    rr = out["cells"][0]["run_rate"]

    # 15 Sep through 31 Oct, today included — it is still a booking day.
    assert rr["days_left"] == 47
    assert rr["room_nights_per_day"] == 10.0
    assert rr["room_nights"] == pytest.approx(662 + 470, abs=1)
    # Priced at what the window itself sold at: 600,000 over 300 nights.
    assert rr["adr"] == 2000.0
    assert rr["revenue_native"] == pytest.approx(1_324_000 + 470 * 2000)


def test_the_window_control_moves_the_whole_answer(monkeypatch):
    """Reading the speed over 90 days instead of 30 is a different speed and so
    a different answer — which is what the control on the page is for."""
    occ = {("b-1948", 2026, 10): {"revenue": 1_324_000.0, "nights": 662, "adr": 2000.0}}
    fast = _run(monkeypatch, [rr_cell(window_days=30, pickup_nights=300.0)], occ=occ)
    slow = _run(monkeypatch, [rr_cell(window_days=90, pickup_nights=300.0)], occ=occ)
    assert fast["cells"][0]["run_rate"]["room_nights_per_day"] == 10.0
    assert slow["cells"][0]["run_rate"]["room_nights_per_day"] == pytest.approx(3.33, abs=0.01)
    assert fast["cells"][0]["run_rate"]["room_nights"] > slow["cells"][0]["run_rate"]["room_nights"]


def test_a_month_that_is_over_has_no_days_left_to_sell(monkeypatch):
    out = _run(monkeypatch, [rr_cell(status="finished", otb=1700)])
    rr = out["cells"][0]["run_rate"]
    assert rr["days_left"] == 0
    assert rr["room_nights"] == pytest.approx(1700, abs=1)


def test_the_run_rate_cannot_sell_more_than_the_house_holds(monkeypatch):
    """Nothing about extrapolating a speed knows inventory exists."""
    out = _run(monkeypatch, [rr_cell(otb=1500, pickup_nights=3000.0)])
    rr = out["cells"][0]["run_rate"]
    assert rr["room_nights"] == pytest.approx(2139 * MAX_FORECAST_OCC, abs=1)
    assert rr["capacity_capped"] is True


def test_the_run_rate_rolls_up_the_way_everything_else_does(monkeypatch):
    """Nights and money summed, the rate divided once, and the target paired
    with the branch-months that actually contributed revenue."""
    occ = {
        ("b-1948", 2026, 10): {"revenue": 1_324_000.0, "nights": 662, "adr": 2000.0},
        ("b-1948", 2026, 11): {"revenue": 500_000.0, "nights": 250, "adr": 2000.0},
    }
    targets = {("b-1948", 2026, 10): {"native": 4_500_000.0, "vnd": 0.0},
               ("b-1948", 2026, 11): {"native": 4_000_000.0, "vnd": 0.0}}
    out = _run(monkeypatch,
               [rr_cell(month=10), rr_cell(month=11, capacity=2070, otb=250, days_out=47)],
               occ=occ, targets=targets)
    total = out["total"]["run_rate"]
    months = [m["run_rate"] for m in out["months"]]

    assert total["room_nights"] == pytest.approx(sum(m["room_nights"] for m in months))
    assert total["room_nights_per_day"] == 20.0          # 10 a day on each month
    assert total["target_native"] == 8_500_000.0
    assert total["achievement_pct"] == pytest.approx(
        total["revenue_native"] / 8_500_000 * 100, abs=0.1)


# ── the month in points of occupancy ─────────────────────────────────────────

def test_the_four_readings_are_all_points_of_the_same_house(monkeypatch):
    """Sold, what today's speed adds, what last year's run-in added, and what
    the target asks for — one unit, so they can be read against each other."""
    occ = {("b-1948", 2026, 10): {"revenue": 1_324_000.0, "nights": 662, "adr": 2000.0},
           ("b-1948", 2025, 10): {"revenue": 3_510_000.0, "nights": 1755, "adr": 2000.0},
           ("b-1948", 2026, 8): {"revenue": 2_000_000.0, "nights": 1000, "adr": 2000.0},
           ("b-1948", 2025, 8): {"revenue": 2_000_000.0, "nights": 1000, "adr": 2000.0}}
    out = _run(monkeypatch, [rr_cell()], occ=occ,
               targets={("b-1948", 2026, 10): {"native": 3_500_000.0, "vnd": 0.0}})
    r = out["cells"][0]["run_rate"]

    assert r["otb_occ_pct"] == pytest.approx(662 / 2139 * 100, abs=0.1)
    # 300 nights in 30 days, 47 days left: 470 more, on a 2,139-night house.
    assert r["points_added"] == pytest.approx(470 / 2139 * 100, abs=0.2)
    # What last year still had to come from the same countdown position.
    assert r["ly_points_added"] == pytest.approx((1755 - 434) / 2139 * 100, abs=0.1)
    # And the occupancy the revenue target implies at the rate it is selling at.
    needed = 662 + (3_500_000 - 1_324_000) / 2000
    assert r["needed_occ_pct"] == pytest.approx(needed / 2139 * 100, abs=0.1)
    assert r["needed_over_capacity"] is False


def test_a_target_a_full_house_cannot_reach_is_a_pricing_problem(monkeypatch):
    """Osaka's December needs 123% of the house at the rate it is currently
    selling at. No amount of pace fixes that, and calling it a pace problem
    hides the only lever there is."""
    occ = {("b-osaka", 2026, 12): {"revenue": 2_088_822.0, "nights": 129, "adr": 16_190.0},
           ("b-osaka", 2025, 12): {"revenue": 1.0, "nights": 1580, "adr": 10_081.0}}
    out = _run(monkeypatch,
               [rr_cell("b-osaka", month=12, capacity=2201, otb=129,
                        ly_otb=209, ly_final=1580, days_out=77, city="Osaka")],
               occ=occ,
               targets={("b-osaka", 2026, 12): {"native": 28_100_000.0, "vnd": 0.0}})
    r = out["cells"][0]["run_rate"]

    assert r["needed_occ_pct"] > 100
    assert r["needed_over_capacity"] is True
    # The rate that would clear the target with the house full.
    assert r["adr_needed"] == pytest.approx(
        (28_100_000 - 2_088_822) / (2201 * MAX_FORECAST_OCC - 129), rel=1e-3)

    flagged = out["total"]["run_rate"]["over_capacity"]
    assert [f["stay_month"] for f in flagged] == ["2026-12"]
    assert flagged[0]["adr_needed"] > flagged[0]["adr_now"]


def test_points_are_divided_out_of_the_totals_not_averaged(monkeypatch):
    """A 31-night month and a 30-night one do not carry equal weight, and a
    69-unit branch does not outvote a 138-unit one."""
    occ = {("b-1948", 2026, 10): {"revenue": 1_000.0, "nights": 662, "adr": 2.0},
           ("b-taipei", 2026, 10): {"revenue": 1_000.0, "nights": 936, "adr": 2.0}}
    out = _run(monkeypatch, [
        rr_cell("b-1948", capacity=2139, otb=662),
        rr_cell("b-taipei", capacity=4278, otb=936),
    ], occ=occ)
    total = out["total"]["run_rate"]
    assert total["otb_occ_pct"] == pytest.approx((662 + 936) / (2139 + 4278) * 100, abs=0.1)
    # Not the mean of 31.0% and 21.9%.
    assert total["otb_occ_pct"] < 26.5


def test_needed_and_the_money_line_divide_by_the_same_rate(monkeypatch):
    """The card is named after the window's own rate, so both halves use it.
    Dividing `needed` by last year's rate while multiplying the revenue by the
    window's put "4.7 points short" directly above "101% of target" — same
    inputs, same card, opposite answers."""
    occ = {("b-1948", 2026, 10): {"revenue": 1_000_000.0, "nights": 662, "adr": 1_500.0},
           # Last year's rate is far below what the window is selling at.
           ("b-1948", 2025, 10): {"revenue": 1_755_000.0, "nights": 1755, "adr": 1_000.0}}
    out = _run(monkeypatch,
               [rr_cell(pickup_nights=300.0, pickup_revenue=900_000.0)],   # window ADR 3,000
               occ=occ,
               targets={("b-1948", 2026, 10): {"native": 2_500_000.0, "vnd": 0.0}})
    r = out["cells"][0]["run_rate"]

    assert r["adr"] == 3000.0
    # Needed divides the money still owed by that same 3,000, not by 1,000.
    assert r["needed_room_nights"] == pytest.approx(662 + (2_500_000 - 1_000_000) / 3000, abs=1)
    # So the two readings agree: clearing the target on points means clearing
    # it on money too.
    reach_pts = r["otb_occ_pct"] + r["points_added"]
    assert (reach_pts >= r["needed_occ_pct"]) == (r["revenue_native"] >= 2_500_000)


def test_the_gap_is_also_given_as_a_change_of_speed(monkeypatch):
    """"Three more room-nights a day" can be acted on; "4.7 points" cannot."""
    occ = {("b-1948", 2026, 10): {"revenue": 500_000.0, "nights": 662, "adr": 1_500.0}}
    out = _run(monkeypatch, [rr_cell()], occ=occ,
               targets={("b-1948", 2026, 10): {"native": 4_000_000.0, "vnd": 0.0}})
    r = out["cells"][0]["run_rate"]

    assert r["needed_extra_room_nights"] == pytest.approx(
        r["needed_room_nights"] - r["room_nights"], abs=0.2)
    assert r["needed_extra_per_day"] == pytest.approx(
        r["needed_extra_room_nights"] / r["days_left"], abs=0.02)
    # And a month already clear of its target asks for nothing extra.
    clear = _run(monkeypatch, [rr_cell()], occ=occ,
                 targets={("b-1948", 2026, 10): {"native": 100_000.0, "vnd": 0.0}})
    assert clear["cells"][0]["run_rate"]["needed_extra_per_day"] == 0


def test_the_branch_adjustments_are_applied_where_the_money_is_made(monkeypatch):
    """A Cloudbeds month becomes the KPI figure as `revenue × (1 − deduct%) +
    other revenue`, and the target was set against that. Osaka carries a 6%
    deduction and Saigon adds a fixed 48m a month; left off, a month reads
    above target that the KPI page would call short."""
    occ = {("b-osaka", 2026, 10): {"revenue": 1_000_000.0, "nights": 500, "adr": 2_000.0}}
    meta = {**BRANCHES}
    meta["b-osaka"] = {**meta["b-osaka"], "deduction_pct": 6.0,
                       "other_revenue_native": 100_000.0}
    monkeypatch.setattr(pace_forecast, "_monthly_adr", lambda *a, **k: occ)
    monkeypatch.setattr(pace_forecast, "_targets", lambda *a, **k: {})
    monkeypatch.setattr(pace_forecast, "get_cached_rate", lambda c, t="VND": 1.0)
    out = build_forecast(
        None,
        [rr_cell("b-osaka", capacity=2201, otb=500, ly_otb=1082, ly_final=1892,
                 city="Osaka", pickup_nights=300.0, pickup_revenue=900_000.0)],
        as_of=date(2026, 9, 15), branch_meta=meta, scoped_sources=False,
    )
    r = out["cells"][0]["run_rate"]
    raw = 1_000_000.0 + r["room_nights_added"] * r["adr"]
    assert r["revenue_native"] == pytest.approx(raw * 0.94 + 100_000, rel=1e-3)


def test_needed_undoes_the_adjustments_before_dividing(monkeypatch):
    """The target is a figure that already has the deduction taken and the
    other revenue added. Dividing it straight by the rate asks how many nights
    reach a number the branch never has to reach."""
    occ = {("b-osaka", 2026, 10): {"revenue": 1_000_000.0, "nights": 500, "adr": 2_000.0}}
    meta = {**BRANCHES}
    meta["b-osaka"] = {**meta["b-osaka"], "deduction_pct": 6.0,
                       "other_revenue_native": 100_000.0}
    monkeypatch.setattr(pace_forecast, "_monthly_adr", lambda *a, **k: occ)
    monkeypatch.setattr(pace_forecast, "_targets", lambda *a, **k:
                        {("b-osaka", 2026, 10): {"native": 3_000_000.0, "vnd": 0.0}})
    monkeypatch.setattr(pace_forecast, "get_cached_rate", lambda c, t="VND": 1.0)
    out = build_forecast(
        None,
        [rr_cell("b-osaka", capacity=2201, otb=500, ly_otb=1082, ly_final=1892,
                 city="Osaka", pickup_nights=300.0, pickup_revenue=900_000.0)],
        as_of=date(2026, 9, 15), branch_meta=meta, scoped_sources=False,
    )
    r = out["cells"][0]["run_rate"]
    raw_target = (3_000_000 - 100_000) / 0.94
    assert r["needed_room_nights"] == pytest.approx(
        500 + (raw_target - 1_000_000) / r["adr"], abs=1)


def test_every_revenue_in_the_payload_is_on_one_basis(monkeypatch):
    """Two estimators still price nights — the run-rate one the page reads, and
    the year-ago one left in the payload. Both carry the branch's deduction and
    other revenue, or a reader picking the wrong field gets a number off by the
    deduction with nothing to say so."""
    occ = {("b-osaka", 2026, 10): {"revenue": 1_000_000.0, "nights": 500, "adr": 2_000.0},
           ("b-osaka", 2025, 10): {"revenue": 3_000_000.0, "nights": 1500, "adr": 2_000.0}}
    meta = {**BRANCHES}
    meta["b-osaka"] = {**meta["b-osaka"], "deduction_pct": 6.0,
                       "other_revenue_native": 100_000.0}
    monkeypatch.setattr(pace_forecast, "_monthly_adr", lambda *a, **k: occ)
    monkeypatch.setattr(pace_forecast, "_targets", lambda *a, **k: {})
    monkeypatch.setattr(pace_forecast, "get_cached_rate", lambda c, t="VND": 1.0)
    out = build_forecast(
        None,
        [rr_cell("b-osaka", capacity=2201, otb=500, ly_otb=400, ly_final=1500,
                 city="Osaka", pickup_nights=300.0, pickup_revenue=900_000.0)],
        as_of=date(2026, 9, 15), branch_meta=meta, scoped_sources=False,
    )
    cell_row = out["cells"][0]
    # The year-ago estimator: 1m booked + the nights it adds, then adjusted.
    raw = 1_000_000.0 + max(0.0, cell_row["room_nights"] - 500) * cell_row["adr_remaining"]
    assert cell_row["revenue_native"] == pytest.approx(raw * 0.94 + 100_000, rel=1e-3)
    # And the run-rate one, which the page actually shows.
    r = cell_row["run_rate"]
    raw_rr = 1_000_000.0 + r["room_nights_added"] * r["adr"]
    assert r["revenue_native"] == pytest.approx(raw_rr * 0.94 + 100_000, rel=1e-3)
