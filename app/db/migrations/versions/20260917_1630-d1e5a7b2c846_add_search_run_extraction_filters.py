"""add contact role and company size to lead search run

Revision ID: d1e5a7b2c846
Revises: c9d2f4a6b013
Create Date: 2026-09-17 16:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d1e5a7b2c846"
down_revision: str | None = "c9d2f4a6b013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Both nullable with no default, as for `country` and `company_type`:
    # existing runs predate these filters, and NULL is exactly what "no filter"
    # means, so nothing needs backfilling.
    op.add_column(
        "lead_search_run",
        sa.Column("contact_role", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "lead_search_run",
        sa.Column("company_size", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("lead_search_run", "company_size")
    op.drop_column("lead_search_run", "contact_role")
