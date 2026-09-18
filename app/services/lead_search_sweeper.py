"""Closes lead-search runs the workflow never finished.

A run is driven entirely by callbacks from n8n. If the workflow dies mid-run,
or fails in a way its Error Trigger cannot attribute to our run, no callback
ever arrives and the row would stay "running" forever: the tracker keeps
spinning, the Searches badge keeps counting it, and the user is never told.

The sweeper is that missing signal. On a timer it fails:

* runs still "running" with no checkpoint for LEAD_SEARCH_STALE_MINUTES, and
* runs whose last checkpoint said "completed" but whose results callback did
  not arrive within LEAD_SEARCH_RESULTS_TIMEOUT_MINUTES (nothing was saved and
  nothing was charged, so "failed" is the honest state).

Both use a conditional UPDATE keyed on the current status, so a callback that
lands at the same moment still wins exactly once -- the same guard the
progress and results callbacks rely on -- and several workers can sweep
concurrently without double-notifying.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.models.lead_search import LeadSearchRun, RunStatus
from app.models.notification import NotificationKind
from app.repositories.lead_search import LeadSearchRunRepository
from app.services.base import BaseService
from app.services.notification import NotificationService
from app.services.progress_stream import progress_stream

log = get_logger(__name__)

STALE_REASON = "No progress from the lead search workflow for {minutes} minutes."
NO_RESULTS_REASON = (
    "The workflow finished but its results never arrived. Nothing was saved or charged."
)


@dataclass
class SweepReport:
    stale: list[str] = field(default_factory=list)
    without_results: list[str] = field(default_factory=list)

    @property
    def closed(self) -> int:
        return len(self.stale) + len(self.without_results)


class LeadSearchSweeper(BaseService):
    def __init__(self, db: AsyncSession) -> None:
        super().__init__(db)
        self.runs = LeadSearchRunRepository(db)
        self.notifications = NotificationService(db)

    async def sweep(self, *, now: datetime | None = None) -> SweepReport:
        """One pass. Returns the ids of the runs it closed."""
        now = now or datetime.now(UTC)
        report = SweepReport()

        stale_cutoff = now - timedelta(minutes=settings.LEAD_SEARCH_STALE_MINUTES)
        for run in await self.runs.list_stale_running(stale_cutoff):
            if await self._fail(
                run,
                expected_status=RunStatus.RUNNING.value,
                reason=STALE_REASON.format(minutes=settings.LEAD_SEARCH_STALE_MINUTES),
                now=now,
            ):
                report.stale.append(run.id)

        results_cutoff = now - timedelta(
            minutes=settings.LEAD_SEARCH_RESULTS_TIMEOUT_MINUTES
        )
        for run in await self.runs.list_awaiting_results(results_cutoff):
            if await self._fail(
                run,
                expected_status=RunStatus.COMPLETED.value,
                reason=NO_RESULTS_REASON,
                now=now,
            ):
                report.without_results.append(run.id)

        if report.closed:
            log.warning(
                "lead_search.sweep",
                stale=len(report.stale),
                without_results=len(report.without_results),
            )

        return report

    async def _fail(
        self, run: LeadSearchRun, *, expected_status: str, reason: str, now: datetime
    ) -> bool:
        """Fail one run, if it is still in the state the sweep saw. Commits.

        Returns False when a callback got there first (the status changed
        between the SELECT and this UPDATE), in which case nothing is touched.
        """
        result = await self.db.execute(
            update(LeadSearchRun)
            .where(
                LeadSearchRun.id == run.id,
                LeadSearchRun.status == expected_status,
                # A completed run only counts while its results are missing.
                LeadSearchRun.results_received_at.is_(None),
            )
            .values(
                status=RunStatus.FAILED.value,
                error=reason[:500],
                finished_at=now,
            )
        )
        if result.rowcount != 1:  # type: ignore[attr-defined]
            await self.db.rollback()
            return False

        await self.db.refresh(run)

        notification = await self.notifications.create(
            run.user_id,
            kind=NotificationKind.MONITOR,
            title=f'Search for "{run.original_keyword}" failed',
            subtitle=reason,
            link="/lead-radar",
            commit=False,
        )

        await self.commit()

        # Only announce what is durable.
        progress_stream.publish(run.user_id, run.id)
        if notification is not None:
            NotificationService.publish(run.user_id, notification.id)

        log.warning("lead_search.run_abandoned", run_id=run.id, reason=reason)

        return True


async def run_sweeper(stop: asyncio.Event) -> None:
    """Sweep on a timer until `stop` is set. Started from the app's lifespan.

    Each pass opens its own short-lived session, so a database hiccup costs
    one pass rather than the loop: errors are logged and the next tick runs.
    """
    # Imported here: app.db.session builds the engine at import time, and the
    # test-suite swaps that engine out; keeping the import local means the
    # sweeper only touches the real engine when it actually runs.
    from app.db.session import SessionLocal

    interval = settings.LEAD_SEARCH_SWEEP_INTERVAL_SECONDS
    log.info("lead_search.sweeper_started", interval_seconds=interval)

    while not stop.is_set():
        try:
            async with SessionLocal() as db:
                await LeadSearchSweeper(db).sweep()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("lead_search.sweep_failed")

        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            continue

    log.info("lead_search.sweeper_stopped")


__all__ = ["LeadSearchSweeper", "SweepReport", "run_sweeper"]
