"""Persona engine — derive a per-branch guest persona from reservation data.

Aggregates demographics (gender, age, country), booking behaviour (lead time,
length of stay, room vs dorm, channel mix, party size, cancellation rate) and
value (ADR, avg booking value) over a trailing window, and synthesises a short
human-readable headline from the dominant value in each dimension.

Demographic columns (gender, date_of_birth) are backfilled asynchronously from
Cloudbeds, so coverage is reported per dimension — the UI can show "based on
N% of bookings" and the headline omits demographic clauses when coverage is
too thin to be meaningful. See [[demographics-backfill]].
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from sqlalchemy import and_, case, func
from sqlalchemy.orm import Session

from app.models.branch import Branch
from app.models.reservation import Reservation
from app.services.metrics_engine import EXCLUDED_STATUSES

# Below this share of bookings carrying a real gender/age value, we treat the
# demographic signal as too sparse to headline (still returned as raw data).
DEMO_HEADLINE_MIN_COVERAGE = 0.15

AGE_BANDS = [
    ("16-24", 16, 24),
    ("25-34", 25, 34),
    ("35-44", 35, 44),
    ("45-54", 45, 54),
    ("55+", 55, 200),
]


def _pct(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0


def _valid_filter(branch_id: str, df: date, dt: date):
    """Base filter: this branch, check-in in window, not cancelled/no-show."""
    return and_(
        Reservation.branch_id == branch_id,
        Reservation.check_in_date >= df,
        Reservation.check_in_date <= dt,
        func.lower(Reservation.status).notin_(list(EXCLUDED_STATUSES)),
    )


def build_persona(db: Session, branch: Branch, df: date, dt: date) -> dict:
    bid = str(branch.id)
    base = _valid_filter(bid, df, dt)

    total = db.query(func.count(Reservation.id)).filter(base).scalar() or 0

    # ── Cancellation rate (over ALL statuses in the window) ──────────────
    all_in_window = db.query(func.count(Reservation.id)).filter(
        Reservation.branch_id == bid,
        Reservation.check_in_date >= df,
        Reservation.check_in_date <= dt,
    ).scalar() or 0
    cancelled = db.query(func.count(Reservation.id)).filter(
        Reservation.branch_id == bid,
        Reservation.check_in_date >= df,
        Reservation.check_in_date <= dt,
        func.lower(Reservation.status).in_(list(EXCLUDED_STATUSES)),
    ).scalar() or 0

    persona = {
        "branch_id": bid,
        "branch_name": branch.name,
        "currency": branch.currency or "VND",
        "window": {"from": df.isoformat(), "to": dt.isoformat()},
        "total_bookings": total,
        "cancellation_rate_pct": _pct(cancelled, all_in_window),
    }

    if total == 0:
        persona["empty"] = True
        persona["headline"] = "No bookings in this window yet."
        return persona

    # ── Gender ───────────────────────────────────────────────────────────
    g_rows = (
        db.query(Reservation.gender, func.count(Reservation.id))
        .filter(base)
        .group_by(Reservation.gender)
        .all()
    )
    g = {(k or "PENDING"): v for k, v in g_rows}
    male, female = g.get("M", 0), g.get("F", 0)
    na = g.get("N/A", 0)
    gender_known = male + female
    gender_attempted = gender_known + na  # rows Cloudbeds has answered (incl. N/A)
    persona["gender"] = {
        "male": male, "female": female, "na": na,
        "male_pct": _pct(male, gender_known),
        "female_pct": _pct(female, gender_known),
        "known": gender_known,
        "coverage_pct": _pct(gender_attempted, total),
    }

    # ── Age (from date_of_birth) ─────────────────────────────────────────
    age_expr = func.extract("year", func.age(Reservation.date_of_birth))
    age_base = and_(base, Reservation.date_of_birth.isnot(None),
                    age_expr >= 16, age_expr <= 100)
    band_cols = [
        func.count(case((and_(age_expr >= lo, age_expr <= hi), 1)))
        for _, lo, hi in AGE_BANDS
    ]
    age_row = db.query(
        func.count(Reservation.id), func.avg(age_expr), *band_cols
    ).filter(age_base).first()
    age_known = age_row[0] or 0
    avg_age = round(float(age_row[1]), 1) if age_row[1] is not None else None
    bands = [
        {"label": AGE_BANDS[i][0], "count": age_row[2 + i] or 0,
         "pct": _pct(age_row[2 + i] or 0, age_known)}
        for i in range(len(AGE_BANDS))
    ]
    persona["age"] = {
        "known": age_known, "coverage_pct": _pct(age_known, total),
        "avg": avg_age, "bands": bands,
    }

    # ── Top countries ────────────────────────────────────────────────────
    c_rows = (
        db.query(Reservation.guest_country, Reservation.guest_country_code,
                 func.count(Reservation.id).label("cnt"))
        .filter(base, Reservation.guest_country.isnot(None),
                Reservation.guest_country != "Unknown")
        .group_by(Reservation.guest_country, Reservation.guest_country_code)
        .order_by(func.count(Reservation.id).desc())
        .limit(8)
        .all()
    )
    country_known = sum(r.cnt for r in c_rows)
    persona["top_countries"] = [
        {"country": r.guest_country, "code": r.guest_country_code,
         "count": r.cnt, "pct": _pct(r.cnt, total)}
        for r in c_rows
    ]
    persona["country_coverage_pct"] = _pct(country_known, total)

    # ── Channel mix (source_category) ────────────────────────────────────
    s_rows = (
        db.query(Reservation.source_category, func.count(Reservation.id))
        .filter(base)
        .group_by(Reservation.source_category)
        .order_by(func.count(Reservation.id).desc())
        .all()
    )
    persona["source_mix"] = [
        {"category": k or "Unknown", "count": v, "pct": _pct(v, total)}
        for k, v in s_rows
    ]

    # ── Room vs Dorm ─────────────────────────────────────────────────────
    rt_rows = (
        db.query(Reservation.room_type_category, func.count(Reservation.id))
        .filter(base)
        .group_by(Reservation.room_type_category)
        .all()
    )
    rt = {(k or "Unknown"): v for k, v in rt_rows}
    room_n, dorm_n = rt.get("Room", 0), rt.get("Dorm", 0)
    persona["room_type"] = {
        "room": room_n, "dorm": dorm_n,
        "room_pct": _pct(room_n, room_n + dorm_n),
        "dorm_pct": _pct(dorm_n, room_n + dorm_n),
    }

    # ── Party size (adults) ──────────────────────────────────────────────
    party_row = db.query(
        func.count(case((Reservation.adults == 1, 1))),
        func.count(case((Reservation.adults == 2, 1))),
        func.count(case((Reservation.adults >= 3, 1))),
        func.avg(Reservation.adults),
    ).filter(base, Reservation.adults.isnot(None)).first()
    solo, couple, group = party_row[0] or 0, party_row[1] or 0, party_row[2] or 0
    party_known = solo + couple + group
    persona["party"] = {
        "solo": solo, "couple": couple, "group": group,
        "solo_pct": _pct(solo, party_known),
        "couple_pct": _pct(couple, party_known),
        "group_pct": _pct(group, party_known),
        "avg_adults": round(float(party_row[3]), 1) if party_row[3] is not None else None,
    }

    # ── Lead time (booking → check-in, days) ─────────────────────────────
    lead_expr = Reservation.check_in_date - Reservation.reservation_date
    lead_row = db.query(
        func.avg(lead_expr),
        func.percentile_cont(0.5).within_group(lead_expr.asc()),
    ).filter(base, Reservation.reservation_date.isnot(None), lead_expr >= 0).first()
    persona["lead_time"] = {
        "avg_days": round(float(lead_row[0]), 1) if lead_row[0] is not None else None,
        "median_days": round(float(lead_row[1]), 1) if lead_row[1] is not None else None,
    }

    # ── Length of stay (nights) ──────────────────────────────────────────
    los_row = db.query(
        func.avg(Reservation.nights),
        func.percentile_cont(0.5).within_group(Reservation.nights.asc()),
    ).filter(base, Reservation.nights > 0).first()
    persona["length_of_stay"] = {
        "avg_nights": round(float(los_row[0]), 1) if los_row[0] is not None else None,
        "median_nights": round(float(los_row[1]), 1) if los_row[1] is not None else None,
    }

    # ── Value (ADR + avg booking value) — exclude non-revenue sources ────
    from app.services.metrics_engine import EXCLUDED_SOURCES_REVENUE
    rev_base = and_(
        base,
        func.lower(func.coalesce(Reservation.source, "")).notin_(list(EXCLUDED_SOURCES_REVENUE)),
        Reservation.grand_total_native.isnot(None),
    )
    val_row = db.query(
        func.sum(Reservation.grand_total_native),
        func.sum(Reservation.grand_total_vnd),
        func.sum(Reservation.nights),
        func.count(Reservation.id),
    ).filter(rev_base).first()
    rev_native = float(val_row[0] or 0)
    rev_vnd = float(val_row[1] or 0)
    rev_nights = int(val_row[2] or 0)
    rev_count = int(val_row[3] or 0)
    persona["value"] = {
        "adr_native": round(rev_native / rev_nights, 2) if rev_nights else None,
        "adr_vnd": round(rev_vnd / rev_nights, 2) if rev_nights else None,
        "avg_booking_native": round(rev_native / rev_count, 2) if rev_count else None,
        "avg_booking_vnd": round(rev_vnd / rev_count, 2) if rev_count else None,
    }

    persona["headline"] = _build_headline(persona)
    return persona


def _dominant(items: list[dict], key_label: str, key_pct: str) -> Optional[tuple]:
    """Return (label, pct) of the highest-pct item, or None."""
    if not items:
        return None
    top = max(items, key=lambda x: x.get(key_pct, 0))
    return (top[key_label], top[key_pct]) if top.get(key_pct, 0) > 0 else None


def _build_headline(p: dict) -> str:
    """Compose a one-line persona from the dominant value in each dimension.

    English to match the dashboard UI. Demographic clauses are dropped when
    coverage is below DEMO_HEADLINE_MIN_COVERAGE so the line never asserts a
    gender/age skew the data can't yet support.
    """
    clauses: list[str] = []

    # Gender
    gen = p.get("gender", {})
    if gen.get("coverage_pct", 0) >= DEMO_HEADLINE_MIN_COVERAGE * 100 and gen.get("known"):
        if gen["female_pct"] >= gen["male_pct"]:
            clauses.append(f"mostly female ({gen['female_pct']:.0f}%)")
        else:
            clauses.append(f"mostly male ({gen['male_pct']:.0f}%)")

    # Age
    age = p.get("age", {})
    if age.get("coverage_pct", 0) >= DEMO_HEADLINE_MIN_COVERAGE * 100 and age.get("known"):
        dom = _dominant(age["bands"], "label", "pct")
        if dom:
            clauses.append(f"aged {dom[0]}")

    # Party
    party = p.get("party", {})
    party_label = {
        "solo_pct": "travelling solo",
        "couple_pct": "travelling as couples",
        "group_pct": "in groups of 3+",
    }
    party_best = max(
        ["solo_pct", "couple_pct", "group_pct"],
        key=lambda k: party.get(k, 0),
    )
    if party.get(party_best, 0) > 0:
        clauses.append(party_label[party_best])

    # Channel
    chan = _dominant(p.get("source_mix", []), "category", "pct")
    if chan:
        verb = "booking direct" if chan[0] == "Direct" else f"booking via {chan[0]}"
        clauses.append(verb)

    # Length of stay
    los = p.get("length_of_stay", {}).get("median_nights") or p.get("length_of_stay", {}).get("avg_nights")
    if los:
        n = round(los)
        clauses.append(f"staying {n} night{'s' if n != 1 else ''}")

    # Room type
    rt = p.get("room_type", {})
    if (rt.get("room", 0) + rt.get("dorm", 0)) > 0:
        clauses.append("in private rooms" if rt["room_pct"] >= rt["dorm_pct"] else "in dorms")

    # Country
    if p.get("top_countries"):
        clauses.append(f"mostly from {p['top_countries'][0]['country']}")

    if not clauses:
        return "Not enough data to characterise this branch yet."
    return "Guests are " + ", ".join(clauses) + "."


def build_all_personas(
    db: Session,
    branch_id: Optional[str] = None,
    months: int = 12,
) -> dict:
    """Build personas for one branch (if branch_id) or all active branches."""
    dt = date.today()
    df = dt - timedelta(days=round(months * 30.44))

    q = db.query(Branch).filter_by(is_active=True)
    if branch_id:
        q = q.filter(Branch.id == branch_id)
    branches = q.order_by(Branch.name).all()

    personas = [build_persona(db, b, df, dt) for b in branches]
    return {
        "window": {"from": df.isoformat(), "to": dt.isoformat(), "months": months},
        "personas": personas,
    }
