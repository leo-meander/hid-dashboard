"""Rate plan quotas: per-branch caps + per-branch alert buckets.

Revision ID: 039
Revises: 038

Why
----
Original 037 stored a single `limit_count` and `branch_scope`/`branch_ids`,
treating every selected branch as one shared cap. Marketing now wants a
distinct cap per branch (Saigon: 30, Taipei: 50, …) with alert buckets
tracked independently — one branch hitting 95% emails just for that branch
without re-arming buckets for the others.

Schema changes
--------------
rate_plan_quotas
  + branch_limits     JSON   {"<branch_id>": <int_cap>, …}
                              keys ARE the in-scope branches; absence = excluded
  - limit_count
  - branch_scope
  - branch_ids

rate_plan_quota_status
  + last_alert_buckets JSON  {"<branch_id>": 0|90|95|100, …}
                              per-branch bucket history for email dedupe
  - last_alert_bucket

Data migration
--------------
For each existing quota, resolve its previous scope (all_excl_oani →
all active branches except Oani) and seed `branch_limits` with the old
`limit_count` for every resolved branch. Status's per-branch bucket
history is seeded by copying the previous single bucket value to every
in-scope branch so an in-flight 95% alert doesn't re-fire after deploy.
"""
import json

from alembic import op
import sqlalchemy as sa


revision = "039"
down_revision = "038"
branch_labels = None
depends_on = None


def upgrade():
    # 1. Add new nullable columns so we can backfill before enforcing NOT NULL.
    op.add_column(
        "rate_plan_quotas",
        sa.Column("branch_limits", sa.JSON, nullable=True),
    )
    op.add_column(
        "rate_plan_quota_status",
        sa.Column("last_alert_buckets", sa.JSON, nullable=True),
    )

    conn = op.get_bind()

    # 2. Resolve current branches once (used by all_excl_oani backfill).
    branch_rows = conn.execute(sa.text(
        "SELECT id, name FROM branches WHERE is_active = true"
    )).fetchall()
    excl_oani_ids = [
        str(r.id) for r in branch_rows
        if (r.name or "").strip().lower() != "oani"
    ]

    # 3. Backfill rate_plan_quotas.branch_limits.
    quota_rows = conn.execute(sa.text(
        "SELECT id, limit_count, branch_scope, branch_ids "
        "FROM rate_plan_quotas"
    )).fetchall()
    for row in quota_rows:
        if row.branch_scope == "specific" and row.branch_ids:
            # branch_ids stored as JSON array of UUID strings.
            ids = [str(x) for x in row.branch_ids]
        else:
            ids = list(excl_oani_ids)
        limits = {bid: int(row.limit_count) for bid in ids}
        conn.execute(
            sa.text(
                "UPDATE rate_plan_quotas SET branch_limits = :limits "
                "WHERE id = :id"
            ),
            {"limits": json.dumps(limits), "id": row.id},
        )

    # 4. Backfill rate_plan_quota_status.last_alert_buckets.
    status_rows = conn.execute(sa.text(
        "SELECT s.id, s.last_alert_bucket, q.branch_limits "
        "FROM rate_plan_quota_status s "
        "JOIN rate_plan_quotas q ON q.id = s.quota_id"
    )).fetchall()
    for s in status_rows:
        limits = s.branch_limits or {}
        bucket = int(s.last_alert_bucket or 0)
        # Preserve "we already emailed at bucket X" for every in-scope branch.
        # 0 just means "no live alert" so we save an empty-ish dict either way.
        buckets = {bid: bucket for bid in limits.keys()} if bucket else {}
        conn.execute(
            sa.text(
                "UPDATE rate_plan_quota_status SET last_alert_buckets = :b "
                "WHERE id = :id"
            ),
            {"b": json.dumps(buckets), "id": s.id},
        )

    # 5. Enforce NOT NULL now that everything is backfilled.
    op.alter_column(
        "rate_plan_quotas", "branch_limits",
        existing_type=sa.JSON, nullable=False,
    )
    op.alter_column(
        "rate_plan_quota_status", "last_alert_buckets",
        existing_type=sa.JSON, nullable=False,
        server_default=sa.text("'{}'::json"),
    )

    # 6. Drop the legacy columns.
    op.drop_column("rate_plan_quotas", "limit_count")
    op.drop_column("rate_plan_quotas", "branch_scope")
    op.drop_column("rate_plan_quotas", "branch_ids")
    op.drop_column("rate_plan_quota_status", "last_alert_bucket")


def downgrade():
    """Rebuild the old single-cap shape.

    `limit_count` is recovered as MAX(branch_limits.value) — best effort,
    since the new model can express caps the old one can't. Per-branch
    bucket history is collapsed via MAX so re-alerts don't spam after
    rollback.
    """
    op.add_column(
        "rate_plan_quotas",
        sa.Column("limit_count", sa.Integer, nullable=True),
    )
    op.add_column(
        "rate_plan_quotas",
        sa.Column("branch_scope", sa.String(20), nullable=True),
    )
    op.add_column(
        "rate_plan_quotas",
        sa.Column("branch_ids", sa.JSON, nullable=True),
    )
    op.add_column(
        "rate_plan_quota_status",
        sa.Column("last_alert_bucket", sa.Integer, nullable=True),
    )

    conn = op.get_bind()

    branch_rows = conn.execute(sa.text(
        "SELECT id, name FROM branches WHERE is_active = true"
    )).fetchall()
    excl_oani_set = {
        str(r.id) for r in branch_rows
        if (r.name or "").strip().lower() != "oani"
    }

    for row in conn.execute(sa.text(
        "SELECT id, branch_limits FROM rate_plan_quotas"
    )).fetchall():
        limits = row.branch_limits or {}
        cap = max(limits.values()) if limits else 1
        ids = set(limits.keys())
        if ids and ids == excl_oani_set:
            scope, branch_ids = "all_excl_oani", None
        else:
            scope, branch_ids = "specific", list(ids)
        conn.execute(
            sa.text(
                "UPDATE rate_plan_quotas "
                "SET limit_count = :cap, branch_scope = :scope, "
                "    branch_ids = :ids "
                "WHERE id = :id"
            ),
            {
                "cap": cap, "scope": scope,
                "ids": json.dumps(branch_ids) if branch_ids else None,
                "id": row.id,
            },
        )

    for s in conn.execute(sa.text(
        "SELECT id, last_alert_buckets FROM rate_plan_quota_status"
    )).fetchall():
        buckets = s.last_alert_buckets or {}
        bucket = max(buckets.values()) if buckets else 0
        conn.execute(
            sa.text(
                "UPDATE rate_plan_quota_status SET last_alert_bucket = :b "
                "WHERE id = :id"
            ),
            {"b": int(bucket), "id": s.id},
        )

    op.alter_column("rate_plan_quotas", "limit_count",
                    existing_type=sa.Integer, nullable=False)
    op.alter_column("rate_plan_quotas", "branch_scope",
                    existing_type=sa.String(20), nullable=False,
                    server_default="all_excl_oani")
    op.alter_column("rate_plan_quota_status", "last_alert_bucket",
                    existing_type=sa.Integer, nullable=False,
                    server_default="0")

    op.drop_column("rate_plan_quotas", "branch_limits")
    op.drop_column("rate_plan_quota_status", "last_alert_buckets")
