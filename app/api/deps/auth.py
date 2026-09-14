"""Authentication dependencies.

get_current_user decodes the bearer token and loads the user from the database.
"""

from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt.exceptions import InvalidTokenError

from app.api.deps.db import DbSession
from app.core.exceptions import ForbiddenError, UnauthorizedError
from app.core.security import decode_token
from app.models.user import User
from app.repositories.user import UserRepository

# auto_error=False so we raise our own JSON-shaped error instead of FastAPI's.
bearer_scheme = HTTPBearer(auto_error=False)

BearerToken = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)]


async def get_current_user(credentials: BearerToken, db: DbSession) -> User:
    """Resolve the authenticated user from the Authorization header."""
    if credentials is None:
        raise UnauthorizedError(
            "Missing authentication credentials.", challenge="Bearer"
        )

    try:
        payload = decode_token(credentials.credentials, expected_type="access")
    except InvalidTokenError as exc:
        raise UnauthorizedError("Invalid or expired token.", challenge="Bearer") from exc

    user_id = payload.get("sub")
    if not user_id:
        raise UnauthorizedError("Malformed token.", challenge="Bearer")

    user = await UserRepository(db).get(user_id)
    if user is None or not user.is_active:
        raise UnauthorizedError("User no longer active.", challenge="Bearer")

    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def get_current_superuser(current_user: CurrentUser) -> User:
    """Restrict a route to superusers."""
    if not current_user.is_superuser:
        raise ForbiddenError()
    return current_user


CurrentSuperuser = Annotated[User, Depends(get_current_superuser)]
