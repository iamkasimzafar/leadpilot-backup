"""add lead tracking columns

Revision ID: d4e9f1a2b3c5
Revises: c1d83a6f92b7
Create Date: 2026-09-16 17:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4e9f1a2b3c5"
down_revision: str | None = "c1d83a6f92b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Written by hand, mirroring app/models/lead.py and lead_search.py.
    with op.batch_alter_table("company", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("added_to_leads_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "status", sa.String(length=16), server_default="new", nullable=False
            )
        )
        batch_op.add_column(sa.Column("notes", sa.Text(), nullable=True))
        batch_op.create_index(
            "ix_company_user_id_status",
            ["user_id", "added_to_leads_at", "status"],
            unique=False,
        )

    with op.batch_alter_table("lead_search_run", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "auto_add_to_leads",
                sa.Boolean(),
                server_default=sa.true(),
                nullable=False,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("lead_search_run", schema=None) as batch_op:
        batch_op.drop_column("auto_add_to_leads")

    with op.batch_alter_table("company", schema=None) as batch_op:
        batch_op.drop_index("ix_company_user_id_status")
        batch_op.drop_column("notes")
        batch_op.drop_column("status")
        batch_op.drop_column("added_to_leads_at")
