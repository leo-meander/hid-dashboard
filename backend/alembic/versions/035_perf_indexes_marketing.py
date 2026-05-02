"""Performance indexes for marketing-activity, CRM, KOL queries.

Revision ID: 035
Revises: 034

Targets the slow queries on the reservations table:

1. (branch_id, reservation_date) compound index — Marketing Activity CRM/KOL
   "Date Booked" views filter by reservation_date but only check_in_date is
   indexed. Every load was a sequential scan.

2. GIN trigram indexes on room_type and rate_plan_name — _crm_filter() runs
   8 ILIKE conditions and KOL endpoints scan room_type ILIKE '%KOL_%'.
   pg_trgm makes these indexed instead of full table scans.
"""
from alembic import op


revision = "035"
down_revision = "034"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_index(
        "idx_reservations_branch_reservation_date",
        "reservations",
        ["branch_id", "reservation_date"],
    )

    op.execute(
        "CREATE INDEX idx_reservations_room_type_trgm "
        "ON reservations USING gin (room_type gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX idx_reservations_rate_plan_trgm "
        "ON reservations USING gin (rate_plan_name gin_trgm_ops)"
    )


def downgrade():
    op.drop_index("idx_reservations_rate_plan_trgm", table_name="reservations")
    op.drop_index("idx_reservations_room_type_trgm", table_name="reservations")
    op.drop_index(
        "idx_reservations_branch_reservation_date",
        table_name="reservations",
    )
    # pg_trgm extension left in place — other features may depend on it.
