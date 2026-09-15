"""add notification table

Revision ID: 7c4e1a9b2d38
Revises: 3f9c2b7e8a1d
Create Date: 2026-09-15 14:20:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7c4e1a9b2d38"
down_revision: str | None = "3f9c2b7e8a1d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Written by hand, mirroring app/models/notification.py.
    op.create_table(
        "notification",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("subtitle", sa.Text(), nullable=False),
        sa.Column("link", sa.String(length=255), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("notification", schema=None) as batch_op:
        batch_op.create_index(
            "ix_notification_user_id_created_at", ["user_id", "created_at"], unique=False
        )
        batch_op.create_index(
            "ix_notification_user_id_read_at", ["user_id", "read_at"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("notification", schema=None) as batch_op:
        batch_op.drop_index("ix_notification_user_id_read_at")
        batch_op.drop_index("ix_notification_user_id_created_at")

    op.drop_table("notification")
