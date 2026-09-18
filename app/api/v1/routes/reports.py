"""Reports endpoints: the analytics summary and its file export."""

from fastapi import APIRouter, Query, Response

from app.api.deps import CurrentUser, DbSession
from app.schemas.reports import ReportSummary
from app.services.report_export import CONTENT_TYPES, ExportFormat, filename_for, render
from app.services.reports import MAX_DAYS, MIN_DAYS, ReportsService

router = APIRouter()


@router.get("/summary", response_model=ReportSummary, summary="Reports analytics")
async def reports_summary(
    db: DbSession,
    current_user: CurrentUser,
    days: int = Query(
        30,
        ge=MIN_DAYS,
        le=MAX_DAYS,
        description="How many of the user's local days the window covers, ending today.",
    ),
    tz_offset: int = Query(
        0,
        ge=-840,
        le=840,
        description=(
            "The client's Date.getTimezoneOffset() in minutes, so days are the "
            "user's local days. UTC+5 sends -300."
        ),
    ),
) -> ReportSummary:
    """Real analytics for the signed-in user over the chosen window: leads and
    contacts found, credits spent, the daily trend behind them, the lead funnel,
    where the leads are, which keywords produced them, and how the searches
    themselves fared.

    Every figure is a count of this user's own rows.
    """
    return await ReportsService(db).summary(
        current_user.id, days=days, tz_offset_minutes=tz_offset
    )


@router.get(
    "/export",
    summary="Download the report as CSV or Excel",
    response_class=Response,
    responses={200: {"content": {"text/csv": {}, CONTENT_TYPES["xlsx"]: {}}}},
)
async def export_report(
    db: DbSession,
    current_user: CurrentUser,
    fmt: ExportFormat = Query("csv", alias="format", description="csv or xlsx."),
    days: int = Query(30, ge=MIN_DAYS, le=MAX_DAYS),
    tz_offset: int = Query(0, ge=-840, le=840),
) -> Response:
    """The same figures the page draws, as a file.

    Built from the same service call, so the export can never disagree with
    what was on screen. Excel gets one sheet per section; CSV writes the
    sections one after another under their own titles.
    """
    report = await ReportsService(db).summary(
        current_user.id, days=days, tz_offset_minutes=tz_offset
    )

    body = render(report, fmt)
    filename = filename_for(fmt, report)

    return Response(
        content=body,
        media_type=CONTENT_TYPES[fmt],
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
