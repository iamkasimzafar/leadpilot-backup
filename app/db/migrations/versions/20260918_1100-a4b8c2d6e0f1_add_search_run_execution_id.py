"""add n8n execution id to lead search runs

Revision ID: a4b8c2d6e0f1
Revises: f3a7c1d9e5b2
Create Date: 2026-09-18 11:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a4b8c2d6e0f1"
down_revision: str | None = "f3a7c1d9e5b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # n8n's Error Trigger reports failures by execution id, never by our run
    # id. Remembering which execution handles a run is what lets a failure be
    # attributed to the user whose search it was.
    with op.batch_alter_table("lead_search_run", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("n8n_execution_id", sa.String(length=64), nullable=True)
        )
        batch_op.create_index("ix_lead_search_run_n8n_execution_id", ["n8n_execution_id"])


def downgrade() -> None:
    with op.batch_alter_table("lead_search_run", schema=None) as batch_op:
        batch_op.drop_index("ix_lead_search_run_n8n_execution_id")
        batch_op.drop_column("n8n_execution_id")
