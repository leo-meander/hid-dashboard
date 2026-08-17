"""Emailing one branch's Bi-Weekly report, and the no-login page it opens.

Two pieces of HTML live here, and they are deliberately different documents:

  * `build_summary_email_html` — what lands in the inbox. Short: the headline
    numbers, where the month stands, what to watch, and one button. Email
    clients mangle long documents, previews truncate, and a wall of tables in
    a notification is a wall nobody reads. The email's job is to say whether
    this fortnight needs attention and to get the reader to the real thing.

  * `build_share_page_html` — what the button opens. The full report for that
    one branch, rendered by the same `_build_html` the dashboard uses, plus
    every note and discussion thread flattened underneath it. The recipient
    has no HiD login, so anything the page does not print, they cannot go and
    look up.

The comment appendix is why this module renders rather than the frontend:
the dashboard fetches threads through an authenticated API and stitches them
onto `[data-metric-key]` cells with JavaScript. A shared page has neither the
API nor a reason to trust the caller, so the threads are baked into the HTML
server-side, read-only, and the labels are lifted out of the markup that was
just rendered rather than kept in a second table that could drift.
"""
from __future__ import annotations

import html
import re
from datetime import date, datetime
from typing import Optional

from app.services.biweekly_period import Period
from app.services.biweekly_render import (
    C,
    _LIGHT,
    _LIGHT_BG,
    _NEUTRAL_DARK,
    _brand,
    _build_html,
    _shade,
)
from app.services.report_common import fmt, signed_pct, signed_pts

# Boards that are not tied to a metric cell — the running logs under the
# report and the two "add your own" boards inside Highlights & Watch-outs.
# Keys must match NOTE_BOARDS / FLAG_BOARDS in the frontend page.
_BOARD_LABELS = {
    "bw._general": "Branch Manager's Notes",
    "bw._growth": "Growth Team — What We Did & How It Went",
    "bw._support": "Support Needed From The Branch",
    "bw._highlight": "Added highlights",
    "bw._watchout": "Added watch-outs",
}

_LABEL_RE = re.compile(
    r'data-metric-key="(?P<key>[^"]+)"[^>]*?data-metric-label="(?P<label>[^"]*)"'
)


def _labels_from_html(markup: str) -> dict:
    """Map metric_key → human label, read off the report that was just built.

    `cell_attrs` emits both attributes on every clickable cell, so the report
    already carries the only label a reader would recognise — including for
    the dynamic keys (per-country, per-rate-plan) that no static table could
    enumerate.
    """
    return {m.group("key"): m.group("label") for m in _LABEL_RE.finditer(markup)}


def _esc(text: Optional[str]) -> str:
    """Comment bodies are user-typed and go into HTML. Newlines survive as
    <br>, everything else is inert."""
    return html.escape(text or "", quote=False).replace("\n", "<br>")


def _when(value) -> str:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    return value.strftime("%d %b %Y") if value else ""


def _comment_card(c: dict, is_reply: bool) -> str:
    author = _esc(c.get("author_name") or "Someone")
    stamp = _when(c.get("created_at"))
    done = c.get("is_resolved")
    tag = (
        f"<span style='font-size:10px;font-weight:600;color:{C['good']};"
        f"background:{C['good_bg']};border-radius:20px;padding:1px 7px;"
        f"margin-left:6px;'>Done</span>" if done else ""
    )
    return (
        f"<div style='margin:0 0 8px {'26px' if is_reply else '0'};padding:9px 12px;"
        f"background:{'#faf7f3' if is_reply else C['card']};"
        f"border:1px solid {C['line']};border-radius:9px;"
        f"{'border-left:3px solid ' + C['line'] + ';' if is_reply else ''}'>"
        f"<div style='font-size:11px;color:{C['muted']};margin-bottom:3px;'>"
        f"<b style='color:{C['charcoal']};font-weight:600;'>{author}</b>"
        f"{' · ' + stamp if stamp else ''}{tag}</div>"
        f"<div style='font-size:12.5px;color:{C['ink']};line-height:1.55;'>"
        f"{_esc(c.get('body'))}</div></div>"
    )


def _thread(comments: list[dict]) -> str:
    """One metric's thread — top-level notes, each followed by its replies.

    Replies are nested one level only, matching the drawer. A reply whose
    parent was deleted would otherwise vanish from the page entirely, so it
    falls back to being rendered at the top level.
    """
    by_parent: dict = {}
    for c in comments:
        by_parent.setdefault(c.get("parent_comment_id"), []).append(c)
    top_ids = {c["id"] for c in comments if not c.get("parent_comment_id")}
    orphans = [
        c for c in comments
        if c.get("parent_comment_id") and c["parent_comment_id"] not in top_ids
    ]

    out = ""
    for c in by_parent.get(None, []) + orphans:
        out += _comment_card(c, is_reply=False)
        for reply in by_parent.get(c["id"], []):
            out += _comment_card(reply, is_reply=True)
    return out


def render_comment_appendix(comments: list[dict], report_html: str) -> str:
    """Every note on this branch's report, grouped by what it is about.

    Returns "" when there are none — an empty "Notes" heading on a shared
    page reads as "the team said nothing", which is a different claim from
    "nobody has written here yet".
    """
    if not comments:
        return ""

    labels = _labels_from_html(report_html)
    groups: dict = {}
    for c in comments:
        groups.setdefault(c.get("metric_key") or "bw._general", []).append(c)

    # Boards first, in the order the page shows them, then the per-metric
    # threads alphabetically by label — a reader scanning for "did anyone
    # explain the ADR drop" is looking for a name, not a key.
    board_keys = [k for k in _BOARD_LABELS if k in groups]
    metric_keys = sorted(
        (k for k in groups if k not in _BOARD_LABELS),
        key=lambda k: (labels.get(k) or k).lower(),
    )

    blocks = ""
    for key in board_keys + metric_keys:
        label = _BOARD_LABELS.get(key) or labels.get(key) or key
        blocks += (
            f"<div style='margin-bottom:18px;'>"
            # quote=False: this is element text, not an attribute value, and
            # escaping the apostrophe in "Branch Manager's Notes" would print
            # the entity rather than the word.
            f"<div style='font-size:12px;font-weight:600;color:{C['charcoal']};"
            f"margin-bottom:7px;'>{html.escape(label, quote=False)}"
            f"<span style='color:{C['muted']};font-weight:400;'> · "
            f"{len(groups[key])} note{'s' if len(groups[key]) != 1 else ''}</span>"
            f"</div>{_thread(groups[key])}</div>"
        )

    return (
        f"<div style='padding:0 40px 34px;'>"
        f"<div style='background:{C['cream']};border:1px solid {C['line']};"
        f"border-radius:13px;padding:22px 24px;'>"
        f"<div style='font-size:16px;font-weight:700;color:{C['charcoal']};"
        f"margin-bottom:4px;'>Notes &amp; discussion</div>"
        f"<div style='font-size:12px;color:{C['muted']};margin-bottom:16px;'>"
        f"Everything the team wrote on this report, in one place. This page is "
        f"read-only — reply on the HiD dashboard.</div>"
        f"{blocks}</div></div>"
    )


def build_share_page_html(branch_payload: dict, p: Period,
                          computed_at: Optional[datetime],
                          comments: list[dict]) -> str:
    """The full single-branch report, plus its notes, for a no-login reader."""
    report_html = _build_html([branch_payload], p, computed_at)
    appendix = render_comment_appendix(comments, report_html)
    if not appendix:
        return report_html
    # Slot the appendix in ahead of the footer rather than after </body>, so
    # it sits inside the page's centred column like every other section.
    marker = '<div style="padding:18px 40px 26px;'
    idx = report_html.find(marker)
    if idx == -1:
        return report_html.replace("</body>", f"{appendix}</body>")
    return report_html[:idx] + appendix + report_html[idx:]


# ── The email ────────────────────────────────────────────────────────────────


def _stat(label: str, value: str, deltas: str, accent: str) -> str:
    return (
        f"<td style='padding:0 6px 12px 0;vertical-align:top;width:50%;'>"
        f"<div style='border:1px solid {C['line']};border-radius:10px;"
        f"padding:12px 14px;background:{C['card']};'>"
        f"<div style='font-size:10.5px;font-weight:600;color:{C['muted']};"
        f"letter-spacing:.04em;'>{label}</div>"
        f"<div style='font-size:20px;font-weight:700;color:{accent};"
        f"margin:3px 0 5px;'>{value}</div>{deltas}</div></td>"
    )


def _mini_delta(value, label: str, kind: str = "pct") -> str:
    if value is None:
        return ""
    text = signed_pts(value) if kind == "pts" else signed_pct(value)
    good, bad = (3.0, -3.0) if kind == "pts" else (5.0, -5.0)
    k = "g" if value >= good else "b" if value < bad else "w"
    arrow = "▲" if k == "g" else "▼" if k == "b" else "≈"
    return (
        f"<span style='font-size:10.5px;font-weight:600;color:{_LIGHT[k]};"
        f"background:{_LIGHT_BG[k]};border-radius:5px;padding:2px 6px;"
        f"display:inline-block;margin:0 4px 3px 0;'>{arrow} {text} "
        f"<span style='color:{C['muted']};font-weight:400;'>{label}</span></span>"
    )


def _flag_texts(b: dict, key: str) -> list[tuple[str, bool]]:
    """Flag lines as `(text, was_typed_by_an_operator)`.

    A generated line carries the builder's own `<b>` emphasis and has to go
    into the email as markup. An overridden one is whatever a person typed
    into the flag editor, so it is escaped — the report page renders those
    raw today, but there is no reason for an outbound email to be the laxer
    of the two.

    Both list-of-strings and list-of-dicts shapes are accepted: the payload
    is cached, and periods computed before `key` was added to each flag are
    still readable. An email that raises on a six-week-old cache is worse
    than one that shows no bullets.
    """
    raw = b.get(key)
    if not isinstance(raw, list):
        return []
    out: list[tuple[str, bool]] = []
    for item in raw:
        if isinstance(item, str):
            out.append((item, False))
        elif isinstance(item, dict):
            text = item.get("text") or item.get("body") or item.get("label")
            if text:
                out.append((str(text), bool(item.get("edited"))))
    return out


def _bullets(items: list[tuple[str, bool]], accent: str, limit: int) -> str:
    if not items:
        return ""
    return "".join(
        f"<li style='font-size:12.5px;color:{C['ink']};line-height:1.55;"
        f"padding:3px 0 3px 15px;position:relative;list-style:none;'>"
        f"<span style='position:absolute;left:0;color:{accent};"
        f"font-weight:700;'>•</span>{html.escape(text, quote=False) if typed else text}</li>"
        for text, typed in items[:limit]
    )


def build_summary_email_html(b: dict, p: Period, share_url: str,
                             recipient_name: Optional[str] = None,
                             expires_on: Optional[date] = None) -> str:
    """The digest that lands in the inbox, with the button to the full report.

    Everything here is also in the full report — this is a decision aid, not
    a second source of truth. If the two ever disagree, the full report wins,
    which is why every number below is read straight off the same payload
    rather than recomputed.
    """
    br = _brand(b)
    currency = b.get("currency") or ""
    kpi = b.get("kpi") or {}
    cur = kpi.get("this") or {}
    vy, vp = kpi.get("vs_yoy") or {}, kpi.get("vs_prior") or {}
    has_yoy = kpi.get("yoy_has_data")

    mom_lbl = "vs last month/day" if vp.get("per_day") else "vs last month"
    yoy_lbl = "vs last year"

    def deltas(yoy_key, mom_key, kind="pct"):
        out = _mini_delta(vp.get(mom_key), mom_lbl, kind)
        if has_yoy:
            out += _mini_delta(vy.get(yoy_key), yoy_lbl, kind)
        return out or (f"<span style='font-size:10.5px;color:{C['muted']};'>"
                       f"no comparison data</span>")

    occ = cur.get("occ_pct")
    stats = (
        f"<tr>{_stat('REVENUE', fmt(cur.get('revenue'), currency), deltas('revenue_pct', 'revenue_pct'), br['deep'])}"
        f"{_stat('AVG ROOM RATE (ADR)', fmt(cur.get('adr'), currency), deltas('adr_pct', 'adr_pct'), br['deep'])}</tr>"
        f"<tr>{_stat('OCCUPANCY', f'{occ * 100:.1f}%' if occ is not None else '—', deltas('occ_pts', 'occ_pts', 'pts'), br['deep'])}"
        f"{_stat('REVPAR', fmt(cur.get('revpar'), currency), deltas('revpar_pct', 'revpar_pct'), br['deep'])}</tr>"
    )

    # Where the month stands, and what is already booked for the ones after
    # it — the two questions the gauges in the full report answer.
    target = b.get("target") or {}
    months = target.get("months") or []
    month_lines = ""
    for m in months:
        pct_val = m.get("pct")
        if pct_val is None:
            continue
        upcoming = m.get("status") == "upcoming"
        k = ("w" if upcoming else
             "g" if pct_val >= 100 else "w" if pct_val >= 80 else "b")
        # Built outside the f-string: an expression part cannot contain a
        # backslash before Python 3.12, and the container runs 3.11.
        note = (
            f"<span style='color:{C['muted']};'> · booked so far</span>"
            if upcoming else ""
        )
        month_lines += (
            f"<tr><td style='font-size:12.5px;color:{C['ink']};padding:4px 0;'>"
            f"{html.escape(m.get('label') or '', quote=False)}{note}"
            f"</td><td style='text-align:right;padding:4px 0;'>"
            f"<span style='font-size:12.5px;font-weight:700;color:{_LIGHT[k]};'>"
            f"{pct_val:.0f}%</span></td></tr>"
        )
    target_block = (
        f"<div style='border:1px solid {C['line']};border-radius:10px;"
        f"padding:12px 14px;background:{C['card']};margin-bottom:16px;'>"
        f"<div style='font-size:10.5px;font-weight:600;color:{C['muted']};"
        f"letter-spacing:.04em;margin-bottom:5px;'>TARGET ACHIEVEMENT</div>"
        f"<table style='width:100%;border-collapse:collapse;'>{month_lines}</table>"
        f"</div>" if month_lines else ""
    )

    highlights = _bullets(_flag_texts(b, "highlights"), C["good"], 3)
    watchouts = _bullets(_flag_texts(b, "watchouts"), C["warn"], 3)
    flags_block = ""
    if highlights or watchouts:
        cols = ""
        if highlights:
            cols += (f"<div style='margin-bottom:10px;'><div style='font-size:10.5px;"
                     f"font-weight:600;color:{C['muted']};letter-spacing:.04em;"
                     f"margin-bottom:3px;'>HIGHLIGHTS</div>"
                     f"<ul style='margin:0;padding:0;'>{highlights}</ul></div>")
        if watchouts:
            cols += (f"<div><div style='font-size:10.5px;font-weight:600;"
                     f"color:{C['muted']};letter-spacing:.04em;margin-bottom:3px;'>"
                     f"WATCH-OUTS</div>"
                     f"<ul style='margin:0;padding:0;'>{watchouts}</ul></div>")
        flags_block = (
            f"<div style='border:1px solid {C['line']};border-radius:10px;"
            f"padding:12px 14px;background:{C['card']};margin-bottom:16px;'>"
            f"{cols}</div>"
        )

    greeting = f"Hi {html.escape(recipient_name, quote=False)}," if recipient_name else "Hi,"
    expiry_line = (
        f" The link works until {expires_on:%d %b %Y}." if expires_on else ""
    )

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(b.get('branch_name') or 'Branch', quote=False)} — Bi-Weekly Report</title></head>
<body style="margin:0;padding:0;background:{C['cream']};
      font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;
      color:{C['ink']};line-height:1.5;">
  <div style="max-width:620px;margin:0 auto;background:{C['cream']};">
    <div style="background:linear-gradient(135deg,{_NEUTRAL_DARK} 0%,
         {_shade(_NEUTRAL_DARK, 0.7)} 100%);color:#fff;padding:24px 28px 20px;">
      <div style="font-weight:600;letter-spacing:.14em;font-size:13px;opacity:.92;">MEANDER</div>
      <h1 style="font-weight:600;font-size:21px;margin:6px 0 3px;">
        {html.escape(b.get('branch_name') or 'Branch', quote=False)} — Bi-Weekly Report</h1>
      <div style="opacity:.9;font-size:13.5px;">{p.date_label} · {p.days} days</div>
    </div>
    <div style="padding:22px 28px 26px;">
      <p style="font-size:13.5px;margin:0 0 16px;">
        {greeting} here is how {html.escape(b.get('branch_name') or 'the branch', quote=False)}
        did over {p.date_label}, against the same dates last month and last year.</p>
      <table style="width:100%;border-collapse:collapse;margin-bottom:4px;">{stats}</table>
      {target_block}
      {flags_block}
      <div style="text-align:center;margin:22px 0 8px;">
        <a href="{html.escape(share_url, quote=True)}"
           style="display:inline-block;background:{br['deep']};color:#fff;
           text-decoration:none;font-size:14.5px;font-weight:600;
           padding:13px 30px;border-radius:9px;">
          View the full report →</a>
        <div style="font-size:11.5px;color:{C['muted']};margin-top:9px;">
          Opens straight from this email — no HiD login needed, and every note
          the team wrote is on the page.{expiry_line}</div>
      </div>
      <div style="border-top:1px solid {C['line']};margin-top:18px;padding-top:12px;
           font-size:11px;color:{C['muted']};">
        This link is private to you — anyone who has it can read
        {html.escape(b.get('branch_name') or 'this branch', quote=False)}'s figures for this
        period, so please don't forward it. Sources: HiD Dashboard · Ads
        Platform · KOL records.
      </div>
    </div>
  </div>
</body></html>"""
