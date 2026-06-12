"""Add gender + date_of_birth columns to reservations (guest demographics).

Backfilled from Cloudbeds /getReservation guestList[*].guestGender /
guestBirthdate by backfill_guest_demographics. Stored as dedicated columns
(NOT raw_data) because ingest_reservations overwrites raw_data with the bulk
/getReservations payload on every sync — which carries no guest detail — and
would clobber anything merged into it. This is the same reason guest_country
uses a column rather than raw_data.

Revision ID: 051
Revises: 050
"""
from alembic import op
import sqlalchemy as sa

revision = "051"
down_revision = "050"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("reservations", sa.Column("gender", sa.String(length=10), nullable=True))
    op.add_column("reservations", sa.Column("date_of_birth", sa.Date(), nullable=True))


def downgrade():
    op.drop_column("reservations", "date_of_birth")
    op.drop_column("reservations", "gender")
