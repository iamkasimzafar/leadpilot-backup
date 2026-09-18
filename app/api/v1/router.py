"""Aggregates every v1 route module into a single router."""

from fastapi import APIRouter

from app.api.v1.routes import (
    auth,
    billing,
    dashboard,
    health,
    lead_radar,
    leads,
    notification_preferences,
    notifications,
    users,
)

api_router = APIRouter()

api_router.include_router(health.router, tags=["health"])
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(users.router, prefix="/users", tags=["users"])
api_router.include_router(lead_radar.router, prefix="/lead-radar", tags=["lead-radar"])
api_router.include_router(billing.router, prefix="/billing", tags=["billing"])
api_router.include_router(leads.router, prefix="/leads", tags=["leads"])
api_router.include_router(dashboard.router, prefix="/dashboard", tags=["dashboard"])
api_router.include_router(
    notifications.router, prefix="/notifications", tags=["notifications"]
)

# Mounted before nothing else claims the path: "/notifications/preferences" would
# collide with the "/{notification_id}" delete route, so preferences live under
# their own prefix.
api_router.include_router(
    notification_preferences.router,
    prefix="/notification-preferences",
    tags=["notifications"],
)
