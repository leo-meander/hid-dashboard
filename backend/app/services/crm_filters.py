"""Single source of truth for the "what counts as a CRM reservation" rule.

Imported by CRM Dashboard, Marketing Activity, and the Weekly Report so
the three surfaces never drift again. To add a new CRM tag, edit the
pattern lists below.
"""
from sqlalchemy import func, literal_column, or_

from app.models.reservation import Reservation

# Tags that signal CRM activity on either room_type or rate_plan_name.
# Cloudbeds sometimes packs the campaign tag inside the room_type
# parentheses (e.g. "Female Dorm* (CRM_May 2026 Event)") and sometimes
# in rate_plan_name — both surfaces have to be checked.
CRM_TAGS = (
    "CRM",
    "MEANDER'S FRIEND",
    "Travel guide",
    "Grand Open",
    "Extension Promotion",
    "WELCOME",
)

# Reserved for tags confirmed to appear only on rate_plan_name (no room_type packing).
RATE_PLAN_ONLY_TAGS = ()


def crm_reservation_filter():
    """Return an or_() clause matching reservations counted as CRM."""
    clauses = []
    for tag in CRM_TAGS:
        like = f"%{tag}%"
        clauses.append(Reservation.room_type.ilike(like))
        clauses.append(Reservation.rate_plan_name.ilike(like))
    for tag in RATE_PLAN_ONLY_TAGS:
        clauses.append(Reservation.rate_plan_name.ilike(f"%{tag}%"))
    return or_(*clauses)


def rate_plan_pattern_filter(patterns):
    """Return an or_() clause matching any of `patterns` on a reservation.

    Same rule the Rate Plan Quota engine counts by: a booking belongs to a
    rate plan when EITHER rate_plan_name OR room_type contains the pattern
    (case-insensitive) — Cloudbeds packs the campaign tag into room_type
    when the rate plan field is blank. Returns None when nothing usable is
    passed so callers can skip the filter entirely.
    """
    clauses = []
    for pattern in patterns or []:
        pattern = (pattern or "").strip()
        if not pattern:
            continue
        like = f"%{pattern}%"
        clauses.append(Reservation.rate_plan_name.ilike(like))
        clauses.append(Reservation.room_type.ilike(like))
    return or_(*clauses) if clauses else None


def crm_rate_plan_value_expr():
    """The rate-plan expression itself, without the ``.label()``.

    Same value ``crm_rate_plan_label_expr`` groups by — kept separate because a
    labelled expression cannot be reused in a WHERE clause. Drilling into one
    rate plan has to filter on exactly what the grouping produced, or the
    drill-down would select a different set of rows than the row it opened.

    The tag pattern tolerates ONE level of nesting inside the group, because
    the plan names carry their own parenthesised qualifier:

        "8 Beds Mixed Dorm Shared Bathroom (Extension Promotion (>2 night))"
                                            ^--------- rate plan ---------^

    The body is "anything but a bracket, OR a complete bracketed group", so the
    match runs past the inner ``)`` to the real closer. The previous
    ``\\(([^)]+)\\)`` forbade brackets in the body so it could find a closer,
    and therefore stopped at the INNER one — labelling that row
    "Extension Promotion (>2 night", a name with no closing bracket that reads
    like real data. Every non-nested shape resolves identically to before: a
    plain tag, a tag with trailing text after it, and a multi-room room_type
    (where the first group still wins) are all unchanged. Two levels of nesting
    would still truncate; no such room_type exists.
    """
    crm_tag = literal_column(
        r"substring(reservations.room_type from E'\\(((?:[^()]|\\([^()]*\\))+)\\)')"
    )
    return func.coalesce(
        func.nullif(func.trim(Reservation.rate_plan_name), ""),
        func.nullif(func.trim(crm_tag), ""),
        func.nullif(func.trim(Reservation.room_type), ""),
        "(unknown)",
    )


def crm_rate_plan_label_expr():
    """SQL expression that labels/groups a CRM reservation by its rate plan.

    Cloudbeds packs the campaign tag inside the room_type parentheses
    (e.g. 'Female Dorm* (CRM_May 2026 Event)') when rate_plan_name is
    blank, so we extract just the parenthesised tag — otherwise each base
    room type would get its own row instead of collapsing under one rate
    plan. Fallback order:
        rate_plan_name → first (…) group in room_type → room_type → '(unknown)'

    Shared by Marketing Activity and the Weekly Report so the two surfaces
    group CRM reservations identically. PostgreSQL-specific: SUBSTRING(col
    FROM 'pattern') returns the first capture group or NULL; wrapped in
    literal_column because SQLAlchemy's func.substring emits the positional
    (int) form instead of the FROM form.
    """
    return crm_rate_plan_value_expr().label("rate_plan")
