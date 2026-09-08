"""Index for stay-overlap queries (Fill Pace)

Fill Pace asks a question no other page asks: which reservations OVERLAP a stay
month. That is an interval predicate —

    check_in_date < month_end AND check_out_date > month_start

— and nothing in the existing indexes serves it. `idx_reservations_branch_checkin`
leads on branch_id, so it is unusable when the group is in scope, and
check_out_date is not indexed at all.

The measured effect: one Fill Pace query cost ~3 seconds against production, and
it cost the same for January 2021 — a month with almost no reservations — which
is the signature of a predicate that touches every row regardless of how many
match. The comparable OTA Mix query, whose check_in_date range is bounded on
both sides, returns in 0.35s.

This index puts check_out_date first because it is the selective half for the
months the page is actually opened on: for a stay month in the future,
`check_out_date > month_start` excludes every stay that has already ended, which
is most of the table. check_in_date rides along as the second column so the
other half of the overlap can be tested without going back to the heap.

Revision ID: 064
Revises: 063
"""
from alembic import op

revision = "064"
down_revision = "063"
branch_labels = None
depends_on = None

INDEX_NAME = "idx_reservations_stay_overlap"


def upgrade():
    op.create_index(
        INDEX_NAME,
        "reservations",
        ["check_out_date", "check_in_date"],
        if_not_exists=True,
    )


def downgrade():
    op.drop_index(INDEX_NAME, table_name="reservations", if_exists=True)
