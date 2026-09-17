"""Notification preference endpoints.

Backs the notification tab of the settings page: which kinds of notification the
signed-in user wants to receive.
"""

from fastapi import APIRouter

from app.api.deps import CurrentUser, DbSession
from app.schemas.notification_preference import (
    NotificationPreferenceRead,
    NotificationPreferenceUpdate,
)
from app.services.notification_preference import NotificationPreferenceService

router = APIRouter()


@router.get(
    "",
    response_model=NotificationPreferenceRead,
    summary="Get notification preferences",
)
async def get_preferences(
    db: DbSession, current_user: CurrentUser
) -> NotificationPreferenceRead:
    """The signed-in user's flags, creating the default row on first read."""
    preference = await NotificationPreferenceService(db).get_or_create(current_user.id)

    return NotificationPreferenceRead.model_validate(preference)


@router.patch(
    "",
    response_model=NotificationPreferenceRead,
    summary="Update notification preferences",
)
async def update_preferences(
    payload: NotificationPreferenceUpdate, db: DbSession, current_user: CurrentUser
) -> NotificationPreferenceRead:
    """Apply a partial update.

    Omitted flags keep their stored value, so a client can send one toggle
    without echoing the rest back and overwriting a change made elsewhere.
    """
    preference = await NotificationPreferenceService(db).update(
        current_user.id, payload.changes()
    )

    return NotificationPreferenceRead.model_validate(preference)
