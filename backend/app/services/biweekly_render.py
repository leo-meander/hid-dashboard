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

from app.services.biweekly_period import Period, previous_period, yoy_window
from app.services.country_codes import iso_code_for
from app.services.report_common import (
    cell_attrs,
    fmt,
    num,
    signed_pct,
    signed_pts,
)


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


def _kpi_card(branch_id, metric_key: str, label: str, value: str,
              light: str, chips: str, why: str) -> str:
    return f"""
    <div{cell_attrs(branch_id, metric_key, label)} style="background:{C['card']};
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


def _render_exec_summary(b: dict, p: Period) -> str:
    br = _brand(b)
    kpi = b.get("kpi") or {}
    cur = kpi.get("this") or {}
    vy, vp = kpi.get("vs_yoy") or {}, kpi.get("vs_prior") or {}
    lights = kpi.get("lights") or {}
    currency = b.get("currency") or ""
    bid = b["branch_id"]

    yoy_lbl = "vs last year"
    prior_lbl = "vs prior /day" if vp.get("per_day") else "vs prior"

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
    roas_chips = (
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
    ])

    headline = _render_headline(b, p)
    return _section(1, "Executive Summary", "", f"""
      {headline}
      <div style="display:grid;grid-template-columns:repeat(2,1fr);gap:14px;">{cards}</div>""",
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
        story = ("No data for the same period last year, so this report compares "
                 "against the prior period only.")
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

    `size` is "lg" for the common single-month case (this is what shipped
    before periods could show two months, unchanged) or "sm" for a compact
    card used when two sit side by side.
    """
    m_pct = m.get("pct")
    ach = m.get("achievement") or {}
    m_actual, m_goal = ach.get("actual_revenue"), ach.get("target_revenue")
    label = m.get("label") or "this month"

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
    pill_color = "g" if m_pct >= 100 else "w" if m_pct >= 80 else "b"
    pill = (f"<span style='font-size:11px;font-weight:600;padding:2px 8px;border-radius:20px;"
            f"color:{_LIGHT[pill_color]};background:{_LIGHT_BG[pill_color]};'>"
            f"{'✓ Beat by' if diff >= 0 else '▼ Short by'} {fmt(abs(diff), currency)}</span>")
    state = " — fully closed" if m.get("closed") else f" — through {m.get('through')}"

    heading = (
        f"Hit <span style='color:{_LIGHT[pill_color]};'>{m_pct:.0f}%</span> "
        f"of the {label} target"
        if size == "lg" else
        f"{label}: <span style='color:{_LIGHT[pill_color]};'>{m_pct:.0f}%</span>"
    )
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

    if len(months) >= 2:
        # The period crosses a month boundary — show each month's own
        # progress rather than folding both into whichever one it ends in.
        gauges = "".join(
            _target_gauge(bid, m, currency, br, i, size="sm")
            for i, m in enumerate(months)
        )
        gauges_html = (
            f"<div style='display:grid;grid-template-columns:1fr 1fr;gap:12px;'>{gauges}</div>"
        )
        note = ("This period spans two calendar months, so each gets its own target — "
                "prorated to a daily goal and measured against however much of that "
                "month has actually elapsed. The vertical mark is the target.")
    elif months:
        gauges_html = _target_gauge(bid, months[0], currency, br, 0, size="lg")
        note = ("Monthly target prorated to a daily goal, then measured against however "
                "much of the month has actually elapsed. The vertical mark is the target.")
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

    rows = []
    for s in rows_in:
        name = s["source"]
        n = s.get("bookings") or 0
        share = s.get("share_pct")
        bar_color = br["deep"] if s.get("is_direct") else "#9bbfbc"
        badge = ("<span style='font-size:9.5px;font-weight:600;color:#fff;"
                 f"background:{br['primary']};border-radius:4px;padding:1px 5px;"
                 "margin-left:5px;'>GOOD</span>") if s.get("is_direct") else ""
        rows.append(f"""
        <div{cell_attrs(bid, f"bw.channel.{name}", f"Channel — {name}")}
             style="display:flex;align-items:center;gap:12px;margin:9px 0;">
          <div style="flex:0 0 128px;font-size:13.5px;font-weight:600;">{name}{badge}</div>
          <div style="flex:1;height:22px;background:#f1ece5;border-radius:6px;overflow:hidden;">
            <span style="display:block;height:100%;width:{n / top * 100:.1f}%;
                  background:{bar_color};border-radius:6px;"></span>
          </div>
          <div style="flex:0 0 128px;text-align:right;font-size:12.5px;color:{C['muted']};">
            <b style="color:{C['charcoal']};font-size:13.5px;">
            {share:.1f}%</b> · {num(n)}</div>
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
    note = (f"{num(total)} bookings this period — counted once each if any night "
            "fell inside it. More <b>direct</b> is better.")
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

    trs = []
    for r in rows:
        d = r.get("vs_prior_pct")
        if d is None:
            pill = f"<span style='color:{C['muted']};font-size:11px;'>new</span>"
        else:
            up = d >= 0
            pill = (f"<span style='font-size:11px;font-weight:600;padding:2px 8px;"
                    f"border-radius:20px;color:{C['good'] if up else C['bad']};"
                    f"background:{C['good_bg'] if up else C['bad_bg']};'>"
                    f"{'▲' if up else '▼'} {signed_pct(d)}</span>")
        country = r["country"]
        attrs = cell_attrs(bid, "bw.market." + country, "Market — " + country)
        trs.append(
            f"<tr{attrs}>"
            f"<td style='{_TD}font-weight:600;color:{C['charcoal']};'>"
            f"{_flag(r.get('country_code') or country)} {country}</td>"
            f"<td style='{_TD}text-align:right;'>{fmt(r['revenue'], currency)}</td>"
            f"<td style='{_TD}text-align:right;'>{num(r['bookings'])}</td>"
            f"<td style='{_TD}text-align:right;'>{pill}</td></tr>"
        )

    unknown_note = ""
    if m.get("unknown_share_pct"):
        unknown_note = (
            f" — Data note: {num(m.get('unknown_bookings'))} booking(s) "
            f"({fmt(m.get('unknown_revenue'), currency)}, {m['unknown_share_pct']:.0f}% of "
            f"revenue) have no source market recorded and are excluded from this table."
        )

    body = f"""
    <table style="width:100%;border-collapse:collapse;background:{C['card']};
           border:1px solid {C['line']};border-radius:11px;overflow:hidden;font-size:13px;">
      <thead><tr><th style="{_TH}">Market</th><th style="{_TH}text-align:right;">Revenue</th>
      <th style="{_TH}text-align:right;">Bookings</th>
      <th style="{_TH}text-align:right;">vs prior</th></tr></thead>
      <tbody>{''.join(trs)}</tbody>
    </table>
    <div style="font-size:11.5px;color:{C['muted']};margin-top:8px;font-style:italic;">
      Ranked by revenue in the period, with growth against the prior period.{unknown_note}</div>"""
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

    trs = []
    for c in channels:
        roas = c.get("roas")
        if roas is None:
            pill = f"<span style='color:{C['muted']};font-size:11px;'>no spend</span>"
        else:
            k = "g" if roas >= 4 else "b" if roas < 2 else "w"
            word = "Excellent" if roas >= 8 else "Good" if roas >= 4 else \
                   "Weak" if roas < 2 else "OK"
            pill = (f"<span style='font-size:11px;font-weight:600;padding:2px 8px;"
                    f"border-radius:20px;color:{_LIGHT[k]};background:{_LIGHT_BG[k]};'>"
                    f"{roas:.2f}× · {word}</span>")
        name = c["channel"]
        attrs = cell_attrs(bid, "bw.ads." + name, "Ads — " + name)
        trs.append(
            f"<tr{attrs}>"
            f"<td style='{_TD}font-weight:600;color:{C['charcoal']};'>{name}</td>"
            f"<td style='{_TD}text-align:right;'>{fmt(c.get('cost'), currency)}</td>"
            f"<td style='{_TD}text-align:right;'>{fmt(c.get('revenue'), currency)}</td>"
            f"<td style='{_TD}text-align:right;'>{pill}</td>"
            f"<td style='{_TD}text-align:right;'>{num(c.get('bookings'))}</td></tr>"
        )

    tot = ads.get("last_week") or {}
    footer = ""
    if tot.get("cost"):
        footer = (f"Total ad spend {fmt(tot['cost'], currency)} in the period. ")
    footer += ("Google spend reaches HiD only via the Ads Platform aggregator — a missing "
               "Google row means it is not connected for this branch, not that spend was zero.")

    body = f"""
    <table style="width:100%;border-collapse:collapse;background:{C['card']};
           border:1px solid {C['line']};border-radius:11px;overflow:hidden;font-size:13px;">
      <thead><tr><th style="{_TH}">Channel</th><th style="{_TH}text-align:right;">Spend</th>
      <th style="{_TH}text-align:right;">Revenue</th>
      <th style="{_TH}text-align:right;">Efficiency</th>
      <th style="{_TH}text-align:right;">Bookings</th></tr></thead>
      <tbody>{''.join(trs)}</tbody>
    </table>
    <div style="font-size:11.5px;color:{C['muted']};margin-top:8px;font-style:italic;">{footer}</div>"""
    return _section(5, "Ad campaigns that ran",
                    "ROAS = revenue returned per 1 unit of ad spend. Higher is more profitable.",
                    body, br["primary"])


def _render_kol(b: dict) -> str:
    br = _brand(b)
    k = b.get("kol") or {}
    bid = b["branch_id"]
    currency = b.get("currency") or ""
    reach = b.get("kol_reach") or {}
    rows = [
        ("Posts published this period", num(k.get("posts_this_week")), "TikTok / IG / XHS"),
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
            ("Views / reach", num(reach.get("reach")),
             f"{num(total_posts)} post(s) in period"),
            ("Engagements", num(reach.get("engagements")), er_note),
        ]
    rows += [
        ("KOL-driven bookings", num(k.get("organic_bookings")), f"{num(k.get('organic_nights'))} nights"),
        ("KOL-driven revenue", fmt(k.get("organic_revenue_native"), currency),
         f"ROI {k['roi']:.2f}×" if k.get("roi") else "—"),
    ]
    # Distinguish "the Engine had nothing to give us" from "the posts got no
    # views" — a zero in this row would be read as a performance claim.
    if reach.get("available"):
        reach_note = ("Reach and engagement come from the KOL Engine, counted on the "
                      "post's publish date.")
    else:
        reach_note = ("Reach and engagement are <b>unavailable for this period</b> — "
                      "the KOL Engine returned no scored posts, so views and engagement "
                      "rate are omitted rather than shown as zero.")

    trs = "".join(
        f"<tr{cell_attrs(bid, f'bw.kol.{i}', label)}>"
        f"<td style='{_TD}font-weight:600;color:{C['charcoal']};'>{label}</td>"
        f"<td style='{_TD}text-align:right;'>{value}</td>"
        f"<td style='{_TD}text-align:right;color:{C['muted']};font-size:11.5px;'>{extra}</td></tr>"
        for i, (label, value, extra) in enumerate(rows)
    )
    body = f"""
    <table style="width:100%;border-collapse:collapse;background:{C['card']};
           border:1px solid {C['line']};border-radius:11px;overflow:hidden;font-size:13px;">
      <thead><tr><th style="{_TH}">KOL metric</th>
      <th style="{_TH}text-align:right;">This period</th>
      <th style="{_TH}text-align:right;"></th></tr></thead>
      <tbody>{trs}</tbody>
    </table>
    <div style="font-size:11.5px;color:{C['muted']};margin-top:8px;font-style:italic;">
      {reach_note} Affiliate commission is not recorded in HiD.</div>"""
    return _section(6, "KOL / Influencer Performance",
                    "Sources: KOL records in HiD, reach from the KOL Engine, and "
                    "bookings tagged to a KOL room type.", body, br["primary"])


def _render_crm(b: dict) -> str:
    br = _brand(b)
    crm = b.get("crm") or {}
    ch = b.get("channel_mix") or {}
    bid = b["branch_id"]
    currency = b.get("currency") or ""
    direct = next((c for c in ch.get("categories") or []
                   if (c.get("source_category") or "").lower() == "direct"), {})

    rows = [
        ("Direct room-nights", num(direct.get("room_nights")),
         signed_pct(direct.get("wow_nights_pct")) if direct.get("wow_nights_pct") is not None else "—"),
        ("Direct revenue", fmt(direct.get("revenue_native"), currency),
         signed_pct(direct.get("wow_revenue_pct")) if direct.get("wow_revenue_pct") is not None else "—"),
        ("Direct share of revenue",
         f"{direct['revenue_share_pct']:.1f}%" if direct.get("revenue_share_pct") is not None else "—",
         "no OTA commission"),
        ("CRM-tagged revenue", fmt(crm.get("revenue_native"), currency),
         signed_pct(crm.get("wow_revenue_pct")) if crm.get("wow_revenue_pct") is not None else "—"),
    ]
    trs = "".join(
        f"<tr{cell_attrs(bid, f'bw.crm.{i}', label)}>"
        f"<td style='{_TD}font-weight:600;color:{C['charcoal']};'>{label}</td>"
        f"<td style='{_TD}text-align:right;'>{value}</td>"
        f"<td style='{_TD}text-align:right;color:{C['muted']};font-size:11.5px;'>{extra}</td></tr>"
        for i, (label, value, extra) in enumerate(rows)
    )
    body = f"""
    <table style="width:100%;border-collapse:collapse;background:{C['card']};
           border:1px solid {C['line']};border-radius:11px;overflow:hidden;font-size:13px;">
      <thead><tr><th style="{_TH}">Metric</th>
      <th style="{_TH}text-align:right;">This period</th>
      <th style="{_TH}text-align:right;">vs prior</th></tr></thead>
      <tbody>{trs}</tbody>
    </table>
    <div style="font-size:11.5px;color:{C['muted']};margin-top:8px;font-style:italic;">
      "Direct" = booked through owned channels, no OTA commission. CRM figures come from
      CRM-tagged rate plans in Cloudbeds and the GoHighLevel integration.</div>"""
    return _section(7, "CRM / Direct Booking Performance",
                    "Sources: HiD Direct channel + CRM-tagged rate plans.", body, br["primary"])


def _render_flags(b: dict) -> str:
    good = b.get("highlights") or []
    watch = b.get("watchouts") or []
    if not good and not watch:
        return ""

    def panel(title, items, is_good):
        if not items:
            items = ["Nothing notable this period."]
        bullet = "▲" if is_good else "!"
        color = "#0c7a44" if is_good else "#9a6a00"
        bg = C["good_bg"] if is_good else C["warn_bg"]
        border = "#bfe3cc" if is_good else "#ecd9a8"
        lis = "".join(
            f"<li style='font-size:13px;padding:6px 0 6px 20px;position:relative;"
            f"border-top:1px solid rgba(0,0,0,.05);list-style:none;'>"
            f"<span style='position:absolute;left:0;font-weight:700;"
            f"color:{C['good'] if is_good else C['warn']};'>{bullet}</span>{t}</li>"
            for t in items
        )
        return (f"<div style='border-radius:11px;padding:16px 18px;background:{bg};"
                f"border:1px solid {border};'>"
                f"<h4 style='font-size:14px;font-weight:600;margin:0 0 10px;color:{color};'>"
                f"{bullet} {title}</h4><ul style='margin:0;padding:0;'>{lis}</ul></div>")

    body = (f"<div style='display:grid;grid-template-columns:1fr 1fr;gap:14px;'>"
            f"{panel('Highlights', good, True)}{panel('Watch-outs', watch, False)}</div>")
    return _section(8, "Highlights &amp; Watch-outs", "", body, _brand(b)["primary"])


def _render_actions(b: dict) -> str:
    br = _brand(b)
    actions = b.get("actions") or []
    if not actions:
        return ""
    items = "".join(
        f"""<div style="display:flex;gap:13px;padding:13px 16px;border-top:1px solid {C['line']};">
          <div style="flex:0 0 26px;height:26px;border-radius:7px;background:{br['pale']};
               color:{br['deep']};font-weight:700;font-size:13px;display:flex;
               align-items:center;justify-content:center;">{i + 1}</div>
          <div style="font-size:13.5px;"><b style="color:{C['charcoal']};">{a['title']}</b>
            <span style="font-size:11px;font-weight:600;color:{br['deep']};
                  background:{br['pale']};padding:1px 7px;border-radius:5px;
                  margin-left:6px;white-space:nowrap;">{a['when']}</span>
            <div style="margin-top:3px;">{a['body']}</div></div>
        </div>"""
        for i, a in enumerate(actions)
    )
    body = (f"<div style='background:{C['card']};border:1px solid {C['line']};"
            f"border-radius:11px;overflow:hidden;'>{items}</div>")
    return _section(9, "Recommended Actions (next period)",
                    "Growing markets matched against upcoming holidays in those markets.",
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
      {_render_actions(b)}
      {_render_notes(b)}
      {_render_glossary(br)}
    </div>"""


def _build_html(report: list, p: Period, computed_at: Optional[datetime]) -> str:
    yoy = yoy_window(p)
    prev = previous_period(p)
    stamp = (computed_at or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M UTC")

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
        {chip("Report period", f"{p.label} · {p.date_label}")}
        {chip("Main comparison · same weeks last year",
              f"W{p.week_a}–{min(p.week_b, 52)} {p.iso_year - 1} ({yoy[0]:%b %d} – {yoy[1]:%b %d})")}
        {chip("Reference · prior period", f"{prev.label.split(' · ')[0]} ({prev.date_label})")}
        {chip("Generated", stamp)}
      </div>
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
