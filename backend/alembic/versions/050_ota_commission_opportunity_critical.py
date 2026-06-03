"""Promote the OTA commission opportunity (soft week) alert to CRITICAL.

A soft upcoming week below the occupancy floor is a time-sensitive, money-on-
the-table situation — there is still lead time to fill it, but only if the team
acts now. Surface it as CRITICAL rather than WARNING so it rises to the top.

Revision ID: 050
Revises: 049
"""
from alembic import op

revision = "050"
down_revision = "049"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "UPDATE alert_rules SET severity = 'CRITICAL' "
        "WHERE metric_key = 'ota_commission_opportunity'"
    )


def downgrade():
    op.execute(
        "UPDATE alert_rules SET severity = 'WARNING' "
        "WHERE metric_key = 'ota_commission_opportunity'"
    )
