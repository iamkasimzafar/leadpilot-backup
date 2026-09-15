"""Notification ORM model.

One row per notification per user. `read_at` doubles as the read flag and the
audit of when it happened, so no separate boolean is needed.

`kind` is a plain string rather than a database ENUM: portable across MySQL and
the SQLite used by the test suite, and adding a kind never needs a migration.
"""

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _uuid_str() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(UTC)


class NotificationKind(StrEnum):
    """Matches the icon/colour map the frontend keys off."""

    LEAD = "lead"
    REPLY = "reply"
    MONITOR = "monitor"
    CREDITS = "credits"
    SEQUENCE = "sequence"


class Notification(Base):
    __table_args__ = (
        # The list query: newest first for one user.
        Index("ix_notification_user_id_created_at", "user_id", "created_at"),
        # The unread-count query.
        Index("ix_notification_user_id_read_at", "user_id", "read_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid_str)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    )

    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    subtitle: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # Where clicking the notification should take the user, e.g. "/wallet".
    # Null when there is nowhere useful to go.
    link: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Null until read. Carries the timestamp so "when did they see it" is
    # answerable without a second column.
    read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Python-side default (microsecond precision) as well as the server default,
    # so two notifications written in the same second still order correctly.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        server_default=func.now(),
        nullable=False,
    )

    @property
    def is_read(self) -> bool:
        return self.read_at is not None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Notification user_id={self.user_id} {self.kind}>"
