"""unique company website per user

Revision ID: e5f0a3b7c9d1
Revises: d4e7b1c85f30
Create Date: 2026-09-16 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5f0a3b7c9d1"
down_revision: str | None = "d4e7b1c85f30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Concurrent results callbacks could insert the same company several times
    # before this constraint existed, so collapse any duplicates first: keep
    # the earliest row per (user_id, website) and drop the rest. Their
    # decision makers go with them (FK ON DELETE CASCADE).
    bind = op.get_bind()

    # One survivor per group, chosen by a single rule -- the smallest
    # (created_at, id) -- so "earliest" and "tie-break" can never disagree
    # about who stays. (A burst lands inside one second, so created_at alone
    # would leave every copy standing.) A row is a duplicate if some other row
    # in its group sorts before it.
    duplicates = bind.execute(
        sa.text(
            """
            SELECT c.id
            FROM company AS c
            WHERE c.website IS NOT NULL
              AND EXISTS (
                SELECT 1
                FROM company AS k
                WHERE k.user_id = c.user_id
                  AND k.website = c.website
                  AND (
                    k.created_at < c.created_at
                    OR (k.created_at = c.created_at AND k.id < c.id)
                  )
              )
            """
        )
    ).fetchall()

    to_delete = sorted({row[0] for row in duplicates})
    for company_id in to_delete:
        # Explicit, not via ON DELETE CASCADE: SQLite only honours foreign
        # keys when the connection asks it to, and a migration should not
        # leave orphans behind on any engine.
        bind.execute(
            sa.text("DELETE FROM decision_maker WHERE company_id = :id"),
            {"id": company_id},
        )
        bind.execute(sa.text("DELETE FROM company WHERE id = :id"), {"id": company_id})

    with op.batch_alter_table("company", schema=None) as batch_op:
        batch_op.create_unique_constraint(
            "uq_company_user_id_website", ["user_id", "website"]
        )


def downgrade() -> None:
    with op.batch_alter_table("company", schema=None) as batch_op:
        batch_op.drop_constraint("uq_company_user_id_website", type_="unique")
