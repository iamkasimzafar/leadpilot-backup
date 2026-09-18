"""add company type to lead search run

Revision ID: c9d2f4a6b013
Revises: b7c4e2d9a851
Create Date: 2026-09-17 15:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9d2f4a6b013"
down_revision: str | None = "b7c4e2d9a851"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable with no default, for the same reason as `country`: existing runs
    # predate company-type targeting, and NULL is exactly what "any type" means.
    op.add_column(
        "lead_search_run",
        sa.Column("company_type", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("lead_search_run", "company_type")
