"""Data access for notifications."""

from datetime import UTC, datetime

from sqlalchemy import func, select, update

from app.models.notification import Notification
from app.repositories.base import BaseRepository


class NotificationRepository(BaseRepository[Notification]):
    model = Notification

    async def list_for_user(
        self, user_id: str, *, offset: int = 0, limit: int = 20, unread_only: bool = False
    ) -> list[Notification]:
        """Newest first."""
        stmt = select(Notification).where(Notification.user_id == user_id)
        if unread_only:
            stmt = stmt.where(Notification.read_at.is_(None))

        stmt = stmt.order_by(Notification.created_at.desc()).offset(offset).limit(limit)
        result = await self.db.execute(stmt)

        return list(result.scalars().all())

    async def count_for_user(self, user_id: str, *, unread_only: bool = False) -> int:
        stmt = (
            select(func.count())
            .select_from(Notification)
            .where(Notification.user_id == user_id)
        )
        if unread_only:
            stmt = stmt.where(Notification.read_at.is_(None))

        result = await self.db.execute(stmt)

        return result.scalar_one()

    async def get_for_user(
        self, user_id: str, notification_id: str
    ) -> Notification | None:
        """Scoped by user so one account can never touch another's row."""
        return await self.find_one_by(id=notification_id, user_id=user_id)

    async def mark_read(self, user_id: str, ids: list[str], *, read: bool = True) -> int:
        """Flip the read flag on several rows at once. Returns rows changed.

        Filtered by user_id as well as id, so a forged id list cannot reach
        another account's notifications.
        """
        if not ids:
            return 0

        stmt = (
            update(Notification)
            .where(Notification.user_id == user_id, Notification.id.in_(ids))
            .values(read_at=datetime.now(UTC) if read else None)
        )
        result = await self.db.execute(stmt)
        await self.db.flush()

        return result.rowcount  # type: ignore[attr-defined,no-any-return]

    async def mark_all_read(self, user_id: str) -> int:
        """Mark every unread notification for a user as read."""
        stmt = (
            update(Notification)
            .where(Notification.user_id == user_id, Notification.read_at.is_(None))
            .values(read_at=datetime.now(UTC))
        )
        result = await self.db.execute(stmt)
        await self.db.flush()

        return result.rowcount  # type: ignore[attr-defined,no-any-return]
