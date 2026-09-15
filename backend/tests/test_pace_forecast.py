"""Pace forecast — where a stay month lands, and whether that clears target.

The estimator is one line of arithmetic; everything that can make it lie lives
in the conditions around it, so that is what is asserted here.

  · The year-ago base must be real. Under 30 room-nights on the books, or a
    year-ago month that never traded normally, and the branch-month is not
    forecast from itself — Taipei opened mid-October 2025 and finished that
    month at 26%, which predicts an opening, not an October.
  · Where the base fails, the neighbours answer instead, as points of
    occupancy so a 92-room hotel can borrow from a 69-room one.
  · Nothing sells more than the house holds.
  · A month already over is not forecast. It is reported.
  · A total missing one branch is not a total.

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


@pytest.fixture
def no_money(monkeypatch):
    """Nights only: no daily_metrics, no targets, no FX."""
    monkeypatch.setattr(pace_forecast, "_monthly_adr", lambda *a, **k: {})
    monkeypatch.setattr(pace_forecast, "_targets", lambda *a, **k: {})
    monkeypatch.setattr(pace_forecast, "get_cached_rate", lambda *a, **k: 1.0)


@pytest.fixture
def money(monkeypatch):
    """A branch that sold at 2,000 last October and is running 10% up on ADR."""
    adr = {
        ("b-1948", 2025, 10): {"revenue": 3_510_000.0, "nights": 1755, "adr": 2000.0},
        ("b-1948", 2026, 10): {"revenue": 1_324_000.0, "nights": 662, "adr": 2000.0},
        # Settled reference months, this year against last, pooled 1.10x.
        ("b-1948", 2026, 8): {"revenue": 2_200_000.0, "nights": 1000, "adr": 2200.0},
        ("b-1948", 2025, 8): {"revenue": 2_000_000.0, "nights": 1000, "adr": 2000.0},
    }
    monkeypatch.setattr(pace_forecast, "_monthly_adr", lambda *a, **k: adr)
    monkeypatch.setattr(
        pace_forecast, "_targets",
        lambda *a, **k: {("b-1948", 2026, 10): {"native": 4_500_000.0, "vnd": 0.0}},
    )
    monkeypatch.setattr(pace_forecast, "get_cached_rate", lambda *a, **k: 830.0)


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

def test_forecast_adds_last_years_remaining_pickup(no_money):
    """The whole method: what is on the books now, plus what last year still
    had to come at this same distance from the month."""
    out = build_forecast(None, [cell()], as_of=date(2026, 9, 15),
                         branch_meta=BRANCHES, scoped_sources=False)
    total = only(out)

    assert total["room_nights"] == pytest.approx(662 + (1755 - 434))
    assert total["basis"] == ["ly_pickup"]
    assert total["occ_pct"] == pytest.approx(1983 / 2139 * 100, abs=0.1)
    # Ahead of where last year finished, stated in points of the same house.
    assert total["occ_pts_vs_ly"] == pytest.approx((1983 - 1755) / 2139 * 100, abs=0.1)


def test_band_is_the_measured_error_and_never_dips_under_the_book(no_money):
    """p10/p90 of the backtest, applied to the forecast — except that the low
    end cannot fall below what is already sold. A book does not shrink."""
    out = build_forecast(None, [cell()], as_of=date(2026, 9, 15),
                         branch_meta=BRANCHES, scoped_sources=False)
    total = only(out)
    assert total["room_nights_low"] < total["room_nights"] < total["room_nights_high"]
    assert total["room_nights_low"] >= total["otb_room_nights"]

    # A month barely sold: the low end clamps to the book rather than going under.
    thin = build_forecast(None, [cell(otb=1700, ly_otb=434, ly_final=1755)],
                          as_of=date(2026, 9, 15), branch_meta=BRANCHES,
                          scoped_sources=False)
    assert only(thin)["room_nights_low"] >= 1700


def test_a_month_further_out_than_the_backtest_gets_a_wider_band(no_money):
    near = only(build_forecast(None, [cell(days_out=60)], as_of=date(2026, 9, 15),
                               branch_meta=BRANCHES, scoped_sources=False))
    far = only(build_forecast(None, [cell(days_out=107)], as_of=date(2026, 9, 15),
                              branch_meta=BRANCHES, scoped_sources=False))
    near_spread = near["room_nights_high"] - near["room_nights_low"]
    far_spread = far["room_nights_high"] - far["room_nights_low"]
    assert far_spread > near_spread


def test_nothing_sells_more_than_the_house_holds(no_money):
    """The estimator has no idea inventory exists. Left alone it returns 104%
    occupancy; the ceiling stops it and the month says the ceiling bound."""
    out = build_forecast(None, [cell(otb=1500, ly_otb=434, ly_final=1755)],
                         as_of=date(2026, 9, 15), branch_meta=BRANCHES,
                         scoped_sources=False)
    total = only(out)
    assert total["room_nights"] == pytest.approx(2139 * MAX_FORECAST_OCC, abs=0.1)
    assert total["capacity_capped"] is True


def test_a_finished_month_is_reported_not_forecast(no_money):
    out = build_forecast(None, [cell(status="finished", otb=1700)],
                         as_of=date(2026, 9, 15), branch_meta=BRANCHES,
                         scoped_sources=False)
    total = only(out)
    assert total["basis"] == ["actual"]
    assert total["room_nights"] == total["room_nights_low"] == 1700


# ── the fallback ─────────────────────────────────────────────────────────────

def test_a_branch_with_no_year_ago_base_keeps_its_level_and_borrows_the_shape(monkeypatch):
    """Oani had not opened a year ago, so its October cannot come from its own
    history. What it does have is a present: it has run 70% every month this
    year. 1948 — same city, same calendar — finished last October 6% above its
    own summer, and that 6% is the only thing borrowed.

    The alternative, handing Oani 1948's remaining run-up in nights, sold Oani
    out at 95% for three months running: Oani is 41% booked at sixteen days out
    where 1948 is 31%, so its early bookings would have been counted twice.
    """
    occ = {}
    occ.update(dm("b-1948", 2025, 0.772, rooms=69))     # donor, a year ago
    occ.update(dm("b-oani", 2026, 0.700, rooms=92))     # recipient, this year
    monkeypatch.setattr(pace_forecast, "_monthly_adr", lambda *a, **k: occ)
    monkeypatch.setattr(pace_forecast, "_targets", lambda *a, **k: {})
    monkeypatch.setattr(pace_forecast, "get_cached_rate", lambda *a, **k: 1.0)

    sibling = cell("b-1948")                            # 1755 of 2139 = 82.0%
    oani = cell("b-oani", capacity=2852, otb=1159, ly_otb=0, ly_final=0)
    out = build_forecast(None, [sibling, oani], as_of=date(2026, 9, 15),
                         branch_meta=BRANCHES, scoped_sources=False)

    row = [b for b in out["branches"] if b["branch_id"] == "b-oani"][0]
    index = (1755 / 2139) / 0.772
    assert row["basis"] == ["proxy"]
    assert row["room_nights"] == pytest.approx(0.70 * index * 2852, abs=15)
    # Nowhere near the ceiling the old method pinned it to.
    assert row["occ_pct"] < 80
    assert row["proxy_donors"] == ["1948"]

    # The workings ride on the month, because the index is a different number
    # in October and in December.
    used = out["total"]["proxy_months"][0]
    assert used["branch_name"] == "Oani"
    assert used["stay_month"] == "2026-10"
    assert used["basis"] == "proxy"
    assert used["donors"] == ["1948"]
    assert used["level_occ_pct"] == pytest.approx(70.0, abs=0.2)
    assert used["seasonal_index"] == pytest.approx(index, abs=0.01)


def test_with_nobody_to_borrow_from_no_number_is_published(no_money):
    """Refusing is the honest answer, and it must not quietly become a total
    that looks whole."""
    alone = cell("b-taipei", capacity=2852, otb=1159, ly_otb=0, ly_final=0)
    out = build_forecast(None, [alone], as_of=date(2026, 9, 15),
                         branch_meta=BRANCHES, scoped_sources=False)
    total = only(out)
    assert total["room_nights"] is None
    assert total["unforecastable"] == [
        {"branch_id": "b-taipei", "branch_name": "Taipei", "stay_month": "2026-10"}
    ]


def test_borrowing_across_markets_says_so(monkeypatch):
    """A Taipei shape lent to Osaka is a guess about Japan made from Taiwan. It
    beats publishing nothing, and it must not look like the same thing as Oani
    borrowing from 1948 down the road."""
    occ = {}
    occ.update(dm("b-1948", 2025, 0.772, rooms=69))
    occ.update(dm("b-osaka", 2026, 0.817, rooms=71))
    monkeypatch.setattr(pace_forecast, "_monthly_adr", lambda *a, **k: occ)
    monkeypatch.setattr(pace_forecast, "_targets", lambda *a, **k: {})
    monkeypatch.setattr(pace_forecast, "get_cached_rate", lambda *a, **k: 1.0)

    out = build_forecast(
        None,
        [cell("b-1948"), cell("b-osaka", capacity=2201, otb=1259,
                              ly_otb=0, ly_final=0, city="Osaka")],
        as_of=date(2026, 9, 15), branch_meta=BRANCHES, scoped_sources=False,
    )
    osaka = [b for b in out["branches"] if b["branch_id"] == "b-osaka"][0]
    assert osaka["basis"] == ["proxy_other_market"]
    assert osaka["proxy_donors"] == ["1948"]
    assert out["total"]["proxy_months"][0]["basis"] == "proxy_other_market"


def test_a_property_opening_inside_the_window_is_a_guess_and_says_so(monkeypatch):
    """No year-ago month AND no months of its own. Nothing is left but to
    assume it trades like the market it is opening into — which is a guess, and
    is labelled one rather than dressed up as a forecast."""
    occ = dm("b-1948", 2025, 0.772, rooms=69)
    monkeypatch.setattr(pace_forecast, "_monthly_adr", lambda *a, **k: occ)
    monkeypatch.setattr(pace_forecast, "_targets", lambda *a, **k: {})
    monkeypatch.setattr(pace_forecast, "get_cached_rate", lambda *a, **k: 1.0)

    out = build_forecast(
        None,
        [cell("b-1948"), cell("b-oani", capacity=2852, otb=100,
                              ly_otb=0, ly_final=0)],
        as_of=date(2026, 9, 15), branch_meta=BRANCHES, scoped_sources=False,
    )
    new_property = [b for b in out["branches"] if b["branch_id"] == "b-oani"][0]
    assert new_property["basis"] == ["proxy_level"]
    assert out["total"]["proxy_months"][0]["level_occ_pct"] == pytest.approx(
        1755 / 2139 * 100, abs=0.1)


def test_a_branch_that_cannot_be_priced_voids_the_money_not_the_nights(money):
    """A quarter missing one branch's revenue is not a quarter of revenue — the
    roll-up says so instead of quietly reporting the branches it happens to be
    able to price. The nights it CAN count still come back."""
    out = build_forecast(
        None,
        [cell("b-1948"), cell("b-osaka", capacity=2201, otb=1259,
                              ly_otb=1082, ly_final=1892, city="Osaka")],
        as_of=date(2026, 9, 15), branch_meta=BRANCHES, scoped_sources=False,
    )
    total = only(out)
    # Osaka has no daily_metrics history in this fixture, so it has no ADR.
    assert total["revenue_native"] is None
    assert total["achievement_pct"] is None
    assert total["room_nights"] is not None
    priced = [b for b in out["branches"] if b["branch_id"] == "b-1948"][0]
    assert priced["revenue_native"] is not None


# ── several months, several branches ─────────────────────────────────────────

def test_the_donor_lends_the_same_month_not_the_quarter(monkeypatch):
    """A quarter is read three months at a time. Pooling the donors across them
    lends December's shape to October and hands every month the same seasonal
    index — which is exactly how you spot it."""
    occ = {}
    occ.update(dm("b-1948", 2025, 0.772, rooms=69))
    occ.update(dm("b-oani", 2026, 0.700, rooms=92))
    monkeypatch.setattr(pace_forecast, "_monthly_adr", lambda *a, **k: occ)
    monkeypatch.setattr(pace_forecast, "_targets", lambda *a, **k: {})
    monkeypatch.setattr(pace_forecast, "get_cached_rate", lambda *a, **k: 1.0)

    # 1948 fills 82% of its October and 62% of its December; Oani has neither.
    cells = [
        cell("b-1948", month=10, capacity=2139, ly_otb=434, ly_final=1755),
        cell("b-1948", month=12, capacity=2139, ly_otb=33, ly_final=1320),
        cell("b-oani", month=10, capacity=2852, otb=1159, ly_otb=0, ly_final=0),
        cell("b-oani", month=12, capacity=2852, otb=304, ly_otb=0, ly_final=0),
    ]
    out = build_forecast(None, cells, as_of=date(2026, 9, 15),
                         branch_meta=BRANCHES, scoped_sources=False)

    by_month = {p["stay_month"]: p for p in out["total"]["proxy_months"]}
    assert by_month["2026-10"]["seasonal_index"] > by_month["2026-12"]["seasonal_index"]
    assert by_month["2026-10"]["seasonal_index"] == pytest.approx(
        (1755 / 2139) / 0.772, abs=0.01)
    assert by_month["2026-12"]["seasonal_index"] == pytest.approx(
        (1320 / 2139) / 0.772, abs=0.01)


def test_a_wild_seasonal_index_is_clipped_and_flagged(monkeypatch):
    """The donor's summer is the index's denominator, so a donor that spent it
    closed for works reads as a season that never happened. Oani has never
    cleared 73% in any month of its life; an unclipped 1.34 would have put its
    November at 92%."""
    occ = {}
    occ.update(dm("b-1948", 2025, 0.52, rooms=69))      # a disrupted year-ago summer
    occ.update(dm("b-oani", 2026, 0.700, rooms=92))
    monkeypatch.setattr(pace_forecast, "_monthly_adr", lambda *a, **k: occ)
    monkeypatch.setattr(pace_forecast, "_targets", lambda *a, **k: {})
    monkeypatch.setattr(pace_forecast, "get_cached_rate", lambda *a, **k: 1.0)

    out = build_forecast(
        None,
        [cell("b-1948"), cell("b-oani", capacity=2852, otb=1159,
                              ly_otb=0, ly_final=0)],
        as_of=date(2026, 9, 15), branch_meta=BRANCHES, scoped_sources=False,
    )
    used = out["total"]["proxy_months"][0]
    assert (1755 / 2139) / 0.52 > pace_forecast.PROXY_INDEX_MAX   # raw is wilder
    assert used["seasonal_index"] == pace_forecast.PROXY_INDEX_MAX
    assert used["index_clipped"] is True
    row = [b for b in out["branches"] if b["branch_id"] == "b-oani"][0]
    assert row["occ_pct"] == pytest.approx(70 * pace_forecast.PROXY_INDEX_MAX, abs=0.5)


def test_months_are_summed_and_the_rate_divided_once(no_money):
    """A quarter's occupancy is not the mean of three months' percentages: a
    31-night month and a 30-night one do not carry equal weight."""
    oct_ = cell(month=10, capacity=2139, otb=662, ly_otb=434, ly_final=1755)
    nov = cell(month=11, capacity=2600, otb=308, ly_otb=101, ly_final=1811)
    out = build_forecast(None, [oct_, nov], as_of=date(2026, 9, 15),
                         branch_meta=BRANCHES, scoped_sources=False)

    total = only(out)
    nights = (662 + 1755 - 434) + (308 + 1811 - 101)
    assert total["room_nights"] == pytest.approx(nights)
    assert total["occ_pct"] == pytest.approx(nights / (2139 + 2600) * 100, abs=0.1)
    assert [m["stay_month"] for m in out["months"]] == ["2026-10", "2026-11"]


def test_a_source_filter_removes_the_house_from_the_answer(no_money):
    """Forecasting "Agoda only" against 95% of the house is meaningless — the
    rest of the house is being filled by everyone else. Nights still forecast;
    occupancy does not."""
    out = build_forecast(None, [cell()], as_of=date(2026, 9, 15),
                         branch_meta=BRANCHES, scoped_sources=True)
    total = only(out)
    assert total["room_nights"] is not None
    assert total["occ_pct"] is None
    assert total["available_room_nights"] is None


def test_mixed_currencies_lose_the_symbol_but_keep_the_vnd_total(money):
    """The group runs TWD, JPY and VND. One symbol over the sum would pick one
    and be wrong about the other two."""
    out = build_forecast(
        None,
        [cell("b-1948"), cell("b-osaka", capacity=2201, otb=1259,
                              ly_otb=1082, ly_final=1892, city="Osaka")],
        as_of=date(2026, 9, 15), branch_meta=BRANCHES, scoped_sources=False,
    )
    assert out["total"]["currency"] is None
    single = build_forecast(None, [cell("b-1948")], as_of=date(2026, 9, 15),
                            branch_meta=BRANCHES, scoped_sources=False)
    assert single["total"]["currency"] == "TWD"


# ── money ────────────────────────────────────────────────────────────────────

def test_only_the_nights_still_to_come_are_priced(money):
    """Nights already sold are already priced, at whatever they were sold for.
    Applying a forecast ADR to the whole month would re-price the book."""
    out = build_forecast(None, [cell()], as_of=date(2026, 9, 15),
                         branch_meta=BRANCHES, scoped_sources=False)
    total = only(out)

    remaining = 1983 - 662          # daily_metrics book is the same 662 nights
    assert total["revenue_native"] == pytest.approx(1_324_000 + remaining * 2000 * 1.1)
    assert total["target_native"] == 4_500_000.0
    assert total["achievement_pct"] == pytest.approx(
        total["revenue_native"] / 4_500_000 * 100, abs=0.1)


def test_adr_is_last_years_month_moved_by_this_years_trend(money):
    out = build_forecast(None, [cell()], as_of=date(2026, 9, 15),
                         branch_meta=BRANCHES, scoped_sources=False)
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


def test_a_branch_with_no_year_ago_money_is_not_priced(no_money):
    out = build_forecast(None, [cell()], as_of=date(2026, 9, 15),
                         branch_meta=BRANCHES, scoped_sources=False)
    total = only(out)
    assert total["room_nights"] is not None
    assert total["revenue_native"] is None
    assert total["achievement_pct"] is None


def test_settled_months_walks_backwards_across_the_year_boundary():
    assert _settled_months(date(2026, 2, 10), 3) == [(2026, 1), (2025, 12), (2025, 11)]
