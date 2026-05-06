"""Perf indexes for Country Reservations endpoint.

Revision ID: 038
Revises: 037

Reason
------
/api/metrics/country-reservations All Branches view scans 25K+ reservations
in a 7-month range and was taking 12-32s in production. EXPLAIN ANALYZE
showed Postgres using `idx_reservations_branch_checkin` (branch_id,
check_in_date) in skip-scan mode when no branch_id was given — slow because
it walks per-branch partitions instead of a clean date range.

Adding plain (check_in_date) and (reservation_date) indexes lets the
optimiser do a normal range scan for the All Branches view. The existing
composite indexes still serve the per-branch path.

Same logic applies to the OTA Mix and Cancellation dashboards which run
similar date-range scans without a branch filter.

Also adds (updated_at) — _last_reservations_synced_at runs MAX(updated_at)
on every metrics request to surface the freshness badge, which was a full
table scan and contributed several seconds on every dashboard load.
"""
from alembic import op


revision = "038"
down_revision = "037"
branch_labels = None
depends_on = None


def upgrade():
    # Plain check_in_date index — used by All Branches Country/OTA/Cancellation views.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_reservations_checkin "
        "ON reservations (check_in_date)"
    )
    # Plain reservation_date index — used by All Branches "By Date Booked" views.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_reservations_reservation_date "
        "ON reservations (reservation_date)"
    )
    # updated_at index — _last_reservations_synced_at uses MAX(updated_at)
    # on every metrics request; without an index this is a full table scan.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_reservations_updated_at "
        "ON reservations (updated_at)"
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS idx_reservations_checkin")
    op.execute("DROP INDEX IF EXISTS idx_reservations_reservation_date")
    op.execute("DROP INDEX IF EXISTS idx_reservations_updated_at")
