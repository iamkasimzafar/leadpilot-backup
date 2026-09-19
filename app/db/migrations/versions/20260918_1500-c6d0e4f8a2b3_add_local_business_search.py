"""add local business search fields to lead search runs

Revision ID: c6d0e4f8a2b3
Revises: b5c9d3e7f1a2
Create Date: 2026-09-18 15:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c6d0e4f8a2b3"
down_revision: str | None = "b5c9d3e7f1a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A run is now one of two kinds: the original B2B company search, or a
    # Local Offline Business search (Google Maps data). Existing rows are all
    # B2B, which the server default records without a backfill.
    with op.batch_alter_table("lead_search_run", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "search_type",
                sa.String(length=16),
                server_default="b2b",
                nullable=False,
            )
        )
        # Local searches only; NULL on a B2B run.
        batch_op.add_column(sa.Column("location", sa.String(length=255), nullable=True))
        batch_op.add_column(
            sa.Column("business_categories_json", sa.Text(), nullable=True)
        )
        # Google Maps rating / review-count window. NULL means "no bound".
        batch_op.add_column(sa.Column("min_rating", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("max_rating", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("min_reviews", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("max_reviews", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("lead_search_run", schema=None) as batch_op:
        batch_op.drop_column("max_reviews")
        batch_op.drop_column("min_reviews")
        batch_op.drop_column("max_rating")
        batch_op.drop_column("min_rating")
        batch_op.drop_column("business_categories_json")
        batch_op.drop_column("location")
        batch_op.drop_column("search_type")
