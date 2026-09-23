"""Dispatches an HS Code search (Advanced Filters) to its n8n webhook.

Phase 1 (free AI translation of a product description into candidate HS
codes) lives in services/hs_code.py and never touches this file -- nothing is
dispatched until the user has confirmed a code. This service is Phase 2: the
user clicked a candidate and "Find Target Buyers", so a run is opened and the
confirmed 6-digit code, the selected country (resolved to its ccTLD), and the
same Snov.io extraction filters the keyword flow uses are handed to n8n. The
workflow assembles the Google dork itself; the ccTLD travels pre-resolved so
it does not need country_tld.py's own copy of the exceptions table.

Results land on the ordinary /lead-radar/results endpoint and are billed
exactly like a b2b search -- ingestion and settlement are LeadResultsService's
job, unchanged, once dispatched.
"""

import httpx

from app.core.config import settings
from app.core.exceptions import ServiceUnavailableError, UpstreamError
from app.core.logging import get_logger
from app.schemas.lead_radar import StartHsCodeSearchRequest, StartHsCodeSearchResponse
from app.services.billing_catalog import WHATSAPP_VALIDATION_CREDITS
from app.services.company_types import name_for as company_type_name_for
from app.services.countries import name_for
from app.services.country_tld import tld_for
from app.services.search_targeting import (
    role_name_for,
    role_titles_for,
    size_bounds_for,
    size_name_for,
)

log = get_logger(__name__)

_TEST_MODE_HINT = (
    "The n8n workflow is not listening. Open the workflow and click "
    "'Execute workflow', or activate it and switch the webhook URL to the "
    "/webhook/ path."
)


class HsCodeSearchService:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        # Injectable so tests can hand in a MockTransport-backed client.
        self._client = client

    async def start(
        self,
        payload: StartHsCodeSearchRequest,
        *,
        user_id: str,
        run_id: str,
        callback_token: str,
    ) -> StartHsCodeSearchResponse:
        webhook_url = settings.N8N_HS_CODE_WEBHOOK_URL
        if not webhook_url:
            log.warning(
                "hs_code_search.disabled", reason="N8N_HS_CODE_WEBHOOK_URL not set"
            )
            raise ServiceUnavailableError(
                "HS code search is not configured on this server."
            )

        country_name = name_for(payload.country) if payload.country else None
        country_tld = tld_for(payload.country)

        role_name = role_name_for(payload.contact_role) if payload.contact_role else None
        role_titles = (
            role_titles_for(payload.contact_role) if payload.contact_role else None
        )
        size_name = size_name_for(payload.company_size) if payload.company_size else None
        size_bounds = (
            size_bounds_for(payload.company_size) if payload.company_size else None
        )

        base = settings.PUBLIC_API_URL.rstrip("/")
        prefix = f"{base}{settings.API_V1_PREFIX}"

        n8n_payload: dict[str, object] = {
            "run_id": run_id,
            "progress_url": f"{prefix}/lead-radar/runs/{run_id}/progress",
            "results_url": f"{prefix}/lead-radar/results",
            "progress_token": callback_token,
            "hs_code": payload.hs_code,
            "hs_description": payload.hs_description,
            # A hint for the workflow's own AI step, which turns the code into
            # the commercial product names it actually searches. The code is
            # never searched literally -- see the field's note on
            # StartHsCodeSearchRequest.
            "product_term": payload.product_term,
            # Which trade words to pair each product name with.
            "company_type": payload.company_type,
            "company_type_name": company_type_name_for(payload.company_type)
            if payload.company_type
            else None,
            # None for a worldwide search: the workflow adds no site: filter.
            "country": payload.country,
            "country_name": country_name,
            "country_tld": country_tld,
            "contact_role": payload.contact_role,
            "contact_role_name": role_name,
            "contact_role_titles": role_titles,
            "company_size": payload.company_size,
            "company_size_name": size_name,
            "company_size_min": size_bounds[0] if size_bounds else None,
            "company_size_max": size_bounds[1] if size_bounds else None,
            "result_limit": payload.result_limit,
            "validate_whatsapp": payload.validate_whatsapp,
            "whatsapp_credits_per_check": (
                WHATSAPP_VALIDATION_CREDITS if payload.validate_whatsapp else None
            ),
            "requested_by": user_id,
        }

        headers = {}
        if settings.N8N_WEBHOOK_SECRET:
            headers[settings.N8N_WEBHOOK_HEADER] = settings.N8N_WEBHOOK_SECRET

        await self._post(webhook_url, n8n_payload, headers)

        log.info(
            "hs_code_search.dispatched",
            run_id=run_id,
            hs_code=payload.hs_code,
            country=payload.country or "worldwide",
            contact_role=payload.contact_role or "any",
            company_size=payload.company_size or "any",
        )

        return StartHsCodeSearchResponse(
            dispatched=True,
            run_id=run_id,
            hs_code=payload.hs_code,
            country=payload.country,
            country_name=country_name,
        )

    async def _post(
        self, url: str, payload: dict[str, object], headers: dict[str, str]
    ) -> None:
        try:
            if self._client is not None:
                response = await self._client.post(url, json=payload, headers=headers)
            else:
                async with httpx.AsyncClient(timeout=settings.N8N_TIMEOUT) as client:
                    response = await client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            log.warning("hs_code_search.timeout", url=url)
            raise UpstreamError(
                "The HS code search workflow took too long to respond. Try again."
            ) from exc
        except httpx.HTTPError as exc:
            log.error("hs_code_search.transport_error", error=str(exc))
            raise UpstreamError("Could not reach the HS code search workflow.") from exc

        if response.status_code == 404 and "not registered" in response.text:
            log.warning("hs_code_search.webhook_not_registered", url=url)
            raise UpstreamError(_TEST_MODE_HINT)

        if response.status_code in (401, 403):
            log.error("hs_code_search.unauthorized", status=response.status_code)
            raise UpstreamError("The HS code search workflow rejected the request.")

        if response.status_code >= 400:
            log.error(
                "hs_code_search.http_error",
                status=response.status_code,
                body=response.text[:500],
            )
            raise UpstreamError("The HS code search workflow returned an error.")


__all__ = ["HsCodeSearchService"]
