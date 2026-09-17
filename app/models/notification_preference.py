"""Notification preference ORM model.

One row per user, holding a flag per notification kind. The row is created on
demand: an account with no row yet is treated as "everything on" (see
NotificationPreferenceService.get_or_create), so a user who has never opened the
settings panel still receives notifications.

The columns are named for the values of `NotificationKind` rather than for the
labels the settings panel happens to show, so enforcement is a direct lookup by
kind and adding a kind is a migration rather than a mapping table.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.notification import NotificationKind


class NotificationPreference(Base):
    """Which notifications one user wants to receive, in-app."""

    # The user owns exactly one row, so the foreign key is the primary key:
    # a duplicate is impossible without a second table constraint.
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("user.id", ondelete="CASCADE"),
        primary_key=True,
    )

    # One flag per NotificationKind. Defaulting to true on both the Python and
    # the server side means a row inserted by either path starts opted-in.
    lead: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=func.true(), nullable=False
    )
    reply: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=func.true(), nullable=False
    )
    monitor: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=func.true(), nullable=False
    )
    credits: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=func.true(), nullable=False
    )
    sequence: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=func.true(), nullable=False
    )

    # Not a NotificationKind: nothing raises a "digest" notification. It records
    # a standing choice for the weekly summary email, which is off by default
    # because it is a recurring outbound message rather than an event.
    weekly_digest: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=func.false(), nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def allows(self, kind: NotificationKind | str) -> bool:
        """Whether a notification of this kind should be delivered.

        An unknown kind is allowed rather than dropped: a new kind that predates
        its column must not be silently swallowed.
        """
        value = kind.value if isinstance(kind, NotificationKind) else str(kind)

        return bool(getattr(self, value, True))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<NotificationPreference user_id={self.user_id}>"
