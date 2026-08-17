"""biweekly_report_shares table

Revision ID: 060
Revises: 059
Create Date: 2026-08-17

Unlisted, expiring, revocable links that open ONE branch's Bi-Weekly report
without a HiD login — so the report can be emailed to a branch manager who has
no account. See app/models/biweekly_report_share.py for why each column is
there; the short version is that the link is the credential, so it is scoped,
time-boxed, killable and audited.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "060"
down_revision = "059"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "biweekly_report_shares",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("token", sa.String(64), nullable=False),
        sa.Column("period_key", sa.String(16), nullable=False),
        sa.Column(
            "branch_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("branches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("view_count", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("last_viewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("period_key", "branch_id",
                            name="uq_biweekly_share_period_branch"),
    )
    # Every public read is a lookup on the token alone, by an unauthenticated
    # caller — so it is the one index that has to exist before traffic does.
    op.create_index(
        "ix_biweekly_share_token", "biweekly_report_shares", ["token"],
        unique=True,
    )


def downgrade():
    op.drop_index("ix_biweekly_share_token", table_name="biweekly_report_shares")
    op.drop_table("biweekly_report_shares")
