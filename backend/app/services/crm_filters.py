"""Single source of truth for the "what counts as a CRM reservation" rule.

Imported by CRM Dashboard, Marketing Activity, and the Weekly Report so
the three surfaces never drift again. To add a new CRM tag, edit the
pattern lists below.
"""
from sqlalchemy import or_

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
