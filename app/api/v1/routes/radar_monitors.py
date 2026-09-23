"""Radar Monitors: CRUD for the panel, plus the two endpoints n8n calls.

The scheduler lives in n8n, not here. Its workflow wakes on its own cron, asks
`GET /monitors/due` what to run, runs each monitor through the same
Serper -> DeepSeek -> Snov.io pipeline a manual search uses, and posts back to
`POST /monitors/results`.

Both n8n-facing endpoints are authenticated without a session, because n8n has
no user: the due list by the shared workflow secret, the results callback by
the per-monitor token handed out with that monitor.
"""

from fastapi import APIRouter, Header, Query, status

from app.api.deps import CurrentUser, DbSession
from app.core.config import settings
from app.core.exceptions import UnauthorizedError
from app.core.logging import get_logger
from app.models.radar_monitor import RadarMonitor
from app.schemas.common import Message
from app.schemas.radar_monitor import (
    DueMonitorsResponse,
    MonitorCreateRequest,
    MonitorFilters,
    MonitorListResponse,
    MonitorOut,
    MonitorResultsRequest,
    MonitorResultsResponse,
    MonitorUpdateRequest,
)
from app.services.radar_monitor import RadarMonitorService

log = get_logger(__name__)

router = APIRouter(prefix="/lead-radar/monitors", tags=["radar-monitors"])


def _out(monitor: RadarMonitor) -> MonitorOut:
    """Flatten filters_json so the UI never parses JSON itself."""
    return MonitorOut(
        id=monitor.id,
        name=monitor.name,
        search_type=monitor.search_type,
        search_value=monitor.search_value,
        search_label=monitor.search_label,
        frequency=monitor.frequency,
        limit_per_run=monitor.limit_per_run,
        status=monitor.status,
        total_leads_generated=monitor.total_leads_generated,
        running_days=monitor.running_days,
        serper_offset=monitor.serper_offset,
        last_run_at=monitor.last_run_at,
        last_completed_at=monitor.last_completed_at,
        next_run_at=monitor.next_run_at,
        last_error=monitor.last_error,
        created_at=monitor.created_at,
        filters=MonitorFilters(**monitor.filters),
    )


# --- The panel --------------------------------------------------------------


@router.get("", response_model=MonitorListResponse)
async def list_monitors(
    current_user: CurrentUser,
    db: DbSession,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=100),
) -> MonitorListResponse:
    """Every monitor this user has, newest first."""
    items, total = await RadarMonitorService(db).list_for_user(
        current_user.id, offset=(page - 1) * per_page, limit=per_page
    )

    return MonitorListResponse(items=[_out(item) for item in items], total=total)


@router.post("", response_model=MonitorOut, status_code=status.HTTP_201_CREATED)
async def create_monitor(
    payload: MonitorCreateRequest,
    current_user: CurrentUser,
    db: DbSession,
) -> MonitorOut:
    """Save a monitor. It is due immediately, so the first run does not wait
    a whole day to prove the setup works."""
    monitor = await RadarMonitorService(db).create(current_user.id, payload)

    return _out(monitor)


@router.patch("/{monitor_id}", response_model=MonitorOut)
async def update_monitor(
    monitor_id: str,
    payload: MonitorUpdateRequest,
    current_user: CurrentUser,
    db: DbSession,
) -> MonitorOut:
    """Edit, pause or resume. Only the fields present are changed."""
    monitor = await RadarMonitorService(db).update(monitor_id, current_user.id, payload)

    return _out(monitor)


@router.delete("/{monitor_id}", response_model=Message)
async def delete_monitor(
    monitor_id: str,
    current_user: CurrentUser,
    db: DbSession,
) -> Message:
    """Delete a monitor. Leads it already found stay in My Leads."""
    await RadarMonitorService(db).delete(monitor_id, current_user.id)

    return Message(message="Monitor deleted.")


# --- The n8n contract -------------------------------------------------------


@router.get("/due", response_model=DueMonitorsResponse)
async def due_monitors(
    db: DbSession,
    limit: int = Query(default=100, ge=1, le=500),
    shared_secret: str | None = Header(default=None, alias=settings.N8N_WEBHOOK_HEADER),
) -> DueMonitorsResponse:
    """The monitors ready to run now, for n8n's Schedule Trigger.

    Handing a monitor out also claims it: each row is stamped with its next
    run time under a conditional UPDATE, so an overlapping pass -- a retry, or
    two schedulers -- cannot dispatch the same monitor twice and bill the user
    twice over.

    Authentication is the shared workflow secret, since the scheduler runs
    without a user. When N8N_WEBHOOK_SECRET is unset this endpoint refuses
    everything rather than defaulting open: it hands out per-monitor callback
    tokens, so an unauthenticated caller could write results into any monitor.
    """
    if not settings.N8N_WEBHOOK_SECRET:
        log.error("radar_monitor.due_rejected", reason="N8N_WEBHOOK_SECRET not set")
        raise UnauthorizedError("Monitor scheduling is not configured.")

    if shared_secret != settings.N8N_WEBHOOK_SECRET:
        log.warning("radar_monitor.due_rejected", reason="bad shared secret")
        raise UnauthorizedError("Invalid workflow secret.")

    monitors = await RadarMonitorService(db).claim_due(limit=limit)

    return DueMonitorsResponse(monitors=monitors, total=len(monitors))


@router.post("/results", response_model=MonitorResultsResponse)
async def monitor_results(
    payload: MonitorResultsRequest,
    db: DbSession,
    token: str | None = Header(default=None, alias="X-LeadPilot-Run-Token"),
) -> MonitorResultsResponse:
    """Ingest one monitor run's results.

    Every contact is checked against the emails this user already has. Only
    genuinely new ones are inserted, counted and charged -- a lead the user
    already had costs nothing, however often a directory re-surfaces it.

    Authenticated by the monitor's own callback token, the same way a manual
    search's results callback is authenticated by its run token.
    """
    service = RadarMonitorService(db)

    monitor = await service.monitors.get_for_callback(payload.monitor_id, token)
    if monitor is None:
        log.warning("radar_monitor.results_rejected", monitor_id=payload.monitor_id)
        raise UnauthorizedError("Invalid monitor or token.")

    return await service.ingest(monitor, payload)
