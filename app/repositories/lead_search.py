"""Data access for lead-search runs and their progress events."""

from datetime import datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.sql.elements import ColumnElement

from app.models.lead_search import LeadSearchEvent, LeadSearchRun, RunStatus
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

    async def get_by_execution_id(self, execution_id: str) -> LeadSearchRun | None:
        """The run an n8n execution is handling, once a callback told us which.

        Execution ids are unique within an n8n instance, so a match is exact.
        """
        return await self.find_one_by(n8n_execution_id=execution_id)

    async def sole_running_since(self, since: datetime) -> LeadSearchRun | None:
        """The one run in flight, if there is exactly one.

        The Error Trigger's payload carries no run id. When only a single
        search is running, the failing execution can only be that one; with
        two or more it is ambiguous and nothing is returned.
        """
        stmt = (
            select(LeadSearchRun)
            .where(
                LeadSearchRun.status == RunStatus.RUNNING.value,
                LeadSearchRun.created_at >= since,
            )
            .limit(2)
        )
        result = await self.db.execute(stmt)
        runs = list(result.scalars().all())

        return runs[0] if len(runs) == 1 else None

    async def list_stale_running(self, cutoff: datetime) -> list[LeadSearchRun]:
        """Runs still marked running with no activity since `cutoff`.

        `updated_at` is touched by every progress callback, so it is the time
        of the last checkpoint (or of dispatch, for a run n8n never reported
        on at all).
        """
        stmt = select(LeadSearchRun).where(
            LeadSearchRun.status == RunStatus.RUNNING.value,
            LeadSearchRun.updated_at < cutoff,
        )
        result = await self.db.execute(stmt)

        return list(result.scalars().all())

    async def list_awaiting_results(self, cutoff: datetime) -> list[LeadSearchRun]:
        """Runs whose last checkpoint said "completed" before `cutoff` but whose
        results callback never arrived."""
        stmt = select(LeadSearchRun).where(
            LeadSearchRun.status == RunStatus.COMPLETED.value,
            LeadSearchRun.results_received_at.is_(None),
            LeadSearchRun.finished_at < cutoff,
        )
        result = await self.db.execute(stmt)

        return list(result.scalars().all())

    async def count_created_since(self, user_id: str, since: datetime) -> int:
        """Searches this user started at or after `since`."""
        stmt = (
            select(func.count())
            .select_from(LeadSearchRun)
            .where(LeadSearchRun.user_id == user_id, LeadSearchRun.created_at >= since)
        )
        result = await self.db.execute(stmt)

        return result.scalar_one()

    async def recent_completed(
        self, user_id: str, *, limit: int = 10
    ) -> list[LeadSearchRun]:
        """The user's latest runs whose results actually arrived, newest first.

        Feeds the cost estimate, so it wants real outcomes: a run that failed or
        never reported back says nothing about how many companies a keyword
        tends to yield.
        """
        stmt = (
            select(LeadSearchRun)
            .where(
                LeadSearchRun.user_id == user_id,
                LeadSearchRun.status == RunStatus.COMPLETED.value,
                LeadSearchRun.results_received_at.is_not(None),
            )
            .order_by(LeadSearchRun.created_at.desc())
            .limit(limit)
        )
        result = await self.db.execute(stmt)

        return list(result.scalars().all())

    @staticmethod
    def _active_clause() -> ColumnElement[bool]:
        """A search the user is still waiting on: running, or closed but with
        the workflow's results not yet in."""
        return or_(
            LeadSearchRun.status == RunStatus.RUNNING.value,
            and_(
                LeadSearchRun.status == RunStatus.COMPLETED.value,
                LeadSearchRun.results_received_at.is_(None),
            ),
        )

    async def count_for_user(self, user_id: str, *, active_only: bool = False) -> int:
        stmt = (
            select(func.count())
            .select_from(LeadSearchRun)
            .where(LeadSearchRun.user_id == user_id)
        )
        if active_only:
            stmt = stmt.where(self._active_clause())

        result = await self.db.execute(stmt)

        return result.scalar_one()

    async def list_for_user(
        self,
        user_id: str,
        *,
        offset: int = 0,
        limit: int = 20,
        active_only: bool = False,
    ) -> list[LeadSearchRun]:
        stmt = select(LeadSearchRun).where(LeadSearchRun.user_id == user_id)

        if active_only:
            stmt = stmt.where(self._active_clause())

        stmt = stmt.order_by(LeadSearchRun.created_at.desc()).offset(offset).limit(limit)
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
