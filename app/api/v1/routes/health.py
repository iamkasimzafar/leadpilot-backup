"""Liveness and readiness probes."""

from fastapi import APIRouter, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.api.deps import DbSession
from app.core.config import settings
from app.core.exceptions import AppError

router = APIRouter()


class DatabaseUnavailableError(AppError):
    """The API is up but cannot reach its database."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "database_unavailable"
    message = "Cannot reach the database."


@router.get("/health", summary="Liveness probe")
async def health() -> dict[str, str]:
    """Returns OK as long as the process is serving requests."""
    return {"status": "ok", "environment": settings.ENVIRONMENT}


@router.get("/health/ready", summary="Readiness probe")
async def readiness(db: DbSession) -> dict[str, str]:
    """Verifies the database is reachable.

    Returns 503 with an actionable message rather than a raw driver traceback,
    since the usual cause in local development is a closed SSH tunnel.
    """
    try:
        await db.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        raise DatabaseUnavailableError(
            "Cannot reach the database. In local development this usually means "
            "the SSH tunnel is not running -- start it with .\\scripts\\tunnel.ps1",
            details={"driver_error": str(exc).splitlines()[0][:200]},
        ) from exc

    return {"status": "ready", "database": "ok"}


@router.get("/health/live", status_code=status.HTTP_204_NO_CONTENT)
async def liveness() -> None:
    return None
