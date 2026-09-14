"""Reusable FastAPI dependencies.

Import the annotated aliases (DbSession, CurrentUser) directly in route
signatures rather than repeating Depends(...) everywhere.
"""

from app.api.deps.auth import (
    CurrentSuperuser,
    CurrentUser,
    get_current_superuser,
    get_current_user,
)
from app.api.deps.db import DbSession
from app.api.deps.pagination import Pagination, PaginationParams

__all__ = [
    "CurrentSuperuser",
    "CurrentUser",
    "DbSession",
    "Pagination",
    "PaginationParams",
    "get_current_superuser",
    "get_current_user",
]
