"""Widen source_category to VARCHAR(32) and backfill Direct + Local travel agency.

Revision ID: 027
Revises: 026

Expands the source_category derivation beyond the prior Website/Booking-Engine/Blogger/Direct
keyword set. All walk-in, extension, phone, email, Facebook, and PR bookings are now Direct.
Corporate/travel-agency sources (Công Ty / TNHH / Co.,Ltd / Company / Corp / Travel Agency /
Agency) become a new third category: "Local travel agency".
"""

from alembic import op
import sqlalchemy as sa


revision = "027"
down_revision = "026"
branch_labels = None
depends_on = None


DIRECT_PATTERN = (
    r"(website|booking engine|direct|blogger|"
    r"walk-in|walk in|walkin|"
    r"extension|phone|email|"
    r"facebook|public relations)"
)

LOCAL_TA_PATTERN = (
    r"(công ty|cong ty|tnhh|"
    r"co\., ?ltd\.?|co ltd|co\.ltd|"
    r"company|corp|corporate|"
    r"travel agent|travel agency|agency|"
    r"wholesaler|tour operator|"
    r"株式会社|有限会社|"
    r"有限公司|股份有限公司)"
)


def upgrade():
    # 1. Widen the column on both tables so "Local travel agency" (19 chars) fits comfortably.
    op.alter_column(
        "reservations",
        "source_category",
        type_=sa.String(32),
        existing_type=sa.String(20),
        existing_nullable=True,
    )
    op.alter_column(
        "reservation_daily",
        "source_category",
        type_=sa.String(32),
        existing_type=sa.String(20),
        existing_nullable=True,
    )

    # 2. Backfill. Direct takes precedence over Local travel agency; OTA is the fallback.
    conn = op.get_bind()
    for table in ("reservations", "reservation_daily"):
        conn.execute(sa.text(f"""
            UPDATE {table}
               SET source_category = 'Direct'
             WHERE source IS NOT NULL
               AND LOWER(source) ~ :pattern
        """), {"pattern": DIRECT_PATTERN})

        conn.execute(sa.text(f"""
            UPDATE {table}
               SET source_category = 'Local travel agency'
             WHERE source IS NOT NULL
               AND source_category IS DISTINCT FROM 'Direct'
               AND LOWER(source) ~ :pattern
        """), {"pattern": LOCAL_TA_PATTERN})

        conn.execute(sa.text(f"""
            UPDATE {table}
               SET source_category = 'OTA'
             WHERE source_category NOT IN ('Direct', 'Local travel agency')
                OR source_category IS NULL
        """))


def downgrade():
    # Collapse Local travel agency back into OTA, then narrow the column.
    conn = op.get_bind()
    for table in ("reservations", "reservation_daily"):
        conn.execute(sa.text(f"""
            UPDATE {table}
               SET source_category = 'OTA'
             WHERE source_category = 'Local travel agency'
        """))

    op.alter_column(
        "reservation_daily",
        "source_category",
        type_=sa.String(20),
        existing_type=sa.String(32),
        existing_nullable=True,
    )
    op.alter_column(
        "reservations",
        "source_category",
        type_=sa.String(20),
        existing_type=sa.String(32),
        existing_nullable=True,
    )
