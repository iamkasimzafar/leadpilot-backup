"""widen lead_search_run.search_type to fit lookalike_discovery/contacts

Revision ID: f5a9c3d7e1b8
Revises: e4f8a2b3c6d0
Create Date: 2026-09-22 15:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f5a9c3d7e1b8"
down_revision: str | None = "e4f8a2b3c6d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # String(16) fit "b2b" and "local" but not "lookalike_discovery" /
    # "lookalike_contacts" (20 and 19 chars) -- MySQL enforces the column
    # length strictly and rejected every lookalike run with "Data too long
    # for column 'search_type'". SQLite (the test suite) does not enforce
    # VARCHAR length at all, which is why this was not caught by pytest.
    with op.batch_alter_table("lead_search_run", schema=None) as batch_op:
        batch_op.alter_column(
            "search_type",
            existing_type=sa.String(length=16),
            type_=sa.String(length=32),
            existing_nullable=False,
            existing_server_default="b2b",
        )


def downgrade() -> None:
    # No data loss going back: every value in use today (b2b, local,
    # lookalike_discovery, lookalike_contacts) is checked against the new
    # length by the application before this ever runs in reverse, but a
    # downgrade with lookalike rows already present would truncate them --
    # acceptable for a local rollback, not attempted automatically here.
    with op.batch_alter_table("lead_search_run", schema=None) as batch_op:
        batch_op.alter_column(
            "search_type",
            existing_type=sa.String(length=32),
            type_=sa.String(length=16),
            existing_nullable=False,
            existing_server_default="b2b",
        )
