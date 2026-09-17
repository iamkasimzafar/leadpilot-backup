"""add company and decision maker tables

Revision ID: c1d83a6f92b7
Revises: 9b2f5c7a1e44
Create Date: 2026-09-16 15:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c1d83a6f92b7"
down_revision: str | None = "9b2f5c7a1e44"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Written by hand, mirroring app/models/lead.py.
    op.create_table(
        "company",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=True),
        sa.Column("company_name", sa.String(length=255), nullable=False),
        sa.Column("website", sa.String(length=500), nullable=True),
        sa.Column("location", sa.String(length=255), nullable=True),
        sa.Column("industry", sa.String(length=255), nullable=True),
        sa.Column("company_size", sa.String(length=64), nullable=True),
        sa.Column("hq_phone", sa.String(length=64), nullable=True),
        sa.Column("extra_json", sa.Text(), nullable=False),
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
        # The lead outlives the run that found it; only the link is cleared.
        sa.ForeignKeyConstraint(
            ["run_id"], ["lead_search_run.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("company", schema=None) as batch_op:
        batch_op.create_index(
            "ix_company_user_id_created_at", ["user_id", "created_at"], unique=False
        )
        batch_op.create_index("ix_company_run_id", ["run_id"], unique=False)

    op.create_table(
        "decision_maker",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=False),
        sa.Column("job_title", sa.String(length=255), nullable=True),
        sa.Column("verified_email", sa.String(length=320), nullable=True),
        sa.Column("email_status", sa.String(length=32), nullable=True),
        sa.Column("linkedin_url", sa.String(length=500), nullable=True),
        sa.Column("phone_number", sa.String(length=64), nullable=True),
        sa.Column("whatsapp_status", sa.String(length=32), nullable=True),
        sa.Column("extra_json", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["company_id"], ["company.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("decision_maker", schema=None) as batch_op:
        batch_op.create_index(
            "ix_decision_maker_company_id", ["company_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("decision_maker", schema=None) as batch_op:
        batch_op.drop_index("ix_decision_maker_company_id")
    op.drop_table("decision_maker")

    with op.batch_alter_table("company", schema=None) as batch_op:
        batch_op.drop_index("ix_company_run_id")
        batch_op.drop_index("ix_company_user_id_created_at")
    op.drop_table("company")
