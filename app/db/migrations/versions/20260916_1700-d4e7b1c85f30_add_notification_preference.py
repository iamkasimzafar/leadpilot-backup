"""add notification preference table

Revision ID: d4e7b1c85f30
Revises: d4e9f1a2b3c5
Create Date: 2026-09-16 17:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4e7b1c85f30"

# Chained after the lead-tracking migration rather than beside it: both were
# written against c1d83a6f92b7, which left the graph with two heads.
down_revision: str | None = "d4e9f1a2b3c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Written by hand, mirroring app/models/notification_preference.py.
    #
    # No backfill: a user without a row is treated as opted in to everything,
    # so existing accounts keep receiving notifications and get a row the first
    # time they save a preference.
    op.create_table(
        "notification_preference",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column(
            "lead", sa.Boolean(), server_default=sa.true(), nullable=False
        ),
        sa.Column(
            "reply", sa.Boolean(), server_default=sa.true(), nullable=False
        ),
        sa.Column(
            "monitor", sa.Boolean(), server_default=sa.true(), nullable=False
        ),
        sa.Column(
            "credits", sa.Boolean(), server_default=sa.true(), nullable=False
        ),
        sa.Column(
            "sequence", sa.Boolean(), server_default=sa.true(), nullable=False
        ),
        sa.Column(
            "weekly_digest", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )


def downgrade() -> None:
    op.drop_table("notification_preference")
