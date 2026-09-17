"""Data access for lead-search runs and their progress events."""

from sqlalchemy import select

from app.models.lead_search import LeadSearchEvent, LeadSearchRun
from app.repositories.base import BaseRepository


class LeadSearchRunRepository(BaseRepository[LeadSearchRun]):
    model = LeadSearchRun

    async def get_for_user(self, user_id: str, run_id: str) -> LeadSearchRun | None:
        """Scoped by user, so one account cannot read another's run."""
        return await self.find_one_by(id=run_id, user_id=user_id)

    async def get_for_callback(self, run_id: str, token: str) -> LeadSearchRun | None:
        """Look a run up the way the webhook does: id plus its own secret.

        n8n has no user session, so the token is what proves the caller is the
        workflow handling this particular run.
        """
        return await self.find_one_by(id=run_id, callback_token=token)

    async def list_for_user(
        self, user_id: str, *, offset: int = 0, limit: int = 20
    ) -> list[LeadSearchRun]:
        stmt = (
            select(LeadSearchRun)
            .where(LeadSearchRun.user_id == user_id)
            .order_by(LeadSearchRun.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        result = await self.db.execute(stmt)

        return list(result.scalars().all())


class LeadSearchEventRepository(BaseRepository[LeadSearchEvent]):
    model = LeadSearchEvent

    async def list_for_run(self, run_id: str) -> list[LeadSearchEvent]:
        """Oldest first: this is the run's history in the order it happened."""
        stmt = (
            select(LeadSearchEvent)
            .where(LeadSearchEvent.run_id == run_id)
            .order_by(LeadSearchEvent.created_at.asc())
        )
        result = await self.db.execute(stmt)

        return list(result.scalars().all())
