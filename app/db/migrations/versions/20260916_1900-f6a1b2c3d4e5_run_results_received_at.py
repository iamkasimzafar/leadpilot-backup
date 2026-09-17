"""record when a run's results were received

Revision ID: f6a1b2c3d4e5
Revises: e5f0a3b7c9d1
Create Date: 2026-09-16 19:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f6a1b2c3d4e5"
down_revision: str | None = "e5f0a3b7c9d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("lead_search_run", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("results_received_at", sa.DateTime(timezone=True), nullable=True)
        )

    # Runs that already have companies attached did receive results; backfill
    # so their result views do not sit on "collecting results" forever.
    op.execute(
        sa.text(
            """
            UPDATE lead_search_run
            SET results_received_at = finished_at
            WHERE results_received_at IS NULL
              AND finished_at IS NOT NULL
              AND id IN (SELECT DISTINCT run_id FROM company WHERE run_id IS NOT NULL)
            """
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("lead_search_run", schema=None) as batch_op:
        batch_op.drop_column("results_received_at")
