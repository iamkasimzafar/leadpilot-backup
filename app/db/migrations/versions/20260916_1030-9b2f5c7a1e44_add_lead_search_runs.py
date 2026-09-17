"""add lead search runs and progress events

Revision ID: 9b2f5c7a1e44
Revises: 7c4e1a9b2d38
Create Date: 2026-09-16 10:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9b2f5c7a1e44"
down_revision: str | None = "7c4e1a9b2d38"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Written by hand, mirroring app/models/lead_search.py.
    op.create_table(
        "lead_search_run",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("callback_token", sa.String(length=64), nullable=False),
        sa.Column("original_keyword", sa.String(length=255), nullable=False),
        sa.Column("keywords_json", sa.Text(), nullable=False),
        sa.Column("keyword_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("stage", sa.String(length=32), nullable=False),
        sa.Column("companies_found", sa.Integer(), nullable=False),
        sa.Column("contacts_found", sa.Integer(), nullable=False),
        sa.Column("error", sa.String(length=500), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
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
    with op.batch_alter_table("lead_search_run", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_lead_search_run_callback_token"),
            ["callback_token"],
            unique=False,
        )
        batch_op.create_index(
            "ix_lead_search_run_user_id_created_at",
            ["user_id", "created_at"],
            unique=False,
        )

    op.create_table(
        "lead_search_event",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("stage", sa.String(length=32), nullable=False),
        sa.Column("message", sa.String(length=500), nullable=True),
        sa.Column("count", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["lead_search_run.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("lead_search_event", schema=None) as batch_op:
        batch_op.create_index(
            "ix_lead_search_event_run_id_created_at",
            ["run_id", "created_at"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("lead_search_event", schema=None) as batch_op:
        batch_op.drop_index("ix_lead_search_event_run_id_created_at")
    op.drop_table("lead_search_event")

    with op.batch_alter_table("lead_search_run", schema=None) as batch_op:
        batch_op.drop_index("ix_lead_search_run_user_id_created_at")
        batch_op.drop_index(batch_op.f("ix_lead_search_run_callback_token"))
    op.drop_table("lead_search_run")
