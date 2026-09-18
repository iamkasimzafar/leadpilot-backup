"""Dashboard endpoint."""

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, DbSession
from app.schemas.dashboard import DashboardSummary
from app.services.dashboard import DashboardService

router = APIRouter()


@router.get("/summary", response_model=DashboardSummary, summary="Dashboard figures")
async def dashboard_summary(
    db: DbSession,
    current_user: CurrentUser,
    tz_offset: int = Query(
        0,
        ge=-840,
        le=840,
        description=(
            "The client's Date.getTimezoneOffset() in minutes, so 'today' is "
            "the user's local day. UTC+5 sends -300."
        ),
    ),
) -> DashboardSummary:
    """Real counts for the signed-in user: leads added today, leads awaiting
    contact, searches started today, contacts found today, credits, and the
    most recent searches."""
    return await DashboardService(db).summary(
        current_user.id, tz_offset_minutes=tz_offset
    )
