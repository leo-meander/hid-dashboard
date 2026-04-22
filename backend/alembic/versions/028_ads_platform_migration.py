"""Migrate ads stack to centralised Ads Platform API.

Revision ID: 028
Revises: 027

Replaces the legacy Meta Graph API + Google Sheets ads pipelines with a single
upstream source (``ads-performance-fuls.zeabur.app``). Reshapes ``ads_performance``
to support two row grains (daily aggregate vs. ad metadata) and adds mirror
tables for budget plans and booking-match records exposed by the new API.

All pre-migration ads_performance rows are deleted per user decision to
redesign schema cleanly around the new source. Pre-prod snapshot via
``pg_dump -t ads_performance`` is the recovery path.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "028"
down_revision = "027"
branch_labels = None
depends_on = None


# Map known branch UUIDs → Ads Platform branch slug (seed data migration).
BRANCH_SLUG_SEED = {
    "11111111-1111-1111-1111-111111111101": "taipei",
    "11111111-1111-1111-1111-111111111102": "saigon",
    "11111111-1111-1111-1111-111111111103": "1948",
    "11111111-1111-1111-1111-111111111104": "oani",
    "11111111-1111-1111-1111-111111111105": "osaka",
}


def upgrade():
    # ── 1. ads_performance: add data_source, grain, external IDs ──────────
    op.add_column("ads_performance",
                  sa.Column("data_source", sa.String(32), nullable=True))
    op.add_column("ads_performance",
                  sa.Column("grain", sa.String(16), nullable=True))
    op.add_column("ads_performance",
                  sa.Column("account_id", sa.String(64), nullable=True))
    op.add_column("ads_performance",
                  sa.Column("external_ad_id", sa.String(64), nullable=True))
    op.add_column("ads_performance",
                  sa.Column("external_campaign_id", sa.String(64), nullable=True))

    # Purge pre-cutover rows — new source owns the data model from here on.
    op.execute("DELETE FROM ads_performance")

    op.create_index(
        "ix_ads_performance_branch_grain_channel_date",
        "ads_performance",
        ["branch_id", "grain", "channel", "date_from"],
    )
    # Unique constraint per grain — use partial indexes for DB-enforced uniqueness.
    op.execute("""
        CREATE UNIQUE INDEX ux_ads_performance_daily
        ON ads_performance (branch_id, channel, date_from, account_id)
        WHERE grain = 'daily'
    """)
    op.execute("""
        CREATE UNIQUE INDEX ux_ads_performance_ad_external
        ON ads_performance (external_ad_id)
        WHERE grain = 'ad' AND external_ad_id IS NOT NULL
    """)

    # ── 2. ad_angles: add external_angle_id for upstream sync key ─────────
    op.add_column("ad_angles",
                  sa.Column("external_angle_id", sa.String(64), nullable=True))
    op.create_index(
        "ux_ad_angles_external",
        "ad_angles",
        ["external_angle_id"],
        unique=True,
        postgresql_where=sa.text("external_angle_id IS NOT NULL"),
    )

    # ── 3. branches: add ads_platform_slug + backfill known mappings ──────
    op.add_column("branches",
                  sa.Column("ads_platform_slug", sa.String(32), nullable=True))
    conn = op.get_bind()
    for branch_id, slug in BRANCH_SLUG_SEED.items():
        conn.execute(
            sa.text("UPDATE branches SET ads_platform_slug = :slug WHERE id = :bid"),
            {"slug": slug, "bid": branch_id},
        )

    # ── 4. ads_budgets (new table, mirror of /api/export/budget/monthly) ──
    op.create_table(
        "ads_budgets",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("branch_id", UUID(as_uuid=True),
                  sa.ForeignKey("branches.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("external_plan_id", sa.String(64), nullable=False),
        sa.Column("month", sa.String(7), nullable=False),          # "YYYY-MM"
        sa.Column("plan_name", sa.String(200), nullable=True),
        sa.Column("channel", sa.String(32), nullable=True),        # Meta, Google, TikTok
        sa.Column("total_budget_native", sa.Numeric(12, 2), nullable=True),
        sa.Column("total_budget_vnd", sa.Numeric(15, 2), nullable=True),
        sa.Column("currency", sa.String(10), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("NOW()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("NOW()"), nullable=False),
        sa.UniqueConstraint(
            "branch_id", "month", "channel", "external_plan_id",
            name="ux_ads_budgets_branch_month_channel_plan",
        ),
    )
    op.create_index(
        "ix_ads_budgets_branch_month",
        "ads_budgets",
        ["branch_id", "month"],
    )

    # ── 5. ads_booking_matches (new table, /api/export/booking-matches) ───
    op.create_table(
        "ads_booking_matches",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("external_match_id", sa.String(64), nullable=False, unique=True),
        sa.Column("branch_id", UUID(as_uuid=True),
                  sa.ForeignKey("branches.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("channel", sa.String(32), nullable=True),
        sa.Column("match_result", sa.String(32), nullable=True),
        sa.Column("purchase_kind", sa.String(32), nullable=True),
        sa.Column("booking_date", sa.Date, nullable=True),
        sa.Column("match_date", sa.Date, nullable=True),
        sa.Column("revenue_native", sa.Numeric(12, 2), nullable=True),
        sa.Column("revenue_vnd", sa.Numeric(15, 2), nullable=True),
        sa.Column("currency", sa.String(10), nullable=True),
        sa.Column("reservation_ref", sa.String(100), nullable=True),
        sa.Column("external_ad_id", sa.String(64), nullable=True),
        sa.Column("external_campaign_id", sa.String(64), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True),
                  server_default=sa.text("NOW()"), nullable=False),
    )
    op.create_index(
        "ix_ads_booking_matches_branch_date",
        "ads_booking_matches",
        ["branch_id", "booking_date"],
    )


def downgrade():
    op.drop_index("ix_ads_booking_matches_branch_date",
                  table_name="ads_booking_matches")
    op.drop_table("ads_booking_matches")

    op.drop_index("ix_ads_budgets_branch_month", table_name="ads_budgets")
    op.drop_table("ads_budgets")

    op.drop_column("branches", "ads_platform_slug")

    op.drop_index("ux_ad_angles_external", table_name="ad_angles")
    op.drop_column("ad_angles", "external_angle_id")

    op.execute("DROP INDEX IF EXISTS ux_ads_performance_ad_external")
    op.execute("DROP INDEX IF EXISTS ux_ads_performance_daily")
    op.drop_index("ix_ads_performance_branch_grain_channel_date",
                  table_name="ads_performance")
    op.drop_column("ads_performance", "external_campaign_id")
    op.drop_column("ads_performance", "external_ad_id")
    op.drop_column("ads_performance", "account_id")
    op.drop_column("ads_performance", "grain")
    op.drop_column("ads_performance", "data_source")
