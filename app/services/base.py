"""Service layer base.

Services hold business rules and own the transaction boundary; routes stay thin
and repositories stay query-only. Raise app.core.exceptions errors from here.
"""

from sqlalchemy.ext.asyncio import AsyncSession


class BaseService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def commit(self) -> None:
        """Commit the unit of work. Call once per successful operation."""
        await self.db.commit()
