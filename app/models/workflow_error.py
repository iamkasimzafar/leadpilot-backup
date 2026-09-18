"""Workflow error ORM model.

Every failure n8n reports is kept, including the ones that cannot be tied to a
user's run: an unattributable error is still the best evidence of a broken
workflow, and dropping it would hide the outage.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _uuid_str() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(UTC)


class WorkflowError(Base):
    """One failure reported by the n8n error trigger."""

    __table_args__ = (
        Index("ix_workflow_error_created_at", "created_at"),
        Index("ix_workflow_error_run_id", "run_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid_str)

    # Null when the error could not be attributed: n8n's error trigger does not
    # carry our run id unless the workflow passes it through. The row is still
    # recorded so the failure is not lost.
    run_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("lead_search_run.id", ondelete="SET NULL"),
        nullable=True,
    )
    user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("user.id", ondelete="CASCADE"), nullable=True
    )

    # n8n's own identifiers, kept verbatim so a failure can be found in the
    # n8n UI from what we show the user.
    workflow_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    execution_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    error_message: Mapped[str] = mapped_column(Text, nullable=False)
    last_node_executed: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        server_default=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<WorkflowError {self.execution_id} {self.last_node_executed}>"
