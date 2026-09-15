"""Lead Radar endpoints."""

from fastapi import APIRouter

from app.api.deps import CurrentUser, DbSession
from app.schemas.lead_radar import (
    ExpandKeywordRequest,
    ExpandKeywordResponse,
    StartSearchRequest,
    StartSearchResponse,
)
from app.services.billing import BillingService
from app.services.billing_catalog import AI_KEYWORD_EXPANSION_CREDITS
from app.services.keyword_expansion import KeywordExpansionService
from app.services.lead_search import LeadSearchService

router = APIRouter()


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

    result = await KeywordExpansionService().expand(payload.keyword)

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
    "/search",
    response_model=StartSearchResponse,
    summary="Start a lead search via the n8n workflow",
)
async def start_search(
    payload: StartSearchRequest, current_user: CurrentUser
) -> StartSearchResponse:
    """Forward the selected keywords to the n8n lead-search webhook. The
    original keyword always leads the list; duplicates are dropped."""
    return await LeadSearchService().start(
        payload.original_keyword,
        payload.expanded_keywords,
        auto_add_to_leads=payload.auto_add_to_leads,
        user_id=str(current_user.id),
    )
