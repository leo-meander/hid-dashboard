"""Backfill NULL guest_country / guest_country_code as "Unknown".

Revision ID: 036
Revises: 035

Reason
------
Cloudbeds ingestion previously skipped `guest_country` when the API returned
NULL for `guestCountry`, so a non-trivial fraction of reservations sat with
NULL country. Downstream:

1. /api/metrics/country-reservations grouped NULLs into a synthetic "Unknown"
   bucket but the per-period trend query filtered with
   `guest_country IN (...)` — `NULL IN (...)` evaluates to NULL in SQL, so
   those reservations never appeared in the chart even though they showed up
   in the totals.
2. /api/metrics/country-yoy-insights explicitly filtered out NULL/empty/short
   country values, silently shrinking YoY totals.

The application code is now fixed to write "Unknown" at ingestion time and to
COALESCE on read. This migration backfills existing rows so historical data
behaves the same way without requiring a re-pull from Cloudbeds.

Per user semantics: "Unknown" = country is missing; the literal "Others"
bucket is reserved for a future change (country present but unrecognised).
"""
from alembic import op


revision = "036"
down_revision = "035"
branch_labels = None
depends_on = None


def upgrade():
    # NULL or empty/whitespace/"0" → "Unknown"
    op.execute(
        """
        UPDATE reservations
        SET guest_country = 'Unknown'
        WHERE guest_country IS NULL
           OR btrim(guest_country) = ''
           OR btrim(guest_country) = '0'
        """
    )
    op.execute(
        """
        UPDATE reservations
        SET guest_country_code = 'Unknown'
        WHERE guest_country_code IS NULL
           OR btrim(guest_country_code) = ''
           OR btrim(guest_country_code) = '0'
        """
    )


def downgrade():
    # Reverse the bucket back to NULL — note this loses the distinction
    # between "originally NULL" and "originally the literal string 'Unknown'",
    # but the latter is rare and was previously aliased to "Others" anyway.
    op.execute(
        """
        UPDATE reservations
        SET guest_country = NULL
        WHERE guest_country = 'Unknown'
        """
    )
    op.execute(
        """
        UPDATE reservations
        SET guest_country_code = NULL
        WHERE guest_country_code = 'Unknown'
        """
    )
