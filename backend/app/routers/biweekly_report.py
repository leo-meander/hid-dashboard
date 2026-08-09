"""
Bi-Weekly Branch Manager Report router
- GET  /biweekly/periods       → selectable ISO-week-pair periods
- GET  /biweekly/report        → report payload (JSON)
- GET  /biweekly/preview       → rendered HTML (what the dashboard shows)
- POST /biweekly/refresh-cache → rebuild a period's snapshot (X-Sync-Token)
- CRUD /biweekly/comments      → manager's-notes threads

Kept out of `report.py`, which is already ~3k lines for the weekly report.

The HTML is inline-styled on purpose. It is rendered into the dashboard
today, but the same string has to survive an email client when the delivery
step lands — email clients drop <style> blocks, so every rule is on the
element. That constraint is why this reads more verbosely than page CSS.
"""
import logging
from datetime import date, datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.biweekly_report_cache import BiweeklyReportCache
from app.models.branch import Branch
from app.models.user import User
from app.models.weekly_report_comment import WeeklyReportComment
from app.routers.auth import get_current_user
from app.routers.sync import verify_sync_token
from app.services.biweekly_period import (
    Period,
    current_period,
    list_periods,
    parse_period_key,
)
from app.services.biweekly_report_builder import build_biweekly_report
from app.services.report_common import (
    cell_attrs,
    envelope,
    fmt,
    ict_today,
    num,
    signed_pct,
    signed_pts,
)

router = APIRouter()
logger = logging.getLogger(__name__)

REPORT_TYPE = "biweekly"
GENERAL_METRIC_KEY = "bw._general"

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

_TH = (f"padding:10px 13px;text-align:left;background:#f4efe8;font-size:11px;"
       f"color:{C['muted']};font-weight:600;")
_TD = (f"padding:10px 13px;color:{C['ink']};font-size:13px;"
       f"border-top:1px solid {C['line']};")


# ── Cache ────────────────────────────────────────────────────────────────────


def _load_cached(db: Session, key: str):
    row = db.query(BiweeklyReportCache).filter_by(period_key=key).first()
    return (row.payload, row.computed_at) if row else None


def _save_cached(db: Session, p: Period, payload: list, source: str = "manual"):
    now = datetime.now(timezone.utc)
    row = db.query(BiweeklyReportCache).filter_by(period_key=p.key).first()
    if row:
        row.payload = payload
        row.computed_at = now
        row.source = source
    else:
        db.add(BiweeklyReportCache(
            period_key=p.key, period_start=p.start, period_end=p.end,
            payload=payload, computed_at=now, source=source,
        ))
    db.commit()
    return now


def _get_report(db: Session, p: Period, force_fresh: bool = False):
    """Cached payload for a period, building it if absent.

    A completed period's numbers do not change, so unlike the weekly
    report's singleton cache this never needs a scheduled refresh — the
    first read of a new period computes it, every later read is free.
    `?fresh=1` exists for the case where upstream data was backfilled after
    the fact.
    """
    if not force_fresh:
        cached = _load_cached(db, p.key)
        if cached is not None:
            return cached
    payload = build_biweekly_report(db, p)
    return payload, _save_cached(db, p, payload)


def _resolve_period(period: Optional[str]) -> Period:
    if not period:
        return current_period(ict_today())
    try:
        return parse_period_key(period)
    except ValueError as e:
        raise HTTPException(400, str(e))


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


def _section(n, title: str, note: str, body: str) -> str:
    note_html = (
        f"<div style='font-size:13px;color:{C['muted']};margin:-6px 0 14px 38px;'>{note}</div>"
        if note else ""
    )
    return f"""
    <div style="margin-top:30px;">
      <div style="display:flex;align-items:center;gap:11px;margin-bottom:14px;">
        <div style="flex:0 0 auto;width:27px;height:27px;border-radius:50%;
             background:{C['primary']};color:#fff;font-weight:600;font-size:14px;
             display:flex;align-items:center;justify-content:center;">{n}</div>
        <div style="font-weight:600;font-size:19px;color:{C['charcoal']};">{title}</div>
      </div>
      {note_html}
      {body}
    </div>"""


def _render_exec_summary(b: dict, p: Period) -> str:
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
        f"About <b style='color:{C['primary_deep']}'>{fmt(abs(rev_abs), currency)} "
        f"{'higher' if rev_abs >= 0 else 'lower'}</b> than the same period last year."
        if (has_yoy and rev_abs is not None)
        else "No comparable data for the same period last year."
    )

    adr_why = (
        f"<b style='color:{C['primary_deep']}'>The main driver</b> of revenue change — "
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
        f"<b style='color:{C['primary_deep']}'>Every 1 of ad spend returns {roas:.2f}</b> "
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
      <div style="display:grid;grid-template-columns:repeat(2,1fr);gap:14px;">{cards}</div>""")


def _render_headline(b: dict, p: Period) -> str:
    """One-paragraph plain-English story of the period.

    Written from the actual deltas rather than a template with holes: the
    interesting case for a hotel is revenue and volume moving in OPPOSITE
    directions, which is what tells a manager whether growth came from price
    or from filling more beds.
    """
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
    <div style="background:{C['primary_pale']};border-radius:11px;padding:15px 18px;
         margin-bottom:16px;font-size:15px;">
      <span style="font-weight:600;font-size:11px;
            color:{C['primary_deep']};display:block;margin-bottom:4px;">
        The story of these {p.days} days</span>
      {story}
    </div>"""


def _render_target(b: dict) -> str:
    t = b.get("target") or {}
    if not t:
        return ""
    currency = b.get("currency") or ""
    pct = t.get("period_pct")
    bid = b["branch_id"]
    actual = (t.get("period") or {}).get("actual_revenue")
    goal = (t.get("period") or {}).get("target_revenue")

    if pct is None:
        body = (f"<div style='background:{C['card']};border:1px solid {C['line']};"
                f"border-radius:11px;padding:18px;font-size:13px;color:{C['muted']};'>"
                f"No revenue target set for this period — add one on the KPI Targets page.</div>")
        return _section(2, "Target Achievement", "", body)

    bar_pct = max(0, min(100, pct))
    diff = (actual or 0) - (goal or 0)
    pill_color = "g" if pct >= 100 else "w" if pct >= 80 else "b"
    pill = (f"<span style='font-size:11px;font-weight:600;padding:2px 8px;border-radius:20px;"
            f"color:{_LIGHT[pill_color]};background:{_LIGHT_BG[pill_color]};'>"
            f"{'✓ Beat target by' if diff >= 0 else '▼ Short of target by'} "
            f"{fmt(abs(diff), currency)}</span>")

    month_line = ""
    if t.get("month_pct") is not None:
        state = "fully closed" if t.get("month_closed") else f"through {t['month_through']}"
        month_line = (
            f"<div style='font-size:13px;color:{C['muted']};margin-top:10px;'>"
            f"{t['month_label']} ({state}) — <b style='color:{C['ink']}'>"
            f"{t['month_pct']:.0f}%</b> of the monthly target.</div>"
        )

    body = f"""
    <div{cell_attrs(bid, "bw.target", "Target Achievement")}
         style="background:{C['card']};border:1px solid {C['line']};border-radius:11px;padding:18px;">
      <div style="display:flex;justify-content:space-between;align-items:baseline;
           margin-bottom:11px;flex-wrap:wrap;gap:6px;">
        <div style="font-size:22px;font-weight:700;color:{C['charcoal']};">
          Hit <span style="color:{_LIGHT[pill_color]};">{pct:.0f}%</span> of the period target</div>
        <div>{pill}</div>
      </div>
      <div style="height:16px;background:#eee7df;border-radius:9px;position:relative;overflow:hidden;">
        <span style="position:absolute;left:0;top:0;bottom:0;width:{bar_pct:.1f}%;
              background:linear-gradient(90deg,{C['primary']},{C['primary_deep']});
              border-radius:9px;display:block;"></span>
      </div>
      <div style="display:flex;justify-content:space-between;font-size:12px;
           color:{C['muted']};margin-top:8px;">
        <span>Actual: <b style="color:{C['ink']}">{fmt(actual, currency)}</b></span>
        <span>Target: <b style="color:{C['ink']}">{fmt(goal, currency)}</b></span>
      </div>
      {month_line}
    </div>"""
    note = ("Monthly targets prorated to a daily goal, then summed across the period — "
            "so a period spanning two months is measured against both.")
    return _section(2, "Target Achievement", note, body)


def _render_channel_mix(b: dict) -> str:
    ch = b.get("channel_mix") or {}
    sources = ch.get("top_sources") or []
    if not sources:
        return _section(3, "Which channels do guests book through?", "",
                        f"<div style='color:{C['muted']};font-size:13px;'>No booking data "
                        f"for this period.</div>")
    bid = b["branch_id"]
    top = max((s.get("room_nights") or 0) for s in sources) or 1
    total_nights = ch.get("total_nights") or 0

    rows = []
    for s in sources[:7]:
        name = s.get("source") or "Unknown"
        nights = s.get("room_nights") or 0
        share = s.get("nights_share_pct")
        is_direct = "direct" in name.lower() or "website" in name.lower()
        bar_color = C["primary_deep"] if is_direct else "#7fbfbb"
        badge = ("<span style='font-size:9.5px;font-weight:600;color:#fff;"
                 f"background:{C['primary']};border-radius:4px;padding:1px 5px;"
                 "margin-left:5px;'>GOOD</span>") if is_direct else ""
        rows.append(f"""
        <div{cell_attrs(bid, f"bw.channel.{name}", f"Channel — {name}")}
             style="display:flex;align-items:center;gap:12px;margin:9px 0;">
          <div style="flex:0 0 128px;font-size:13.5px;font-weight:600;">{name}{badge}</div>
          <div style="flex:1;height:22px;background:#f1ece5;border-radius:6px;overflow:hidden;">
            <span style="display:block;height:100%;width:{nights / top * 100:.1f}%;
                  background:{bar_color};border-radius:6px;"></span>
          </div>
          <div style="flex:0 0 128px;text-align:right;font-size:12.5px;color:{C['muted']};">
            <b style="color:{C['charcoal']};font-size:13.5px;">
            {share:.1f}%</b> · {num(nights)} nights</div>
        </div>""" if share is not None else "")

    cats = {(c.get("source_category") or ""): c for c in ch.get("categories") or []}
    direct = cats.get("Direct") or {}
    tnote = ""
    if direct.get("revenue_share_pct") is not None:
        tnote = (f"<div style='font-size:11.5px;color:{C['muted']};margin-top:8px;"
                 f"font-style:italic;'>Direct is {direct['revenue_share_pct']:.1f}% of revenue "
                 f"— every direct booking avoids OTA commission (~15–18%).</div>")

    body = (f"<div style='background:{C['card']};border:1px solid {C['line']};"
            f"border-radius:11px;padding:16px 18px;'>{''.join(rows)}</div>{tnote}")
    note = (f"{num(total_nights)} room-nights this period, counted on an occupancy basis "
            "(one row per night actually stayed). More <b>direct</b> is better.")
    return _section(3, "Which channels do guests book through?", note, body)


def _render_markets(b: dict) -> str:
    m = b.get("markets") or {}
    rows = m.get("rows") or []
    bid = b["branch_id"]
    currency = b.get("currency") or ""
    if not rows:
        return _section(4, "Which markets do guests come from?", "",
                        f"<div style='color:{C['muted']};font-size:13px;'>No market data "
                        f"for this period.</div>")

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
            f"<td style='{_TD}font-weight:600;color:{C['charcoal']};'>{country}</td>"
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
                    "they were actually used.", body)


def _render_ads(b: dict) -> str:
    ads = b.get("paid_ads") or {}
    channels = ads.get("by_channel") or []
    bid = b["branch_id"]
    currency = b.get("currency") or ""
    if not channels:
        return _section(5, "Ad campaigns that ran", "",
                        f"<div style='color:{C['muted']};font-size:13px;'>No ad spend "
                        f"recorded for this branch in the period.</div>")

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
                    body)


def _render_kol(b: dict) -> str:
    k = b.get("kol") or {}
    bid = b["branch_id"]
    currency = b.get("currency") or ""
    rows = [
        ("Posts published this period", num(k.get("posts_this_week")), "TikTok / IG / XHS"),
        ("KOL-driven bookings", num(k.get("organic_bookings")), f"{num(k.get('organic_nights'))} nights"),
        ("KOL-driven revenue", fmt(k.get("organic_revenue_native"), currency),
         f"ROI {k['roi']:.2f}×" if k.get("roi") else "—"),
    ]
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
      Reach and engagement (views, likes, engagement rate) are <b>not tracked in HiD</b> —
      only posts published and bookings attributed to KOLs. Affiliate commission is not
      recorded at all.</div>"""
    return _section(6, "KOL / Influencer Performance",
                    "Sources: KOL records in HiD + bookings tagged to a KOL room type.", body)


def _render_crm(b: dict) -> str:
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
                    "Sources: HiD Direct channel + CRM-tagged rate plans.", body)


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
    return _section(8, "Highlights &amp; Watch-outs", "", body)


def _render_actions(b: dict) -> str:
    actions = b.get("actions") or []
    if not actions:
        return ""
    items = "".join(
        f"""<div style="display:flex;gap:13px;padding:13px 16px;border-top:1px solid {C['line']};">
          <div style="flex:0 0 26px;height:26px;border-radius:7px;background:{C['primary_pale']};
               color:{C['primary_deep']};font-weight:700;font-size:13px;display:flex;
               align-items:center;justify-content:center;">{i + 1}</div>
          <div style="font-size:13.5px;"><b style="color:{C['charcoal']};">{a['title']}</b>
            <span style="font-size:11px;font-weight:600;color:{C['primary_deep']};
                  background:{C['primary_pale']};padding:1px 7px;border-radius:5px;
                  margin-left:6px;white-space:nowrap;">{a['when']}</span>
            <div style="margin-top:3px;">{a['body']}</div></div>
        </div>"""
        for i, a in enumerate(actions)
    )
    body = (f"<div style='background:{C['card']};border:1px solid {C['line']};"
            f"border-radius:11px;overflow:hidden;'>{items}</div>")
    return _section(9, "Recommended Actions (next period)",
                    "Growing markets matched against upcoming holidays in those markets.",
                    body)


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


def _render_glossary() -> str:
    dl = "".join(
        f"<dt style='font-weight:600;color:{C['primary_deep']};'>{t}</dt>"
        f"<dd style='color:{C['ink']};margin:0;'>{d}</dd>"
        for t, d in _GLOSSARY
    )
    body = (f"<div style='background:#f4efe8;border-radius:11px;padding:16px 18px;'>"
            f"<dl style='display:grid;grid-template-columns:auto 1fr;gap:6px 12px;"
            f"font-size:12.5px;margin:0;'>{dl}</dl></div>")
    return _section("?", "Quick Glossary", "", body)


def _render_branch(b: dict, p: Period) -> str:
    """One branch's full report, wrapped in the anchor the frontend slices on."""
    return f"""
    <div class="hid-bw-branch" data-branch-id="{b['branch_id']}"
         data-branch-name="{b['branch_name']}">
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
      {_render_glossary()}
    </div>"""


def _build_html(report: list, p: Period, computed_at: Optional[datetime]) -> str:
    from app.services.biweekly_period import previous_period, yoy_window

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
    <div id="bw-header" style="background:linear-gradient(135deg,{C['primary']} 0%,
         {C['primary_deep']} 100%);color:#fff;padding:30px 40px 26px;">
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


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.get("/periods")
def list_available_periods(
    back: int = Query(12, ge=1, le=52),
    db: Session = Depends(get_db),
):
    """Selectable periods, newest first, flagged with whether they're cached."""
    periods = list_periods(ict_today(), back)
    cached = {
        r.period_key for r in
        db.query(BiweeklyReportCache.period_key).filter(
            BiweeklyReportCache.period_key.in_([p.key for p in periods])
        ).all()
    }
    return envelope([{**p.to_dict(), "has_cache": p.key in cached} for p in periods])


@router.get("/report")
def biweekly_report(
    period: Optional[str] = None,
    fresh: int = 0,
    db: Session = Depends(get_db),
):
    """Bi-weekly report payload for a period (defaults to the latest completed)."""
    p = _resolve_period(period)
    payload, computed_at = _get_report(db, p, force_fresh=bool(fresh))
    return envelope({
        "period": p.to_dict(),
        "computed_at": computed_at.isoformat() if computed_at else None,
        "from_cache": not bool(fresh),
        "branches": payload,
    })


@router.get("/preview", response_class=HTMLResponse)
def biweekly_preview(
    period: Optional[str] = None,
    fresh: int = 0,
    db: Session = Depends(get_db),
):
    """Rendered HTML for a period — what the dashboard page displays."""
    p = _resolve_period(period)
    payload, computed_at = _get_report(db, p, force_fresh=bool(fresh))
    return HTMLResponse(_build_html(payload, p, computed_at))


@router.post("/refresh-cache", dependencies=[Depends(verify_sync_token)])
def refresh_cache(period: Optional[str] = None, db: Session = Depends(get_db)):
    """Rebuild a period's snapshot. Token-gated — it runs the full build."""
    p = _resolve_period(period)
    payload = build_biweekly_report(db, p)
    computed_at = _save_cached(db, p, payload, source="cron")
    return envelope({
        "period": p.to_dict(),
        "branches_included": len(payload),
        "computed_at": computed_at.isoformat(),
    })


# ── Manager's notes (reuses weekly_report_comments, report_type='biweekly') ──


class BiweeklyNoteIn(BaseModel):
    period: str
    branch_id: Optional[UUID] = None
    body: str
    metric_key: str = GENERAL_METRIC_KEY
    parent_comment_id: Optional[UUID] = None


class BiweeklyNotePatchIn(BaseModel):
    body: Optional[str] = None
    is_action_item: Optional[bool] = None
    is_resolved: Optional[bool] = None


def _note_out(c: WeeklyReportComment, author: Optional[User]) -> dict:
    return {
        "id": str(c.id),
        "branch_id": str(c.branch_id) if c.branch_id else None,
        "metric_key": c.metric_key,
        "parent_comment_id": str(c.parent_comment_id) if c.parent_comment_id else None,
        "body": c.body,
        "is_action_item": c.is_action_item,
        "is_resolved": c.is_resolved,
        "author_id": str(c.author_id) if c.author_id else None,
        "author_name": (author.name or author.email) if author else None,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }


def _hydrate(db: Session, rows: list) -> list[dict]:
    ids = {c.author_id for c in rows if c.author_id}
    authors = (
        {u.id: u for u in db.query(User).filter(User.id.in_(ids)).all()} if ids else {}
    )
    return [_note_out(c, authors.get(c.author_id)) for c in rows]


@router.get("/comments")
def list_notes(
    period: str,
    branch_id: Optional[UUID] = None,
    metric_key: Optional[str] = None,
    _current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    p = _resolve_period(period)
    q = db.query(WeeklyReportComment).filter(
        WeeklyReportComment.report_type == REPORT_TYPE,
        WeeklyReportComment.week_start == p.start,
        WeeklyReportComment.is_deleted == False,  # noqa: E712
    )
    if branch_id is not None:
        q = q.filter(WeeklyReportComment.branch_id == branch_id)
    if metric_key is not None:
        q = q.filter(WeeklyReportComment.metric_key == metric_key)
    rows = q.order_by(WeeklyReportComment.created_at.asc()).all()
    return envelope(_hydrate(db, rows))


@router.post("/comments", status_code=201)
def create_note(
    body: BiweeklyNoteIn,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    text = (body.body or "").strip()
    if not text:
        raise HTTPException(400, "body is required")
    if len(text) > 5000:
        raise HTTPException(400, "body too long (max 5000 chars)")
    p = _resolve_period(body.period)
    if body.parent_comment_id is not None:
        # Scope the parent lookup to this report type AND this period, so a
        # reply can't be grafted onto a weekly comment or onto a thread from
        # a different period — either would orphan it in the drawer.
        parent = db.query(WeeklyReportComment).filter_by(
            id=body.parent_comment_id,
            report_type=REPORT_TYPE,
            week_start=p.start,
            is_deleted=False,
        ).first()
        if not parent:
            raise HTTPException(404, "Parent note not found")
    c = WeeklyReportComment(
        report_type=REPORT_TYPE,
        week_start=p.start,
        branch_id=body.branch_id,
        metric_key=body.metric_key or GENERAL_METRIC_KEY,
        parent_comment_id=body.parent_comment_id,
        author_id=current.id,
        body=text,
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return envelope(_note_out(c, current))


@router.patch("/comments/{comment_id}")
def update_note(
    comment_id: UUID,
    body: BiweeklyNotePatchIn,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    c = db.query(WeeklyReportComment).filter_by(
        id=comment_id, report_type=REPORT_TYPE, is_deleted=False,
    ).first()
    if not c:
        raise HTTPException(404, "Note not found")
    # Rewriting a note is the author's (or an admin's) call, but resolving one
    # is not: the Support Needed board exists so Growth can ask the branch team
    # for something, and it is the branch team — never the author — who marks
    # it handled. Gating resolve on authorship would make that board unusable.
    # Same split the weekly report uses.
    if body.body is not None:
        if c.author_id != current.id and (current.role or "") != "admin":
            raise HTTPException(403, "Only the author or an admin can edit the body")
        text = body.body.strip()
        if not text:
            raise HTTPException(400, "body cannot be empty")
        if len(text) > 5000:
            raise HTTPException(400, "body too long (max 5000 chars)")
        c.body = text
    if body.is_action_item is not None:
        c.is_action_item = body.is_action_item
    if body.is_resolved is not None:
        c.is_resolved = body.is_resolved
        c.resolved_by = current.id if body.is_resolved else None
        c.resolved_at = datetime.now(timezone.utc) if body.is_resolved else None
    db.commit()
    db.refresh(c)
    # Re-read the author: an admin may be editing someone else's note, and
    # echoing `current` back would relabel the note as theirs in the drawer.
    author = db.query(User).filter_by(id=c.author_id).first() if c.author_id else None
    return envelope(_note_out(c, author))


@router.delete("/comments/{comment_id}")
def delete_note(
    comment_id: UUID,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    c = db.query(WeeklyReportComment).filter_by(
        id=comment_id, report_type=REPORT_TYPE, is_deleted=False,
    ).first()
    if not c:
        raise HTTPException(404, "Note not found")
    if c.author_id != current.id and (current.role or "") != "admin":
        raise HTTPException(403, "You can only delete your own notes")
    c.is_deleted = True
    db.commit()
    return envelope({"id": str(comment_id), "deleted": True})
