"""Add radar_monitors, and a per-user email index on decision_maker.

Written by hand, mirroring app/models/radar_monitor.py and the DecisionMaker
changes in app/models/lead.py.

The decision_maker.user_id column is denormalised from the owning company so a
radar monitor can ask "does this user already have this email?" without a
join -- MySQL cannot index across one. It is backfilled from company here and
written on every insert thereafter.

The accompanying index is deliberately NOT unique: the same contact
legitimately appears under two company domains, and production already holds
such rows, so a unique index would fail this migration and reject valid
manual-search results. The never-bill-twice guarantee is enforced in
MonitorResultsService, which is the only path that dedupes on email.

Revision ID: a1b7d4e9c250
Revises: f5a9c3d7e1b8
Create Date: 2026-09-23 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1b7d4e9c250"
down_revision: str | None = "f5a9c3d7e1b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "radar_monitors",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("search_type", sa.String(length=32), nullable=False),
        sa.Column("search_value", sa.String(length=500), nullable=False),
        sa.Column("search_label", sa.String(length=500), nullable=True),
        sa.Column("filters_json", sa.Text(), nullable=False),
        sa.Column("frequency", sa.String(length=16), nullable=False),
        sa.Column("limit_per_run", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("total_leads_generated", sa.Integer(), nullable=False),
        sa.Column("serper_offset", sa.Integer(), nullable=False),
        sa.Column("callback_token", sa.String(length=64), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    with op.batch_alter_table("radar_monitors", schema=None) as batch_op:
        batch_op.create_index("ix_radar_monitors_user_id", ["user_id"], unique=False)
        batch_op.create_index(
            "ix_radar_monitors_status_next_run_at",
            ["status", "next_run_at"],
            unique=False,
        )
        batch_op.create_index(
            "ix_radar_monitors_user_id_created_at",
            ["user_id", "created_at"],
            unique=False,
        )

    # --- decision_maker.user_id ---------------------------------------------
    with op.batch_alter_table("decision_maker", schema=None) as batch_op:
        batch_op.add_column(sa.Column("user_id", sa.String(length=36), nullable=True))

    # Backfill from the owning company. Existing rows have to carry the owner
    # or the dedupe would miss every lead found before this migration and
    # happily bill for it again.
    op.execute(
        """
        UPDATE decision_maker d
        JOIN company c ON c.id = d.company_id
        SET d.user_id = c.user_id
        WHERE d.user_id IS NULL
        """
        if op.get_bind().dialect.name == "mysql"
        else """
        UPDATE decision_maker
        SET user_id = (
            SELECT c.user_id FROM company c WHERE c.id = decision_maker.company_id
        )
        WHERE user_id IS NULL
        """
    )

    with op.batch_alter_table("decision_maker", schema=None) as batch_op:
        batch_op.create_foreign_key(
            "fk_decision_maker_user_id",
            "user",
            ["user_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_index(
            "ix_decision_maker_user_id_email",
            ["user_id", "verified_email"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("decision_maker", schema=None) as batch_op:
        batch_op.drop_index("ix_decision_maker_user_id_email")
        batch_op.drop_constraint("fk_decision_maker_user_id", type_="foreignkey")
        batch_op.drop_column("user_id")

    with op.batch_alter_table("radar_monitors", schema=None) as batch_op:
        batch_op.drop_index("ix_radar_monitors_user_id_created_at")
        batch_op.drop_index("ix_radar_monitors_status_next_run_at")
        batch_op.drop_index("ix_radar_monitors_user_id")

    op.drop_table("radar_monitors")
