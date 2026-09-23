"""add lookalike companies search fields to lead search runs

Revision ID: e4f8a2b3c6d0
Revises: d7e1f5a9b3c4
Create Date: 2026-09-22 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e4f8a2b3c6d0"
down_revision: str | None = "d7e1f5a9b3c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Two new search_type values (lookalike_discovery, lookalike_contacts) --
    # no migration needed for those, search_type is a plain string. These two
    # columns are what the lookalike flow actually needs:
    #
    #  discovered_domains_json  a discovery run's results (the list the user
    #                           picks from). NULL on every other kind of run.
    #  source_run_id            a contacts run's originating discovery run,
    #                           so the UI can link back to "the list you
    #                           picked from". NULL on every other kind of run,
    #                           and on a contacts run whose discovery run was
    #                           later deleted.
    with op.batch_alter_table("lead_search_run", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("discovered_domains_json", sa.Text(), nullable=True)
        )
        batch_op.add_column(sa.Column("source_run_id", sa.String(length=36), nullable=True))
        batch_op.create_foreign_key(
            "fk_lead_search_run_source_run_id",
            "lead_search_run",
            ["source_run_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("lead_search_run", schema=None) as batch_op:
        batch_op.drop_constraint(
            "fk_lead_search_run_source_run_id", type_="foreignkey"
        )
        batch_op.drop_column("source_run_id")
        batch_op.drop_column("discovered_domains_json")
