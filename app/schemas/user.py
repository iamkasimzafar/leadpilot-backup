"""User response bodies."""

from datetime import datetime

from app.schemas.common import BaseSchema


class UserRead(BaseSchema):
    id: str
    email: str
    full_name: str | None = None
    is_active: bool
    is_superuser: bool
    is_verified: bool
    created_at: datetime
