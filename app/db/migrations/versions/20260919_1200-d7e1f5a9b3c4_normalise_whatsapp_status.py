"""normalise stored whatsapp_status labels

Revision ID: d7e1f5a9b3c4
Revises: c6d0e4f8a2b3
Create Date: 2026-09-19 12:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d7e1f5a9b3c4"
down_revision: str | None = "c6d0e4f8a2b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Results are now normalised on ingest (normalise_whatsapp_status) to
# "Active" / "Not Active" / NULL. Rows saved before that carry whatever the
# workflow wrote. The one that did harm is "Not Checked": it is not NULL, so
# the UI showed the contact as reachable on WhatsApp and the reports and
# billing treated it as a check that had been made.
_UNCHECKED = ("not checked", "unchecked", "pending", "skipped", "error")
_POSITIVE = ("valid", "exists", "true", "yes", "registered")
_NEGATIVE = ("invalid", "inactive", "not found", "false", "no", "not registered")


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.execute(
        "UPDATE decision_maker SET whatsapp_status = NULL "
        f"WHERE LOWER(TRIM(whatsapp_status)) IN ({_in(_UNCHECKED)})"
    )
    op.execute(
        "UPDATE decision_maker SET whatsapp_status = 'Active' "
        f"WHERE LOWER(TRIM(whatsapp_status)) IN ({_in(_POSITIVE)})"
    )
    op.execute(
        "UPDATE decision_maker SET whatsapp_status = 'Not Active' "
        f"WHERE LOWER(TRIM(whatsapp_status)) IN ({_in(_NEGATIVE)})"
    )


def downgrade() -> None:
    # The original wording is not recoverable, and nothing reads it any more.
    pass
