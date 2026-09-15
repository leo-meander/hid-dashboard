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


def test_a_source_filter_removes_the_house_from_the_answer(nights_only):
    """Forecasting "Agoda only" against 95% of the house is meaningless — the
    rest of the house is being filled by everyone else. Nights still forecast;
    occupancy does not."""
    total = only(nights_only([cell()], scoped_sources=True))
    assert total["room_nights"] is not None
    assert total["occ_pct"] is None
    assert total["available_room_nights"] is None


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
