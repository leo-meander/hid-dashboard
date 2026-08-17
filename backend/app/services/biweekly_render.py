"""
Bi-Weekly Branch Manager Report — HTML rendering.

Split out of `routers/biweekly_report.py` so the renderer can be tested
without dragging in FastAPI, the auth stack and the ORM: importing the router
pulls `app.routers.auth`, which needs bcrypt and a JWT library just to check
that a target gauge draws its goal marker in the right place.

The HTML is inline-styled on purpose. It renders into the dashboard today,
but the same string has to survive an email client once the delivery step
lands — email clients drop <style> blocks, so every rule sits on the element.
That constraint is why this reads more verbosely than page CSS would.
"""
from datetime import datetime, timezone
from typing import Optional

from app.services.biweekly_period import (
    Period,
    comparable_as_totals,
    is_complete,
    mom_window,
    yoy_window,
)
from app.services.country_codes import iso_code_for
from app.services.report_common import (
    cell_attrs,
    fmt,
    ict_today,
    num,
    signed_pct,
    signed_pts,
)
from app.services.weekly_report_builder import pct_change


# Brand palette from the report design — teal on cream, traffic lights.
C = {
    "primary": "#028782", "primary_deep": "#016b67", "primary_pale": "#e6f2f1",
    "cream": "#FBF7F4", "charcoal": "#1C1C1E", "ink": "#2b2b2b", "muted": "#8A8270",
    "line": "#e7e2db", "card": "#ffffff",
    "good": "#0f9d58", "good_bg": "#e7f5ec",
    "warn": "#c98a00", "warn_bg": "#fbf1dc",
    "bad": "#d03b3b", "bad_bg": "#fbe7e7",
}
_LIGHT = {"g": C["good"], "w": C["warn"], "b": C["bad"]}
_LIGHT_BG = {"g": C["good_bg"], "w": C["warn_bg"], "b": C["bad_bg"]}

# ── Per-branch brand identity ────────────────────────────────────────────────
#
# Official primaries from the MEANDER Logomark Guideline. Until now every
# branch rendered in Saigon's teal, because the report was designed from a
# Saigon mockup — five identical-looking reports is exactly what made them
# hard to tell apart when read side by side.
#
# `deep` and `pale` are derived, not hand-picked, so adding a branch needs
# one hex. The factors are calibrated against Saigon's documented pair:
# #028782 → deep #016b67, pale #e6f2f1.

_BRAND_PRIMARY = {
    "saigon": "#028782",   # teal
    "taipei": "#8fad63",   # sage green
    "1948": "#5b8561",     # heritage forest green
    "osaka": "#798a5d",    # olive gray-green
    "oani": "#485d42",     # deep moss
}
_BRAND_FALLBACK = "#028782"
# Neutral used for the report-level header — it spans all five branches, so
# wearing any one branch's colour there would be wrong. From the guideline's
# premium-dark cover swatch.
_NEUTRAL_DARK = "#3f3b3a"


def _rgb(hex_color: str) -> tuple:
    return tuple(int(hex_color[i:i + 2], 16) for i in (1, 3, 5))


def _shade(hex_color: str, factor: float) -> str:
    """Darken toward black by `factor`."""
    return "#%02x%02x%02x" % tuple(round(c * factor) for c in _rgb(hex_color))


def _tint(hex_color: str, weight: float) -> str:
    """Mix with white; `weight` is the colour's share of the result."""
    return "#%02x%02x%02x" % tuple(
        round(c * weight + 255 * (1 - weight)) for c in _rgb(hex_color)
    )


def _brand(b: dict) -> dict:
    """Brand palette for one branch payload."""
    name = (b.get("branch_name") or "").lower()
    primary = next(
        (hexv for token, hexv in _BRAND_PRIMARY.items() if token in name),
        _BRAND_FALLBACK,
    )
    return {
        "primary": primary,
        "deep": _shade(primary, 0.79),
        "pale": _tint(primary, 0.10),
    }

def _flag(country: Optional[str]) -> str:
    """Country → regional-indicator flag emoji.

    Takes a country *name* as well as a code, because that is what the data
    actually holds: `map_country_code` normalises everything on ingestion to
    a canonical display name, so `reservations.guest_country_code` stores
    "Taiwan", not "TW". Reading that column as an ISO code renders a globe
    for every row — which is exactly what shipped the first time.

    The emoji itself is computed, not tabulated: the two regional indicator
    symbols sit at U+1F1E6 + the letter's offset from 'A'. Unrecognised
    countries get a globe rather than a broken box.
    """
    code = iso_code_for(country)
    if not code:
        return "🌐"
    return "".join(chr(0x1F1E6 + ord(ch) - ord("A")) for ch in code)


_TH = (f"padding:10px 13px;text-align:left;background:#f4efe8;font-size:11px;"
       f"color:{C['muted']};font-weight:600;")
_TD = (f"padding:10px 13px;color:{C['ink']};font-size:13px;"
       f"border-top:1px solid {C['line']};")

# The label the year-ago arrow wears in tables. The report header explains it
# by name, so it lives here rather than being spelled out at each call site —
# renaming it in one place renames it in the legend too.
_YOY_TAG = "vs LY"
# Explained ONCE, in the report header, next to the two comparison windows it
# refers to. It shipped in all five section notes, which put the same three
# lines of boilerplate between a manager and every table on the page.
_ARROW_LEGEND = (
    f"In every table the ▲▼ beside a number is vs the same dates last month; "
    f"the line below it ({_YOY_TAG}) is vs the same dates last year. A missing "
    "second line means there is nothing to compare against a year ago. Neither "
    "arrow is ever the half-month before this one."
)


# ── Renderers ────────────────────────────────────────────────────────────────


def _delta_chip(value, label: str, kind: str = "pct") -> str:
    """One coloured ▲/▼ delta pill under a KPI value."""
    if value is None:
        return ""
    text = signed_pts(value) if kind == "pts" else signed_pct(value)
    if kind == "pts":
        up, down = value >= 3.0, value < -3.0
    else:
        up, down = value >= 5.0, value < -5.0
    arrow = "▲" if up else "▼" if down else "≈"
    color = C["good"] if up else C["bad"] if down else C["warn"]
    bg = C["good_bg"] if up else C["bad_bg"] if down else C["warn_bg"]
    return (
        f"<span style='font-size:12px;font-weight:600;padding:3px 8px;border-radius:6px;"
        f"color:{color};background:{bg};display:inline-block;margin:0 6px 4px 0;'>"
        f"{arrow} {text} <span style='font-weight:600;color:{C['muted']};font-size:10.5px;'>"
        f"{label}</span></span>"
    )


def _inline_arrow(pct_value, good: float = 5.0, bad: float = -5.0,
                  label: str = "", kind: str = "pct") -> str:
    """Small ▲/▼/≈ shown next to a number in a table cell. `_delta_chip` is
    the pill version used on KPI cards; this is the compact version that fits
    inside a table row. `label` names the comparison when the cell carries
    more than one.

    `kind="pts"` formats an occupancy move, which is a percentage-POINT delta
    — same distinction `_delta_chip` already carries, and the caller is
    expected to pass the ±3 thresholds that go with it.
    """
    if pct_value is None:
        return ""
    text = signed_pts(pct_value) if kind == "pts" else signed_pct(pct_value)
    up, down = pct_value >= good, pct_value < bad
    arrow = "▲" if up else "▼" if down else "≈"
    color = C["good"] if up else C["bad"] if down else C["warn"]
    tag = (f"<span style='color:{C['muted']};font-weight:600;font-size:9.5px;'>"
           f" {label}</span>") if label else ""
    return (f" <span style='color:{color};font-weight:700;font-size:10.5px;"
            f"white-space:nowrap;'>{arrow} {text}{tag}</span>")


def _delta_pair(prior_pct, yoy_pct, per_day: bool = False,
                good: float = 5.0, bad: float = -5.0) -> str:
    """Both of the report's comparisons for one table cell.

    Month-over-month sits inline with the number, unlabelled — that is the
    arrow operators read first, and the header legend names it. The year-ago
    comparison goes on a second line, labelled, because an unlabelled second
    arrow beside the first is indistinguishable from it.

    A year-ago delta of None draws nothing: the year-ago window has a zero
    base (a channel, market or campaign that did not exist twelve months ago),
    and an arrow off a zero base would read as performance rather than as
    "nothing to compare against". The section footers say so in words.

    `per_day` labels the year-ago arrow as a per-day comparison, which is what
    the builder falls back to when the year-ago window is a different length —
    a leap February. The month-back window can differ too (15–31 Mar against
    15–28 Feb); the header says so once for the whole report rather than
    decorating every arrow on the page.
    """
    out = _inline_arrow(prior_pct, good, bad)
    if yoy_pct is not None:
        out += (
            "<div style='margin-top:1px;'>"
            + _inline_arrow(yoy_pct, good, bad,
                            label=f"{_YOY_TAG}/day" if per_day else _YOY_TAG)
            + "</div>"
        )
    return out


def _efficiency_pill(roas: Optional[float]) -> str:
    """The ROAS badge — factored out so Ads and KOL render the exact same pill
    for the exact same number, instead of two near-identical copies drifting
    apart.

    The number carries no verdict word. "2.24× · OK" / "9.82× · Excellent"
    went out per team feedback (2026-08-12): the pill's colour already says
    where the number sits, and the vs-prior arrow beside it says which way it
    is moving — which is the part an operator acts on. The word only restated
    the figure in prose.
    """
    if roas is None:
        return f"<span style='color:{C['muted']};font-size:11px;'>no spend</span>"
    k = "g" if roas >= 4 else "b" if roas < 2 else "w"
    return (f"<span style='font-size:11px;font-weight:600;padding:2px 8px;"
            f"border-radius:20px;color:{_LIGHT[k]};background:{_LIGHT_BG[k]};'>"
            f"{roas:.2f}×</span>")


def _channel_row(bid, metric_key: str, name: str, cost, revenue, roas, bookings,
                 currency: str, cost_pct=None, revenue_pct=None, bookings_pct=None,
                 roas_pct=None, cost_yoy_pct=None, revenue_yoy_pct=None,
                 bookings_yoy_pct=None, roas_yoy_pct=None,
                 yoy_per_day: bool = False) -> str:
    """One row of the Channel | Spend | Revenue | Efficiency | Bookings table
    shared by Ads and KOL, so cost and ROAS read identically wherever a
    manager finds them in this report. Every column carries both of the
    report's comparisons when the data for it exists — the prior period
    inline, the same period last year underneath.
    """
    attrs = cell_attrs(bid, metric_key, f"{name} — cost & ROAS")

    def pair(prior, yoy):
        return _delta_pair(prior, yoy, yoy_per_day)

    return (
        f"<tr{attrs}>"
        f"<td style='{_TD}font-weight:600;color:{C['charcoal']};'>{name}</td>"
        f"<td style='{_TD}text-align:right;'>{fmt(cost, currency)}"
        f"{pair(cost_pct, cost_yoy_pct)}</td>"
        f"<td style='{_TD}text-align:right;'>{fmt(revenue, currency)}"
        f"{pair(revenue_pct, revenue_yoy_pct)}</td>"
        f"<td style='{_TD}text-align:right;'>{_efficiency_pill(roas)}"
        f"{pair(roas_pct, roas_yoy_pct)}</td>"
        f"<td style='{_TD}text-align:right;'>{num(bookings)}"
        f"{pair(bookings_pct, bookings_yoy_pct)}</td></tr>"
    )


def _channel_table(rows_html: str) -> str:
    return f"""
    <table style="width:100%;border-collapse:collapse;background:{C['card']};
           border:1px solid {C['line']};border-radius:11px;overflow:hidden;font-size:13px;">
      <thead><tr><th style="{_TH}">Channel</th><th style="{_TH}text-align:right;">Spend</th>
      <th style="{_TH}text-align:right;">Revenue</th>
      <th style="{_TH}text-align:right;">ROAS</th>
      <th style="{_TH}text-align:right;">Bookings</th></tr></thead>
      <tbody>{rows_html}</tbody>
    </table>"""


def _kpi_card(branch_id, metric_key: str, label: str, value: str,
              light: str, chips: str, why: str, span2: bool = False) -> str:
    """`span2` makes the card fill both columns of the Executive Summary grid —
    used by RevPAR, which reads as the summary of the four cards above it
    rather than a fifth peer, and would otherwise leave a hole in the grid.
    """
    span = "grid-column:1/-1;" if span2 else ""
    return f"""
    <div{cell_attrs(branch_id, metric_key, label)} style="{span}background:{C['card']};
         border:1px solid {C['line']};border-left:5px solid {_LIGHT.get(light, C['warn'])};
         border-radius:11px;padding:15px 17px;">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;">
        <span style="font-size:12.5px;color:{C['muted']};font-weight:600;
              ">{label}</span>
        <span style="width:12px;height:12px;border-radius:50%;flex:0 0 auto;margin-top:3px;
              background:{_LIGHT.get(light, C['warn'])};display:inline-block;"></span>
      </div>
      <div style="font-size:28px;font-weight:700;color:{C['charcoal']};margin:5px 0 2px;">{value}</div>
      <div style="margin:7px 0 4px;">{chips}</div>
      <div style="font-size:12.8px;color:{C['ink']};margin-top:8px;padding-top:8px;
           border-top:1px dashed {C['line']};">{why}</div>
    </div>"""


def _section(n, title: str, note: str, body: str, accent: str = _BRAND_FALLBACK) -> str:
    note_html = (
        f"<div style='font-size:13px;color:{C['muted']};margin:-6px 0 14px 38px;'>{note}</div>"
        if note else ""
    )
    return f"""
    <div style="margin-top:30px;">
      <div style="display:flex;align-items:center;gap:11px;margin-bottom:14px;">
        <div style="flex:0 0 auto;width:27px;height:27px;border-radius:50%;
             background:{accent};color:#fff;font-weight:600;font-size:14px;
             display:flex;align-items:center;justify-content:center;">{n}</div>
        <div style="font-weight:600;font-size:19px;color:{C['charcoal']};">{title}</div>
      </div>
      {note_html}
      {body}
    </div>"""


def _render_room_split(b: dict) -> str:
    """Private room vs dorm bed, for the three rate metrics.

    Sits inside the Executive Summary rather than in a section of its own,
    for the same reason RevPAR was folded in on 2026-08-12: it decomposes the
    ADR / OCC / RevPAR cards directly above it, and a manager reading a
    segment against the blended number should not have to scroll between them.

    Renders nothing for rooms-only properties (Osaka) — there the blended
    cards already are the private-room numbers.
    """
    rt = b.get("room_types") or {}
    segments = rt.get("segments") or []
    if not rt.get("has_split") or not segments:
        return ""

    br = _brand(b)
    bid = b["branch_id"]
    currency = b.get("currency") or ""
    show_yoy = bool(rt.get("yoy_has_data"))
    # "48 beds · 582 sold" read as 582 beds sold out of 48. The unit here is
    # bed-NIGHTS over the period, so the period length has to sit in the same
    # line as the inventory that multiplies it.
    days = rt.get("days")

    th = (f"font-size:11px;color:{C['muted']};font-weight:600;"
          f"padding:0 0 7px;border-bottom:1px solid {C['line']};")
    td = f"font-size:13.5px;color:{C['charcoal']};padding:9px 0;vertical-align:top;"

    rows = ""
    for s in segments:
        occ = s.get("occ_pct")
        inventory = f"{num(s['capacity'])} {s['unit']}"
        if days:
            inventory += f" &times; {num(days)} nights"
        adr_arrow = (_inline_arrow(s.get("adr_vs_yoy_pct"), label=_YOY_TAG)
                     if show_yoy else "")
        occ_arrow = (_inline_arrow(s.get("occ_vs_yoy_pts"), 3.0, -3.0,
                                   _YOY_TAG, "pts") if show_yoy else "")
        revpar_arrow = (_inline_arrow(s.get("revpar_vs_yoy_pct"), label=_YOY_TAG)
                        if show_yoy else "")
        rows += f"""
        <tr{cell_attrs(bid, f"bw.split.{s['key']}",
                       f"{s['label']} — ADR, occupancy, RevPAR")}>
          <td style="{td}text-align:left;">
            <b style="color:{br['deep']};">{s['label']}</b>
            <div style="font-size:11px;color:{C['muted']};margin-top:2px;">
              {inventory} · {num(s['nights'])} sold</div>
          </td>
          <td style="{td}text-align:right;">{fmt(s.get('adr'), currency)}{adr_arrow}</td>
          <td style="{td}text-align:right;">
            {f"{occ * 100:.1f}%" if occ is not None else "—"}{occ_arrow}</td>
          <td style="{td}text-align:right;font-weight:600;">
            {fmt(s.get('revpar'), currency)}{revpar_arrow}</td>
        </tr>"""

    return f"""
    <div style="margin-top:14px;background:{C['card']};border:1px solid {C['line']};
         border-radius:11px;padding:14px 17px;">
      <div style="font-size:12.5px;color:{C['muted']};font-weight:600;margin-bottom:6px;">
        Split by room type</div>
      <table style="width:100%;border-collapse:collapse;">
        <tr>
          <th style="{th}text-align:left;">Room type</th>
          <th style="{th}text-align:right;">Avg rate (ADR)</th>
          <th style="{th}text-align:right;">Occupancy</th>
          <th style="{th}text-align:right;">RevPAR</th>
        </tr>{rows}
      </table>
      <div style="font-size:11.5px;color:{C['muted']};margin-top:9px;padding-top:9px;
           border-top:1px dashed {C['line']};">
        Each row divides by its own inventory — private RevPAR is per
        <b>room</b>, dorm RevPAR is per <b>bed</b>. Compare a row against the
        same row a year ago, never against the other row. The RevPAR card above
        is a capacity-weighted average of these two, not their sum.</div>
    </div>"""


def _render_exec_summary(b: dict, p: Period) -> str:
    br = _brand(b)
    kpi = b.get("kpi") or {}
    cur = kpi.get("this") or {}
    vy, vp = kpi.get("vs_yoy") or {}, kpi.get("vs_prior") or {}
    lights = kpi.get("lights") or {}
    currency = b.get("currency") or ""
    bid = b["branch_id"]

    yoy_lbl = "vs last year"
    prior_lbl = "vs last month /day" if vp.get("per_day") else "vs last month"

    # When the prior year has no data at all, a "−100%" chip would be a lie
    # about performance rather than a statement about coverage.
    has_yoy = kpi.get("yoy_has_data")

    def chips(yoy_key, prior_key, kind="pct"):
        out = ""
        if has_yoy:
            out += _delta_chip(vy.get(yoy_key), yoy_lbl, kind)
        out += _delta_chip(vp.get(prior_key), prior_lbl, kind)
        return out or f"<span style='font-size:12px;color:{C['muted']};'>no comparison data</span>"

    rev_abs = vy.get("revenue_abs")
    rev_why = (
        f"About <b style='color:{br['deep']}'>{fmt(abs(rev_abs), currency)} "
        f"{'higher' if rev_abs >= 0 else 'lower'}</b> than the same period last year."
        if (has_yoy and rev_abs is not None)
        else "No comparable data for the same period last year."
    )

    adr_why = (
        f"<b style='color:{br['deep']}'>The main driver</b> of revenue change — "
        "the average price each sold room achieved."
    )
    occ_why = (
        f"Share of rooms filled across the {p.days} days. Read together with ADR: "
        "a higher rate usually costs some occupancy."
    )

    roas = None
    ads = b.get("paid_ads") or {}
    tot = ads.get("last_week") or {}
    if tot.get("cost"):
        roas = round((tot.get("revenue") or 0) / tot["cost"], 2)
    roas_light = "g" if (roas or 0) >= 4 else "b" if (roas is not None and roas < 2) else "w"
    parts = [
        f"{c['channel']} {c['roas']:.1f}×"
        for c in (ads.get("by_channel") or []) if c.get("roas") is not None
    ]
    # `wow_roas_pct` is misleadingly named (weekly-report leftover) but is
    # already the aggregate ROAS vs THIS report's comparison window — the
    # bi-weekly builder passes `compare=mom_window(p)` into
    # `paid_ads_section`, so every `wow_*` field it emits is month-over-month
    # here even though the name still says week.
    roas_chips = ""
    if ads.get("yoy_roas_pct") is not None:
        roas_chips += _delta_chip(ads["yoy_roas_pct"], yoy_lbl)
    roas_chips += _delta_chip(ads.get("wow_roas_pct"), "vs last month")
    roas_chips += (
        f"<span style='font-size:12px;font-weight:600;padding:3px 8px;border-radius:6px;"
        f"color:{_LIGHT[roas_light]};background:{_LIGHT_BG[roas_light]};display:inline-block;'>"
        f"{' · '.join(parts)}</span>" if parts else
        f"<span style='font-size:12px;color:{C['muted']};'>no ad spend recorded</span>"
    )
    roas_why = (
        f"<b style='color:{br['deep']}'>Every 1 of ad spend returns {roas:.2f}</b> "
        "in attributed revenue." if roas is not None else
        "No ad spend recorded for this branch in the period."
    )

    # RevPAR used to be a section of its own, immediately below this one.
    # Folded in here per team feedback (2026-08-12): it is a headline metric,
    # and a manager comparing it against the ADR and OCC cards it is derived
    # from had to hold two of them in their head while scrolling to the third.
    # Full-width under the four, since it summarises them rather than ranking
    # alongside them.
    adr_v, occ_v = cur.get("adr"), cur.get("occ_pct")
    revpar_why = (
        f"<b style='color:{br['deep']}'>RevPAR = ADR × Occupancy</b> — "
        f"{fmt(adr_v, currency)} average rate × {occ_v * 100:.1f}% occupancy. "
        "A rate rise that quietly costs occupancy looks fine on the two cards "
        "above and shows up here."
        if (adr_v is not None and occ_v is not None) else
        "Revenue earned per available room — blends rate and occupancy into "
        "one number, so a rate rise that costs occupancy shows up here."
    )

    cards = "".join([
        _kpi_card(bid, "bw.revenue", "Room revenue", fmt(cur.get("revenue"), currency),
                  lights.get("revenue", "w"), chips("revenue_pct", "revenue_pct"), rev_why),
        _kpi_card(bid, "bw.adr", "Avg room rate (ADR)", fmt(cur.get("adr"), currency),
                  lights.get("adr", "w"), chips("adr_pct", "adr_pct"), adr_why),
        _kpi_card(bid, "bw.occ", "Occupancy (OCC)",
                  f"{cur['occ_pct'] * 100:.1f}%" if cur.get("occ_pct") is not None else "—",
                  lights.get("occ", "w"), chips("occ_pts", "occ_pts", kind="pts"), occ_why),
        _kpi_card(bid, "bw.roas", "Ad efficiency (ROAS)",
                  f"{roas:.2f}×" if roas is not None else "—",
                  roas_light, roas_chips, roas_why),
        _kpi_card(bid, "bw.revpar", "RevPAR (revenue per available room)",
                  fmt(cur.get("revpar"), currency), lights.get("revpar", "w"),
                  chips("revpar_pct", "revpar_pct"), revpar_why, span2=True),
    ])

    headline = _render_headline(b, p)
    return _section(1, "Executive Summary", "", f"""
      {headline}
      <div style="display:grid;grid-template-columns:repeat(2,1fr);gap:14px;">{cards}</div>
      {_render_room_split(b)}""",
                    br["primary"])


def _render_headline(b: dict, p: Period) -> str:
    """One-paragraph plain-English story of the period.

    Written from the actual deltas rather than a template with holes: the
    interesting case for a hotel is revenue and volume moving in OPPOSITE
    directions, which is what tells a manager whether growth came from price
    or from filling more beds.
    """
    br = _brand(b)
    kpi = b.get("kpi") or {}
    vy = kpi.get("vs_yoy") or {}
    rev, sold, adr = vy.get("revenue_pct"), vy.get("sold_pct"), vy.get("adr_pct")
    target = b.get("target") or {}

    if not kpi.get("yoy_has_data") or rev is None:
        story = ("No data for the same dates last year, so this report compares "
                 "against last month only.")
    elif rev >= 0 and (sold is not None and sold < 0):
        story = (f"Revenue is <b>up {rev:.0f}%</b> versus the same period last year — "
                 f"<b>not from selling more rooms</b> (room-nights {sold:+.0f}%), but from "
                 f"a <b>{adr:+.0f}% move in the average room rate</b>. Slightly fewer rooms "
                 f"sold, each at a higher price.")
    elif rev >= 0:
        story = (f"Revenue is <b>up {rev:.0f}%</b> versus the same period last year, with "
                 f"room-nights {sold:+.0f}% and the average rate {adr:+.0f}%.")
    else:
        story = (f"Revenue is <b>down {abs(rev):.0f}%</b> versus the same period last year "
                 f"(room-nights {sold:+.0f}%, average rate {adr:+.0f}%).")

    if target.get("period_pct") is not None:
        verdict = "beat" if target["period_pct"] >= 100 else "landed under"
        story += (f" The period <b>{verdict} its prorated target "
                  f"({target['period_pct']:.0f}%)</b>.")

    return f"""
    <div style="background:{br['pale']};border-radius:11px;padding:15px 18px;
         margin-bottom:16px;font-size:15px;">
      <span style="font-weight:600;font-size:11px;
            color:{br['deep']};display:block;margin-bottom:4px;">
        The story of these {p.days} days</span>
      {story}
    </div>"""


def _target_gauge(bid, m: dict, currency: str, br: dict, idx: int, size: str) -> str:
    """One gauge card for a single calendar month's achievement.

    `size` is "lg" for the common single-month case or "sm" for a compact
    card used when several sit side by side.

    A month with `status == "upcoming"` is drawn differently on purpose. Its
    percentage is pickup — rooms already sold for nights that have not
    happened — so the red/amber/green scale would call a perfectly normal
    30%-booked October a failure, and a manager who saw that twice would stop
    reading the block. Upcoming months get neutral colour, "still to sell"
    instead of "short by", and wording that says what the number is.
    """
    m_pct = m.get("pct")
    ach = m.get("achievement") or {}
    m_actual, m_goal = ach.get("actual_revenue"), ach.get("target_revenue")
    label = m.get("label") or "this month"
    upcoming = m.get("status") == "upcoming"

    if m_pct is None:
        return (f"<div style='background:{C['card']};border:1px solid {C['line']};"
                f"border-radius:11px;padding:16px 17px;font-size:12.5px;color:{C['muted']};'>"
                f"No target set for {label} — add one on the KPI Targets page.</div>")

    # Scales to whichever is larger, achievement or 100%, so beating target
    # fills the bar and pushes the goal marker inward instead of clipping the
    # overshoot invisibly at the right edge.
    scale = max(100.0, m_pct)
    fill_pct = max(0.0, min(100.0, m_pct / scale * 100))
    goal_left = 100.0 / scale * 100

    diff = (m_actual or 0) - (m_goal or 0)
    if upcoming:
        pill_color = "w"
        pill_text = (f"✓ Already past target by {fmt(diff, currency)}" if diff >= 0
                     else f"Still to sell {fmt(abs(diff), currency)}")
    else:
        pill_color = "g" if m_pct >= 100 else "w" if m_pct >= 80 else "b"
        pill_text = (f"{'✓ Beat by' if diff >= 0 else '▼ Short by'} "
                     f"{fmt(abs(diff), currency)}")
    pill = (f"<span style='font-size:11px;font-weight:600;padding:2px 8px;border-radius:20px;"
            f"color:{_LIGHT[pill_color]};background:{_LIGHT_BG[pill_color]};'>"
            f"{pill_text}</span>")
    # Not "through {date}" — actual is the whole month's on-the-books revenue,
    # not capped at today, so a date here would imply a cutoff that isn't real.
    if m.get("is_override"):
        state = " — manually entered"
    elif upcoming:
        state = " — booked so far; the month has not started"
    elif m.get("closed"):
        state = " — fully closed"
    else:
        state = " — month in progress, incl. nights already on the books"

    if upcoming:
        heading = (f"{label}: <span style='color:{_LIGHT[pill_color]};'>"
                   f"{m_pct:.0f}%</span> booked")
    elif size == "lg":
        heading = (f"Hit <span style='color:{_LIGHT[pill_color]};'>{m_pct:.0f}%</span> "
                   f"of the {label} target")
    else:
        heading = f"{label}: <span style='color:{_LIGHT[pill_color]};'>{m_pct:.0f}%</span>"
    head_size = "22px" if size == "lg" else "15.5px"
    bar_h = "16px" if size == "lg" else "13px"
    pad = "18px" if size == "lg" else "14px 15px"

    return f"""
    <div{cell_attrs(bid, f"bw.target.{idx}", label)}
         style="background:{C['card']};border:1px solid {C['line']};border-radius:11px;
         padding:{pad};">
      <div style="display:flex;justify-content:space-between;align-items:baseline;
           margin-bottom:11px;flex-wrap:wrap;gap:6px;">
        <div style="font-size:{head_size};font-weight:700;color:{C['charcoal']};">
          {heading}</div>
        <div>{pill}</div>
      </div>
      <div style="height:{bar_h};background:#eee7df;border-radius:9px;position:relative;">
        <span style="position:absolute;left:0;top:0;bottom:0;width:{fill_pct:.1f}%;
              background:linear-gradient(90deg,{br['primary']},{br['deep']});
              border-radius:9px;display:block;"></span>
        <span style="position:absolute;top:-4px;bottom:-4px;left:{goal_left:.1f}%;
              width:2px;background:{C['charcoal']};display:block;" title="Target"></span>
      </div>
      <div style="display:flex;justify-content:space-between;font-size:12px;
           color:{C['muted']};margin-top:8px;">
        <span>Actual: <b style="color:{C['ink']}">{fmt(m_actual, currency)}</b>{state}</span>
        <span>Target: <b style="color:{C['ink']}">{fmt(m_goal, currency)}</b> ▎</span>
      </div>
    </div>"""


def _render_target(b: dict) -> str:
    br = _brand(b)
    t = b.get("target") or {}
    if not t:
        return ""
    currency = b.get("currency") or ""
    pct = t.get("period_pct")
    bid = b["branch_id"]
    actual = (t.get("period") or {}).get("actual_revenue")
    goal = (t.get("period") or {}).get("target_revenue")

    months = t.get("months")
    if not months:
        # A period cached before this shipped carries the old single-month
        # fields but no `months` list. Synthesise one entry from them rather
        # than going blank — the cache is never rebuilt on its own, so an
        # already-computed period would otherwise stay broken until someone
        # happens to hit Rebuild.
        if t.get("month_pct") is not None or t.get("month"):
            months = [{
                "label": t.get("month_label") or "this month",
                "achievement": t.get("month") or {},
                "pct": t.get("month_pct"),
                "closed": t.get("month_closed"),
                "through": t.get("month_through"),
            }]
        else:
            months = []

    if pct is None and not any(m.get("pct") is not None for m in months):
        body = (f"<div style='background:{C['card']};border:1px solid {C['line']};"
                f"border-radius:11px;padding:18px;font-size:13px;color:{C['muted']};'>"
                f"No revenue target set for this period — add one on the KPI Targets page.</div>")
        return _section(2, "Target Achievement", "", body, br["primary"])

    ahead = [m for m in months if m.get("status") == "upcoming"]
    if len(months) >= 2:
        # The reporting month leads at full width; the months ahead of it sit
        # underneath in a row. They are forecast, not results, so giving them
        # equal billing with the month being reported on would misread the
        # page at a glance.
        gauges_html = _target_gauge(bid, months[0], currency, br, 0, size="lg")
        rest = "".join(
            _target_gauge(bid, m, currency, br, i, size="sm")
            for i, m in enumerate(months[1:], start=1)
        )
        cols = min(len(months) - 1, 2)
        gauges_html += (
            f"<div style='display:grid;grid-template-columns:{'1fr ' * cols};"
            f"gap:12px;margin-top:12px;'>{rest}</div>"
        )
        # Singular by default — the block ships showing one month ahead, and
        # "the ones below" over a single card reads as a rendering bug.
        subject = ("The ones below it are the months ahead: their percentage is"
                   if len(ahead) > 1 else
                   "The one below it is the month ahead: its percentage is")
        note = (
            f"The first gauge is the month this report covers. {subject} "
            "what is <b>already booked</b> "
            "against that month's target, so the gap is what there is still time "
            "to sell. Same Target and Actual as the KPI Targets page; the vertical "
            "mark is the target."
            if ahead else
            "Each calendar month gets its own gauge — same Target and Actual as the "
            "KPI Targets page. The vertical mark is the target."
        )
    elif months:
        gauges_html = _target_gauge(bid, months[0], currency, br, 0, size="lg")
        note = ("Same Target and Actual as the KPI Targets page for this month — "
                "Actual includes nights already on the books, not just ones that "
                "have happened. The vertical mark is the target. No targets are "
                "set for the months ahead yet, so there is nothing to plan "
                "against on the KPI Targets page.")
    else:
        gauges_html = ""
        note = ""

    period_line = ""
    if pct is not None:
        period_line = (
            f"<div style='background:{C['card']};border:1px solid {C['line']};"
            f"border-radius:11px;padding:12px 16px;margin-top:{'12px' if gauges_html else '0'};"
            f"font-size:13px;color:{C['muted']};'>"
            f"This period on its own — <b style='color:{C['ink']}'>{pct:.0f}%</b> "
            f"of its prorated target ({fmt(actual, currency)} of {fmt(goal, currency)}).</div>"
        )

    body = gauges_html + period_line
    return _section(2, "Target Achievement", note, body, br["primary"])


def _render_channel_mix(b: dict) -> str:
    br = _brand(b)
    cb = b.get("channel_bookings") or {}
    rows_in = cb.get("rows") or []
    if not rows_in:
        return _section(3, "Which channels do guests book through?", "",
                        f"<div style='color:{C['muted']};font-size:13px;'>No booking data "
                        f"for this period.</div>", br["primary"])
    bid = b["branch_id"]
    top = max((r.get("bookings") or 0) for r in rows_in) or 1
    total = cb.get("total_bookings") or 0
    yoy_per_day = bool(cb.get("yoy_per_day"))

    rows = []
    for s in rows_in:
        name = s["source"]
        n = s.get("bookings") or 0
        share = s.get("share_pct")
        # Direct rows keep the brand-colour bar, which is what actually reads
        # as "this is the good one" at a glance. The "GOOD" badge that used to
        # sit beside the name went out per team feedback (2026-08-12) — it
        # labelled the channel rather than its performance, so a Direct
        # channel down 30% still wore a green GOOD.
        bar_color = br["deep"] if s.get("is_direct") else "#9bbfbc"
        rows.append(f"""
        <div{cell_attrs(bid, f"bw.channel.{name}", f"Channel — {name}")}
             style="display:flex;align-items:center;gap:12px;margin:9px 0;">
          <div style="flex:0 0 128px;font-size:13.5px;font-weight:600;">{name}</div>
          <div style="flex:1;height:22px;background:#f1ece5;border-radius:6px;overflow:hidden;">
            <span style="display:block;height:100%;width:{n / top * 100:.1f}%;
                  background:{bar_color};border-radius:6px;"></span>
          </div>
          <div style="flex:0 0 165px;text-align:right;font-size:12.5px;color:{C['muted']};">
            <b style="color:{C['charcoal']};font-size:13.5px;">
            {share:.1f}%</b> · {num(n)}
            {_delta_pair(s.get("vs_prior_pct"), s.get("vs_yoy_pct"), yoy_per_day)}</div>
        </div>""" if share is not None else "")

    # Revenue share still comes from the occupancy-based channel_mix — the two
    # answer different questions and a channel can be big in one and small in
    # the other (OTAs skew to short cheap stays).
    cats = {(c.get("source_category") or ""): c for c in
            (b.get("channel_mix") or {}).get("categories") or []}
    direct = cats.get("Direct") or {}
    tnote = ""
    if cb.get("direct_share_pct") is not None:
        rev_part = ""
        if direct.get("revenue_share_pct") is not None:
            rev_part = (f" and {direct['revenue_share_pct']:.1f}% of revenue")
        tnote = (f"<div style='font-size:11.5px;color:{C['muted']};margin-top:8px;"
                 f"font-style:italic;'>Direct is {cb['direct_share_pct']:.1f}% of bookings"
                 f"{rev_part} — every direct booking avoids OTA commission (~15–18%).</div>")

    body = (f"<div style='background:{C['card']};border:1px solid {C['line']};"
            f"border-radius:11px;padding:16px 18px;'>{''.join(rows)}</div>{tnote}")
    note = (f"{num(total)} bookings, counted once each if any night fell inside "
            "the period. More <b>direct</b> is better.")
    return _section(3, "Which channels do guests book through?", note, body, br["primary"])


def _render_markets(b: dict) -> str:
    br = _brand(b)
    m = b.get("markets") or {}
    rows = m.get("rows") or []
    bid = b["branch_id"]
    currency = b.get("currency") or ""
    if not rows:
        return _section(4, "Which markets do guests come from?", "",
                        f"<div style='color:{C['muted']};font-size:13px;'>No market data "
                        f"for this period.</div>", br["primary"])

    # Arrows sit next to Revenue and Bookings themselves now, same as every
    # other table in this report (Ads/KOL/CRM) — a separate trailing "vs
    # prior" column was the one place in the report that broke that pattern.
    yoy_per_day = bool(m.get("yoy_per_day"))
    trs = []
    for r in rows:
        country = r["country"]
        attrs = cell_attrs(bid, "bw.market." + country, "Market — " + country)
        trs.append(
            f"<tr{attrs}>"
            f"<td style='{_TD}font-weight:600;color:{C['charcoal']};'>"
            f"{_flag(r.get('country_code') or country)} {country}</td>"
            f"<td style='{_TD}text-align:right;'>{fmt(r['revenue'], currency)}"
            f"{_delta_pair(r.get('vs_prior_pct'), r.get('vs_yoy_pct'), yoy_per_day)}</td>"
            f"<td style='{_TD}text-align:right;'>{num(r['bookings'])}"
            f"{_delta_pair(r.get('bookings_vs_prior_pct'), r.get('bookings_vs_yoy_pct'), yoy_per_day)}"
            f"</td></tr>"
        )

    # The "How to read these numbers" block at the foot of the report says this
    # too, at length. One clause is enough here — the point is only that the
    # shares in THIS table do not add up to 100%.
    unknown_note = ""
    if m.get("unknown_share_pct"):
        unknown_note = (
            f" Excludes {num(m.get('unknown_bookings'))} booking(s) "
            f"({m['unknown_share_pct']:.0f}% of revenue) with no source market."
        )

    body = f"""
    <table style="width:100%;border-collapse:collapse;background:{C['card']};
           border:1px solid {C['line']};border-radius:11px;overflow:hidden;font-size:13px;">
      <thead><tr><th style="{_TH}">Market</th><th style="{_TH}text-align:right;">Revenue</th>
      <th style="{_TH}text-align:right;">Bookings</th></tr></thead>
      <tbody>{''.join(trs)}</tbody>
    </table>
    <div style="font-size:11.5px;color:{C['muted']};margin-top:8px;font-style:italic;">
      Ranked by revenue. The {_YOY_TAG} line compares <b>bookings</b> only.
      {unknown_note}</div>"""
    return _section(4, "Which markets do guests come from?",
                    "Revenue counted per night stayed, so long stays land in the period "
                    "they were actually used.", body, br["primary"])


def _render_ads(b: dict) -> str:
    br = _brand(b)
    ads = b.get("paid_ads") or {}
    channels = ads.get("by_channel") or []
    bid = b["branch_id"]
    currency = b.get("currency") or ""
    if not channels:
        return _section(5, "Ad campaigns that ran", "",
                        f"<div style='color:{C['muted']};font-size:13px;'>No ad spend "
                        f"recorded for this branch in the period.</div>", br["primary"])

    yoy_per_day = bool(ads.get("yoy_per_day"))
    trs = [
        _channel_row(bid, "bw.ads." + c["channel"], c["channel"],
                    c.get("cost"), c.get("revenue"), c.get("roas"), c.get("bookings"),
                    currency, cost_pct=c.get("wow_cost_pct"), revenue_pct=c.get("wow_revenue_pct"),
                    bookings_pct=c.get("wow_bookings_pct"), roas_pct=c.get("wow_roas_pct"),
                    cost_yoy_pct=c.get("yoy_cost_pct"),
                    revenue_yoy_pct=c.get("yoy_revenue_pct"),
                    bookings_yoy_pct=c.get("yoy_bookings_pct"),
                    roas_yoy_pct=c.get("yoy_roas_pct"), yoy_per_day=yoy_per_day)
        for c in channels
    ]

    tot = ads.get("last_week") or {}
    ly_tot = ads.get("yoy_total") or {}
    footer = ""
    if tot.get("cost"):
        footer = f"Total ad spend {fmt(tot['cost'], currency)}"
        if ly_tot.get("cost"):
            footer += f", against {fmt(ly_tot['cost'], currency)} a year ago"
        footer += "."
    # The Google caveat only means something when the Google row is ABSENT —
    # printing it under a table that already has one answers a question nobody
    # reading that table had, and `data_notes_block` raises the same point when
    # it actually applies.
    if not any((c.get("channel") or "").lower().startswith("google") for c in channels):
        footer += (" No Google row: HiD receives Google spend only via the Ads Platform "
                   "aggregator, so this means not connected, not zero spend.")

    body = f"""{_channel_table(''.join(trs))}
    <div style="font-size:11.5px;color:{C['muted']};margin-top:8px;font-style:italic;">{footer}</div>"""
    return _section(5, "Ad campaigns that ran",
                    "ROAS = revenue returned per 1 unit of ad spend. Higher is more "
                    "profitable.", body, br["primary"])


def _render_kol(b: dict) -> str:
    br = _brand(b)
    k = b.get("kol") or {}
    bid = b["branch_id"]
    currency = b.get("currency") or ""
    reach = b.get("kol_reach") or {}

    # Same Channel | Spend | Revenue | Efficiency | Bookings shape as Ads —
    # cost is `kol_records.cost_native` dated to this exact window, the
    # exact-dated counterpart to Ads' daily-grain spend rows.
    kol_per_day = bool(k.get("yoy_per_day"))
    reach_per_day = bool(reach.get("yoy_per_day"))
    channel_row = _channel_row(
        bid, "bw.kol.channel", "KOL / Influencer",
        k.get("period_cost_native"), k.get("organic_revenue_native"),
        k.get("period_roas"), k.get("organic_bookings"), currency,
        cost_pct=k.get("cost_vs_prior_pct"), revenue_pct=k.get("revenue_vs_prior_pct"),
        bookings_pct=k.get("bookings_vs_prior_pct"), roas_pct=k.get("roas_vs_prior_pct"),
        cost_yoy_pct=k.get("cost_vs_yoy_pct"), revenue_yoy_pct=k.get("revenue_vs_yoy_pct"),
        bookings_yoy_pct=k.get("bookings_vs_yoy_pct"), roas_yoy_pct=k.get("roas_vs_yoy_pct"),
        yoy_per_day=kol_per_day,
    )

    rows = [
        ("Posts published this period",
         num(k.get("posts_this_week"))
         + _delta_pair(k.get("posts_vs_prior_pct"), k.get("posts_vs_yoy_pct"), kol_per_day),
         "TikTok / IG / XHS"),
    ]
    if reach.get("available"):
        er = reach.get("engagement_rate_pct")
        er_posts = reach.get("engagement_rate_posts") or 0
        total_posts = reach.get("posts") or 0
        # Xiaohongshu reports engagements with no view count, so the rate is
        # computed only over posts that reported reach. Say so on the row —
        # an unqualified ER here invites comparison with the KOL Engine's
        # headline figure, which is a per-post average, not this ratio.
        if er is None:
            er_note = "no post reported reach"
        elif er_posts < total_posts:
            er_note = f"ER {er:.2f}% · {er_posts} of {total_posts} posts"
        else:
            er_note = f"ER {er:.2f}%"
        rows += [
            ("Views / reach",
             num(reach.get("reach"))
             + _delta_pair(reach.get("reach_vs_prior_pct"),
                           reach.get("reach_vs_yoy_pct"), reach_per_day),
             f"{num(total_posts)} post(s) in period"),
            ("Engagements",
             num(reach.get("engagements"))
             + _delta_pair(reach.get("engagements_vs_prior_pct"),
                           reach.get("engagements_vs_yoy_pct"), reach_per_day),
             er_note),
        ]
    rows += [
        ("KOL-driven bookings",
         num(k.get("organic_bookings"))
         + _delta_pair(k.get("bookings_vs_prior_pct"), k.get("bookings_vs_yoy_pct"),
                       kol_per_day),
         f"{num(k.get('organic_nights'))} nights"),
        ("KOL-driven revenue",
         fmt(k.get("organic_revenue_native"), currency)
         + _delta_pair(k.get("revenue_vs_prior_pct"), k.get("revenue_vs_yoy_pct"),
                       kol_per_day),
         f"ROI {k['roi']:.2f}×" if k.get("roi") else "—"),
    ]
    # Distinguish "the Engine had nothing to give us" from "the posts got no
    # views" — a zero in this row would be read as a performance claim.
    # The affiliate-commission caveat that used to trail this line lives in the
    # "How to read these numbers" block, which is where every other
    # what-HiD-does-not-track note already is.
    if reach.get("available"):
        reach_note = "Reach and engagement come from the KOL Engine, dated to the publish date."
    else:
        reach_note = ("Reach and engagement <b>unavailable this period</b> — the KOL Engine "
                      "returned no scored posts, so they are omitted rather than shown as zero.")

    trs = "".join(
        f"<tr{cell_attrs(bid, f'bw.kol.{i}', label)}>"
        f"<td style='{_TD}font-weight:600;color:{C['charcoal']};'>{label}</td>"
        f"<td style='{_TD}text-align:right;'>{value}</td>"
        f"<td style='{_TD}text-align:right;color:{C['muted']};font-size:11.5px;'>{extra}</td></tr>"
        for i, (label, value, extra) in enumerate(rows)
    )
    body = f"""{_channel_table(channel_row)}
    <div style="height:14px;"></div>
    <table style="width:100%;border-collapse:collapse;background:{C['card']};
           border:1px solid {C['line']};border-radius:11px;overflow:hidden;font-size:13px;">
      <thead><tr><th style="{_TH}">KOL metric</th>
      <th style="{_TH}text-align:right;">This period</th>
      <th style="{_TH}text-align:right;"></th></tr></thead>
      <tbody>{trs}</tbody>
    </table>
    <div style="font-size:11.5px;color:{C['muted']};margin-top:8px;font-style:italic;">
      {reach_note}</div>"""
    return _section(6, "KOL / Influencer Performance",
                    "Cost is KOL spend dated to this exact period, not calendar "
                    "month-to-date. Bookings are the ones tagged to a KOL room type.",
                    body, br["primary"])


def _render_crm(b: dict) -> str:
    br = _brand(b)
    crm = b.get("crm") or {}
    bid = b["branch_id"]
    currency = b.get("currency") or ""
    by_plan = crm.get("by_rate_plan") or []

    if not by_plan:
        return _section(7, "CRM Performance", "",
                        f"<div style='color:{C['muted']};font-size:13px;'>No CRM "
                        f"reservations for this branch in the period.</div>", br["primary"])

    # Per-campaign breakdown — same "By Rate Plan" grouping the Marketing
    # Activity → CRM Reservations tab uses, so this table and that one never
    # tell a different story for the same window. No Channel/Spend/Efficiency
    # row anymore — CRM cost isn't tracked per campaign, only as one
    # branch-wide monthly figure, so a single "Spend" number next to a
    # per-campaign table implied a precision that didn't exist. Nights/ADR
    # dropped too, per team feedback (2026-08-11) — Bookings and Revenue are
    # what this section is actually for.
    yoy_per_day = bool(by_plan[0].get("yoy_per_day"))
    plan_rows = "".join(
        f"<tr{cell_attrs(bid, 'bw.crm.plan.' + (r.get('rate_plan_name') or r['label']), 'CRM — ' + r['label'])}>"
        f"<td style='{_TD}font-weight:600;color:{C['charcoal']};'>{r['label']}</td>"
        f"<td style='{_TD}text-align:right;'>{num(r.get('bookings'))}"
        f"{_delta_pair(r.get('bookings_vs_prior_pct'), r.get('bookings_vs_yoy_pct'), yoy_per_day)}</td>"
        f"<td style='{_TD}text-align:right;'>{fmt(r.get('revenue'), currency)}"
        f"{_delta_pair(r.get('revenue_vs_prior_pct'), r.get('revenue_vs_yoy_pct'), yoy_per_day)}</td></tr>"
        for r in by_plan
    )

    # Totals come from the builder (`_crm_rate_plan_totals`) so the year-ago
    # percentage is normalised for a period whose year-ago window is a
    # different length. A payload cached before that shipped has no totals, so
    # fall back to summing the rows here — the arrows are then un-normalised,
    # which only differs on the one 21-day period a year.
    tot = crm.get("rate_plan_totals") or {
        "bookings": sum(r.get("bookings") or 0 for r in by_plan),
        "revenue": sum(r.get("revenue") or 0 for r in by_plan),
        "bookings_vs_prior_pct": pct_change(
            sum(r.get("bookings") or 0 for r in by_plan),
            sum(r.get("prior_bookings") or 0 for r in by_plan)),
        "revenue_vs_prior_pct": pct_change(
            sum(r.get("revenue") or 0 for r in by_plan),
            sum(r.get("prior_revenue") or 0 for r in by_plan)),
        "bookings_vs_yoy_pct": pct_change(
            sum(r.get("bookings") or 0 for r in by_plan),
            sum(r.get("yoy_bookings") or 0 for r in by_plan)),
        "revenue_vs_yoy_pct": pct_change(
            sum(r.get("revenue") or 0 for r in by_plan),
            sum(r.get("yoy_revenue") or 0 for r in by_plan)),
    }
    total_row = (
        f"<tr{cell_attrs(bid, 'bw.crm.total', 'CRM — Total')}>"
        f"<td style='{_TD}font-weight:700;color:{C['charcoal']};border-top:2px solid {C['line']};'>Total</td>"
        f"<td style='{_TD}text-align:right;font-weight:700;border-top:2px solid {C['line']};'>"
        f"{num(tot.get('bookings'))}"
        f"{_delta_pair(tot.get('bookings_vs_prior_pct'), tot.get('bookings_vs_yoy_pct'), yoy_per_day)}</td>"
        f"<td style='{_TD}text-align:right;font-weight:700;border-top:2px solid {C['line']};'>"
        f"{fmt(tot.get('revenue'), currency)}"
        f"{_delta_pair(tot.get('revenue_vs_prior_pct'), tot.get('revenue_vs_yoy_pct'), yoy_per_day)}</td></tr>"
    )

    body = f"""
    <table style="width:100%;border-collapse:collapse;background:{C['card']};
           border:1px solid {C['line']};border-radius:11px;overflow:hidden;font-size:13px;">
      <thead><tr><th style="{_TH}">Rate plan / campaign</th>
      <th style="{_TH}text-align:right;">Bookings</th>
      <th style="{_TH}text-align:right;">Revenue</th></tr></thead>
      <tbody>{plan_rows}{total_row}</tbody>
    </table>"""
    # No footer: it restated the section note almost word for word, then sent
    # the reader back up to a section they had already scrolled past.
    return _section(7, "CRM Performance",
                    "Revenue from CRM-tagged reservations (Cloudbeds rate plans + "
                    "GoHighLevel), per campaign.",
                    body, br["primary"])


_EDITED_MARK = (
    "<span style='font-size:9.5px;font-weight:600;color:#6b6b6b;background:#fff;"
    "border:1px solid rgba(0,0,0,.12);padding:0 5px;border-radius:4px;"
    "margin-left:6px;white-space:nowrap;' title='Edited by hand — shown as "
    "entered, not recomputed'>edited</span>"
)


def _flag_panel(title: str, items: list, bullet: str, color: str, bg: str,
                border: str) -> str:
    """One coloured bullet-list card — Highlights, Watch-outs and Recommended
    Actions all render through this one function now, so "make Actions look
    like the other two" can never drift out of sync again.

    Each item is `{"key", "text", "edited"}`. The key is emitted as
    `data-flag-key` so the page can hang an inline editor off the exact line,
    and an edited line wears a marker: it is shown as the operator typed it and
    is NOT recomputed, so it must not be mistaken for a live number.

    A plain string is still accepted — a payload cached before flags carried
    keys renders read-only rather than not at all.
    """
    if not items:
        items = [{"key": "", "text": "Nothing notable this period."}]
    lis = []
    for it in items:
        if isinstance(it, str):
            it = {"key": "", "text": it}
        key_attr = f' data-flag-key="{it["key"]}"' if it.get("key") else ""
        mark = _EDITED_MARK if it.get("edited") else ""
        # Only a keyed line is clickable, so only a keyed line gets the cursor.
        cursor = "cursor:pointer;" if it.get("key") else ""
        # The sentence itself is wrapped in `data-flag-text` so the editor can
        # seed its textarea with exactly that. Reading the <li> instead swept up
        # the bullet — the first save then stored "▲Room revenue …" and the
        # line rendered with two bullets.
        lis.append(
            f"<li{key_attr} title='{'Click to correct or hide this line' if it.get('key') else ''}' "
            f"style='{cursor}font-size:13px;padding:6px 0 6px 20px;"
            f"position:relative;border-top:1px solid rgba(0,0,0,.05);"
            f"list-style:none;'>"
            f"<span style='position:absolute;left:0;font-weight:700;"
            f"color:{color};'>{bullet}</span>"
            f"<span data-flag-text>{it['text']}</span>{mark}</li>"
        )
    return (f"<div style='border-radius:11px;padding:16px 18px;background:{bg};"
            f"border:1px solid {border};'>"
            f"<h4 style='font-size:14px;font-weight:600;margin:0 0 10px;color:{color};'>"
            f"{bullet} {title}</h4><ul style='margin:0;padding:0;'>"
            f"{''.join(lis)}</ul></div>")


def _render_flags(b: dict) -> str:
    """Highlights, and Watch-outs merged with Recommended Actions, as two
    matching bullet-list cards in one section, plus an anchor div the
    frontend portals a "add your own" editor into. An action IS a watch-out
    with a "do this about it" attached, so they share one card instead of
    sending a manager to a second box for what to do about the first.
    """
    br = _brand(b)
    good = b.get("highlights") or []
    watch = b.get("watchouts") or []
    actions = b.get("actions") or []
    bid = b.get("branch_id")

    # An edited action is one sentence the operator wrote, so it replaces the
    # whole title/when/body assembly rather than being spliced back into it.
    action_items = []
    for a in actions:
        if a.get("edited"):
            action_items.append({"key": a.get("key", ""), "edited": True,
                                 "text": a.get("text") or a.get("body") or ""})
            continue
        action_items.append({"key": a.get("key", ""), "text": (
            f"<b>{a['title']}</b> "
            f"<span style='font-size:10.5px;font-weight:600;color:#9a6a00;"
            f"background:#fff;padding:1px 6px;border-radius:5px;white-space:nowrap;'>"
            f"{a['when']}</span> — {a['body']}")})
    watch_panel = _flag_panel(
        "Watch-outs / Recommended Actions", watch + action_items, "!",
        "#9a6a00", C["warn_bg"], "#ecd9a8",
    )

    body = (
        f"<div style='display:grid;grid-template-columns:1fr 1fr;gap:14px;'>"
        f"{_flag_panel('Highlights', good, '▲', '#0c7a44', C['good_bg'], '#bfe3cc')}"
        f"{watch_panel}"
        f"</div>"
        + f'<div id="bw-flags-anchor-{bid}"></div>'
    )
    return _section(8, "Highlights &amp; Watch-outs",
                    "Automatic, from the numbers above — click any line to "
                    "correct or hide it, or add your own below.",
                    body, br["primary"])


def _render_notes(b: dict) -> str:
    notes = b.get("data_notes") or []
    if not notes:
        return ""
    color = {"bad": C["bad"], "warn": C["warn"], "info": C["muted"]}
    items = "".join(
        f"<li style='font-size:12.5px;color:{C['ink']};padding:5px 0 5px 18px;"
        f"position:relative;list-style:none;'>"
        f"<span style='position:absolute;left:0;color:{color.get(n['level'], C['muted'])};"
        f"font-weight:700;'>•</span>{n['text']}</li>"
        for n in notes
    )
    return (f"<div style='background:#f4efe8;border-radius:11px;padding:14px 18px;margin-top:18px;'>"
            f"<div style='font-size:11px;font-weight:600;"
            f"color:{C['muted']};margin-bottom:6px;'>"
            f"How to read these numbers</div><ul style='margin:0;padding:0;'>{items}</ul></div>")


_GLOSSARY = [
    ("Occupancy (OCC)", "Share of rooms filled. 86% means on average 86% of rooms had guests."),
    ("Avg room rate (ADR)", "Average revenue per room-night sold."),
    ("RevPAR", "Revenue per available room — ADR × occupancy."),
    ("ROAS", "Revenue returned per 1 unit of ad spend. 20× = very profitable."),
    ("OTA", "Third-party booking channels (Booking.com, Agoda, Ctrip…) — they charge commission."),
    ("Direct", "Guest books through the hotel's own channels — no commission."),
    ("KOL", "Influencer who features the hotel, generating organic bookings."),
]


def _render_glossary(br: dict) -> str:
    dl = "".join(
        f"<dt style='font-weight:600;color:{br['deep']};'>{t}</dt>"
        f"<dd style='color:{C['ink']};margin:0;'>{d}</dd>"
        for t, d in _GLOSSARY
    )
    body = (f"<div style='background:#f4efe8;border-radius:11px;padding:16px 18px;'>"
            f"<dl style='display:grid;grid-template-columns:auto 1fr;gap:6px 12px;"
            f"font-size:12.5px;margin:0;'>{dl}</dl></div>")
    return _section("?", "Quick Glossary", "", body, br["primary"])


def _render_branch(b: dict, p: Period) -> str:
    """One branch's full report, wrapped in the anchor the frontend slices on."""
    br = _brand(b)
    city = b.get("branch_city") or ""
    # A branch banner in the brand colour. In the dashboard the tab already
    # names the branch, but the raw preview runs all five together with no
    # divider — without this they read as one continuous report.
    banner = f"""
    <div style="background:{br['primary']};color:#fff;border-radius:11px;
         padding:13px 18px;margin-bottom:4px;display:flex;align-items:baseline;
         justify-content:space-between;gap:10px;flex-wrap:wrap;">
      <div style="font-weight:700;font-size:17px;">{b['branch_name']}</div>
      <div style="font-size:12px;opacity:.85;">{city}</div>
    </div>"""
    return f"""
    <div class="hid-bw-branch" data-branch-id="{b['branch_id']}"
         data-branch-name="{b['branch_name']}" data-branch-color="{br['primary']}">
      {banner}
      {_render_exec_summary(b, p)}
      {_render_target(b)}
      {_render_channel_mix(b)}
      {_render_markets(b)}
      {_render_ads(b)}
      {_render_kol(b)}
      {_render_crm(b)}
      {_render_flags(b)}
      {_render_notes(b)}
      {_render_glossary(br)}
    </div>"""


def _build_html(report: list, p: Period, computed_at: Optional[datetime]) -> str:
    yoy = yoy_window(p)
    mom = mom_window(p)
    stamp = (computed_at or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M UTC")

    def _win(w) -> str:
        return f"{w[0]:%b %d} – {w[1]:%b %d}, {w[1].year}"

    # A comparison window of a different length is compared per day, not as a
    # total. Said once here rather than on every arrow — see `_delta_pair`.
    uneven = [
        name for name, w in (("last month", mom), ("last year", yoy))
        if not comparable_as_totals(p, w)
    ]
    uneven_note = (
        f" Totals against {' and '.join(uneven)} are compared per day, because "
        f"that window is a different number of days than this one."
        if uneven else ""
    )
    # The end-of-month report is due out ON the last day of the month, so the
    # period's final day is still running when this is built. Saying so beats
    # a manager discovering it from a number that looks soft.
    open_note = (
        f"<div style='margin-top:10px;font-size:12px;background:rgba(255,255,255,.16);"
        f"border-radius:7px;padding:7px 11px;display:inline-block;'>"
        f"⏳ {p.end:%b %d} is still in progress — its bookings are not final yet."
        f"</div>"
        if not is_complete(p, ict_today()) else ""
    )

    def chip(label, value):
        return (f"<div style='background:rgba(255,255,255,.15);"
                f"border:1px solid rgba(255,255,255,.28);border-radius:8px;padding:7px 12px;"
                f"font-size:12.5px;'><span style='display:block;font-size:10.5px;"
                f"opacity:.8;margin-bottom:1px;'>"
                f"{label}</span><b style='font-weight:600;'>{value}</b></div>")

    branches = "".join(_render_branch(b, p) for b in report)

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Bi-Weekly Marketing Report — {p.label}</title></head>
<body style="font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;
      color:{C['ink']};background:{C['cream']};margin:0;padding:0;line-height:1.5;">
  <div style="max-width:880px;margin:0 auto;background:{C['cream']};">
    <div id="bw-header" style="background:linear-gradient(135deg,{_NEUTRAL_DARK} 0%,
         {_shade(_NEUTRAL_DARK, 0.7)} 100%);color:#fff;padding:30px 40px 26px;">
      <div style="font-weight:600;letter-spacing:.14em;font-size:15px;opacity:.92;">MEANDER</div>
      <h1 style="font-weight:600;font-size:29px;margin:8px 0 4px;">Bi-Weekly Marketing Report</h1>
      <div style="opacity:.9;font-size:14.5px;">
        Business metrics &amp; campaign performance — for the Branch Manager &amp; Leadership</div>
      <div style="display:flex;flex-wrap:wrap;gap:10px;margin-top:18px;">
        {chip("Report period", f"{p.date_label} ({p.days} days)")}
        {chip("Main comparison · same dates last year", _win(yoy))}
        {chip("Reference · same dates last month", _win(mom))}
        {chip("Generated", stamp)}
      </div>
      {open_note}
      <div style="margin-top:14px;font-size:12px;opacity:.8;max-width:640px;">
        {_ARROW_LEGEND}{uneven_note}</div>
    </div>
    <div style="padding:34px 40px;">{branches}</div>
    <div style="padding:18px 40px 26px;color:{C['muted']};font-size:11.5px;
         border-top:1px solid {C['line']};margin-top:30px;">
      <b style="color:{C['ink']}">MEANDER</b> · Bi-weekly Branch Manager Report ·
      Sources: HiD Dashboard · Ads Platform · KOL records.<br>
      Period {p.key} · {p.days} days · generated {stamp}.
    </div>
  </div>
</body></html>"""
