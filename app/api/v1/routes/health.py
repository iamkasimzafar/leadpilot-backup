"""Liveness and readiness probes."""

from fastapi import APIRouter, status
from sqlalchemy import text

from app.api.deps import DbSession
from app.core.config import settings

router = APIRouter()


@router.get("/health", summary="Liveness probe")
async def health() -> dict[str, str]:
    """Returns OK as long as the process is serving requests."""
    return {"status": "ok", "environment": settings.ENVIRONMENT}


@router.get("/health/ready", summary="Readiness probe")
async def readiness(db: DbSession) -> dict[str, str]:
    """Verifies the database is reachable."""
    await db.execute(text("SELECT 1"))
    return {"status": "ready", "database": "ok"}


@router.get("/health/live", status_code=status.HTTP_204_NO_CONTENT)
async def liveness() -> None:
    return None
