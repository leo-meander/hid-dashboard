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


def crm_rate_plan_label_expr():
    """SQL expression that labels/groups a CRM reservation by its rate plan.

    Cloudbeds packs the campaign tag inside the room_type parentheses
    (e.g. 'Female Dorm* (CRM_May 2026 Event)') when rate_plan_name is
    blank, so we extract just the parenthesised tag — otherwise each base
    room type would get its own row instead of collapsing under one rate
    plan. Fallback order:
        rate_plan_name → first (…) substring in room_type → room_type → '(unknown)'

    Shared by Marketing Activity and the Weekly Report so the two surfaces
    group CRM reservations identically. PostgreSQL-specific: SUBSTRING(col
    FROM 'pattern') returns the first capture group or NULL; wrapped in
    literal_column because SQLAlchemy's func.substring emits the positional
    (int) form instead of the FROM form.
    """
    crm_tag = literal_column(r"substring(reservations.room_type from E'\\(([^)]+)\\)')")
    return func.coalesce(
        func.nullif(func.trim(Reservation.rate_plan_name), ""),
        func.nullif(func.trim(crm_tag), ""),
        func.nullif(func.trim(Reservation.room_type), ""),
        "(unknown)",
    ).label("rate_plan")
