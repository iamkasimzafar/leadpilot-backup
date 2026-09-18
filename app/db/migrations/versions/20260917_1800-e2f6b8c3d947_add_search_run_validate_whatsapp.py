"""add whatsapp validation flag to lead search run

Revision ID: e2f6b8c3d947
Revises: d1e5a7b2c846
Create Date: 2026-09-17 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e2f6b8c3d947"
down_revision: str | None = "d1e5a7b2c846"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # NOT NULL with a false server default: existing runs did not request
    # validation, and false is the correct answer for every one of them. The
    # default also means an insert that predates the column still works.
    op.add_column(
        "lead_search_run",
        sa.Column(
            "validate_whatsapp",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("lead_search_run", "validate_whatsapp")
