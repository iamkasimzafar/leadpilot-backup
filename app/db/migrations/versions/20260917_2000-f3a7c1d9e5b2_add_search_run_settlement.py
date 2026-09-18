"""add settlement columns to lead search run

Revision ID: f3a7c1d9e5b2
Revises: c9d2e4f6a8b3
Create Date: 2026-09-17 20:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f3a7c1d9e5b2"
down_revision: str | None = "c9d2e4f6a8b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing runs predate charging and were never billed: zero charged, zero
    # owed, and a null charged_at, which is exactly what "not settled" means.
    # They are not back-billed.
    op.add_column(
        "lead_search_run",
        sa.Column("credits_charged", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "lead_search_run",
        sa.Column("credits_shortfall", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "lead_search_run",
        sa.Column("whatsapp_checks", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "lead_search_run",
        sa.Column("credits_charged_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("lead_search_run", "credits_charged_at")
    op.drop_column("lead_search_run", "whatsapp_checks")
    op.drop_column("lead_search_run", "credits_shortfall")
    op.drop_column("lead_search_run", "credits_charged")
