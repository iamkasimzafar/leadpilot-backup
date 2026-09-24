"""My Leads endpoints."""

from datetime import UTC, datetime

from fastapi import APIRouter, Query, Response, status

from app.api.deps import CurrentUser, DbSession, Pagination
from app.schemas.lead import (
    AddToLeadsRequest,
    AddToLeadsResponse,
    BulkIdsRequest,
    BulkResult,
    BulkStatusRequest,
    CompanyRead,
    DecisionMakerRead,
    DecisionMakerUpdate,
    LeadCounts,
    LeadPage,
    LeadStatusValue,
    LeadUpdate,
)
from app.services.lead_export import (
    CONTENT_TYPES,
    ExportFormat,
    LeadExportService,
    filename_for,
)
from app.services.leads import LeadService

router = APIRouter()

# Long enough for a company name plus a city; short enough to be a search.
SEARCH_MAX_LENGTH = 100


@router.get("", response_model=LeadPage, summary="List my leads")
async def list_leads(
    db: DbSession,
    pagination: Pagination,
    current_user: CurrentUser,
    status_filter: LeadStatusValue | None = Query(
        None, alias="status", description="Only leads in this status."
    ),
    q: str | None = Query(
        None,
        max_length=SEARCH_MAX_LENGTH,
        description=(
            "Search every lead, not just this page: company name, website, "
            "location, industry, phone, notes, and each contact's name, title, "
            "email and phone. Every word must match somewhere."
        ),
    ),
) -> LeadPage:
    """Most recently added first, with the count for every tab.

    `q` is applied in the database before paging, so `total`, the page and the
    tab counts all describe the matches across the user's whole list.
    """
    service = LeadService(db)
    items, total, counts = await service.list_leads(
        current_user.id,
        status=status_filter,
        search=q,
        offset=pagination.offset,
        limit=pagination.limit,
    )

    # Which feature each lead came from, for the source chip on its row.
    sources = await service.sources_for(items)

    return LeadPage(
        items=[
            CompanyRead.model_validate(c).model_copy(update={"source": sources.get(c.id)})
            for c in items
        ],
        total=total,
        page=pagination.page,
        per_page=pagination.per_page,
        counts=counts,
    )


@router.get("/counts", response_model=LeadCounts, summary="Tab counts")
async def lead_counts(db: DbSession, current_user: CurrentUser) -> LeadCounts:
    return await LeadService(db).counts(current_user.id)


@router.get(
    "/export",
    summary="Download my leads as CSV or Excel",
    response_class=Response,
    responses={200: {"content": {"text/csv": {}, CONTENT_TYPES["xlsx"]: {}}}},
)
async def export_leads(
    db: DbSession,
    current_user: CurrentUser,
    fmt: ExportFormat = Query("csv", alias="format", description="csv or xlsx."),
    status_filter: LeadStatusValue | None = Query(
        None, alias="status", description="Only leads in this status."
    ),
    ids: list[str] = Query(
        default_factory=list,
        description="Only these leads (repeat the parameter). Empty means all.",
        max_length=500,
    ),
    q: str | None = Query(
        None,
        max_length=SEARCH_MAX_LENGTH,
        description="The same search as the list, so the file matches the screen.",
    ),
) -> Response:
    """One row per contact, every matching lead, no page limit.

    Declared above `/{lead_id}` on purpose: a literal path segment has to be
    registered before the parameterised one, or "export" is read as an id.
    """
    body = await LeadExportService(db).export(
        current_user.id, fmt=fmt, status=status_filter, ids=ids or None, search=q
    )
    filename = filename_for(fmt, status_filter, datetime.now(UTC))

    return Response(
        content=body,
        media_type=CONTENT_TYPES[fmt],
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/add", response_model=AddToLeadsResponse, summary="Add results to my leads")
async def add_to_leads(
    payload: AddToLeadsRequest, db: DbSession, current_user: CurrentUser
) -> AddToLeadsResponse:
    """Add specific companies (`company_ids`) or every company from a search
    (`run_id`). Companies already in My Leads are counted, not duplicated."""
    return await LeadService(db).add(
        current_user.id, company_ids=payload.company_ids, run_id=payload.run_id
    )


@router.post("/bulk-status", response_model=BulkResult, summary="Set status on many")
async def bulk_status(
    payload: BulkStatusRequest, db: DbSession, current_user: CurrentUser
) -> BulkResult:
    affected = await LeadService(db).set_status(
        current_user.id, payload.ids, payload.status
    )

    return BulkResult(affected=affected)


@router.post("/bulk-delete", response_model=BulkResult, summary="Delete many")
async def bulk_delete(
    payload: BulkIdsRequest, db: DbSession, current_user: CurrentUser
) -> BulkResult:
    affected = await LeadService(db).delete(current_user.id, payload.ids)

    return BulkResult(affected=affected)


@router.get("/{lead_id}", response_model=CompanyRead, summary="One lead")
async def get_lead(lead_id: str, db: DbSession, current_user: CurrentUser) -> CompanyRead:
    return CompanyRead.model_validate(await LeadService(db).get(current_user.id, lead_id))


@router.patch("/{lead_id}", response_model=CompanyRead, summary="Update a lead")
async def update_lead(
    lead_id: str, payload: LeadUpdate, db: DbSession, current_user: CurrentUser
) -> CompanyRead:
    """Partial: only the fields present in the body change."""
    company = await LeadService(db).update(current_user.id, lead_id, payload)

    return CompanyRead.model_validate(company)


@router.delete(
    "/{lead_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a lead"
)
async def delete_lead(lead_id: str, db: DbSession, current_user: CurrentUser) -> Response:
    service = LeadService(db)
    await service.get(current_user.id, lead_id)  # 404 if not theirs
    await service.delete(current_user.id, [lead_id])

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch(
    "/{lead_id}/contacts/{contact_id}",
    response_model=DecisionMakerRead,
    summary="Update a contact",
)
async def update_contact(
    lead_id: str,
    contact_id: str,
    payload: DecisionMakerUpdate,
    db: DbSession,
    current_user: CurrentUser,
) -> DecisionMakerRead:
    contact = await LeadService(db).update_contact(
        current_user.id, lead_id, contact_id, payload
    )

    return DecisionMakerRead.model_validate(contact)


@router.delete(
    "/{lead_id}/contacts/{contact_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a contact",
)
async def delete_contact(
    lead_id: str, contact_id: str, db: DbSession, current_user: CurrentUser
) -> Response:
    await LeadService(db).delete_contact(current_user.id, lead_id, contact_id)

    return Response(status_code=status.HTTP_204_NO_CONTENT)
