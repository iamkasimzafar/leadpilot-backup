"""Notification request and response bodies."""

from datetime import datetime

from pydantic import BaseModel, Field, computed_field

from app.schemas.common import BaseSchema


class NotificationRead(BaseSchema):
    id: str
    kind: str
    title: str
    subtitle: str
    link: str | None = None
    read_at: datetime | None = None
    created_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_read(self) -> bool:
        """Flattened for the client, which only cares read / unread."""
        return self.read_at is not None


class NotificationPage(BaseModel):
    """A page of notifications plus the badge count.

    The unread total is always for the whole account, not the page, so the bell
    stays correct while looking at a filtered list.
    """

    items: list[NotificationRead]
    total: int
    unread: int
    page: int
    per_page: int


class UnreadCount(BaseModel):
    unread: int


class MarkReadRequest(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=200)
    read: bool = True
