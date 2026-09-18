"""add country to lead search run

Revision ID: b7c4e2d9a851
Revises: a8b9c0d1e2f3
Create Date: 2026-09-17 11:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7c4e2d9a851"
down_revision: str | None = "a8b9c0d1e2f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable with no default: existing runs predate country scoping, and NULL
    # is exactly what a worldwide search means, so no backfill is needed.
    op.add_column(
        "lead_search_run",
        sa.Column("country", sa.String(length=2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("lead_search_run", "country")
