"""webhook_events.reservation_created_at — the lag column

The monitor could show when we fanned a reservation out but not when Cloudbeds
created it, so "is Osaka slower than Oani?" had no answer in the data: a branch
with a quarter of the booking volume looks identical to a branch running hours
behind. Storing Cloudbeds' dateCreated (converted to UTC at write time, using
the branch's configured tz offset) turns that into a subtraction.

Nullable and backfill-free on purpose. The value only exists on the reservation
payload we fan out, so the 7 days of rows already in the table cannot have one;
they show a blank lag until they age out of the retention window.

Revision ID: 065
Revises: 064
"""
import sqlalchemy as sa
from alembic import op

revision = "065"
down_revision = "064"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "webhook_events",
        sa.Column("reservation_created_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade():
    op.drop_column("webhook_events", "reservation_created_at")
