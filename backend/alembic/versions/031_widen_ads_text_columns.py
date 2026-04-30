"""Widen ads_performance text columns to fit longer upstream values.

Revision ID: 031
Revises: 030

The Ads Platform began returning longer campaign / adset / ad names than
fit in VARCHAR(200) (e.g. "[Carousel]  Light & scent — Saigon TOF — ..."),
which crashed every ads-platform sync with StringDataRightTruncation.

Bumping the relevant text columns to VARCHAR(500). Postgres ALTER TYPE
to a wider varchar is metadata-only — no table rewrite, takes < 1s even
on large tables.
"""
from alembic import op
import sqlalchemy as sa


revision = "031"
down_revision = "030"
branch_labels = None
depends_on = None


# Columns observed to overflow + their friends (anything we currently
# pull from the Ads Platform that could realistically be long).
_COLUMNS_TO_WIDEN = [
    "campaign_name",
    "adset_name",
    "ad_name",
    "target_country",
    "target_audience",
    "campaign_category",
]


def upgrade():
    for col in _COLUMNS_TO_WIDEN:
        op.alter_column(
            "ads_performance", col,
            type_=sa.String(500),
            existing_type=sa.String(200),
            existing_nullable=True,
        )


def downgrade():
    for col in _COLUMNS_TO_WIDEN:
        op.alter_column(
            "ads_performance", col,
            type_=sa.String(200),
            existing_type=sa.String(500),
            existing_nullable=True,
        )
