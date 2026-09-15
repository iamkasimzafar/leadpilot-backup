"""Aggregates every v1 route module into a single router."""

from fastapi import APIRouter

from app.api.v1.routes import auth, billing, health, lead_radar, notifications, users

api_router = APIRouter()

api_router.include_router(health.router, tags=["health"])
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(users.router, prefix="/users", tags=["users"])
api_router.include_router(lead_radar.router, prefix="/lead-radar", tags=["lead-radar"])
api_router.include_router(billing.router, prefix="/billing", tags=["billing"])
api_router.include_router(
    notifications.router, prefix="/notifications", tags=["notifications"]
)
