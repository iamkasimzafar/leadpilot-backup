"""Notification preference request and response bodies."""

from pydantic import BaseModel, Field

from app.schemas.common import BaseSchema


class NotificationPreferenceRead(BaseSchema):
    """The full set of flags, always complete so the client never guesses."""

    lead: bool
    reply: bool
    monitor: bool
    credits: bool
    sequence: bool
    weekly_digest: bool


class NotificationPreferenceUpdate(BaseModel):
    """A partial update: omitted flags keep their stored value.

    Every field is optional so the settings panel can send one toggle without
    having to echo back the rest, which would race with another open tab.
    """

    lead: bool | None = Field(default=None)
    reply: bool | None = Field(default=None)
    monitor: bool | None = Field(default=None)
    credits: bool | None = Field(default=None)
    sequence: bool | None = Field(default=None)
    weekly_digest: bool | None = Field(default=None)

    def changes(self) -> dict[str, bool]:
        """Only the flags actually supplied, ready to apply to the row."""
        return {
            key: value
            for key, value in self.model_dump(exclude_unset=True).items()
            if value is not None
        }
