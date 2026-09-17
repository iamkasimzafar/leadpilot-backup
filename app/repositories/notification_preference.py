"""Data access for notification preferences."""

from app.models.notification_preference import NotificationPreference
from app.repositories.base import BaseRepository


class NotificationPreferenceRepository(BaseRepository[NotificationPreference]):
    model = NotificationPreference

    async def get_for_user(self, user_id: str) -> NotificationPreference | None:
        return await self.find_one_by(user_id=user_id)
