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


def _create_engine() -> AsyncEngine:
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
    return create_async_engine(url, **kwargs)


engine: AsyncEngine = _create_engine()

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
