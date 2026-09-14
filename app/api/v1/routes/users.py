"""User endpoints.

A worked example of the route -> service -> repository flow.
"""

from fastapi import APIRouter

from app.api.deps import CurrentUser, DbSession, Pagination
from app.core.exceptions import NotFoundError
from app.repositories.user import UserRepository
from app.schemas.common import Page
from app.schemas.user import UserRead

router = APIRouter()


@router.get("", response_model=Page[UserRead], summary="List users")
async def list_users(
    db: DbSession,
    pagination: Pagination,
    current_user: CurrentUser,
) -> Page[UserRead]:
    """Paginated user list."""
    repo = UserRepository(db)
    users = await repo.list(offset=pagination.offset, limit=pagination.limit)
    total = await repo.count()

    return Page.create(
        items=[UserRead.model_validate(u) for u in users],
        total=total,
        page=pagination.page,
        per_page=pagination.per_page,
    )


@router.get("/{user_id}", response_model=UserRead, summary="Get a user")
async def get_user(
    user_id: str, db: DbSession, current_user: CurrentUser
) -> UserRead:
    user = await UserRepository(db).get(user_id)
    if user is None:
        raise NotFoundError("No user with that id.")

    return UserRead.model_validate(user)
