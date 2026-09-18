"""Lead-search run tracking and the progress callbacks from n8n.

A run row exists before the workflow is dispatched, so every checkpoint n8n
reports has somewhere to land. Recording a checkpoint appends an event, moves
the run's current stage forward, and nudges the browser over SSE.
"""

import json
from datetime import UTC, datetime

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.lead_search import (
    STAGE_ORDER,
    LeadSearchEvent,
    LeadSearchRun,
    RunStatus,
    SearchStage,
)
from app.models.notification import NotificationKind
from app.repositories.lead_search import (
    LeadSearchEventRepository,
    LeadSearchRunRepository,
)
from app.services.base import BaseService
from app.services.notification import NotificationService
from app.services.progress_stream import progress_stream

log = get_logger(__name__)

# Where each checkpoint's count belongs on the run's summary.
_COUNT_FIELD: dict[SearchStage, str] = {
    SearchStage.SEARCHING_COMPANIES: "companies_found",
    SearchStage.AI_ANALYSING: "companies_found",
    SearchStage.DOMAIN_SEARCH: "companies_found",
    SearchStage.FINDING_DECISION_MAKERS: "contacts_found",
    SearchStage.FINDING_EMAILS: "contacts_found",
    SearchStage.VERIFYING_CONTACTS: "contacts_found",
}


def _stage_index(stage: str) -> int:
    """Position in the displayed checkpoint list, or -1 for queued/completed."""
    try:
        return STAGE_ORDER.index(SearchStage(stage))
    except ValueError:
        return -1


class LeadSearchProgressService(BaseService):
    def __init__(self, db: AsyncSession) -> None:
        super().__init__(db)
        self.runs = LeadSearchRunRepository(db)
        self.events = LeadSearchEventRepository(db)
        self.notifications = NotificationService(db)

    # --- Creation -----------------------------------------------------------
    async def create_run(
        self,
        user_id: str,
        original_keyword: str,
        keywords: list[str],
        *,
        auto_add_to_leads: bool = True,
        country: str | None = None,
        company_type: str | None = None,
        contact_role: str | None = None,
        company_size: str | None = None,
        validate_whatsapp: bool = False,
    ) -> LeadSearchRun:
        """Open a run, ready to be dispatched to n8n. Commits immediately so the
        row exists before the workflow can call back."""
        run = await self.runs.create(
            user_id=user_id,
            original_keyword=original_keyword,
            keywords_json=json.dumps(keywords),
            keyword_count=len(keywords),
            auto_add_to_leads=auto_add_to_leads,
            country=country,
            company_type=company_type,
            contact_role=contact_role,
            company_size=company_size,
            validate_whatsapp=validate_whatsapp,
            status=RunStatus.RUNNING.value,
            stage=SearchStage.QUEUED.value,
        )
        await self.commit()

        log.info("lead_search.run_created", run_id=run.id, user_id=user_id)

        return run

    async def mark_dispatch_failed(self, run: LeadSearchRun, reason: str) -> None:
        """The workflow never accepted the job, so the run ends before it starts."""
        run.status = RunStatus.FAILED.value
        run.error = reason[:500]
        run.finished_at = datetime.now(UTC)
        await self.commit()

        progress_stream.publish(run.user_id, run.id)

    # --- Reads --------------------------------------------------------------
    async def get_for_user(self, user_id: str, run_id: str) -> LeadSearchRun:
        run = await self.runs.get_for_user(user_id, run_id)
        if run is None:
            raise NotFoundError("No search run with that id.")

        return run

    async def list_for_user(
        self, user_id: str, *, offset: int, limit: int, active_only: bool = False
    ) -> tuple[list[LeadSearchRun], int]:
        """A page of the user's searches, newest first, plus the total."""
        runs = await self.runs.list_for_user(
            user_id, offset=offset, limit=limit, active_only=active_only
        )
        total = await self.runs.count_for_user(user_id, active_only=active_only)

        return runs, total

    async def events_for(self, run_id: str) -> list[LeadSearchEvent]:
        return await self.events.list_for_run(run_id)

    # --- Progress callbacks -------------------------------------------------
    async def record(
        self,
        run: LeadSearchRun,
        *,
        stage: str,
        message: str | None = None,
        count: int | None = None,
        status: str | None = None,
        error: str | None = None,
    ) -> LeadSearchEvent:
        """Record one checkpoint reported by the workflow.

        Out-of-order and repeated checkpoints are tolerated: the event is always
        appended (it is a true record of what n8n said), but the run's current
        stage only ever moves forward, so a late-arriving earlier checkpoint
        cannot drag the UI backwards.
        """
        try:
            reported = SearchStage(stage)
        except ValueError as exc:
            raise ValidationError(f"Unknown stage '{stage}'.") from exc

        event = await self.events.create(
            run_id=run.id,
            stage=reported.value,
            message=message[:500] if message else None,
            count=count,
        )

        completing = (
            status == RunStatus.COMPLETED.value or reported is SearchStage.COMPLETED
        )

        # On a finishing call the conditional UPDATE below owns `stage`. Setting
        # it here too would let a losing concurrent request flush its stale
        # "verifying_contacts" over the winner's "completed".
        if not (completing or error) and _stage_index(reported.value) >= _stage_index(
            run.stage
        ):
            run.stage = reported.value

        if count is not None:
            field = _COUNT_FIELD.get(reported)
            if field is not None:
                setattr(run, field, count)

        finished = False

        if error or completing:
            # A run finishes exactly once. An n8n HTTP node runs per input item,
            # so the completing checkpoint can arrive dozens of times AT ONCE;
            # checking `run.status` in Python is not enough, because every one
            # of those requests read the row while it still said "running".
            # The guard has to be the database: a conditional UPDATE that only
            # one request can match. Whoever flips the row notifies; the rest
            # are recorded as history and change nothing else.
            await self.db.flush()  # persist the stage/count edits made above

            terminal = RunStatus.FAILED.value if error else RunStatus.COMPLETED.value
            values: dict[str, object] = {
                "status": terminal,
                "finished_at": datetime.now(UTC),
            }
            if error:
                values["error"] = error[:500]
            else:
                values["stage"] = SearchStage.COMPLETED.value

            result = await self.db.execute(
                update(LeadSearchRun)
                .where(
                    LeadSearchRun.id == run.id,
                    LeadSearchRun.status == RunStatus.RUNNING.value,
                )
                .values(**values)
            )
            finished = result.rowcount == 1  # type: ignore[attr-defined]

            # Bring the in-memory row in line with whoever won.
            await self.db.refresh(run)

        notification = None
        if finished:
            notification = await self._finish_notification(run)

        await self.commit()

        # Only announce what is durable.
        progress_stream.publish(run.user_id, run.id)
        if notification is not None:
            NotificationService.publish(run.user_id, notification.id)

        log.info(
            "lead_search.progress",
            run_id=run.id,
            stage=reported.value,
            status=run.status,
        )

        return event

    async def _finish_notification(self, run: LeadSearchRun):  # type: ignore[no-untyped-def]
        """Tell the user their search ended, successfully or not."""
        if run.status == RunStatus.FAILED.value:
            return await self.notifications.create(
                run.user_id,
                kind=NotificationKind.MONITOR,
                title=f'Search for "{run.original_keyword}" failed',
                subtitle=run.error or "The lead search workflow reported an error.",
                link="/lead-radar",
                commit=False,
            )

        return await self.notifications.create(
            run.user_id,
            kind=NotificationKind.LEAD,
            title=f'Search for "{run.original_keyword}" finished',
            subtitle=(
                f"{run.companies_found:,} companies and "
                f"{run.contacts_found:,} contacts found."
            ),
            link="/my-leads",
            commit=False,
        )


__all__ = ["LeadSearchProgressService"]
