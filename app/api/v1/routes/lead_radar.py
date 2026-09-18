"""Lead Radar endpoints: keyword expansion, search dispatch, and progress."""

import asyncio
import json
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Header, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy.exc import OperationalError

from app.api.deps import CurrentUser, DbSession, Pagination
from app.core.exceptions import (
    InsufficientCreditsError,
    NotFoundError,
    UnauthorizedError,
    UpstreamError,
)
from app.core.logging import get_logger
from app.core.security import decode_token
from app.models.lead_search import STAGE_ORDER, RunStatus
from app.repositories.lead import CompanyRepository
from app.repositories.user import UserRepository
from app.schemas.common import Message
from app.schemas.lead import CompanyRead, SearchResultsRequest, SearchResultsResponse
from app.schemas.lead_radar import (
    ExpandKeywordRequest,
    ExpandKeywordResponse,
    ProgressCallbackRequest,
    QuoteRequest,
    SearchEventRead,
    SearchQuote,
    SearchRunPage,
    SearchRunRead,
    StartSearchRequest,
    StartSearchResponse,
    WorkflowErrorRequest,
    WorkflowErrorResponse,
)
from app.services.billing import BillingService
from app.services.billing_catalog import AI_KEYWORD_EXPANSION_CREDITS
from app.services.keyword_expansion import KeywordExpansionService
from app.services.lead_results import LeadResultsService, is_deadlock
from app.services.lead_search import LeadSearchService
from app.services.lead_search_progress import LeadSearchProgressService
from app.services.progress_stream import progress_stream
from app.services.search_pricing import SearchPricingService
from app.services.workflow_errors import WorkflowErrorService, explain

log = get_logger(__name__)

router = APIRouter()

_HEARTBEAT_SECONDS = 25.0

# Attempts for the results callback when MySQL reports a deadlock.
RESULTS_DEADLOCK_RETRIES = 3


@router.post(
    "/expand",
    response_model=ExpandKeywordResponse,
    summary="AI-expand a product keyword",
)
async def expand_keyword(
    payload: ExpandKeywordRequest, db: DbSession, current_user: CurrentUser
) -> ExpandKeywordResponse:
    """Validate the keyword and expand it into synonyms, buyer scenarios and
    multi-language variants. Answers `valid: false` (HTTP 200) for input that
    is not a real product / industry term, so the UI can explain rather than
    error out.

    Costs credits. The wallet is checked before calling DeepSeek (so a user who
    cannot pay is turned away with 402 rather than burning an API call) and
    debited only once a usable expansion comes back -- a timeout, an upstream
    failure or a rejected keyword all leave the balance untouched.
    """
    billing = BillingService(db)

    await billing.ensure_can_afford(current_user.id, AI_KEYWORD_EXPANSION_CREDITS)

    result = await KeywordExpansionService().expand(payload.keyword, payload.company_type)

    # Nothing usable was produced, so there is nothing to charge for.
    if not result.valid:
        return result

    transaction = await billing.spend(
        current_user.id,
        AI_KEYWORD_EXPANSION_CREDITS,
        f"AI keyword expansion — {result.normalized_keyword or payload.keyword}",
        reference_type="keyword_expansion",
    )

    result.credits_charged = AI_KEYWORD_EXPANSION_CREDITS
    result.balance_after = transaction.balance_after

    return result


@router.post(
    "/quote",
    response_model=SearchQuote,
    summary="Estimate what a search will cost",
)
async def quote_search(
    payload: QuoteRequest, db: DbSession, current_user: CurrentUser
) -> SearchQuote:
    """The estimate the confirm step shows, and what the start gate checks.

    Nothing is charged by this call. The bill is settled from what the
    workflow actually returns; this is the likely cost, learned from the
    account's own recent runs where it has any. Takes the same keyword inputs
    as `/search` so the count is made the same way.
    """
    keywords = LeadSearchService.merge_keywords(
        payload.original_keyword, payload.expanded_keywords
    )
    balance = await BillingService(db).balance(current_user.id)

    return await SearchPricingService(db).quote(
        current_user.id,
        keyword_count=len(keywords),
        validate_whatsapp=payload.validate_whatsapp,
        balance=balance,
    )


@router.post(
    "/search",
    response_model=StartSearchResponse,
    summary="Start a lead search via the n8n workflow",
)
async def start_search(
    payload: StartSearchRequest, db: DbSession, current_user: CurrentUser
) -> StartSearchResponse:
    """Forward the selected keywords to the n8n lead-search webhook.

    A run row is created first, so the workflow's progress callbacks have
    something to attach to, and its id is returned for the client to watch.

    Nothing is charged here: the bill is settled when the results arrive.
    But a search the balance could not cover is refused up front (402),
    against the same estimate the confirm step showed.
    """
    progress = LeadSearchProgressService(db)

    keywords = LeadSearchService.merge_keywords(
        payload.original_keyword, payload.expanded_keywords
    )

    # The start gate. Recomputed server-side rather than trusted from the
    # client, from the same inputs, so it matches what was displayed.
    billing = BillingService(db)
    quote = await SearchPricingService(db).quote(
        current_user.id,
        keyword_count=len(keywords),
        validate_whatsapp=payload.validate_whatsapp,
        balance=await billing.balance(current_user.id),
    )
    if not quote.affordable:
        raise InsufficientCreditsError(
            details={
                "balance": quote.balance,
                "required": quote.total,
                "shortfall": quote.shortfall,
            }
        )

    run = await progress.create_run(
        current_user.id,
        " ".join(payload.original_keyword.split()),
        keywords,
        auto_add_to_leads=payload.auto_add_to_leads,
        country=payload.country,
        company_type=payload.company_type,
        contact_role=payload.contact_role,
        company_size=payload.company_size,
        validate_whatsapp=payload.validate_whatsapp,
    )

    try:
        return await LeadSearchService().start(
            payload.original_keyword,
            payload.expanded_keywords,
            auto_add_to_leads=payload.auto_add_to_leads,
            user_id=str(current_user.id),
            run_id=run.id,
            callback_token=run.callback_token,
            country=payload.country,
            company_type=payload.company_type,
            contact_role=payload.contact_role,
            company_size=payload.company_size,
            validate_whatsapp=payload.validate_whatsapp,
        )
    except UpstreamError as exc:
        # The run exists but nothing is coming: close it so the UI does not sit
        # on a spinner forever.
        await progress.mark_dispatch_failed(run, exc.message)
        raise


@router.get(
    "/runs",
    response_model=SearchRunPage,
    summary="This user's searches, newest first",
)
async def list_runs(
    db: DbSession,
    pagination: Pagination,
    current_user: CurrentUser,
    active_only: bool = Query(
        False, description="Only searches still running or awaiting results."
    ),
) -> SearchRunPage:
    """Backs the Searches list and the resume-on-load behaviour: a user can
    leave the page mid-search and come back to it from here.

    Runs carry no events in this listing -- the tracker fetches those per run.
    """
    service = LeadSearchProgressService(db)
    runs, total = await service.list_for_user(
        current_user.id,
        offset=pagination.offset,
        limit=pagination.limit,
        active_only=active_only,
    )

    return SearchRunPage(
        items=[SearchRunRead.model_validate(run) for run in runs],
        total=total,
        page=pagination.page,
        per_page=pagination.per_page,
    )


@router.get(
    "/runs/{run_id}",
    response_model=SearchRunRead,
    summary="A search run and its progress so far",
)
async def get_run(run_id: str, db: DbSession, current_user: CurrentUser) -> SearchRunRead:
    """Everything needed to draw the tracker, including the checkpoints already
    reported -- so a page reload mid-search resumes correctly."""
    service = LeadSearchProgressService(db)
    run = await service.get_for_user(current_user.id, run_id)
    events = await service.events_for(run.id)

    payload = SearchRunRead.model_validate(run)
    payload.events = [SearchEventRead.model_validate(e) for e in events]
    payload.stage_order = [stage.value for stage in STAGE_ORDER]

    # A workflow failure carries more than the message: attach the classified
    # reason and the node it died on, so the UI can advise rather than dump
    # n8n's raw text at the user.
    if run.status == RunStatus.FAILED.value:
        failure = await WorkflowErrorService(db).latest_for_run(run.id)
        if failure is not None:
            payload.error_reason = explain(failure.error_message)
            payload.error_node = failure.last_node_executed
            payload.error = failure.error_message[:500]
        elif run.error:
            payload.error_reason = explain(run.error)

    return payload


@router.post(
    "/runs/{run_id}/progress",
    response_model=Message,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Progress callback for the n8n workflow",
)
async def report_progress(
    run_id: str,
    payload: ProgressCallbackRequest,
    db: DbSession,
    x_leadpilot_run_token: str = Header(..., alias="X-LeadPilot-Run-Token"),
) -> Message:
    """Called by n8n at each checkpoint of the workflow.

    Authenticated by the per-run token issued at dispatch (sent as
    `progress_token` in the workflow payload), NOT by a user session -- n8n has
    no session. The token is unguessable and scoped to one run, so it cannot be
    used to write to anyone else's.

    Send one POST per checkpoint with a `stage` of:
    searching_companies, ai_analysing, domain_search,
    finding_decision_makers, finding_emails, verifying_contacts.
    Add `"status": "completed"` on the final call, or `"error": "..."` to fail
    the run. `message` and `count` are optional detail.
    """
    service = LeadSearchProgressService(db)
    run = await service.runs.get_for_callback(run_id, x_leadpilot_run_token)

    if run is None:
        # Same answer whether the run is unknown or the token is wrong, so this
        # cannot be used to discover which run ids exist.
        log.warning("lead_search.progress_rejected", run_id=run_id)
        raise UnauthorizedError("Invalid run or token.")

    await service.record(
        run,
        stage=payload.stage,
        message=payload.message,
        count=payload.count,
        status=payload.status,
        error=payload.error,
    )

    return Message(message="Progress recorded.")


@router.post(
    "/results",
    response_model=SearchResultsResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Final results callback for the n8n workflow",
)
async def submit_results(
    payload: SearchResultsRequest,
    db: DbSession,
    x_leadpilot_run_token: str = Header(..., alias="X-LeadPilot-Run-Token"),
) -> SearchResultsResponse:
    """Called once by n8n with the complete company + decision-maker output.

    `run_id` travels in the body (not the URL) to match the workflow payload.
    Authenticated by the same per-run token as the progress callbacks.

    This also closes the run: it is the workflow's last word, so the tracker
    flips to complete and the user is notified. Safe to retry -- a company
    already saved is refreshed rather than duplicated, and its decision makers
    are replaced rather than appended.
    """
    # A deadlock means MySQL already rolled us back and wants a retry. It can
    # still happen when two DIFFERENT runs save overlapping companies at the
    # same moment (the per-run lock in ingest() does not cover that), so try
    # a few times with a short back-off before giving up.
    for attempt in range(RESULTS_DEADLOCK_RETRIES):
        run = await LeadSearchProgressService(db).runs.get_for_callback(
            payload.run_id, x_leadpilot_run_token
        )

        if run is None:
            log.warning("lead_results.rejected", run_id=payload.run_id)
            raise UnauthorizedError("Invalid run or token.")

        try:
            return await LeadResultsService(db).ingest(run, payload)
        except OperationalError as exc:
            if not is_deadlock(exc) or attempt == RESULTS_DEADLOCK_RETRIES - 1:
                raise

            log.warning("lead_results.deadlock_retry", run_id=run.id, attempt=attempt + 1)
            await db.rollback()
            await asyncio.sleep(0.05 * (attempt + 1))

    raise AssertionError("unreachable")  # pragma: no cover


@router.post(
    "/error",
    response_model=WorkflowErrorResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Error callback for the n8n workflow",
)
async def report_workflow_error(
    payload: WorkflowErrorRequest, db: DbSession
) -> WorkflowErrorResponse:
    """Called by n8n's Error Trigger when the workflow fails.

    Accepts the error-trigger payload as-is:

        {
          "workflow_id": "...", "execution_id": "...",
          "error_message": "...", "last_node_executed": "..."
        }

    Add `run_id` and `progress_token` (pass them through from the webhook
    data) and the failure is attributed: the user's run is marked failed, the
    progress tracker stops spinning, and they get a notification. Without
    them the error is still stored and logged, but no user can be told.

    Deliberately unauthenticated in the same way the other callbacks are
    token-scoped: n8n's Error Trigger cannot add headers per run, and refusing
    an unattributed failure would mean losing the evidence of an outage. The
    endpoint only ever writes a log row; attribution requires the run token.
    """
    run = None

    if payload.run_id and payload.progress_token:
        run = await LeadSearchProgressService(db).runs.get_for_callback(
            payload.run_id, payload.progress_token
        )
        if run is None:
            log.warning("workflow_error.bad_run_token", run_id=payload.run_id)

    error = await WorkflowErrorService(db).record(
        workflow_id=payload.workflow_id,
        execution_id=payload.execution_id,
        error_message=payload.error_message or "The workflow reported a failure.",
        last_node_executed=payload.last_node_executed,
        run=run,
    )

    return WorkflowErrorResponse(
        recorded=True,
        attributed=run is not None,
        reason=explain(error.error_message),
    )


@router.get(
    "/runs/{run_id}/results",
    response_model=list[CompanyRead],
    summary="Companies found by a run",
)
async def get_run_results(
    run_id: str, db: DbSession, current_user: CurrentUser
) -> list[CompanyRead]:
    """The companies (with their decision makers) a run produced."""
    service = LeadSearchProgressService(db)
    run = await service.get_for_user(current_user.id, run_id)

    companies = await CompanyRepository(db).list_for_run(run.id)

    return [CompanyRead.model_validate(company) for company in companies]


@router.get("/runs/{run_id}/stream", summary="Live progress stream (SSE)")
async def stream_progress(
    run_id: str, request: Request, db: DbSession, token: str
) -> Response:
    """Server-sent events: one `progress` event whenever this run advances.

    Authenticated by a `token` query parameter (the user's access token),
    because EventSource cannot set headers. Each event carries only the run id;
    the client refetches the run so what it shows always matches the database.
    """
    try:
        payload = decode_token(token, expected_type="access")
    except Exception as exc:
        raise UnauthorizedError("Invalid or expired token.") from exc

    user_id = payload.get("sub")
    if not user_id:
        raise UnauthorizedError("Malformed token.")

    user = await UserRepository(db).get(user_id)
    if user is None or not user.is_active:
        raise UnauthorizedError("User no longer active.")

    # Confirm the run belongs to this user before opening the stream.
    run = await LeadSearchProgressService(db).runs.get_for_user(user.id, run_id)
    if run is None:
        raise NotFoundError("No search run with that id.")

    async def events() -> AsyncGenerator[str, None]:
        async with progress_stream.subscribe(user.id) as queue:
            yield f"event: ready\ndata: {json.dumps({'run_id': run_id})}\n\n"

            while True:
                if await request.is_disconnected():
                    break

                try:
                    changed_run_id = await asyncio.wait_for(
                        queue.get(), timeout=_HEARTBEAT_SECONDS
                    )
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue

                # One connection per user may see other runs; only report this one.
                if changed_run_id != run_id:
                    continue

                yield f"event: progress\ndata: {json.dumps({'run_id': run_id})}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
