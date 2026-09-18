"""add error reason to lead search run

Revision ID: b5c9d3e7f1a2
Revises: a4b8c2d6e0f1
Create Date: 2026-09-18 09:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b5c9d3e7f1a2"
down_revision: str | None = "a4b8c2d6e0f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable, no backfill: runs that failed before this column existed keep
    # being classified from their error text at read time, exactly as before.
    op.add_column(
        "lead_search_run",
        sa.Column("error_reason", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("lead_search_run", "error_reason")
