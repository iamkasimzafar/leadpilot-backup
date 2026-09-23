"""Record a monitor's run in lead_search_run, with its dedupe split.

Written by hand, mirroring app/models/lead_search.py.

A Radar Monitor now opens a real run, so its work appears in Your Searches
beside the searches the user started by hand. Two extra counters explain what
a monitor run did: `contacts_added` is what was genuinely new, and
`contacts_duplicate` is what was discarded as already-owned and never billed.
Without them a monitor run reads as "found 8, saved 1" with no explanation.

Both default to 0, so every existing run keeps a sensible value.

Revision ID: c3d9f6a1e472
Revises: b2c8e5f0d361
Create Date: 2026-09-23 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3d9f6a1e472"
down_revision: str | None = "b2c8e5f0d361"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("lead_search_run", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "contacts_added",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.add_column(
            sa.Column(
                "contacts_duplicate",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.add_column(
            sa.Column("monitor_id", sa.String(length=36), nullable=True)
        )

        batch_op.create_index(
            "ix_lead_search_run_monitor_id", ["monitor_id"], unique=False
        )
        batch_op.create_foreign_key(
            "fk_lead_search_run_monitor_id",
            "radar_monitors",
            ["monitor_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("lead_search_run", schema=None) as batch_op:
        batch_op.drop_constraint("fk_lead_search_run_monitor_id", type_="foreignkey")
        batch_op.drop_index("ix_lead_search_run_monitor_id")
        batch_op.drop_column("monitor_id")
        batch_op.drop_column("contacts_duplicate")
        batch_op.drop_column("contacts_added")
