"""encrypt the guest name, keep only the birth year, mask logged emails

Revision ID: 067
Revises: 066
Create Date: 2026-09-18

On 2026-09-17 five Cloudbeds API tokens and the production database password
were found sitting in a PUBLIC GitHub repository, committed since the initial
commit six months earlier. Anyone who read that repo could open this database
and read every guest's name, exact date of birth, and stay history in the
clear. This migration removes as much of that readable personal data as the
dashboard can do without.

  guest_name_enc  new column. Names move out of raw_data and are stored
                  AES-GCM encrypted, with the key in the environment rather
                  than in a column, so a database-only breach yields
                  ciphertext. Nothing this dashboard computes reads a guest's
                  name; only two endpoints ever returned it.

  birth_year      replaces date_of_birth. Every consumer converted the date
                  into an age band, so the day and month were precision nobody
                  used and everybody could read. Derived here from the existing
                  column, which is then dropped.

  guest_email     existing rows in webhook_events are masked to `ab***@host`.
                  The column exists so a human can recognise a row in the
                  Webhook Monitor; the masked form does that just as well.

The name backfill is NOT done here: encrypting needs the application key, which
SQL has no access to. Run POST /api/sync/backfill-guest-name-encryption after
deploying, with PII_ENCRYPTION_KEY set. Until it runs, guest_name_enc is NULL
for existing rows and raw_data still holds their guestName; that endpoint fills
one and strips the other.

Downgrade restores the columns but NOT the data. Names are only recoverable by
re-syncing from Cloudbeds, and exact dates of birth are gone for good — which
is the point.
"""
from alembic import op
import sqlalchemy as sa


revision = "067"
down_revision = "066"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("reservations", sa.Column("guest_name_enc", sa.Text(), nullable=True))
    op.add_column("reservations", sa.Column("birth_year", sa.Integer(), nullable=True))

    # Carry the year across before the date goes away.
    op.execute(
        "UPDATE reservations SET birth_year = EXTRACT(YEAR FROM date_of_birth)::int "
        "WHERE date_of_birth IS NOT NULL"
    )
    op.drop_column("reservations", "date_of_birth")

    # Mask what is already logged. Rows expire after 7 days anyway, so this is
    # only the current window — but that window is exactly what a reader today
    # would see.
    op.execute(
        "UPDATE webhook_events "
        "SET guest_email = left(split_part(guest_email, '@', 1), 2) || '***@' "
        "                  || split_part(guest_email, '@', 2) "
        "WHERE guest_email IS NOT NULL AND guest_email <> '' "
        "  AND position('@' in guest_email) > 0 "
        "  AND guest_email NOT LIKE '%%***@%%'"
    )


def downgrade():
    op.add_column("reservations", sa.Column("date_of_birth", sa.Date(), nullable=True))
    op.drop_column("reservations", "birth_year")
    op.drop_column("reservations", "guest_name_enc")
