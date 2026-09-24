"""Async SQLAlchemy engine and session factory."""

from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings


def create_engine() -> AsyncEngine:
    """A fresh engine for the configured database.

    The module-level `engine` below serves the API. Celery tasks build their
    own through this instead: each task runs under `asyncio.run`, which opens
    a new event loop, and asyncmy connections pooled on a previous loop cannot
    be reused on the next one. A per-task engine, disposed when the task ends,
    sidesteps that entirely.
    """
    url = settings.sqlalchemy_database_uri
    kwargs: dict[str, Any] = {"echo": settings.SQL_ECHO, "future": True}

    # SQLite has no real connection pool to size.
    if not url.startswith("sqlite"):
        kwargs.update(
            pool_size=settings.DB_POOL_SIZE,
            max_overflow=settings.DB_MAX_OVERFLOW,
            # MySQL closes idle connections (wait_timeout, default 8h). Recycle
            # below that and check liveness before handing a connection out.
            pool_recycle=settings.DB_POOL_RECYCLE,
            pool_pre_ping=True,
        )

    # Every timestamp the app writes itself is UTC (datetime.now(UTC)), but
    # server-side defaults (created_at / updated_at via NOW()) follow the MySQL
    # server's own time zone, which is not UTC on the hosted database. Mixing
    # the two skewed run durations by hours and would break any "older than N
    # minutes" comparison. Pinning the session zone makes NOW() UTC as well.
    if url.startswith("mysql"):
        kwargs["connect_args"] = {"init_command": "SET time_zone = '+00:00'"}

    return create_async_engine(url, **kwargs)


engine: AsyncEngine = create_engine()

SessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a session that is rolled back on error."""
    async with SessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
