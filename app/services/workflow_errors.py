"""Turns an n8n workflow failure into something a user can act on.

The raw message from n8n is written for whoever built the workflow ("Error in
sub-node 'Message a model'", an HTTP 429 body, a stack of node names). The
user needs to know three things instead: their search stopped, roughly why,
and whether they should retry or wait. `explain()` is that translation; the
raw text is still stored and shown as detail for whoever debugs it.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.models.lead_search import LeadSearchRun, RunStatus
from app.models.notification import NotificationKind
from app.models.workflow_error import WorkflowError
from app.repositories.base import BaseRepository
from app.repositories.lead_search import LeadSearchRunRepository
from app.schemas.lead_radar import WorkflowErrorRequest
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
    # The workflow found companies but not one verifiable address among their
    # decision makers. Not an outage: a different keyword or looser filters
    # is the fix, so it is "retryable" in the sense of trying again differently.
    (("no valid email", "zero valid email", "no emails found"), "no_valid_emails_found"),
    # "Zero decision-makers found matching target roles" is what the B2B
    # workflow's prospect limiter throws.
    (
        ("zero decision-maker", "zero decision maker", "no decision maker"),
        "no_decision_makers_found",
    ),
)

# What each reason means for the user, and whether retrying is worth it.
REASONS: dict[str, bool] = {
    "rate_limited": True,
    "provider_credit": False,
    "provider_auth": False,
    "timeout": True,
    "unreachable": True,
    "bad_response": True,
    "no_valid_emails_found": True,
    # Local Offline Business: Google Maps had no listing with a phone number
    # inside the rating / review window. A wider area, a different keyword or
    # a looser filter is the fix, so it is worth trying again differently.
    "no_local_businesses_found": True,
    # The "nothing to show" endings of the B2B workflow. Each used to be a
    # silent dead end (a node that outputs no items stops an n8n run without
    # an error); the workflow now throws "LeadPilot Error: <code> - ..." there,
    # so the user is told at once, and told which step came up empty. None is
    # an outage: different keywords or looser filters are the fix.
    "no_companies_found": True,
    "no_company_size_match": True,
    "no_decision_makers_found": True,
    "unknown": True,
}

# Reasons the workflow may name outright in a thrown message. Checked before
# the text patterns: the workflow knows why it stopped better than a guess
# from its wording ("... 0 of 429 results ..." is not a rate limit).
_NAMED_REASONS = tuple(reason for reason in REASONS if reason.startswith("no_"))


def explain(message: str) -> str:
    """Classify a raw n8n error into one of REASONS."""
    text = (message or "").lower()

    for reason in _NAMED_REASONS:
        if reason in text:
            return reason

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

    async def attribute(
        self, payload: WorkflowErrorRequest
    ) -> tuple[LeadSearchRun | None, str | None]:
        """Find the run an Error Trigger report belongs to.

        n8n's error payload identifies the failed *execution*, never our run,
        so this works down a ladder from exact to inferred, and says which rung
        matched:

        1. `run_token`: the workflow passed `run_id` + `progress_token`
           through. Exact -- and a pair that does not match is a bad caller,
           not a missing id, so nothing below is tried for it.
        2. `execution_id`: a progress callback (or the webhook's reply) already
           told us which execution handles which run. Exact.
        3. `sole_running_run`: nothing carried an id, but exactly one search is
           in flight, so the failing execution can only be that one. With two
           or more running the report stays unattributed rather than risk
           failing the wrong user's search.
        """
        if payload.run_id and payload.progress_token:
            run = await self.runs.get_for_callback(payload.run_id, payload.progress_token)
            if run is not None:
                return run, "run_token"

            log.warning("workflow_error.bad_run_token", run_id=payload.run_id)
            return None, None

        if payload.execution_id:
            run = await self.runs.get_by_execution_id(payload.execution_id)
            if run is not None:
                return run, "execution_id"

        since = datetime.now(UTC) - timedelta(minutes=settings.LEAD_SEARCH_STALE_MINUTES)
        run = await self.runs.sole_running_since(since)
        if run is None:
            return None, None

        # The one run in flight is already known to belong to a different
        # execution: this failure is not its.
        if (
            payload.execution_id
            and run.n8n_execution_id
            and run.n8n_execution_id != payload.execution_id
        ):
            return None, None

        return run, "sole_running_run"

    async def record(
        self,
        *,
        workflow_id: str | None,
        execution_id: str | None,
        error_message: str,
        last_node_executed: str | None,
        run: LeadSearchRun | None,
        attributed_by: str | None = None,
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

        # The report just told us which execution this run belongs to.
        if run is not None and execution_id and not run.n8n_execution_id:
            run.n8n_execution_id = execution_id[:64]

        if run is not None and await self._fail_run(run, reason, error_message):
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
            attributed_by=attributed_by,
            execution_id=execution_id,
            node=last_node_executed,
        )

        return error

    async def _fail_run(
        self, run: LeadSearchRun, reason: str, error_message: str
    ) -> bool:
        """Fail the run this error belongs to, if it is still waiting on the
        workflow. Returns True when this call is the one that failed it.

        "Still waiting" is wider than "running". The workflow's last progress
        checkpoint says `status: completed` BEFORE the final branches run and
        before the results are posted, so a crash in those last nodes reaches
        us seconds after the run was closed as completed. No results will ever
        follow, so that run has failed -- leaving it "completed" would show
        the user "collecting results" until the sweeper gave up on it.

        What is never touched: a run whose results arrived or was billed (the
        search delivered; a late error is noise), and one already failed.
        Decided by a conditional UPDATE, not by reading `run.status`, so it
        holds when the error and a callback land at the same moment.
        """
        await self.db.flush()

        result = await self.db.execute(
            update(LeadSearchRun)
            .where(
                LeadSearchRun.id == run.id,
                LeadSearchRun.status.in_(
                    [RunStatus.RUNNING.value, RunStatus.COMPLETED.value]
                ),
                LeadSearchRun.results_received_at.is_(None),
                LeadSearchRun.credits_charged_at.is_(None),
            )
            .values(
                status=RunStatus.FAILED.value,
                # The user-facing reason goes in `error`; the raw n8n text
                # stays on the workflow_error row.
                error=f"{reason}: {error_message}"[:500],
                error_reason=reason,
                finished_at=datetime.now(UTC),
            )
        )
        failed = bool(result.rowcount == 1)  # type: ignore[attr-defined]

        await self.db.refresh(run)

        return failed


__all__ = ["REASONS", "WorkflowErrorService", "explain"]
