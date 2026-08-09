"""Bi-Weekly report rendering — brand palette, flags, and template integrity.

The report HTML is one large tree of f-strings. Nothing else in the suite
executes it, so a renderer that referenced an out-of-scope name or a key the
payload does not carry would only fail in production, on a page that takes a
full report build to reach. These tests render a synthetic payload end to end
so that class of mistake surfaces here instead.
"""
from datetime import datetime, timezone

from app.services.biweekly_render import (
    _brand,
    _build_html,
    _flag,
    _shade,
    _tint,
)
from app.services.biweekly_period import period_for


# ── Brand palette ────────────────────────────────────────────────────────────


class TestBrandPalette:
    def test_each_branch_gets_its_documented_primary(self):
        cases = {
            "MEANDER Saigon": "#028782",
            "MEANDER Taipei": "#8fad63",
            "MEANDER 1948": "#5b8561",
            "MEANDER Osaka": "#798a5d",
            "MEANDER Oani": "#485d42",
        }
        for name, expected in cases.items():
            assert _brand({"branch_name": name})["primary"] == expected

    def test_all_five_primaries_are_distinct(self):
        names = ["MEANDER Saigon", "MEANDER Taipei", "MEANDER 1948",
                 "MEANDER Osaka", "MEANDER Oani"]
        primaries = {_brand({"branch_name": n})["primary"] for n in names}
        assert len(primaries) == 5, "branches must be visually distinguishable"

    def test_unknown_branch_falls_back_rather_than_crashing(self):
        assert _brand({"branch_name": "MEANDER Atlantis"})["primary"] == "#028782"
        assert _brand({})["primary"] == "#028782"

    def test_derived_shades_match_the_documented_saigon_pair(self):
        """The derivation replaces hand-picked deep/pale values, so it has to
        reproduce the one pair the guideline actually documents."""
        assert _shade("#028782", 0.79) == "#026b67"   # documented #016b67
        assert _tint("#028782", 0.10) == "#e6f3f2"    # documented #e6f2f1

    def test_pale_is_lighter_and_deep_is_darker_than_primary(self):
        for name in ["MEANDER Saigon", "MEANDER Osaka", "MEANDER Oani"]:
            br = _brand({"branch_name": name})
            lum = lambda h: sum(int(h[i:i + 2], 16) for i in (1, 3, 5))
            assert lum(br["deep"]) < lum(br["primary"]) < lum(br["pale"])


# ── Country flags ────────────────────────────────────────────────────────────


class TestFlag:
    def test_maps_iso_codes_to_regional_indicators(self):
        assert _flag("VN") == "🇻🇳"
        assert _flag("PH") == "🇵🇭"
        assert _flag("tw") == "🇹🇼"

    def test_maps_country_names_because_that_is_what_the_column_holds(self):
        """`guest_country_code` stores a canonical display name, not a code —
        reading it as ISO rendered a globe on every row in production."""
        assert _flag("Taiwan") == "🇹🇼"
        assert _flag("United States") == "🇺🇸"
        assert _flag("Hong Kong") == "🇭🇰"
        assert _flag("Viet Nam") == "🇻🇳"      # alias
        assert _flag("Philippines") == "🇵🇭"

    def test_unusable_values_fall_back_to_a_globe(self):
        for bad in [None, "", "?", "??", "1A", "  ", "Unknown", "Atlantis"]:
            assert _flag(bad) == "🌐"


# ── Full render ──────────────────────────────────────────────────────────────


def _payload(branch_name="MEANDER Saigon"):
    """A branch payload with every section populated."""
    return {
        "branch_id": "11111111-1111-1111-1111-111111111111",
        "branch_name": branch_name,
        "branch_city": "Ho Chi Minh City",
        "currency": "VND",
        "kpi": {
            "this": {"revenue": 1_210_000_000, "adr": 1_050_000,
                     "occ_pct": 0.859, "revpar": 902_000, "sold": 1152},
            "vs_yoy": {"revenue_pct": 22.2, "adr_pct": 30.2, "occ_pts": -5.7,
                       "sold_pct": -6.0, "revenue_abs": 219_000_000,
                       "per_day": False},
            "vs_prior": {"revenue_pct": 1.7, "adr_pct": 3.0, "occ_pts": -1.0,
                         "sold_pct": -1.2, "revenue_abs": None, "per_day": True},
            "lights": {"revenue": "g", "adr": "g", "occ": "w"},
            "yoy_has_data": True,
        },
        "target": {
            "period": {"actual_revenue": 1_210_000_000,
                       "target_revenue": 1_100_000_000},
            "period_pct": 110.0,
            "month": {"actual_revenue": 2_317_000_000,
                      "target_revenue": 2_190_000_000},
            "month_pct": 106.0,
            "month_label": "July 2026",
            "month_closed": True,
            "month_through": "2026-07-31",
        },
        "channel_bookings": {
            "rows": [
                {"source": "Direct", "category": "Direct", "bookings": 131,
                 "share_pct": 29.6, "prior_bookings": 112,
                 "vs_prior_pct": 17.0, "is_direct": True},
                {"source": "Ctrip", "category": "OTA", "bookings": 94,
                 "share_pct": 21.3, "prior_bookings": 90,
                 "vs_prior_pct": 4.4, "is_direct": False},
            ],
            "total_bookings": 442,
            "direct_bookings": 131,
            "direct_share_pct": 29.6,
        },
        "channel_mix": {
            "categories": [{"source_category": "Direct", "room_nights": 340,
                            "revenue_native": 306_000_000,
                            "revenue_share_pct": 30.0,
                            "wow_nights_pct": 12.0, "wow_revenue_pct": 22.0}],
        },
        "markets": {
            "rows": [
                # Production shape: the "code" column carries a display name.
                {"country": "Philippines", "country_code": "Philippines",
                 "revenue": 514_000_000, "bookings": 102, "vs_prior_pct": 219.0},
                {"country": "Taiwan", "country_code": "Taiwan",
                 "revenue": 164_000_000, "bookings": 40, "vs_prior_pct": 344.0},
                {"country": "Atlantis", "country_code": None,
                 "revenue": 12_000_000, "bookings": 4, "vs_prior_pct": None},
            ],
            "total_revenue": 1_210_000_000,
            "unknown_revenue": 342_000_000,
            "unknown_bookings": 121,
            "unknown_share_pct": 28.0,
        },
        "paid_ads": {
            "by_channel": [
                {"channel": "Google Ads", "cost": 10_600_000,
                 "revenue": 220_800_000, "roas": 20.8, "bookings": 43},
                {"channel": "Meta Ads", "cost": 7_800_000,
                 "revenue": 11_600_000, "roas": 1.48, "bookings": 5},
            ],
            "last_week": {"cost": 18_400_000, "revenue": 232_400_000},
        },
        "kol": {"posts_this_week": 9, "organic_bookings": 30,
                "organic_nights": 61, "organic_revenue_native": 102_500_000,
                "roi": 8.4},
        "kol_reach": {"available": True, "posts": 11, "reach": 4916,
                      "engagements": 1348, "engagement_rate_pct": 3.43},
        "crm": {"revenue_native": 306_000_000, "wow_revenue_pct": 22.0},
        "highlights": ["Room rate +30% vs same period."],
        "watchouts": ["Occupancy down 5.7 pts."],
        "actions": [{"title": "Push Malaysia", "when": "Aug 17–25",
                     "body": "Malaysia school holidays."}],
        "data_notes": [{"level": "warn", "text": "121 bookings lack a source market."}],
    }


class TestBuildHtml:
    def _render(self, payloads):
        return _build_html(payloads, period_for(2026, 29),
                           datetime(2026, 8, 3, tzinfo=timezone.utc))

    def test_renders_every_section(self):
        html = self._render([_payload()])
        for heading in [
            "Executive Summary", "Target Achievement",
            "Which channels do guests book through?",
            "Which markets do guests come from?", "Ad campaigns that ran",
            "KOL / Influencer Performance", "CRM / Direct Booking Performance",
            "Highlights", "Watch-outs", "Recommended Actions", "Quick Glossary",
        ]:
            assert heading in html, f"missing section: {heading}"

    def test_each_branch_block_carries_its_own_colour(self):
        html = self._render([_payload("MEANDER Saigon"),
                             _payload("MEANDER Taipei")])
        assert 'data-branch-color="#028782"' in html
        assert 'data-branch-color="#8fad63"' in html
        # The shared header must not wear either branch's colour.
        assert "#3f3b3a" in html

    def test_market_rows_carry_flags(self):
        html = self._render([_payload()])
        assert "🇵🇭 Philippines" in html
        assert "🇹🇼 Taiwan" in html
        assert "🌐 Atlantis" in html   # unrecognised, but never a broken box

    def test_target_leads_with_the_month_and_marks_the_goal(self):
        html = self._render([_payload()])
        assert "106% of the July 2026 target" in html.replace("</span>", "").replace(
            '<span style="color:#0f9d58;">', "")
        # Beating target scales the gauge, so the goal marker moves inward.
        assert "left:94.3%" in html

    def test_kol_reach_is_shown_when_available(self):
        html = self._render([_payload()])
        assert "Views / reach" in html
        assert "4,916" in html
        assert "ER 3.43%" in html

    def test_kol_reach_absent_is_not_reported_as_zero(self):
        p = _payload()
        p["kol_reach"] = {"available": False, "posts": 0, "reach": 0,
                          "engagements": 0, "engagement_rate_pct": None}
        html = self._render([p])
        assert "Views / reach" not in html
        assert "unavailable for this period" in html

    def test_channel_mix_reports_bookings_not_nights(self):
        html = self._render([_payload()])
        assert "442 bookings this period" in html
        assert "29.6%" in html

    def test_survives_a_completely_empty_branch(self):
        """safe_section defaults mean a failed build hands the renderer {} —
        that must degrade, not raise."""
        bare = {
            "branch_id": "22222222-2222-2222-2222-222222222222",
            "branch_name": "MEANDER Osaka", "branch_city": "", "currency": "JPY",
            "kpi": {}, "target": {}, "channel_mix": {}, "channel_bookings": {},
            "markets": {}, "paid_ads": {}, "kol": {}, "kol_reach": {},
            "crm": {}, "highlights": [], "watchouts": [], "actions": [],
            "data_notes": [],
        }
        html = self._render([bare])
        assert "MEANDER Osaka" in html
        assert "#798a5d" in html

    def test_typography_stays_plain(self):
        """The report deliberately carries no uppercase transforms and no
        weights above 700 — see the typography pass in the git history."""
        html = self._render([_payload()])
        assert "text-transform:uppercase" not in html
        assert "font-weight:800" not in html
        assert "font-weight:900" not in html
