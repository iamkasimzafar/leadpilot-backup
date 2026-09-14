"""User endpoints.

A worked example of the route -> service -> repository flow. Bodies raise
NotImplementedError until a User model is added.
"""

from typing import Any

from fastapi import APIRouter

from app.api.deps import CurrentUser, DbSession, Pagination

router = APIRouter()


@router.get("", summary="List users")
async def list_users(
    db: DbSession,
    pagination: Pagination,
    current_user: CurrentUser,
) -> dict[str, Any]:
    """Paginated user list. Returns a Page[UserRead] once wired up."""
    raise NotImplementedError("Wire to UserService.list once User exists.")


@router.get("/{user_id}", summary="Get a user")
async def get_user(
    user_id: str, db: DbSession, current_user: CurrentUser
) -> dict[str, Any]:
    raise NotImplementedError("Wire to UserService.get once User exists.")
