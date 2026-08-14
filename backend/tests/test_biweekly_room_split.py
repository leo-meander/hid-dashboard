"""Bi-Weekly report — the private-room vs dorm-bed split of ADR / OCC / RevPAR.

Added 2026-08-14. The trap this covers is the denominator: `branches.total_rooms`
mixes private rooms and dorm beds into one capacity number (Saigon 84 = 36 rooms
+ 48 beds), so the blended RevPAR on the Executive Summary cards is a
capacity-weighted average of two figures measured in different units. Each
segment here has to divide by its OWN inventory, or dorm RevPAR silently reads
against a room count and comes out several times too high.
"""
from types import SimpleNamespace

from app.services.biweekly_report_builder import _segment_rates, room_type_block


def _branch(room_count, dorm_count, total_rooms=84):
    return SimpleNamespace(
        id="11111111-1111-1111-1111-111111111111",
        name="MEANDER Test",
        total_rooms=total_rooms,
        total_room_count=room_count,
        total_dorm_count=dorm_count,
    )


_TOTALS = {
    "room_rev": 777_000_000.0, "dorm_rev": 268_000_000.0,
    "room_nights": 433, "dorm_nights": 582,
}


class TestSegmentRates:
    def test_private_rooms_divide_by_the_room_count(self):
        r = _segment_rates(_TOTALS, 36, 14, "room_rev", "room_nights")
        assert r["revpar"] == round(777_000_000 / (36 * 14), 2)
        assert r["adr"] == round(777_000_000 / 433, 2)
        assert r["occ_pct"] == round(433 / (36 * 14), 4)

    def test_dorm_beds_divide_by_the_bed_count(self):
        """The denominator is 48 beds, not the 84 mixed units on the branch and
        not the number of dorm rooms those beds sit in."""
        r = _segment_rates(_TOTALS, 48, 14, "dorm_rev", "dorm_nights")
        assert r["revpar"] == round(268_000_000 / (48 * 14), 2)
        assert r["occ_pct"] == round(582 / (48 * 14), 4)

    def test_revpar_equals_adr_times_occ_within_rounding(self):
        """The two are computed independently, so this identity holding is what
        confirms the ADR denominator (units sold) and the RevPAR denominator
        (units available) belong to the same segment.

        Tolerance is relative: `occ_pct` is stored to 4 decimals, and against a
        VND ADR in the millions that rounding alone moves the product by tens
        of dong.
        """
        for cap, rev_key, nights_key in ((36, "room_rev", "room_nights"),
                                         (48, "dorm_rev", "dorm_nights")):
            r = _segment_rates(_TOTALS, cap, 14, rev_key, nights_key)
            assert abs(r["adr"] * r["occ_pct"] - r["revpar"]) <= r["revpar"] * 1e-3

    def test_the_two_segments_never_sum_to_the_blended_revpar(self):
        """Guards against anyone "fixing" the panel by adding the rows. The
        blended figure is the capacity-weighted average, which is what the
        renderer footer tells the reader."""
        room = _segment_rates(_TOTALS, 36, 14, "room_rev", "room_nights")
        dorm = _segment_rates(_TOTALS, 48, 14, "dorm_rev", "dorm_nights")
        blended = (777_000_000 + 268_000_000) / (84 * 14)
        assert room["revpar"] + dorm["revpar"] != round(blended, 2)
        weighted = (36 / 84) * room["revpar"] + (48 / 84) * dorm["revpar"]
        assert abs(weighted - blended) < 1.0

    def test_no_units_sold_yields_none_rather_than_zero(self):
        """A zero ADR would read as "we sold rooms at no charge"; None renders
        as an em dash."""
        empty = {"room_rev": 0.0, "room_nights": 0}
        r = _segment_rates(empty, 36, 14, "room_rev", "room_nights")
        assert r["adr"] is None
        assert r["revpar"] == 0.0

    def test_zero_capacity_yields_none_rather_than_dividing_by_zero(self):
        r = _segment_rates(_TOTALS, 0, 14, "room_rev", "room_nights")
        assert r["revpar"] is None and r["occ_pct"] is None


class TestRoomTypeBlockGuards:
    """`db` is None on purpose — a branch without both segments must return
    before it ever queries."""

    def test_a_rooms_only_property_reports_no_split(self):
        out = room_type_block(None, _branch(71, 0, total_rooms=71), None)
        assert out["has_split"] is False
        assert out["segments"] == []

    def test_a_branch_with_unset_capacity_reports_no_split(self):
        """`total_room_count` / `total_dorm_count` are nullable — a branch
        nobody has filled in yet must not render a panel of zeros."""
        assert room_type_block(None, _branch(None, None), None)["has_split"] is False
        assert room_type_block(None, _branch(36, None), None)["has_split"] is False
