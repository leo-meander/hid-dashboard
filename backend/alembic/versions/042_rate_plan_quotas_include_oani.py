"""Rate plan quotas: include Oani in existing branch_limits.

Revision ID: 042
Revises: 041

Why
----
Migration 037 seeded all rate_plan_quotas with branch_scope='all_excl_oani'
(Oani was excluded because its Cloudbeds API key lacked Insights:Create
Reports permission — see UPD-211/UPD-212). Migration 039 expanded that into
per-branch `branch_limits` JSON, carrying the exclusion forward — every
existing quota row has Oani missing from branch_limits.

Oani's Cloudbeds add-on landed 2026-05-12; the property now syncs through
the same Insights pipeline as the other four branches and should be subject
to the same rate-plan caps. Add Oani's branch UUID to every existing quota's
branch_limits with the same cap as the other branches in that quota (use
MAX since post-039 all entries share the same value).

Safe to skip Oani for a specific quota: edit via /rate-plan-quotas UI after
deploy — this migration only adds, never overwrites an existing entry.

Idempotent: re-running is a no-op (skips quotas where Oani is already keyed).
"""
import json

from alembic import op
import sqlalchemy as sa


revision = "042"
down_revision = "041"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()

    oani_row = conn.execute(sa.text(
        "SELECT id FROM branches "
        "WHERE LOWER(TRIM(COALESCE(name, ''))) IN ('oani', 'meander oani') "
        "  AND is_active = true "
        "LIMIT 1"
    )).fetchone()
    if not oani_row:
        # Greenfield install with no Oani branch row yet — nothing to backfill.
        return
    oani_id = str(oani_row.id)

    quota_rows = conn.execute(sa.text(
        "SELECT id, branch_limits FROM rate_plan_quotas"
    )).fetchall()
    for row in quota_rows:
        limits = dict(row.branch_limits or {})
        if oani_id in limits or not limits:
            # Already included, or quota has no scope at all (don't invent one).
            continue
        cap = max(int(v or 0) for v in limits.values())
        if cap < 1:
            continue
        limits[oani_id] = cap
        conn.execute(
            sa.text(
                "UPDATE rate_plan_quotas SET branch_limits = :limits "
                "WHERE id = :id"
            ),
            {"limits": json.dumps(limits), "id": row.id},
        )

    # Status's last_alert_buckets is keyed by branch_id too. Seed Oani's
    # entry to 0 (no live alert) so the dedupe map keeps a complete shape;
    # the engine will populate the real value on the next evaluation tick.
    status_rows = conn.execute(sa.text(
        "SELECT s.id, s.last_alert_buckets, q.branch_limits "
        "FROM rate_plan_quota_status s "
        "JOIN rate_plan_quotas q ON q.id = s.quota_id"
    )).fetchall()
    for s in status_rows:
        limits = s.branch_limits or {}
        if oani_id not in limits:
            continue
        buckets = dict(s.last_alert_buckets or {})
        if oani_id in buckets:
            continue
        buckets[oani_id] = 0
        conn.execute(
            sa.text(
                "UPDATE rate_plan_quota_status SET last_alert_buckets = :b "
                "WHERE id = :id"
            ),
            {"b": json.dumps(buckets), "id": s.id},
        )


def downgrade():
    """Strip Oani from every quota's branch_limits + status.last_alert_buckets.

    This rolls back the data change only — the schema is unchanged.
    """
    conn = op.get_bind()

    oani_row = conn.execute(sa.text(
        "SELECT id FROM branches "
        "WHERE LOWER(TRIM(COALESCE(name, ''))) IN ('oani', 'meander oani') "
        "LIMIT 1"
    )).fetchone()
    if not oani_row:
        return
    oani_id = str(oani_row.id)

    for row in conn.execute(sa.text(
        "SELECT id, branch_limits FROM rate_plan_quotas"
    )).fetchall():
        limits = dict(row.branch_limits or {})
        if oani_id not in limits:
            continue
        limits.pop(oani_id, None)
        conn.execute(
            sa.text(
                "UPDATE rate_plan_quotas SET branch_limits = :limits "
                "WHERE id = :id"
            ),
            {"limits": json.dumps(limits), "id": row.id},
        )

    for s in conn.execute(sa.text(
        "SELECT id, last_alert_buckets FROM rate_plan_quota_status"
    )).fetchall():
        buckets = dict(s.last_alert_buckets or {})
        if oani_id not in buckets:
            continue
        buckets.pop(oani_id, None)
        conn.execute(
            sa.text(
                "UPDATE rate_plan_quota_status SET last_alert_buckets = :b "
                "WHERE id = :id"
            ),
            {"b": json.dumps(buckets), "id": s.id},
        )
