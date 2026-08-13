"""Bi-Weekly report rendering — brand palette, flags, and template integrity.

The report HTML is one large tree of f-strings. Nothing else in the suite
executes it, so a renderer that referenced an out-of-scope name or a key the
payload does not carry would only fail in production, on a page that takes a
full report build to reach. These tests render a synthetic payload end to end
so that class of mistake surfaces here instead.
"""
import re
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
            "months": [{
                "label": "July 2026",
                "achievement": {"actual_revenue": 2_317_000_000,
                                "target_revenue": 2_190_000_000},
                "pct": 106.0, "closed": True, "is_override": False,
            }],
            # Legacy top-level mirror — kept so a payload built before the
            # multi-month change still has the fields the old renderer read.
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
                 "vs_prior_pct": 17.0, "yoy_bookings": 101,
                 "vs_yoy_pct": 29.7, "is_direct": True},
                # No year-ago row — a channel that did not exist 12 months ago.
                {"source": "Ctrip", "category": "OTA", "bookings": 94,
                 "share_pct": 21.3, "prior_bookings": 90,
                 "vs_prior_pct": 4.4, "yoy_bookings": 0,
                 "vs_yoy_pct": None, "is_direct": False},
            ],
            "total_bookings": 442,
            "yoy_total_bookings": 380,
            "total_vs_yoy_pct": 16.3,
            "yoy_per_day": False,
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
                 "revenue": 514_000_000, "bookings": 102, "vs_prior_pct": 219.0,
                 "bookings_vs_prior_pct": 18.0, "vs_yoy_pct": 88.0,
                 "bookings_vs_yoy_pct": 41.0},
                {"country": "Taiwan", "country_code": "Taiwan",
                 "revenue": 164_000_000, "bookings": 40, "vs_prior_pct": 344.0,
                 "bookings_vs_prior_pct": -6.0, "vs_yoy_pct": -22.5,
                 "bookings_vs_yoy_pct": -14.0},
                {"country": "Atlantis", "country_code": None,
                 "revenue": 12_000_000, "bookings": 4, "vs_prior_pct": None,
                 "bookings_vs_prior_pct": None, "vs_yoy_pct": None,
                 "bookings_vs_yoy_pct": None},
            ],
            "total_revenue": 1_210_000_000,
            "yoy_total_revenue": 990_000_000,
            "yoy_per_day": False,
            "unknown_revenue": 342_000_000,
            "unknown_bookings": 121,
            "unknown_share_pct": 28.0,
        },
        "paid_ads": {
            "by_channel": [
                {"channel": "Google Ads", "cost": 10_600_000,
                 "revenue": 220_800_000, "roas": 20.8, "bookings": 43,
                 "wow_cost_pct": 8.0, "wow_revenue_pct": 15.0,
                 "wow_bookings_pct": 6.0, "wow_roas_pct": 6.5,
                 "yoy_cost_pct": 31.0, "yoy_revenue_pct": 44.0,
                 "yoy_bookings_pct": 19.0, "yoy_roas_pct": 9.9},
                # No spend a year ago — every yoy delta is None, and the
                # renderer must draw no second arrow rather than a 0%.
                {"channel": "Meta Ads", "cost": 7_800_000,
                 "revenue": 11_600_000, "roas": 1.48, "bookings": 5,
                 "wow_cost_pct": -3.0, "wow_revenue_pct": -20.0,
                 "wow_bookings_pct": -10.0, "wow_roas_pct": -18.0,
                 "yoy_cost_pct": None, "yoy_revenue_pct": None,
                 "yoy_bookings_pct": None, "yoy_roas_pct": None},
            ],
            "last_week": {"cost": 18_400_000, "revenue": 232_400_000},
            "wow_roas_pct": 9.4,
            "yoy_total": {"cost": 12_900_000, "revenue": 161_000_000,
                          "roas": 12.48},
            "yoy_roas_pct": 1.28,
            "yoy_per_day": False,
        },
        "kol": {"posts_this_week": 9, "organic_bookings": 30,
                "organic_nights": 61, "organic_revenue_native": 102_500_000,
                "roi": 8.4, "period_cost_native": 12_200_000, "period_roas": 8.4,
                "cost_vs_prior_pct": 5.0, "revenue_vs_prior_pct": 12.0,
                "bookings_vs_prior_pct": 8.0, "posts_vs_prior_pct": -10.0,
                "roas_vs_prior_pct": 7.0,
                "cost_vs_yoy_pct": 62.0, "revenue_vs_yoy_pct": 33.0,
                "bookings_vs_yoy_pct": 20.0, "posts_vs_yoy_pct": 12.5,
                "roas_vs_yoy_pct": -14.0, "yoy_per_day": False},
        "kol_reach": {"available": True, "posts": 11, "reach": 4916,
                      "engagements": 1348, "engagement_rate_pct": 3.43,
                      "engagement_rate_posts": 3, "reason": "ok",
                      "reach_vs_prior_pct": 25.0, "engagements_vs_prior_pct": -8.0,
                      "reach_vs_yoy_pct": 51.0, "engagements_vs_yoy_pct": 17.0,
                      "yoy_per_day": False},
        "crm": {"crm_revenue_this": {"bookings": 40, "nights": 88, "revenue": 306_000_000},
                "crm_revenue_prev": {"bookings": 33, "nights": 70, "revenue": 250_800_000},
                "wow_revenue_pct": 22.0, "period_cost_native": 5_000_000,
                "period_roas": 61.2, "cost_vs_prior_pct": 0.0,
                "roas_vs_prior_pct": 0.0,
                "revenue_vs_prior_pct": 22.0, "bookings_vs_prior_pct": 21.2,
                "by_rate_plan": [
                    {"rate_plan_name": "Extension Promotion (>2 night", "label": "Extension Promotion (>2 night",
                     "bookings": 17, "nights": 41, "revenue": 40_905, "adr": 998,
                     "prior_revenue": 30_000, "prior_bookings": 12,
                     "revenue_vs_prior_pct": 36.35, "bookings_vs_prior_pct": 41.67,
                     "yoy_revenue": 22_000, "yoy_bookings": 9,
                     "revenue_vs_yoy_pct": 85.93, "bookings_vs_yoy_pct": 88.89,
                     "yoy_per_day": False},
                    # A campaign that ran in neither comparison window.
                    {"rate_plan_name": "Extension Promotion (1 night", "label": "Extension Promotion (1 night",
                     "bookings": 37, "nights": 39, "revenue": 32_895, "adr": 843,
                     "prior_revenue": None, "prior_bookings": None,
                     "revenue_vs_prior_pct": None, "bookings_vs_prior_pct": None,
                     "yoy_revenue": None, "yoy_bookings": None,
                     "revenue_vs_yoy_pct": None, "bookings_vs_yoy_pct": None,
                     "yoy_per_day": False},
                ],
                "rate_plan_totals": {
                    "bookings": 54, "revenue": 73_800,
                    "bookings_vs_prior_pct": 350.0, "revenue_vs_prior_pct": 146.0,
                    "bookings_vs_yoy_pct": 500.0, "revenue_vs_yoy_pct": 235.45,
                    "yoy_per_day": False,
                }},
        "highlights": ["Room rate +30% vs same period."],
        "watchouts": ["Occupancy down 5.7%."],
        "actions": [{"title": "Push Malaysia", "when": "Aug 17–25",
                     "body": "Malaysia school holidays."}],
        "data_notes": [{"level": "warn", "text": "121 bookings lack a source market."}],
    }


class TestBuildHtml:
    def _render(self, payloads):
        return _build_html(payloads, period_for(2026, 29),
                           datetime(2026, 8, 3, tzinfo=timezone.utc))

    def test_occupancy_delta_reads_percent_not_points(self):
        """OCC moves are still computed as percentage points internally, but
        every delta in the report — including OCC's — reads with a '%'
        suffix now. "pts" must never reach the rendered HTML (2026-08-11
        feedback: mixing "%" and "pts" across the report read as broken)."""
        html = self._render([_payload()])
        assert "pts" not in html
        assert "−5.7%" in html   # vs_yoy occ_pts = -5.7
        assert "−1.0%" in html   # vs_prior occ_pts = -1.0

    def test_renders_every_section(self):
        html = self._render([_payload()])
        for heading in [
            "Executive Summary", "Target Achievement",
            "Which channels do guests book through?",
            "Which markets do guests come from?", "Ad campaigns that ran",
            "KOL / Influencer Performance", "CRM Performance",
            "Highlights", "Watch-outs", "Recommended Actions", "Quick Glossary",
        ]:
            assert heading in html, f"missing section: {heading}"

    def test_revpar_is_a_card_in_the_executive_summary_not_its_own_section(self):
        """RevPAR was section 2 of its own until 2026-08-12 feedback folded it
        into the Executive Summary — a manager comparing it against the ADR and
        OCC cards it derives from had to scroll away from them to read it."""
        html = self._render([_payload()])
        assert "RevPAR (Revenue per Available Room)" not in html   # old heading
        assert "RevPAR (revenue per available room)" in html       # card label
        assert 'data-metric-key="bw.revpar"' in html
        # Inside section 1, i.e. before the next section starts.
        assert html.index("bw.revpar") < html.index("Target Achievement")

    def test_sections_are_numbered_without_a_gap(self):
        """Dropping the standalone RevPAR section renumbered everything below
        it. A skipped or duplicated number is the visible symptom of a
        half-finished renumber."""
        html = self._render([_payload()])
        # The numbered circle each section header opens with.
        numbers = re.findall(
            r"align-items:center;justify-content:center;\">(\d+|\?)</div>", html)
        assert numbers == ["1", "2", "3", "4", "5", "6", "7", "8", "?"]

    def test_direct_channels_carry_no_good_badge(self):
        """The green GOOD badge beside Direct channels went out per 2026-08-12
        feedback: it labelled the channel, not its performance, so a Direct
        channel down 30% still wore it. The brand-coloured bar still marks
        Direct rows."""
        html = self._render([_payload()])
        assert "GOOD</span>" not in html
        assert "More <b>direct</b> is better" in html

    def test_kol_shows_cost_and_roas_like_ads(self):
        """Cost + ROAS for KOL render as a Channel | Spend | Revenue |
        Efficiency | Bookings row, the same shape the Ads table already uses.
        CRM has no such row (see test_crm_has_no_channel_summary_row) — CRM
        cost isn't tracked per campaign, only as one branch-wide monthly
        figure."""
        html = self._render([_payload()])
        assert "KOL / Influencer</td>" in html
        assert "8.40×</span>" in html   # KOL period_roas

    def test_vs_prior_arrows_appear_on_channel_rows(self):
        html = self._render([_payload()])
        assert "▲ +12.00%" in html   # KOL revenue_vs_prior_pct
        assert "▼ -10.00%" in html   # KOL posts_vs_prior_pct

    def test_efficiency_column_carries_a_vs_prior_arrow_too(self):
        """Every other column in the Ads Channel|Spend|Revenue|Efficiency|
        Bookings table had a vs-prior arrow — Efficiency (ROAS) was the one
        silently missing it."""
        html = self._render([_payload()])
        assert "▲ +6.50%" in html    # Ads Google Ads wow_roas_pct
        assert "▼ -18.00%" in html   # Ads Meta Ads wow_roas_pct

    def test_crm_has_no_channel_summary_row(self):
        """CRM Performance dropped the Channel|Spend|Revenue|Efficiency|
        Bookings row entirely — CRM cost is one branch-wide monthly figure,
        not trackable per campaign, so showing it next to a per-campaign
        table implied a precision that didn't exist."""
        html = self._render([_payload()])
        assert "CRM / Email</td>" not in html
        assert "61.20×" not in html

    def test_every_table_carries_both_comparisons(self):
        """Until 2026-08-12 only the Executive Summary compared against the
        same period last year — every table below it showed the prior period
        alone. Each of those sections now carries both, the year-ago one
        labelled so the second arrow is not mistaken for the first."""
        html = self._render([_payload()])
        for pct, where in [
            ("+29.70%", "channel bookings"),
            ("+88.00%", "market revenue"),
            ("+41.00%", "market bookings"),
            ("+44.00%", "ads revenue"),
            ("+33.00%", "KOL revenue"),
            ("+51.00%", "KOL reach"),
            ("+85.93%", "CRM rate plan revenue"),
            ("+235.45%", "CRM total revenue"),
        ]:
            assert pct in html, f"missing year-ago delta for {where}"
        # Labelled, and often enough to be the table legend's subject.
        assert html.count("vs LY") >= 8
        assert "is vs the same period last year" in html

    def test_the_arrow_legend_is_stated_once_in_the_header(self):
        """It shipped in all five section notes, which put the same three lines
        of boilerplate between a manager and every table on the page (2026-08-12
        feedback: "dài dòng ... nhiễu loạn thông tin"). It belongs next to the
        two comparison windows the header already names, once."""
        html = self._render([_payload()])
        assert html.count("is vs the same period last year") == 1
        assert html.index("is vs the same period last year") < html.index("Executive Summary")

    def test_the_roas_pill_carries_no_verdict_word(self):
        """"2.24× · OK" / "9.82× · Excellent" went out per 2026-08-12 feedback:
        the pill's colour says where the number sits and the arrow beside it
        says which way it is moving, so the word only restated the figure."""
        html = self._render([_payload()])
        for word in ["Excellent", "· OK", "· Good", "Weak"]:
            assert word not in html, f"verdict word back in the ROAS pill: {word}"
        assert "20.80×</span>" in html   # Google Ads roas, number kept

    def test_the_efficiency_column_is_called_roas(self):
        """"Efficiency" made the reader map a word onto a number that the
        glossary and the section note both already call ROAS."""
        html = self._render([_payload()])
        assert ">ROAS</th>" in html
        assert "Efficiency" not in html

    def test_the_google_caveat_only_appears_when_google_is_missing(self):
        """Printed under a table that already has a Google row, it answered a
        question nobody reading that table had."""
        assert "not connected, not zero spend" not in self._render([_payload()])

        p = _payload()
        p["paid_ads"]["by_channel"] = [
            c for c in p["paid_ads"]["by_channel"] if "Google" not in c["channel"]
        ]
        assert "not connected, not zero spend" in self._render([p])

    def test_crm_does_not_restate_its_own_section_note(self):
        """The footer repeated the note almost word for word, then pointed the
        reader back up to a section they had already scrolled past."""
        html = self._render([_payload()])
        assert "above for the Direct-channel breakdown" not in html
        assert html.count("CRM-tagged reservations") == 1

    def test_affiliate_commission_is_mentioned_once_not_twice(self):
        """It was in the KOL footer AND in the data-notes block below it."""
        p = _payload()
        p["data_notes"] = [{"level": "info",
                            "text": "Affiliate commission is not recorded at all."}]
        html = self._render([p])
        assert html.count("ffiliate commission") == 1

    def test_a_zero_year_ago_base_draws_no_arrow_at_all(self):
        """Meta Ads and Ctrip did not exist a year ago, so their year-ago
        deltas are None. An arrow off a zero base reads as performance; the
        absence plus the legend reads as "nothing to compare"."""
        html = self._render([_payload()])
        meta = html[html.index("Meta Ads</td>"):html.index("Meta Ads</td>") + 1400]
        assert "vs LY" not in meta
        assert "▼ -18.00%" in meta   # its prior-period arrow still renders

    def test_year_ago_arrow_is_labelled_per_day_when_windows_differ(self):
        """The 21-day W51–W53 period compares against a 14-day window in a
        52-week prior year, so the builder normalises those deltas per day.
        The label has to say so, or the number reads as a total."""
        p = _payload()
        p["markets"]["yoy_per_day"] = True
        html = self._render([p])
        assert "vs LY/day" in html

    def test_exec_summary_roas_card_has_a_vs_prior_chip(self):
        """The Executive Summary ROAS card showed a per-channel breakdown but
        no vs-prior delta, unlike Revenue/ADR/OCC beside it."""
        html = self._render([_payload()])
        assert "▲ +9.40%" in html
        assert "vs prior</span></span>" in html

    def test_kol_reach_and_engagements_show_vs_prior_arrows(self):
        html = self._render([_payload()])
        assert "▲ +25.00%" in html   # reach_vs_prior_pct
        assert "▼ -8.00%" in html    # engagements_vs_prior_pct

    def test_crm_section_no_longer_duplicates_direct_channel_metrics(self):
        """Direct room-nights / revenue / share used to be repeated here —
        they already have a row in "Which channels do guests book through?".
        This section is CRM revenue only now."""
        html = self._render([_payload()])
        assert "CRM Performance" in html
        assert "Direct room-nights" not in html
        assert "Direct share of revenue" not in html

    def test_crm_shows_a_by_rate_plan_breakdown(self):
        """CRM Performance breaks revenue down by rate plan/campaign — same
        grouping as Marketing Activity → CRM Reservations "By Rate Plan" —
        with a vs-prior arrow per row, and no arrow for a campaign that
        didn't run last period."""
        html = self._render([_payload()])
        assert "Rate plan / campaign" in html
        assert "Extension Promotion (&gt;2 night" in html or "Extension Promotion (>2 night" in html
        assert "▲ +36.35%" in html   # first rate plan's revenue delta
        # the second rate plan has no prior data — no arrow, not a fake one
        assert "32,895" in html

    def test_crm_rate_plan_table_has_a_total_row(self):
        """Sums Bookings/Nights/Revenue across every rate plan, with its own
        vs-prior arrow — a campaign missing a prior row (new this period)
        contributes 0 to the prior total, not a hole in the sum."""
        html = self._render([_payload()])
        assert ">Total</td>" in html
        assert "73,800" in html     # 40,905 + 32,895
        assert "▲ +146.00%" in html  # (73800-30000)/30000

    def test_watchouts_and_actions_share_one_card(self):
        """Recommended Actions no longer gets its own card — it's merged
        into Watch-outs under one combined title, since an action IS a
        watch-out with a "do this about it" attached."""
        html = self._render([_payload()])
        assert "Watch-outs / Recommended Actions" in html
        assert "Recommended Actions (next period)" not in html
        # Only 2 grid cards now (Highlights + the merged one), not 3.
        assert html.count("border-radius:11px;padding:16px 18px;background:") == 2

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

    def test_markets_table_matches_the_other_tables_inline_arrow_style(self):
        """Revenue and Bookings each carry their own inline vs-prior arrow,
        same as Ads/KOL/CRM — no separate trailing "vs prior" column."""
        html = self._render([_payload()])
        assert "vs prior</th>" not in html
        assert "▲ +219.00%" in html   # Philippines revenue
        assert "▲ +18.00%" in html    # Philippines bookings
        assert "▼ -6.00%" in html     # Taiwan bookings

    def test_target_leads_with_the_month_and_marks_the_goal(self):
        html = self._render([_payload()])
        plain = re.sub(r"</?span[^>]*>", "", html)
        assert "106% of the July 2026 target" in plain
        # Beating target scales the gauge, so the goal marker moves inward.
        assert "left:94.3%" in html

    def test_single_month_period_shows_one_gauge_not_two(self):
        html = self._render([_payload()])
        assert html.count("bw.target.") == 1
        assert "This period spans two calendar months" not in html

    def test_target_falls_back_to_legacy_fields_when_months_is_missing(self):
        """A period cached before the multi-month payload shipped carries the
        old single-month fields but no `months` key. It must still render
        instead of showing the 'no target set' empty state."""
        p = _payload()
        del p["target"]["months"]
        html = self._render([p])
        plain = re.sub(r"</?span[^>]*>", "", html)
        assert "106% of the July 2026 target" in plain
        assert "No revenue target set" not in html

    def test_period_spanning_two_months_shows_both_targets(self):
        """A bi-weekly period like Jul 25 – Aug 2 touches two calendar
        months. Each gets its own full-month achievement — July already
        closed at 107%, August already 67% of its FULL month's target (not
        prorated) — rather than folding both into whichever one the period
        happens to end in."""
        p = _payload()
        p["target"] = {
            "period": {"actual_revenue": 32_000_000, "target_revenue": 15_500_000},
            "period_pct": 206.5,
            "months": [
                {"label": "July 2026",
                 "achievement": {"actual_revenue": 30_000_000, "target_revenue": 28_000_000},
                 "pct": 107.1, "closed": True, "is_override": False},
                {"label": "August 2026",
                 "achievement": {"actual_revenue": 2_000_000, "target_revenue": 3_000_000},
                 "pct": 66.7, "closed": False, "is_override": False},
            ],
        }
        html = self._render([p])
        plain = re.sub(r"</?span[^>]*>", "", html)

        assert "This period spans two calendar months" in html
        assert "July 2026: 107%" in plain
        assert "August 2026: 67%" in plain
        assert "fully closed" in html                          # July's state
        assert "month in progress" in html                     # August's state
        assert "through 2026" not in html   # no false date cutoff on either
        assert html.count("bw.target.") == 2
        assert "grid-template-columns:1fr 1fr" in html
        # The period-on-its-own line still appears underneath both gauges.
        assert "This period on its own" in html
        assert "206%" in html

    def test_manual_override_is_labelled_not_dated(self):
        """A manually-entered month's Actual carries no elapsed-day cutoff
        at all — the state line must say so, not claim a date it never
        respected."""
        p = _payload()
        p["target"]["months"][0]["is_override"] = True
        html = self._render([p])
        assert "manually entered" in html
        assert "fully closed" not in html

    def test_kol_reach_is_shown_when_available(self):
        html = self._render([_payload()])
        assert "Views / reach" in html
        assert "4,916" in html
        # The rate covers 3 of 11 posts, and the row has to say so — the
        # others are zero-view platforms excluded from the denominator.
        assert "ER 3.43% · 3 of 11 posts" in html

    def test_engagement_rate_row_is_unqualified_when_it_covers_everything(self):
        p = _payload()
        p["kol_reach"]["engagement_rate_posts"] = 11
        html = self._render([p])
        assert "ER 3.43%" in html
        assert "of 11 posts" not in html

    def test_no_rate_says_so_instead_of_showing_a_dash(self):
        p = _payload()
        p["kol_reach"]["engagement_rate_pct"] = None
        html = self._render([p])
        assert "no post reported reach" in html

    def test_kol_reach_absent_is_not_reported_as_zero(self):
        p = _payload()
        p["kol_reach"] = {"available": False, "posts": 0, "reach": 0,
                          "engagements": 0, "engagement_rate_pct": None}
        html = self._render([p])
        assert "Views / reach" not in html
        assert "unavailable this period" in html

    def test_channel_mix_reports_bookings_not_nights(self):
        html = self._render([_payload()])
        assert "442 bookings, counted once each" in html
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
