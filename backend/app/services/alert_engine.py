"""Alert engine — evaluates hotel performance metrics against configurable rules.

Runs daily at 03:15 ICT (after nightly metrics compute at 03:00).
Generates alerts with actionable recommendations for each branch.
"""
import calendar
import logging
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, and_, extract
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.alert import AlertRule, AlertHistory
from app.models.branch import Branch
from app.models.daily_metrics import DailyMetrics
from app.models.kpi import KPITarget
from app.models.reservation import Reservation

logger = logging.getLogger(__name__)

# ── Recommendation templates ─────────────────────────────────────────────────

RECOMMENDATIONS = {
    "revenue_pace": (
        "Revenue is {dev:.1f}% behind monthly pace for {branch}. "
        "Consider launching a flash promotion, increasing ad spend on high-converting channels, "
        "or reviewing room pricing for the remainder of the month."
    ),
    "adr_drop_7d": (
        "ADR dropped {dev:.1f}% vs the 7-day average at {branch}. "
        "Check if discounted OTA channels are spiking, review rate parity across platforms, "
        "and ensure direct booking rates remain competitive."
    ),
    "revpar_below_forecast": (
        "RevPAR is {dev:.1f}% below forecast at {branch}. "
        "Optimize channel mix to favor higher-rate channels, consider dynamic pricing adjustments, "
        "and review upcoming demand drivers."
    ),
    "revenue_yoy_decline": (
        "Revenue declined {dev:.1f}% year-over-year at {branch}. "
        "Investigate market shifts, check competitor pricing, and review whether seasonal "
        "patterns have changed for this period."
    ),
    "occ_below_target": (
        "Occupancy is {dev:.1f}% below the predicted target at {branch}. "
        "Push last-minute OTA deals, activate email marketing campaigns to past guests, "
        "and consider short-term promotional packages."
    ),
    "occ_sudden_drop": (
        "Occupancy dropped {dev:.1f} percentage points day-over-day at {branch}. "
        "Check for local event cancellations, system data errors, or sudden market shifts. "
        "Review upcoming bookings pipeline."
    ),
    "cancellation_spike": (
        "Cancellation rate spiked to {dev:.1f}x the 30-day average at {branch}. "
        "Review cancellation sources (OTA vs Direct), consider tightening cancellation policies, "
        "and reach out to guests with upcoming reservations to confirm."
    ),
    "booking_pace_decline": (
        "New booking pace is {dev:.1f}% below the 7-day average at {branch}. "
        "Increase marketing spend on retargeting campaigns, launch email campaigns to warm leads, "
        "and review website conversion funnel for issues."
    ),
    "net_booking_decline": (
        "Net bookings (new minus cancellations) are trending negative at {branch}. "
        "Urgently review pricing strategy, check for competitor rate undercutting, "
        "and activate win-back campaigns for recently cancelled bookings."
    ),
    "ota_dependency": (
        "OTA share reached {dev:.1f}% of room nights at {branch}. "
        "Invest in direct booking campaigns, improve website booking engine experience, "
        "and create exclusive direct-booking perks to reduce commission costs."
    ),
    "ota_commission_opportunity": (
        "Week of {extra} is only {dev:.1f}% booked at {branch} — a soft week with room to fill. "
        "Raise OTA commission for that week so OTAs surface {branch} to more potential guests and accelerate "
        "pickup while there is still lead time. As occupancy firms up, step room rates back up to protect ADR — "
        "the extra commission is the cost of buying incremental volume now, and the ADR recovery offsets it."
    ),
    "country_booking_drop": (
        "Top source country {extra} dropped {dev:.1f}% YoY at {branch}. "
        "Investigate market-specific issues (visa changes, airline routes, competitor pricing). "
        "Adjust marketing spend and ad targeting for this market."
    ),
    "country_surge": (
        "Emerging market {extra} surged {dev:.1f}% YoY at {branch}. "
        "Opportunity: increase targeted ads for this market, create country-specific landing pages, "
        "and consider language-specific content to capture momentum."
    ),
    "country_concentration": (
        "Country {extra} accounts for {dev:.1f}% of room nights at {branch}. "
        "Diversify marketing to reduce dependency on a single market. "
        "Activate campaigns targeting secondary and emerging source countries."
    ),
}


# ── Main entry point ─────────────────────────────────────────────────────────

def run_daily_alerts(session_factory) -> None:
    """Scheduler entry point — evaluate all active rules for all branches."""
    db = session_factory()
    try:
        today = date.today()
        yesterday = today - timedelta(days=1)
        branches = db.query(Branch).filter_by(is_active=True).all()
        rules = db.query(AlertRule).filter_by(is_active=True).all()

        if not rules:
            logger.info("No active alert rules — skipping")
            return

        # Clear previous alerts for this date so rules that no longer trigger
        # don't leave stale alerts behind (e.g. excluded countries)
        db.query(AlertHistory).filter(
            AlertHistory.alert_date == yesterday,
            AlertHistory.status == "active",
        ).delete(synchronize_session=False)
        db.flush()

        total_alerts = 0
        for branch in branches:
            branch_rules = [r for r in rules if r.branch_id is None or r.branch_id == branch.id]
            alerts = _evaluate_branch(db, branch, yesterday, branch_rules)
            total_alerts += len(alerts)

        db.commit()
        logger.info("Alert evaluation complete — %d alerts across %d branches", total_alerts, len(branches))

    except Exception:
        db.rollback()
        logger.exception("Alert evaluation job failed")
    finally:
        db.close()


# ── Branch evaluation ────────────────────────────────────────────────────────

EVALUATORS = {}  # metric_key → function

# Countries to exclude from market alerts (no useful insight)
EXCLUDED_COUNTRIES = {"Others", "Other", "Unknown", ""}


def _register(metric_key):
    def decorator(fn):
        EVALUATORS[metric_key] = fn
        return fn
    return decorator


def _evaluate_branch(db: Session, branch: Branch, target_date: date, rules: list[AlertRule]) -> list[AlertHistory]:
    alerts = []
    for rule in rules:
        evaluator = EVALUATORS.get(rule.metric_key)
        if not evaluator:
            continue
        try:
            alert = evaluator(db, branch, target_date, rule)
            if alert:
                alerts.append(alert)
        except Exception:
            logger.exception("Evaluator %s failed for branch %s", rule.metric_key, branch.name)
    return alerts


# ── Helpers ──────────────────────────────────────────────────────────────────

def _get_rolling_avg(db: Session, branch_id, column_name: str, days: int, before_date: date):
    """Compute rolling average of a daily_metrics column over `days` before `before_date`."""
    col = getattr(DailyMetrics, column_name)
    start = before_date - timedelta(days=days)
    result = (
        db.query(func.avg(col))
        .filter(
            DailyMetrics.branch_id == branch_id,
            DailyMetrics.date >= start,
            DailyMetrics.date < before_date,
        )
        .scalar()
    )
    return float(result) if result is not None else None


def _get_metric(db: Session, branch_id, target_date: date) -> DailyMetrics | None:
    return (
        db.query(DailyMetrics)
        .filter_by(branch_id=branch_id, date=target_date)
        .first()
    )


def _upsert_alert(db: Session, *, branch_id, metric_key, alert_date, severity, category,
                   current_value, threshold_value, baseline_value, deviation_pct,
                   message, recommendation) -> AlertHistory:
    """Insert or update an alert — idempotent on (branch_id, metric_key, alert_date)."""
    stmt = pg_insert(AlertHistory).values(
        branch_id=branch_id,
        metric_key=metric_key,
        alert_date=alert_date,
        severity=severity,
        category=category,
        current_value=current_value,
        threshold_value=threshold_value,
        baseline_value=baseline_value,
        deviation_pct=deviation_pct,
        message=message,
        recommendation=recommendation,
        status="active",
        email_sent=False,
    ).on_conflict_do_update(
        constraint="uq_alert_history_branch_metric_date",
        set_={
            "severity": severity,
            "current_value": current_value,
            "threshold_value": threshold_value,
            "baseline_value": baseline_value,
            "deviation_pct": deviation_pct,
            "message": message,
            "recommendation": recommendation,
        },
    ).returning(AlertHistory.id)

    result = db.execute(stmt)
    alert_id = result.scalar_one()
    db.flush()
    return db.query(AlertHistory).get(alert_id)


def _fmt_rec(metric_key: str, branch_name: str, deviation: float, extra: str = "") -> str:
    template = RECOMMENDATIONS.get(metric_key, "Review {branch} performance for {metric_key}.")
    return template.format(dev=deviation, branch=branch_name, extra=extra, metric_key=metric_key)


# ── Revenue evaluators ───────────────────────────────────────────────────────

@_register("revenue_pace")
def _check_revenue_pace(db: Session, branch: Branch, target_date: date, rule: AlertRule):
    """Actual MTD revenue vs expected pace from KPI target."""
    kpi = (
        db.query(KPITarget)
        .filter_by(branch_id=branch.id, year=target_date.year, month=target_date.month)
        .first()
    )
    if not kpi or not kpi.target_revenue_native:
        return None

    days_in_month = calendar.monthrange(target_date.year, target_date.month)[1]
    day_of_month = target_date.day
    first_of_month = target_date.replace(day=1)

    mtd_revenue = (
        db.query(func.sum(DailyMetrics.revenue_native))
        .filter(
            DailyMetrics.branch_id == branch.id,
            DailyMetrics.date >= first_of_month,
            DailyMetrics.date <= target_date,
        )
        .scalar()
    ) or Decimal("0")

    expected_pace = float(kpi.target_revenue_native) * (day_of_month / days_in_month)
    if expected_pace <= 0:
        return None

    pace_ratio = float(mtd_revenue) / expected_pace
    threshold = float(rule.threshold_value)

    if pace_ratio < threshold:
        deviation = (1 - pace_ratio) * 100
        return _upsert_alert(
            db, branch_id=branch.id, metric_key=rule.metric_key, alert_date=target_date,
            severity=rule.severity, category=rule.category,
            current_value=float(mtd_revenue), threshold_value=expected_pace,
            baseline_value=float(kpi.target_revenue_native),
            deviation_pct=deviation,
            message=f"MTD revenue ({float(mtd_revenue):,.0f}) is {deviation:.1f}% behind expected pace ({expected_pace:,.0f})",
            recommendation=_fmt_rec(rule.metric_key, branch.name, deviation),
        )
    return None


@_register("adr_drop_7d")
def _check_adr_drop(db: Session, branch: Branch, target_date: date, rule: AlertRule):
    """ADR drops more than threshold vs 7-day rolling average."""
    metric = _get_metric(db, branch.id, target_date)
    if not metric or not metric.adr_native:
        return None

    avg_adr = _get_rolling_avg(db, branch.id, "adr_native", rule.lookback_days or 7, target_date)
    if not avg_adr or avg_adr <= 0:
        return None

    current = float(metric.adr_native)
    drop_pct = (avg_adr - current) / avg_adr
    threshold = float(rule.threshold_value)

    if drop_pct > threshold:
        deviation = drop_pct * 100
        return _upsert_alert(
            db, branch_id=branch.id, metric_key=rule.metric_key, alert_date=target_date,
            severity=rule.severity, category=rule.category,
            current_value=current, threshold_value=avg_adr * (1 - threshold),
            baseline_value=avg_adr, deviation_pct=deviation,
            message=f"ADR ({current:,.0f}) dropped {deviation:.1f}% vs 7-day avg ({avg_adr:,.0f})",
            recommendation=_fmt_rec(rule.metric_key, branch.name, deviation),
        )
    return None


@_register("revpar_below_forecast")
def _check_revpar_below_forecast(db: Session, branch: Branch, target_date: date, rule: AlertRule):
    """RevPAR below predicted threshold."""
    metric = _get_metric(db, branch.id, target_date)
    if not metric or not metric.revpar_native:
        return None

    kpi = (
        db.query(KPITarget)
        .filter_by(branch_id=branch.id, year=target_date.year, month=target_date.month)
        .first()
    )
    if not kpi or not kpi.predicted_occ_pct or not kpi.target_revenue_native:
        return None

    days_in_month = calendar.monthrange(target_date.year, target_date.month)[1]
    predicted_daily_rev = float(kpi.target_revenue_native) / days_in_month
    predicted_revpar = predicted_daily_rev / branch.total_rooms if branch.total_rooms else 0
    if predicted_revpar <= 0:
        return None

    current = float(metric.revpar_native)
    ratio = current / predicted_revpar
    threshold = float(rule.threshold_value)

    if ratio < threshold:
        deviation = (1 - ratio) * 100
        return _upsert_alert(
            db, branch_id=branch.id, metric_key=rule.metric_key, alert_date=target_date,
            severity=rule.severity, category=rule.category,
            current_value=current, threshold_value=predicted_revpar * threshold,
            baseline_value=predicted_revpar, deviation_pct=deviation,
            message=f"RevPAR ({current:,.0f}) is {deviation:.1f}% below forecast ({predicted_revpar:,.0f})",
            recommendation=_fmt_rec(rule.metric_key, branch.name, deviation),
        )
    return None


@_register("revenue_yoy_decline")
def _check_revenue_yoy_decline(db: Session, branch: Branch, target_date: date, rule: AlertRule):
    """MTD revenue decline vs same month last year."""
    first_of_month = target_date.replace(day=1)
    last_year_start = first_of_month.replace(year=first_of_month.year - 1)
    last_year_end = target_date.replace(year=target_date.year - 1)

    try:
        # Validate last year dates exist
        _ = last_year_start, last_year_end
    except ValueError:
        return None

    current_rev = (
        db.query(func.sum(DailyMetrics.revenue_native))
        .filter(DailyMetrics.branch_id == branch.id,
                DailyMetrics.date >= first_of_month,
                DailyMetrics.date <= target_date)
        .scalar()
    ) or Decimal("0")

    last_year_rev = (
        db.query(func.sum(DailyMetrics.revenue_native))
        .filter(DailyMetrics.branch_id == branch.id,
                DailyMetrics.date >= last_year_start,
                DailyMetrics.date <= last_year_end)
        .scalar()
    ) or Decimal("0")

    if float(last_year_rev) <= 0:
        return None

    decline_pct = (float(last_year_rev) - float(current_rev)) / float(last_year_rev)
    threshold = float(rule.threshold_value)

    if decline_pct > threshold:
        deviation = decline_pct * 100
        return _upsert_alert(
            db, branch_id=branch.id, metric_key=rule.metric_key, alert_date=target_date,
            severity=rule.severity, category=rule.category,
            current_value=float(current_rev), threshold_value=float(last_year_rev) * (1 - threshold),
            baseline_value=float(last_year_rev), deviation_pct=deviation,
            message=f"MTD revenue ({float(current_rev):,.0f}) is {deviation:.1f}% below last year ({float(last_year_rev):,.0f})",
            recommendation=_fmt_rec(rule.metric_key, branch.name, deviation),
        )
    return None


# ── Occupancy evaluators ─────────────────────────────────────────────────────

@_register("occ_below_target")
def _check_occ_below_target(db: Session, branch: Branch, target_date: date, rule: AlertRule):
    metric = _get_metric(db, branch.id, target_date)
    if not metric or metric.occ_pct is None:
        return None

    kpi = (
        db.query(KPITarget)
        .filter_by(branch_id=branch.id, year=target_date.year, month=target_date.month)
        .first()
    )
    if not kpi or not kpi.predicted_occ_pct:
        return None

    current = float(metric.occ_pct)
    target = float(kpi.predicted_occ_pct)
    threshold = float(rule.threshold_value)

    if target > 0 and current / target < threshold:
        deviation = (1 - current / target) * 100
        return _upsert_alert(
            db, branch_id=branch.id, metric_key=rule.metric_key, alert_date=target_date,
            severity=rule.severity, category=rule.category,
            current_value=current, threshold_value=target * threshold,
            baseline_value=target, deviation_pct=deviation,
            message=f"OCC ({current * 100:.1f}%) is {deviation:.1f}% below target ({target * 100:.1f}%)",
            recommendation=_fmt_rec(rule.metric_key, branch.name, deviation),
        )
    return None


@_register("occ_sudden_drop")
def _check_occ_sudden_drop(db: Session, branch: Branch, target_date: date, rule: AlertRule):
    metric_today = _get_metric(db, branch.id, target_date)
    metric_prev = _get_metric(db, branch.id, target_date - timedelta(days=1))
    if not metric_today or not metric_prev:
        return None
    if metric_today.occ_pct is None or metric_prev.occ_pct is None:
        return None

    current = float(metric_today.occ_pct)
    previous = float(metric_prev.occ_pct)
    drop = previous - current
    threshold = float(rule.threshold_value)

    if drop > threshold:
        deviation = drop * 100  # percentage points
        return _upsert_alert(
            db, branch_id=branch.id, metric_key=rule.metric_key, alert_date=target_date,
            severity=rule.severity, category=rule.category,
            current_value=current, threshold_value=previous - threshold,
            baseline_value=previous, deviation_pct=deviation,
            message=f"OCC dropped {deviation:.1f}pp day-over-day ({previous * 100:.1f}% → {current * 100:.1f}%)",
            recommendation=_fmt_rec(rule.metric_key, branch.name, deviation),
        )
    return None


# ── Bookings & Cancellations ─────────────────────────────────────────────────

@_register("cancellation_spike")
def _check_cancellation_spike(db: Session, branch: Branch, target_date: date, rule: AlertRule):
    metric = _get_metric(db, branch.id, target_date)
    if not metric or metric.cancellation_pct is None:
        return None

    avg_cancel = _get_rolling_avg(db, branch.id, "cancellation_pct", rule.lookback_days or 30, target_date)
    if avg_cancel is None or avg_cancel <= 0:
        return None

    current = float(metric.cancellation_pct)
    threshold_multiplier = float(rule.threshold_value)
    ratio = current / avg_cancel

    if ratio > threshold_multiplier:
        return _upsert_alert(
            db, branch_id=branch.id, metric_key=rule.metric_key, alert_date=target_date,
            severity=rule.severity, category=rule.category,
            current_value=current, threshold_value=avg_cancel * threshold_multiplier,
            baseline_value=avg_cancel, deviation_pct=ratio,
            message=f"Cancellation rate ({current * 100:.1f}%) is {ratio:.1f}x the 30-day avg ({avg_cancel * 100:.1f}%)",
            recommendation=_fmt_rec(rule.metric_key, branch.name, ratio),
        )
    return None


@_register("booking_pace_decline")
def _check_booking_pace_decline(db: Session, branch: Branch, target_date: date, rule: AlertRule):
    metric = _get_metric(db, branch.id, target_date)
    if not metric or metric.new_bookings is None:
        return None

    avg_bookings = _get_rolling_avg(db, branch.id, "new_bookings", rule.lookback_days or 7, target_date)
    if avg_bookings is None or avg_bookings <= 0:
        return None

    current = float(metric.new_bookings)
    threshold = float(rule.threshold_value)
    ratio = current / avg_bookings

    if ratio < threshold:
        deviation = (1 - ratio) * 100
        return _upsert_alert(
            db, branch_id=branch.id, metric_key=rule.metric_key, alert_date=target_date,
            severity=rule.severity, category=rule.category,
            current_value=current, threshold_value=avg_bookings * threshold,
            baseline_value=avg_bookings, deviation_pct=deviation,
            message=f"New bookings ({int(current)}) is {deviation:.1f}% below 7-day avg ({avg_bookings:.1f})",
            recommendation=_fmt_rec(rule.metric_key, branch.name, deviation),
        )
    return None


@_register("net_booking_decline")
def _check_net_booking_decline(db: Session, branch: Branch, target_date: date, rule: AlertRule):
    """Net bookings (new - cancellations) trending negative over lookback window."""
    lookback = rule.lookback_days or 7
    start = target_date - timedelta(days=lookback)

    rows = (
        db.query(DailyMetrics.new_bookings, DailyMetrics.cancellations)
        .filter(
            DailyMetrics.branch_id == branch.id,
            DailyMetrics.date >= start,
            DailyMetrics.date <= target_date,
        )
        .all()
    )
    if len(rows) < 3:
        return None

    net_values = [(r.new_bookings or 0) - (r.cancellations or 0) for r in rows]
    avg_net = sum(net_values) / len(net_values)

    if avg_net < float(rule.threshold_value):
        return _upsert_alert(
            db, branch_id=branch.id, metric_key=rule.metric_key, alert_date=target_date,
            severity=rule.severity, category=rule.category,
            current_value=avg_net, threshold_value=float(rule.threshold_value),
            baseline_value=None, deviation_pct=avg_net,
            message=f"Net bookings averaged {avg_net:.1f}/day over {lookback} days (negative trend)",
            recommendation=_fmt_rec(rule.metric_key, branch.name, abs(avg_net)),
        )
    return None


# ── Channel evaluators ───────────────────────────────────────────────────────

@_register("ota_dependency")
def _check_ota_dependency(db: Session, branch: Branch, target_date: date, rule: AlertRule):
    lookback = rule.lookback_days or 30
    start = target_date - timedelta(days=lookback)

    total_nights = (
        db.query(func.count(Reservation.id))
        .filter(
            Reservation.branch_id == branch.id,
            Reservation.check_in_date >= start,
            Reservation.check_in_date <= target_date,
            ~Reservation.status.in_(["Cancelled", "No-show", "No Show"]),
        )
        .scalar()
    ) or 0

    if total_nights == 0:
        return None

    ota_nights = (
        db.query(func.count(Reservation.id))
        .filter(
            Reservation.branch_id == branch.id,
            Reservation.check_in_date >= start,
            Reservation.check_in_date <= target_date,
            ~Reservation.status.in_(["Cancelled", "No-show", "No Show"]),
            Reservation.source_category == "OTA",
        )
        .scalar()
    ) or 0

    ota_pct = ota_nights / total_nights
    threshold = float(rule.threshold_value)

    if ota_pct > threshold:
        deviation = ota_pct * 100
        return _upsert_alert(
            db, branch_id=branch.id, metric_key=rule.metric_key, alert_date=target_date,
            severity=rule.severity, category=rule.category,
            current_value=ota_pct, threshold_value=threshold,
            baseline_value=None, deviation_pct=deviation,
            message=f"OTA share is {deviation:.1f}% of room nights (last {lookback} days)",
            recommendation=_fmt_rec(rule.metric_key, branch.name, deviation),
        )
    return None


# Forward-looking OTA commission opportunity ──────────────────────────────────
# How far ahead to scan for soft weeks, and the minimum lead time a week needs
# for an OTA-commission push to still move pickup before guests arrive.
_OTA_OPP_HORIZON_DAYS = 56   # scan up to 8 weeks out
_OTA_OPP_MIN_LEAD_DAYS = 10  # skip weeks too close to act on

_CANCELLED_STATUSES = ["Cancelled", "No-show", "No Show"]


@_register("ota_commission_opportunity")
def _check_ota_commission_opportunity(db: Session, branch: Branch, target_date: date, rule: AlertRule):
    """Forward-looking: flag the soonest upcoming week whose on-the-books
    occupancy is low enough that raising OTA commission could drive incremental
    demand while there is still lead time to fill rooms.

    Trigger: projected occupancy for an upcoming week < threshold (e.g. 40%).
    Strategy: extra OTA commission buys visibility/volume now; once the week
    fills, rates can step back up to protect ADR. Reports the single nearest
    actionable soft week so the recommendation names a specific week.
    """
    if not branch.total_rooms:
        return None

    threshold = float(rule.threshold_value)  # e.g. 0.40
    available_per_week = branch.total_rooms * 7

    # First Monday at least MIN_LEAD_DAYS out (snap forward to Monday).
    first_day = target_date + timedelta(days=_OTA_OPP_MIN_LEAD_DAYS)
    week_start = first_day + timedelta(days=(7 - first_day.weekday()) % 7)
    horizon_end = target_date + timedelta(days=_OTA_OPP_HORIZON_DAYS)

    while week_start <= horizon_end:
        week_end_excl = week_start + timedelta(days=7)

        # Occupied room-nights overlapping the week — accurate for multi-night
        # stays: sum of LEAST(checkout, week_end) - GREATEST(checkin, week_start).
        overlap = func.greatest(
            0,
            func.least(Reservation.check_out_date, week_end_excl)
            - func.greatest(Reservation.check_in_date, week_start),
        )
        booked_nights = (
            db.query(func.coalesce(func.sum(overlap), 0))
            .filter(
                Reservation.branch_id == branch.id,
                Reservation.check_in_date < week_end_excl,
                Reservation.check_out_date > week_start,
                ~Reservation.status.in_(_CANCELLED_STATUSES),
            )
            .scalar()
        ) or 0

        projected_occ = float(booked_nights) / available_per_week

        if projected_occ < threshold:
            occ_pct = projected_occ * 100
            week_label = week_start.isoformat()
            lead_days = (week_start - target_date).days
            return _upsert_alert(
                db, branch_id=branch.id, metric_key=rule.metric_key, alert_date=target_date,
                severity=rule.severity, category=rule.category,
                current_value=projected_occ, threshold_value=threshold,
                baseline_value=None, deviation_pct=occ_pct,
                message=(
                    f"Week of {week_label} is only {occ_pct:.1f}% booked "
                    f"({int(booked_nights)}/{available_per_week} room-nights) — below the "
                    f"{threshold * 100:.0f}% floor with {lead_days} days lead time to fill it"
                ),
                recommendation=_fmt_rec(rule.metric_key, branch.name, occ_pct, extra=week_label),
            )

        week_start = week_end_excl

    return None


# ── Guest Market evaluators ──────────────────────────────────────────────────

@_register("country_booking_drop")
def _check_country_booking_drop(db: Session, branch: Branch, target_date: date, rule: AlertRule):
    """Top-5 source countries: alert if any dropped >25% YoY same month."""
    month = target_date.month
    year = target_date.year

    # Current year country breakdown
    current_countries = (
        db.query(
            Reservation.guest_country_code,
            func.count(Reservation.id).label("nights"),
        )
        .filter(
            Reservation.branch_id == branch.id,
            extract("year", Reservation.check_in_date) == year,
            extract("month", Reservation.check_in_date) == month,
            ~Reservation.status.in_(["Cancelled", "No-show", "No Show"]),
        )
        .group_by(Reservation.guest_country_code)
        .order_by(func.count(Reservation.id).desc())
        .limit(5)
        .all()
    )

    alerts_created = []
    threshold = float(rule.threshold_value)

    for country_code, current_nights in current_countries:
        if not country_code or country_code in EXCLUDED_COUNTRIES or current_nights < 5:
            continue

        # Last year same month
        last_year_nights = (
            db.query(func.count(Reservation.id))
            .filter(
                Reservation.branch_id == branch.id,
                Reservation.guest_country_code == country_code,
                extract("year", Reservation.check_in_date) == year - 1,
                extract("month", Reservation.check_in_date) == month,
                ~Reservation.status.in_(["Cancelled", "No-show", "No Show"]),
            )
            .scalar()
        ) or 0

        if last_year_nights <= 0:
            continue

        decline_pct = (last_year_nights - current_nights) / last_year_nights
        if decline_pct > threshold:
            deviation = decline_pct * 100
            # Use a composite metric key to allow multiple country alerts
            composite_key = f"{rule.metric_key}_{country_code}"
            alert = _upsert_alert(
                db, branch_id=branch.id, metric_key=composite_key, alert_date=target_date,
                severity=rule.severity, category=rule.category,
                current_value=current_nights, threshold_value=last_year_nights * (1 - threshold),
                baseline_value=last_year_nights, deviation_pct=deviation,
                message=f"{country_code} bookings dropped {deviation:.1f}% YoY ({last_year_nights} → {current_nights})",
                recommendation=_fmt_rec(rule.metric_key, branch.name, deviation, extra=country_code),
            )
            alerts_created.append(alert)

    return alerts_created[0] if alerts_created else None


@_register("country_surge")
def _check_country_surge(db: Session, branch: Branch, target_date: date, rule: AlertRule):
    """Detect emerging countries with >50% YoY growth and >10 room nights."""
    month = target_date.month
    year = target_date.year

    current_countries = (
        db.query(
            Reservation.guest_country_code,
            func.count(Reservation.id).label("nights"),
        )
        .filter(
            Reservation.branch_id == branch.id,
            extract("year", Reservation.check_in_date) == year,
            extract("month", Reservation.check_in_date) == month,
            ~Reservation.status.in_(["Cancelled", "No-show", "No Show"]),
        )
        .group_by(Reservation.guest_country_code)
        .having(func.count(Reservation.id) > 10)
        .all()
    )

    threshold = float(rule.threshold_value)
    alerts_created = []

    for country_code, current_nights in current_countries:
        if not country_code or country_code in EXCLUDED_COUNTRIES:
            continue

        last_year_nights = (
            db.query(func.count(Reservation.id))
            .filter(
                Reservation.branch_id == branch.id,
                Reservation.guest_country_code == country_code,
                extract("year", Reservation.check_in_date) == year - 1,
                extract("month", Reservation.check_in_date) == month,
                ~Reservation.status.in_(["Cancelled", "No-show", "No Show"]),
            )
            .scalar()
        ) or 0

        if last_year_nights <= 0:
            # No YoY baseline — skip (avoids noise for new branches)
            continue

        growth_pct = (current_nights - last_year_nights) / last_year_nights
        if growth_pct > threshold:
            deviation = growth_pct * 100
            composite_key = f"{rule.metric_key}_{country_code}"
            alert = _upsert_alert(
                db, branch_id=branch.id, metric_key=composite_key, alert_date=target_date,
                severity=rule.severity, category=rule.category,
                current_value=current_nights, threshold_value=last_year_nights * (1 + threshold),
                baseline_value=last_year_nights, deviation_pct=deviation,
                message=f"{country_code} surged {deviation:.1f}% YoY ({last_year_nights} → {current_nights})",
                recommendation=_fmt_rec(rule.metric_key, branch.name, deviation, extra=country_code),
            )
            alerts_created.append(alert)

    return alerts_created[0] if alerts_created else None


@_register("country_concentration")
def _check_country_concentration(db: Session, branch: Branch, target_date: date, rule: AlertRule):
    """Alert if a single country >40% of total room nights in last 30 days."""
    lookback = rule.lookback_days or 30
    start = target_date - timedelta(days=lookback)

    total_nights = (
        db.query(func.count(Reservation.id))
        .filter(
            Reservation.branch_id == branch.id,
            Reservation.check_in_date >= start,
            Reservation.check_in_date <= target_date,
            ~Reservation.status.in_(["Cancelled", "No-show", "No Show"]),
        )
        .scalar()
    ) or 0

    if total_nights < 10:
        return None

    top_country = (
        db.query(
            Reservation.guest_country_code,
            func.count(Reservation.id).label("nights"),
        )
        .filter(
            Reservation.branch_id == branch.id,
            Reservation.check_in_date >= start,
            Reservation.check_in_date <= target_date,
            ~Reservation.status.in_(["Cancelled", "No-show", "No Show"]),
            Reservation.guest_country_code.notin_(EXCLUDED_COUNTRIES),
        )
        .group_by(Reservation.guest_country_code)
        .order_by(func.count(Reservation.id).desc())
        .first()
    )

    if not top_country or not top_country[0]:
        return None

    country_code, country_nights = top_country
    concentration = country_nights / total_nights
    threshold = float(rule.threshold_value)

    if concentration > threshold:
        deviation = concentration * 100
        return _upsert_alert(
            db, branch_id=branch.id, metric_key=rule.metric_key, alert_date=target_date,
            severity=rule.severity, category=rule.category,
            current_value=concentration, threshold_value=threshold,
            baseline_value=None, deviation_pct=deviation,
            message=f"{country_code} accounts for {deviation:.1f}% of room nights (last {lookback} days)",
            recommendation=_fmt_rec(rule.metric_key, branch.name, deviation, extra=country_code),
        )
    return None
