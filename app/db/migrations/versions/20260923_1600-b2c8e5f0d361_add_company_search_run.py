"""Add company_search_run: which runs found which company.

Written by hand, mirroring CompanySearchRun in app/models/lead.py.

`company.run_id` is a single pointer, and a company the user already holds is
reused rather than duplicated when a later search finds it again -- which moves
that pointer onto the newer run. One pointer cannot record two runs, so every
older search that had found the same company quietly lost its results: the run
still reported "3 companies", but opening it listed none.

This table records the link instead of overwriting it, one row per
(run, company). The backfill seeds it from the pointers that survive, which
recovers every run whose companies were not later stolen; a run that already
lost its companies to a newer one cannot be reconstructed, because the
information needed to do so was overwritten rather than stored.

Revision ID: b2c8e5f0d361
Revises: a1b7d4e9c250
Create Date: 2026-09-23 16:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b2c8e5f0d361"
down_revision: str | None = "a1b7d4e9c250"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "company_search_run",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["run_id"], ["lead_search_run.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["company_id"], ["company.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "company_id", name="uq_company_search_run"),
    )

    with op.batch_alter_table("company_search_run", schema=None) as batch_op:
        batch_op.create_index("ix_company_search_run_run_id", ["run_id"], unique=False)
        batch_op.create_index(
            "ix_company_search_run_company_id", ["company_id"], unique=False
        )

    # Seed from the pointers still in place. This is everything that can be
    # recovered: where a later run took a company over, the link to the run
    # that found it first was overwritten, not recorded anywhere else.
    op.execute(
        """
        INSERT INTO company_search_run (id, run_id, company_id, created_at)
        SELECT
            """
        + (
            "UUID()"
            if op.get_bind().dialect.name == "mysql"
            else "lower(hex(randomblob(16)))"
        )
        + """,
            c.run_id,
            c.id,
            c.created_at
        FROM company c
        WHERE c.run_id IS NOT NULL
        """
    )


def downgrade() -> None:
    with op.batch_alter_table("company_search_run", schema=None) as batch_op:
        batch_op.drop_index("ix_company_search_run_company_id")
        batch_op.drop_index("ix_company_search_run_run_id")

    op.drop_table("company_search_run")
