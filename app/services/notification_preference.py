"""Notification preference business rules.

A user's row is created the first time it is needed rather than at registration,
so accounts that predate this feature behave correctly without a backfill. The
defaults on the model mean a freshly created row is opted in to every kind --
the same behaviour as having no row at all.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.notification import NotificationKind
from app.models.notification_preference import NotificationPreference
from app.repositories.notification_preference import NotificationPreferenceRepository
from app.services.base import BaseService

log = get_logger(__name__)


class NotificationPreferenceService(BaseService):
    def __init__(self, db: AsyncSession) -> None:
        super().__init__(db)
        self.preferences = NotificationPreferenceRepository(db)

    async def get_or_create(
        self, user_id: str, *, commit: bool = True
    ) -> NotificationPreference:
        """The user's preferences, creating the default row if absent."""
        existing = await self.preferences.get_for_user(user_id)
        if existing is not None:
            return existing

        created = await self.preferences.create(user_id=user_id)
        if commit:
            await self.commit()

        log.info("notification_preference.created", user_id=user_id)

        return created

    async def update(
        self, user_id: str, changes: dict[str, bool]
    ) -> NotificationPreference:
        """Apply a partial update and return the full, current row."""
        preference = await self.get_or_create(user_id, commit=False)

        if changes:
            await self.preferences.update(preference, **changes)

        await self.commit()

        log.info(
            "notification_preference.updated",
            user_id=user_id,
            changed=sorted(changes),
        )

        return preference

    async def allows(self, user_id: str, kind: NotificationKind | str) -> bool:
        """Whether this user wants notifications of this kind.

        Deliberately does NOT create a row: this runs on the delivery path, and
        a read should not write. A user with no row yet wants everything.
        """
        preference = await self.preferences.get_for_user(user_id)
        if preference is None:
            return True

        return preference.allows(kind)


__all__ = ["NotificationPreferenceService"]
