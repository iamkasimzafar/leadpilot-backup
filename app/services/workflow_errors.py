"""Turns an n8n workflow failure into something a user can act on.

The raw message from n8n is written for whoever built the workflow ("Error in
sub-node 'Message a model'", an HTTP 429 body, a stack of node names). The
user needs to know three things instead: their search stopped, roughly why,
and whether they should retry or wait. `explain()` is that translation; the
raw text is still stored and shown as detail for whoever debugs it.
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.lead_search import LeadSearchRun, RunStatus
from app.models.notification import NotificationKind
from app.models.workflow_error import WorkflowError
from app.repositories.base import BaseRepository
from app.repositories.lead_search import LeadSearchRunRepository
from app.services.base import BaseService
from app.services.notification import NotificationService
from app.services.progress_stream import progress_stream

log = get_logger(__name__)

# Matched against the lower-cased message from n8n, first hit wins. Each maps
# to a short reason the user can actually do something about.
_PATTERNS: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        (
            "rate limit",
            "429",
            "too many requests",
            "quota",
            "exceeded your current quota",
        ),
        "rate_limited",
    ),
    (
        ("insufficient", "credit balance", "payment required", "402", "billing"),
        "provider_credit",
    ),
    (
        ("unauthorized", "401", "403", "forbidden", "invalid api key", "api key"),
        "provider_auth",
    ),
    (("timeout", "timed out", "etimedout", "esockettimedout"), "timeout"),
    (
        ("econnrefused", "enotfound", "socket hang up", "network", "getaddrinfo"),
        "unreachable",
    ),
    (("json", "parse", "unexpected token"), "bad_response"),
)

# What each reason means for the user, and whether retrying is worth it.
REASONS: dict[str, bool] = {
    "rate_limited": True,
    "provider_credit": False,
    "provider_auth": False,
    "timeout": True,
    "unreachable": True,
    "bad_response": True,
    "unknown": True,
}


def explain(message: str) -> str:
    """Classify a raw n8n error into one of REASONS."""
    text = (message or "").lower()

    for needles, reason in _PATTERNS:
        if any(needle in text for needle in needles):
            return reason

    return "unknown"


class WorkflowErrorRepository(BaseRepository[WorkflowError]):
    model = WorkflowError

    async def latest_for_run(self, run_id: str) -> WorkflowError | None:
        """The most recent failure recorded against a run."""
        stmt = (
            select(WorkflowError)
            .where(WorkflowError.run_id == run_id)
            .order_by(WorkflowError.created_at.desc())
            .limit(1)
        )
        result = await self.db.execute(stmt)

        return result.scalar_one_or_none()


class WorkflowErrorService(BaseService):
    def __init__(self, db: AsyncSession) -> None:
        super().__init__(db)
        self.errors = WorkflowErrorRepository(db)
        self.runs = LeadSearchRunRepository(db)
        self.notifications = NotificationService(db)

    async def latest_for_run(self, run_id: str) -> WorkflowError | None:
        return await self.errors.latest_for_run(run_id)

    async def record(
        self,
        *,
        workflow_id: str | None,
        execution_id: str | None,
        error_message: str,
        last_node_executed: str | None,
        run: LeadSearchRun | None,
    ) -> WorkflowError:
        """Store the failure and, when it belongs to a run, fail that run and
        tell the user.

        Without a run there is nobody to notify -- the row is still kept so the
        outage is visible in the database and the logs.
        """
        reason = explain(error_message)

        error = await self.errors.create(
            run_id=run.id if run else None,
            user_id=run.user_id if run else None,
            workflow_id=workflow_id,
            execution_id=execution_id,
            error_message=error_message[:5000],
            last_node_executed=last_node_executed,
        )

        notification = None

        if run is not None and run.status == RunStatus.RUNNING.value:
            # Close the run so the tracker stops spinning. The user-facing
            # reason goes in `error`; the raw n8n text stays on this row.
            run.status = RunStatus.FAILED.value
            run.error = f"{reason}: {error_message}"[:500]
            run.finished_at = datetime.now(UTC)

            notification = await self.notifications.create(
                run.user_id,
                kind=NotificationKind.MONITOR,
                title=f'Search for "{run.original_keyword}" failed',
                subtitle=(
                    "The lead search workflow stopped"
                    + (f" at {last_node_executed}" if last_node_executed else "")
                    + "."
                ),
                link="/lead-radar",
                commit=False,
            )

        await self.commit()

        if run is not None:
            progress_stream.publish(run.user_id, run.id)
            if notification is not None:
                NotificationService.publish(run.user_id, notification.id)

        log.error(
            "workflow.error_reported",
            reason=reason,
            run_id=run.id if run else None,
            execution_id=execution_id,
            node=last_node_executed,
        )

        return error


__all__ = ["REASONS", "WorkflowErrorService", "explain"]
