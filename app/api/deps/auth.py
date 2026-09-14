"""Authentication dependencies.

get_current_user decodes the bearer token and loads the user. The user lookup
is left as a TODO because no User model is defined yet -- wire it to
UserRepository once you add one.
"""

from typing import Annotated, Any

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt.exceptions import InvalidTokenError

from app.api.deps.db import DbSession
from app.core.exceptions import UnauthorizedError
from app.core.security import decode_token

# auto_error=False so we raise our own JSON-shaped error instead of FastAPI's.
bearer_scheme = HTTPBearer(auto_error=False)

BearerToken = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)]


async def get_current_user(credentials: BearerToken, db: DbSession) -> Any:
    """Resolve the authenticated user from the Authorization header."""
    if credentials is None:
        raise UnauthorizedError("Missing authentication credentials.", challenge="Bearer")

    try:
        payload = decode_token(credentials.credentials, expected_type="access")
    except InvalidTokenError as exc:
        raise UnauthorizedError("Invalid or expired token.", challenge="Bearer") from exc

    user_id = payload.get("sub")
    if not user_id:
        raise UnauthorizedError("Malformed token.", challenge="Bearer")

    # TODO: replace with a real lookup once the User model exists, e.g.
    #   user = await UserRepository(db).get(user_id)
    #   if user is None or not user.is_active:
    #       raise UnauthorizedError("User no longer active.")
    #   return user
    return {"id": user_id}


CurrentUser = Annotated[Any, Depends(get_current_user)]
