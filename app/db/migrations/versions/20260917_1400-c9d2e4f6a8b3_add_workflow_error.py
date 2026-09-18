"""add workflow error table

Revision ID: c9d2e4f6a8b3
Revises: b7c4e2d9a851
Create Date: 2026-09-17 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9d2e4f6a8b3"

# Chained after the whatsapp-validation migration rather than beside it: this
# was written against b7c4e2d9a851 at the same time as the company-type work,
# which left the graph with two heads. The table is standalone, so ordering it
# last is safe.
down_revision: str | None = "e2f6b8c3d947"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workflow_error",
        sa.Column("id", sa.String(length=36), nullable=False),
        # Nullable: an error trigger that did not carry our run id is still
        # worth keeping, it just cannot be shown to a particular user.
        sa.Column("run_id", sa.String(length=36), nullable=True),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column("workflow_id", sa.String(length=64), nullable=True),
        sa.Column("execution_id", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=False),
        sa.Column("last_node_executed", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # The error outlives the run it belonged to.
        sa.ForeignKeyConstraint(["run_id"], ["lead_search_run.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("workflow_error", schema=None) as batch_op:
        batch_op.create_index("ix_workflow_error_created_at", ["created_at"])
        batch_op.create_index("ix_workflow_error_run_id", ["run_id"])


def downgrade() -> None:
    with op.batch_alter_table("workflow_error", schema=None) as batch_op:
        batch_op.drop_index("ix_workflow_error_run_id")
        batch_op.drop_index("ix_workflow_error_created_at")

    op.drop_table("workflow_error")
