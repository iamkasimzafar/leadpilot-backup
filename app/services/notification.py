"""Notification business rules.

Creating a notification writes the row and then nudges any open SSE connection
for that user. The database is the source of truth; the push is only a hint to
fetch sooner.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.models.notification import Notification, NotificationKind
from app.repositories.notification import NotificationRepository
from app.services.base import BaseService
from app.services.notification_stream import notification_stream

log = get_logger(__name__)


class NotificationService(BaseService):
    def __init__(self, db: AsyncSession) -> None:
        super().__init__(db)
        self.notifications = NotificationRepository(db)

    # --- Reads --------------------------------------------------------------
    async def list_for_user(
        self, user_id: str, *, offset: int, limit: int, unread_only: bool = False
    ) -> tuple[list[Notification], int, int]:
        """Returns (page of notifications, total matching, unread count)."""
        items = await self.notifications.list_for_user(
            user_id, offset=offset, limit=limit, unread_only=unread_only
        )
        total = await self.notifications.count_for_user(user_id, unread_only=unread_only)
        unread = await self.notifications.count_for_user(user_id, unread_only=True)

        return items, total, unread

    async def unread_count(self, user_id: str) -> int:
        return await self.notifications.count_for_user(user_id, unread_only=True)

    async def get(self, user_id: str, notification_id: str) -> Notification:
        notification = await self.notifications.get_for_user(user_id, notification_id)
        if notification is None:
            raise NotFoundError("No notification with that id.")

        return notification

    # --- Writes -------------------------------------------------------------
    async def create(
        self,
        user_id: str,
        *,
        kind: NotificationKind,
        title: str,
        subtitle: str = "",
        link: str | None = None,
        commit: bool = True,
    ) -> Notification:
        """Raise a notification for one user.

        `commit=False` lets a caller group this with its own transaction (a
        purchase, say) so the notification and the thing it announces land
        together or not at all.
        """
        notification = await self.notifications.create(
            user_id=user_id,
            kind=kind.value,
            title=title,
            subtitle=subtitle,
            link=link,
        )

        if commit:
            await self.commit()
            # Only announce what is actually durable.
            notification_stream.publish(user_id, notification.id)

        log.info("notification.created", user_id=user_id, kind=kind.value)

        return notification

    @staticmethod
    def publish(user_id: str, notification_id: str) -> None:
        """Announce a notification created inside someone else's transaction,
        once that transaction has committed."""
        notification_stream.publish(user_id, notification_id)

    async def set_read(self, user_id: str, ids: list[str], *, read: bool) -> int:
        changed = await self.notifications.mark_read(user_id, ids, read=read)
        await self.commit()

        return changed

    async def mark_all_read(self, user_id: str) -> int:
        changed = await self.notifications.mark_all_read(user_id)
        await self.commit()

        return changed

    async def delete(self, user_id: str, notification_id: str) -> None:
        notification = await self.get(user_id, notification_id)
        await self.notifications.delete(notification)
        await self.commit()


__all__ = ["NotificationService"]
