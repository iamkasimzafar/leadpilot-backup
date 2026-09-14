"""Generic async repository.

Encapsulates SQLAlchemy so services stay query-free. Subclass per model:

    class UserRepository(BaseRepository[User]):
        model = User

        async def get_by_email(self, email: str) -> User | None:
            return await self.find_one_by(email=email)
"""

from typing import Any, Generic, TypeVar

from sqlalchemy import Select, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import Base

ModelT = TypeVar("ModelT", bound=Base)


class BaseRepository(Generic[ModelT]):
    model: type[ModelT]

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    def _base_query(self) -> Select[tuple[ModelT]]:
        return select(self.model)

    async def get(self, id_: Any) -> ModelT | None:
        return await self.db.get(self.model, id_)

    async def find_one_by(self, **filters: Any) -> ModelT | None:
        result = await self.db.execute(self._base_query().filter_by(**filters))
        return result.scalar_one_or_none()

    async def list(
        self, *, offset: int = 0, limit: int = 20, **filters: Any
    ) -> list[ModelT]:
        stmt = self._base_query().filter_by(**filters).offset(offset).limit(limit)
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def count(self, **filters: Any) -> int:
        stmt = select(func.count()).select_from(self.model).filter_by(**filters)
        result = await self.db.execute(stmt)
        return result.scalar_one()

    async def create(self, **values: Any) -> ModelT:
        instance = self.model(**values)
        self.db.add(instance)
        await self.db.flush()
        await self.db.refresh(instance)
        return instance

    async def update(self, instance: ModelT, **values: Any) -> ModelT:
        for key, value in values.items():
            setattr(instance, key, value)
        await self.db.flush()
        await self.db.refresh(instance)
        return instance

    async def delete(self, instance: ModelT) -> None:
        await self.db.delete(instance)
        await self.db.flush()

    async def delete_by_id(self, id_: Any) -> int:
        """Delete without loading the row first. Returns the rows removed."""
        result = await self.db.execute(
            delete(self.model).where(self.model.id == id_)  # type: ignore[attr-defined]
        )
        await self.db.flush()
        return result.rowcount  # type: ignore[attr-defined,no-any-return]
