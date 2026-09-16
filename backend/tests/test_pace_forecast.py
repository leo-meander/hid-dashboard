"""Pace projection — where a stay month gets to at the rate it is filling now.

One reading, and it is arithmetic rather than a prediction: what is on the
books, plus the room-nights a booking day is currently adding, carried flat to
the end of the month and priced at what those bookings are selling for. What
has to hold is everything around that sum.

  · A sold night is counted the way the target counts it. `reservations` holds
    one row per booking, so three dorm beds count once; daily_metrics counts
    the beds, and the target was set against daily_metrics.
  · Money carries the branch's deduction and other revenue, because the target
    already has them.
  · `Needed` divides by the same rate the nights beside it are multiplied by,
    or the card contradicts itself.
  · Nothing sells more than the house holds.
  · Under a source or room-type filter, nothing is returned at all.

No database: the two lookups that need one are faked, and the arithmetic is
the whole of what is checked.
"""
from datetime import date

import pytest

from app.services import pace_forecast
from app.services.pace_forecast import (
    MAX_FORECAST_OCC,
    MIN_FACTOR_BASE_ROOM_NIGHTS,
    _adr_yoy,
    _settled_months,
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

REF = [(2026, 8), (2026, 7), (2026, 6)]      # settled, read at 15 Sep 2026
REF_DAYS = {6: 30, 7: 31, 8: 31}


def dm(bid, year, occ, rooms, revenue_per_night=0.0):
    """daily_metrics rows for the three reference months at a flat occupancy."""
    out = {}
    for (_, m) in REF:
        nights = round(rooms * REF_DAYS[m] * occ)
        out[(bid, year, m)] = {"revenue": nights * revenue_per_night,
                               "nights": nights, "adr": revenue_per_night or None}
    return out


def cell(bid="b-1948", *, year=2026, month=10, days_out=16, capacity=2139,
         otb=662, ly_otb=434, ly_final=1755, status="future", city=None,
         pickup_nights=300.0, pickup_revenue=600_000.0, window_days=30):
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
        "pickup_nights": pickup_nights,
        "pickup_revenue": pickup_revenue,
        "window_days": window_days,
    }


def run(monkeypatch, cells, occ=None, targets=None, meta=None, **kw):
    monkeypatch.setattr(pace_forecast, "_monthly_adr", lambda *a, **k: occ or {})
    monkeypatch.setattr(pace_forecast, "_targets", lambda *a, **k: targets or {})
    monkeypatch.setattr(pace_forecast, "get_cached_rate",
                        lambda c, t="VND": {"TWD": 830.0, "JPY": 165.0}.get(c, 1.0))
    return build_forecast(None, cells, as_of=date(2026, 9, 15),
                          branch_meta=meta or BRANCHES,
                          **{"scoped_sources": False, **kw})


def only(out):
    return out["total"]["run_rate"]


# ── the sum ──────────────────────────────────────────────────────────────────

def test_todays_speed_is_carried_flat_to_the_end_of_the_month(monkeypatch):
    """300 nights in 30 days is 10 a day; 15 Sep through 31 Oct is 47 days left
    to sell, today included, so 470 more."""
    occ = {("b-1948", 2026, 10): {"revenue": 1_324_000.0, "nights": 662, "adr": 2000.0}}
    r = run(monkeypatch, [cell()], occ=occ)["cells"][0]["run_rate"]

    assert r["days_left"] == 47
    assert r["room_nights_per_day"] == 10.0
    assert r["room_nights"] == pytest.approx(662 + 470, abs=1)
    assert r["adr"] == 2000.0                      # 600,000 over 300 nights
    assert r["revenue_native"] == pytest.approx(1_324_000 + 470 * 2000)


def test_the_window_control_moves_the_whole_answer(monkeypatch):
    """Reading the speed over 90 days instead of 30 is a different speed and so
    a different answer — which is what the control on the page is for."""
    occ = {("b-1948", 2026, 10): {"revenue": 1_324_000.0, "nights": 662, "adr": 2000.0}}
    fast = run(monkeypatch, [cell(window_days=30)], occ=occ)["cells"][0]["run_rate"]
    slow = run(monkeypatch, [cell(window_days=90)], occ=occ)["cells"][0]["run_rate"]
    assert fast["room_nights_per_day"] == 10.0
    assert slow["room_nights_per_day"] == pytest.approx(3.33, abs=0.01)
    assert fast["room_nights"] > slow["room_nights"]


def test_a_month_that_is_over_has_no_days_left_to_sell(monkeypatch):
    r = run(monkeypatch, [cell(status="finished", otb=1700)])["cells"][0]["run_rate"]
    assert r["days_left"] == 0
    assert r["room_nights"] == pytest.approx(1700, abs=1)


def test_nothing_sells_more_than_the_house_holds(monkeypatch):
    """Extrapolating a speed knows nothing about inventory."""
    r = run(monkeypatch, [cell(otb=1500, pickup_nights=3000.0)])["cells"][0]["run_rate"]
    assert r["room_nights"] == pytest.approx(2139 * MAX_FORECAST_OCC, abs=1)
    assert r["capacity_capped"] is True


# ── counting a sold night the way the target counts it ──────────────────────

def test_the_book_comes_from_the_table_the_target_is_set_against(monkeypatch):
    """`reservations` counts a three-bed booking once; daily_metrics counts the
    beds, and Taipei is 108 beds to 30 rooms."""
    occ = {("b-taipei", 2026, 10): {"revenue": 1_711_956.0, "nights": 936, "adr": 1_829.0}}
    out = run(monkeypatch, [cell("b-taipei", capacity=4278, otb=729)], occ=occ)
    c = out["cells"][0]

    assert c["otb_reservation_nights"] == 729
    assert c["otb_room_nights"] == 936
    assert c["bed_factor"] == pytest.approx(936 / 729, abs=0.01)
    # And the tile above the card reads the same figure.
    assert out["total"]["otb_room_nights"] == 936


def test_a_month_daily_metrics_has_nothing_for_is_converted_not_mixed(monkeypatch):
    """Half of one basis and half of the other is the one thing not allowed."""
    occ = {("b-taipei", 2026, 10): {"revenue": 1.0, "nights": 936, "adr": 1.0}}
    out = run(monkeypatch, [
        cell("b-taipei", month=10, capacity=4278, otb=729),
        cell("b-taipei", month=12, capacity=4278, otb=100, days_out=77),
    ], occ=occ)
    dec = [c for c in out["cells"] if c["month"] == 12][0]
    assert dec["otb_room_nights"] == pytest.approx(100 * dec["bed_factor"], abs=0.2)


def test_a_thin_book_is_not_enough_to_take_the_ratio_from(monkeypatch):
    """Twelve nights against fourteen is not a 1.17x branch, it is two
    bookings. Under the floor the two counts are treated as the same."""
    occ = {("b-taipei", 2026, 12): {"revenue": 20_000.0, "nights": 14, "adr": 1_400.0}}
    out = run(monkeypatch,
              [cell("b-taipei", month=12, capacity=4278,
                    otb=MIN_FACTOR_BASE_ROOM_NIGHTS - 18, days_out=77)],
              occ=occ)
    assert out["cells"][0]["bed_factor"] == 1.0


def test_the_ratio_is_pooled_across_the_months_in_scope(monkeypatch):
    """A December read at six per cent sold has too little book to divide by,
    but the quarter it sits in does not."""
    occ = {("b-taipei", 2026, 10): {"revenue": 1.0, "nights": 936, "adr": 1.0},
           ("b-taipei", 2026, 12): {"revenue": 1.0, "nights": 20, "adr": 1.0}}
    out = run(monkeypatch, [
        cell("b-taipei", month=10, capacity=4278, otb=729),
        cell("b-taipei", month=12, capacity=4278, otb=12, days_out=77),
    ], occ=occ)
    factors = {c["stay_month"]: c["bed_factor"] for c in out["cells"]}
    assert factors["2026-10"] == factors["2026-12"] == pytest.approx(
        (936 + 20) / (729 + 12), abs=0.01)


# ── the month in points of occupancy ────────────────────────────────────────

def test_the_readings_are_points_of_the_same_house(monkeypatch):
    occ = {("b-1948", 2026, 10): {"revenue": 1_324_000.0, "nights": 662, "adr": 2000.0}}
    r = run(monkeypatch, [cell()], occ=occ,
            targets={("b-1948", 2026, 10): {"native": 3_500_000.0, "vnd": 0.0}}
            )["cells"][0]["run_rate"]

    assert r["otb_occ_pct"] == pytest.approx(662 / 2139 * 100, abs=0.1)
    assert r["points_added"] == pytest.approx(470 / 2139 * 100, abs=0.2)
    assert r["occ_pct"] == pytest.approx(r["otb_occ_pct"] + r["points_added"], abs=0.1)
    needed = 662 + (3_500_000 - 1_324_000) / 2000
    assert r["needed_occ_pct"] == pytest.approx(needed / 2139 * 100, abs=0.1)
    assert r["needed_over_capacity"] is False


def test_a_target_a_full_house_cannot_reach_is_a_pricing_problem(monkeypatch):
    """No amount of pace fixes it, and calling it a pace problem hides the only
    lever there is."""
    occ = {("b-osaka", 2026, 12): {"revenue": 2_088_822.0, "nights": 129, "adr": 16_190.0}}
    out = run(monkeypatch,
              [cell("b-osaka", month=12, capacity=2201, otb=129, days_out=77,
                    city="Osaka")],
              occ=occ,
              targets={("b-osaka", 2026, 12): {"native": 28_100_000.0, "vnd": 0.0}})
    r = out["cells"][0]["run_rate"]

    assert r["needed_occ_pct"] > 100
    assert r["needed_over_capacity"] is True
    assert r["adr_needed"] == pytest.approx(
        (28_100_000 - 2_088_822) / (2201 * MAX_FORECAST_OCC - 129), rel=1e-3)
    flagged = out["total"]["run_rate"]["over_capacity"]
    assert [f["stay_month"] for f in flagged] == ["2026-12"]


def test_the_gap_is_also_given_as_a_change_of_speed(monkeypatch):
    """"Three more room-nights a day" can be acted on; "4.7 points" cannot."""
    occ = {("b-1948", 2026, 10): {"revenue": 500_000.0, "nights": 662, "adr": 1_500.0}}
    r = run(monkeypatch, [cell()], occ=occ,
            targets={("b-1948", 2026, 10): {"native": 4_000_000.0, "vnd": 0.0}}
            )["cells"][0]["run_rate"]

    assert r["needed_extra_room_nights"] == pytest.approx(
        r["needed_room_nights"] - r["room_nights"], abs=0.2)
    assert r["needed_extra_per_day"] == pytest.approx(
        r["needed_extra_room_nights"] / r["days_left"], abs=0.02)


def test_the_ceiling_is_every_remaining_room_at_todays_rate(monkeypatch):
    """The line between "fill faster" and "no amount of filling reaches this"."""
    occ = {("b-1948", 2026, 10): {"revenue": 1_324_000.0, "nights": 662, "adr": 2000.0}}
    c = run(monkeypatch, [cell()], occ=occ)["cells"][0]
    r = c["run_rate"]

    room = 2139 * MAX_FORECAST_OCC - 662
    assert r["room_nights_to_sell"] == pytest.approx(room, abs=1)
    assert r["revenue_max_native"] == pytest.approx(
        1_324_000 + room / c["bed_factor"] * r["adr"], rel=1e-3)
    assert r["revenue_max_native"] > r["revenue_native"]


def test_points_are_divided_out_of_the_totals_not_averaged(monkeypatch):
    """A 69-unit branch does not outvote a 138-unit one."""
    occ = {("b-1948", 2026, 10): {"revenue": 1_000.0, "nights": 662, "adr": 2.0},
           ("b-taipei", 2026, 10): {"revenue": 1_000.0, "nights": 936, "adr": 2.0}}
    total = only(run(monkeypatch, [
        cell("b-1948", capacity=2139, otb=662),
        cell("b-taipei", capacity=4278, otb=936),
    ], occ=occ))
    assert total["otb_occ_pct"] == pytest.approx((662 + 936) / (2139 + 4278) * 100, abs=0.1)
    assert total["otb_occ_pct"] < 26.5          # not the mean of 31.0 and 21.9


# ── money ────────────────────────────────────────────────────────────────────

def test_only_the_nights_still_to_come_are_priced(monkeypatch):
    """Nights already sold are already priced, at whatever they sold for."""
    occ = {("b-1948", 2026, 10): {"revenue": 1_324_000.0, "nights": 662, "adr": 2000.0}}
    r = run(monkeypatch, [cell()], occ=occ)["cells"][0]["run_rate"]
    assert r["revenue_native"] == pytest.approx(
        1_324_000 + r["room_nights_added"] / 1.0 * r["adr"], rel=1e-3)


def test_the_branch_adjustments_are_applied_where_the_money_is_made(monkeypatch):
    """A Cloudbeds month becomes the KPI figure as `revenue × (1 − deduct%) +
    other revenue`, and the target was set against that."""
    occ = {("b-osaka", 2026, 10): {"revenue": 1_000_000.0, "nights": 500, "adr": 2_000.0}}
    meta = {**BRANCHES, "b-osaka": {**BRANCHES["b-osaka"], "deduction_pct": 6.0,
                                    "other_revenue_native": 100_000.0}}
    r = run(monkeypatch,
            [cell("b-osaka", capacity=2201, otb=500, city="Osaka",
                  pickup_nights=300.0, pickup_revenue=900_000.0)],
            occ=occ, meta=meta)["cells"][0]["run_rate"]
    raw = 1_000_000.0 + r["room_nights_added"] * r["adr"]
    assert r["revenue_native"] == pytest.approx(raw * 0.94 + 100_000, rel=1e-3)


def test_needed_undoes_the_adjustments_before_dividing(monkeypatch):
    """The target already has the deduction taken and the other revenue added."""
    occ = {("b-osaka", 2026, 10): {"revenue": 1_000_000.0, "nights": 500, "adr": 2_000.0}}
    meta = {**BRANCHES, "b-osaka": {**BRANCHES["b-osaka"], "deduction_pct": 6.0,
                                    "other_revenue_native": 100_000.0}}
    r = run(monkeypatch,
            [cell("b-osaka", capacity=2201, otb=500, city="Osaka",
                  pickup_nights=300.0, pickup_revenue=900_000.0)],
            occ=occ, meta=meta,
            targets={("b-osaka", 2026, 10): {"native": 3_000_000.0, "vnd": 0.0}}
            )["cells"][0]["run_rate"]
    raw_target = (3_000_000 - 100_000) / 0.94
    assert r["needed_room_nights"] == pytest.approx(
        500 + (raw_target - 1_000_000) / r["adr"], abs=1)


def test_needed_and_the_money_line_divide_by_the_same_rate(monkeypatch):
    """Dividing `needed` by last year's rate while multiplying the revenue by
    the window's put "4.7 points short" above "101% of target" on one card."""
    occ = {("b-1948", 2026, 10): {"revenue": 1_000_000.0, "nights": 662, "adr": 1_500.0},
           ("b-1948", 2025, 10): {"revenue": 1_755_000.0, "nights": 1755, "adr": 1_000.0}}
    r = run(monkeypatch, [cell(pickup_revenue=900_000.0)], occ=occ,
            targets={("b-1948", 2026, 10): {"native": 2_500_000.0, "vnd": 0.0}}
            )["cells"][0]["run_rate"]

    assert r["adr"] == 3000.0
    assert r["needed_room_nights"] == pytest.approx(662 + (2_500_000 - 1_000_000) / 3000, abs=1)
    assert (r["occ_pct"] >= r["needed_occ_pct"]) == (r["revenue_native"] >= 2_500_000)


# ── the rate the remaining nights are priced at ─────────────────────────────

def test_the_rate_ladder_prefers_last_years_month_moved_by_the_trend(monkeypatch):
    occ = {("b-1948", 2025, 10): {"revenue": 3_510_000.0, "nights": 1755, "adr": 2000.0},
           ("b-1948", 2026, 10): {"revenue": 1_324_000.0, "nights": 662, "adr": 2000.0},
           ("b-1948", 2026, 8): {"revenue": 2_200_000.0, "nights": 1000, "adr": 2200.0},
           ("b-1948", 2025, 8): {"revenue": 2_000_000.0, "nights": 1000, "adr": 2000.0}}
    c = run(monkeypatch, [cell(pickup_nights=0.0, pickup_revenue=0.0)], occ=occ)["cells"][0]
    assert c["adr_yoy"] == pytest.approx(1.1)
    # No pickup to take a window rate from, so the ladder's first rung is used.
    assert c["adr_remaining"] == pytest.approx(2000 * 1.1)


def test_a_year_ago_month_that_barely_traded_is_not_a_rate(monkeypatch):
    """Oani's October 2025 ran 37 room-nights; what those few bookings paid is
    not October's rate."""
    occ = {("b-oani", 2025, 10): {"revenue": 807_800.0, "nights": 37, "adr": 21_832.0},
           ("b-oani", 2026, 10): {"revenue": 5_373_798.0, "nights": 1159, "adr": 4_636.0},
           **dm("b-oani", 2026, 0.700, rooms=92, revenue_per_night=3_500.0)}
    c = run(monkeypatch,
            [cell("b-oani", capacity=2852, otb=1159, pickup_nights=0.0,
                  pickup_revenue=0.0)],
            occ=occ)["cells"][0]
    assert c["adr_remaining"] == pytest.approx(3_500.0, rel=1e-3)


def test_adr_trend_is_pooled_across_months_not_averaged():
    """A quiet month must not carry the same weight as a busy one."""
    adr = {("b", 2026, 8): {"revenue": 1000.0, "nights": 10, "adr": 100.0},
           ("b", 2025, 8): {"revenue": 1000.0, "nights": 10, "adr": 100.0},
           ("b", 2026, 7): {"revenue": 20_000.0, "nights": 100, "adr": 200.0},
           ("b", 2025, 7): {"revenue": 10_000.0, "nights": 100, "adr": 100.0}}
    pooled = _adr_yoy(adr, "b", [(2026, 8), (2026, 7)])
    assert pooled == pytest.approx((21_000 / 110) / (11_000 / 110))
    assert pooled > 1.5          # the hundred-night month carries it


# ── scope ────────────────────────────────────────────────────────────────────

def test_a_filtered_selection_gets_no_projection_at_all(monkeypatch):
    """Pace narrows to one source or room type; nothing it is measured against
    does. Every figure would be a slice over a whole."""
    for kw in ({"scoped_sources": True}, {"room_category": "Dorm"}):
        out = run(monkeypatch, [cell()], **kw)
        assert out["available"] is False
        assert out["reason"] == "filtered"
        assert out["filtered_by"] == (["source"] if "scoped_sources" in kw
                                      else ["room_category"])


def test_mixed_currencies_lose_the_symbol_but_keep_the_vnd_total(monkeypatch):
    """The group runs TWD, JPY and VND. One symbol over the sum would pick one
    and be wrong about the other two."""
    occ = {("b-1948", 2026, 10): {"revenue": 1_000_000.0, "nights": 662, "adr": 2000.0},
           ("b-osaka", 2026, 10): {"revenue": 20_000_000.0, "nights": 1259, "adr": 15_886.0}}
    mixed = run(monkeypatch, [
        cell("b-1948"),
        cell("b-osaka", capacity=2201, otb=1259, city="Osaka"),
    ], occ=occ)
    assert mixed["total"]["currency"] is None
    assert mixed["total"]["run_rate"]["revenue_vnd"] is not None

    single = run(monkeypatch, [cell()], occ=occ)
    assert single["total"]["currency"] == "TWD"
    assert single["total"]["run_rate"]["revenue_vnd"] == pytest.approx(
        single["total"]["run_rate"]["revenue_native"] * 830)


def test_months_and_branches_each_carry_their_own_reading(monkeypatch):
    occ = {("b-1948", 2026, 10): {"revenue": 1_000_000.0, "nights": 662, "adr": 2000.0},
           ("b-1948", 2026, 11): {"revenue": 500_000.0, "nights": 340, "adr": 2000.0}}
    out = run(monkeypatch, [
        cell(month=10),
        cell(month=11, capacity=2070, otb=340, days_out=47),
    ], occ=occ)
    assert [m["stay_month"] for m in out["months"]] == ["2026-10", "2026-11"]
    assert all(m["run_rate"]["room_nights"] is not None for m in out["months"])
    assert out["branches"][0]["branch_name"] == "1948"
    assert out["total"]["run_rate"]["room_nights"] == pytest.approx(
        sum(m["run_rate"]["room_nights"] for m in out["months"]))


def test_settled_months_walks_backwards_across_the_year_boundary():
    assert _settled_months(date(2026, 2, 10), 3) == [(2026, 1), (2025, 12), (2025, 11)]


def test_the_warning_quotes_the_rate_it_divided_by(monkeypatch):
    """Quoting one rate while dividing by another printed "needs 121% of the
    house at 2,029 a night, and a full house clears it at 1,841" — which cannot
    both be true. The rate that would clear the target is always above the one
    being taken now, or the month would not be over the ceiling at all."""
    occ = {("b-taipei", 2026, 12): {"revenue": 292_654.0, "nights": 149, "adr": 1_964.0},
           # Last year's rate is far above what the window is selling at, and
           # it used to be the one quoted.
           ("b-taipei", 2025, 12): {"revenue": 6_039_021.0, "nights": 3039, "adr": 1_987.0}}
    out = run(monkeypatch,
              [cell("b-taipei", month=12, capacity=4278, otb=149, days_out=77,
                    pickup_nights=80.0, pickup_revenue=114_000.0)],
              occ=occ,
              targets={("b-taipei", 2026, 12): {"native": 7_500_000.0, "vnd": 0.0}})
    flagged = out["total"]["run_rate"]["over_capacity"]

    assert len(flagged) == 1
    assert flagged[0]["adr_now"] == out["cells"][0]["run_rate"]["adr"]
    assert flagged[0]["adr_needed"] > flagged[0]["adr_now"]
