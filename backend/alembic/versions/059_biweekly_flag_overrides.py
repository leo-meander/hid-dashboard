"""biweekly_flag_overrides table

Revision ID: 059
Revises: 058
Create Date: 2026-08-13

Operator corrections to the Bi-Weekly report's auto-generated Highlights /
Watch-outs / Recommended Actions lines. Keyed by the RULE that produced a line
(`flag.revenue`, `flag.ads.Meta`, `act.kol_posts`), not by its text, so a
correction survives the rebuild that rewrites the sentence with new numbers.
See app/models/biweekly_flag_override.py for why this is not a row in
weekly_report_comments.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "059"
down_revision = "058"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "biweekly_flag_overrides",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("period_key", sa.String(16), nullable=False),
        sa.Column(
            "branch_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("branches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("flag_key", sa.String(64), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("is_hidden", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column(
            "edited_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("period_key", "branch_id", "flag_key",
                            name="uq_biweekly_flag_override"),
    )
    # Every read is "all overrides for this period + branch", which is exactly
    # what the report router asks for once per request.
    op.create_index(
        "ix_bfo_period_branch", "biweekly_flag_overrides",
        ["period_key", "branch_id"],
    )


def downgrade():
    op.drop_index("ix_bfo_period_branch", table_name="biweekly_flag_overrides")
    op.drop_table("biweekly_flag_overrides")
