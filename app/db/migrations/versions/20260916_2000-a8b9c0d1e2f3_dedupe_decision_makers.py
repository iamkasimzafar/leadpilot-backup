"""dedupe decision makers left by concurrent results callbacks

Revision ID: a8b9c0d1e2f3
Revises: f6a1b2c3d4e5
Create Date: 2026-09-16 20:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a8b9c0d1e2f3"
down_revision: str | None = "f6a1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Before the results callback was serialised per run, parallel ingests each
    # re-inserted the same decision makers under the same company. Collapse
    # them: a contact is a duplicate if another row on the same company has
    # the same email (or, with no email, the same name) and sorts before it by
    # (created_at, id) -- one rule, one survivor. Data-only; no schema change.
    bind = op.get_bind()

    duplicates = bind.execute(
        sa.text(
            """
            SELECT d.id
            FROM decision_maker AS d
            WHERE EXISTS (
                SELECT 1
                FROM decision_maker AS k
                WHERE k.company_id = d.company_id
                  AND COALESCE(k.verified_email, k.full_name)
                      = COALESCE(d.verified_email, d.full_name)
                  AND (
                    k.created_at < d.created_at
                    OR (k.created_at = d.created_at AND k.id < d.id)
                  )
            )
            """
        )
    ).fetchall()

    for (contact_id,) in duplicates:
        bind.execute(
            sa.text("DELETE FROM decision_maker WHERE id = :id"), {"id": contact_id}
        )


def downgrade() -> None:
    # Deleted duplicates cannot be reconstructed; nothing to undo.
    pass
