"""Authentication endpoints.

Route shapes are defined; the bodies delegate to AuthService, which is where
the real logic goes once a User model exists.
"""

from typing import Any

from fastapi import APIRouter, status

from app.api.deps import CurrentUser, DbSession
from app.schemas.auth import LoginRequest, RefreshRequest, RegisterRequest, TokenPair

router = APIRouter()


@router.post("/register", response_model=TokenPair, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest, db: DbSession) -> TokenPair:
    """Create an account and return a fresh token pair."""
    raise NotImplementedError("Wire to AuthService.register once User exists.")


@router.post("/login", response_model=TokenPair)
async def login(payload: LoginRequest, db: DbSession) -> TokenPair:
    """Exchange credentials for an access/refresh token pair."""
    raise NotImplementedError("Wire to AuthService.authenticate once User exists.")


@router.post("/refresh", response_model=TokenPair)
async def refresh(payload: RefreshRequest, db: DbSession) -> TokenPair:
    """Exchange a valid refresh token for a new pair."""
    raise NotImplementedError("Wire to AuthService.refresh once User exists.")


@router.get("/me")
async def read_me(current_user: CurrentUser) -> dict[str, Any]:
    """Return the authenticated user. Proves the bearer flow end to end."""
    return {"user": current_user}
