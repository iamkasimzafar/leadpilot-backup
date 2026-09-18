"""Dispatches a lead search to the n8n workflow webhook.

The frontend never calls n8n directly: the webhook lives on the VM's private
network, and routing it through the backend keeps the URL and any shared secret
server-side and configurable per environment.
"""

import httpx

from app.core.config import settings
from app.core.exceptions import ServiceUnavailableError, UpstreamError
from app.core.logging import get_logger
from app.schemas.lead_radar import StartSearchResponse
from app.services.billing_catalog import WHATSAPP_VALIDATION_CREDITS
from app.services.company_types import name_for as company_type_name_for
from app.services.countries import name_for
from app.services.search_targeting import (
    role_name_for,
    role_titles_for,
    size_bounds_for,
    size_name_for,
)

log = get_logger(__name__)

# n8n's Webhook node answers 404 with this wording when the workflow is not
# listening. It is the single most likely failure while building, so it earns a
# message that says what to do rather than a generic upstream error.
_TEST_MODE_HINT = (
    "The n8n workflow is not listening. Open the workflow and click "
    "'Execute workflow', or activate it and switch N8N_WEBHOOK_URL to the "
    "/webhook/ path."
)


def _merge_keywords(original: str, expanded: list[str]) -> list[str]:
    """The original keyword always leads the list, followed by the selected
    expansions in order, with case-insensitive duplicates dropped."""
    merged: list[str] = []
    seen: set[str] = set()
    for raw in [original, *expanded]:
        term = " ".join(raw.split())
        key = term.casefold()
        if term and key not in seen:
            seen.add(key)
            merged.append(term)

    return merged


class LeadSearchService:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        # Injectable so tests can hand in a MockTransport-backed client.
        self._client = client

    @staticmethod
    def merge_keywords(original: str, expanded: list[str]) -> list[str]:
        """Public wrapper: the route needs the final list to open the run row
        before dispatch."""
        return _merge_keywords(" ".join(original.split()), expanded)

    async def start(
        self,
        original_keyword: str,
        expanded_keywords: list[str],
        *,
        auto_add_to_leads: bool = True,
        user_id: str | None = None,
        run_id: str,
        callback_token: str,
        country: str | None = None,
        company_type: str | None = None,
        contact_role: str | None = None,
        company_size: str | None = None,
        validate_whatsapp: bool = False,
    ) -> StartSearchResponse:
        original = " ".join(original_keyword.split())
        keywords = _merge_keywords(original, expanded_keywords)

        # Resolved here so the workflow gets a usable label without a lookup of
        # its own. None stays None: the workflow reads that as "worldwide".
        country_name = name_for(country) if country else None
        company_type_name = company_type_name_for(company_type) if company_type else None

        # The Snov.io extraction filters, resolved to what that API needs: job
        # titles to match, and an employee-count range.
        role_name = role_name_for(contact_role) if contact_role else None
        role_titles = role_titles_for(contact_role) if contact_role else None
        size_name = size_name_for(company_size) if company_size else None
        size_bounds = size_bounds_for(company_size) if company_size else None

        if not settings.N8N_WEBHOOK_URL:
            log.warning("lead_search.disabled", reason="N8N_WEBHOOK_URL not set")
            raise ServiceUnavailableError("Lead search is not configured on this server.")

        # The exact shape the n8n workflow expects. Extra context goes after the
        # two required keys so the workflow can ignore it safely.
        #
        # `progress_url` is pre-built so the workflow's HTTP nodes can POST to
        # it verbatim at each checkpoint -- no string assembly inside n8n.
        base = settings.PUBLIC_API_URL.rstrip("/")

        # Annotated because `country` makes the values heterogeneous, and a
        # dict[str, X] will not pass as dict[str, object] (invariance).
        payload: dict[str, object] = {
            "original_keyword": original,
            "expanded_keywords": keywords,
            # SerpApi `gl` code, or null for a worldwide search. `country_name`
            # is the display label for the same code, so the workflow can use
            # it in prompts and emails without its own lookup table.
            "country": country,
            "country_name": country_name,
            # Which kind of company to look for, or null for any. The code is
            # the stable value; the name is the display label for prompts.
            "company_type": company_type,
            "company_type_name": company_type_name,
            # Snov.io extraction filters. `contact_role_titles` is the job-title
            # list to match and `company_size_min/max` the employee range, both
            # pre-resolved so the workflow can pass them straight to Snov.io.
            # Null everywhere means "extract anyone", as before these existed.
            "contact_role": contact_role,
            "contact_role_name": role_name,
            "contact_role_titles": role_titles,
            "company_size": company_size,
            "company_size_name": size_name,
            "company_size_min": size_bounds[0] if size_bounds else None,
            "company_size_max": size_bounds[1] if size_bounds else None,
            # WhatsApp validation. Only run the check when this is true: it is
            # billed per number, so the workflow must not do it speculatively.
            # The price travels with it so the workflow can report the charge.
            "validate_whatsapp": validate_whatsapp,
            "whatsapp_credits_per_check": (
                WHATSAPP_VALIDATION_CREDITS if validate_whatsapp else None
            ),
            "auto_add_to_leads": auto_add_to_leads,
            "requested_by": user_id,
            "run_id": run_id,
            "progress_url": (
                f"{base}{settings.API_V1_PREFIX}/lead-radar/runs/{run_id}/progress"
            ),
            # Where the final results -- or the failure -- are POSTed, with
            # `progress_token` as X-LeadPilot-Run-Token and `run_id` as
            # X-LeadPilot-Run-Id (or in the body).
            "results_url": f"{base}{settings.API_V1_PREFIX}/lead-radar/results",
            "progress_token": callback_token,
        }

        headers = {}
        if settings.N8N_WEBHOOK_SECRET:
            headers[settings.N8N_WEBHOOK_HEADER] = settings.N8N_WEBHOOK_SECRET

        execution_id = await self._post(payload, headers)

        log.info(
            "lead_search.dispatched",
            original_keyword=original,
            keyword_count=len(keywords),
            run_id=run_id,
            country=country or "worldwide",
            company_type=company_type or "any",
            contact_role=contact_role or "any",
            company_size=company_size or "any",
            validate_whatsapp=validate_whatsapp,
        )

        return StartSearchResponse(
            dispatched=True,
            original_keyword=original,
            expanded_keywords=keywords,
            run_id=run_id,
            country=country,
            country_name=country_name,
            company_type=company_type,
            company_type_name=company_type_name,
            contact_role=contact_role,
            contact_role_name=role_name,
            company_size=company_size,
            company_size_name=size_name,
            validate_whatsapp=validate_whatsapp,
            whatsapp_credits_per_check=(
                WHATSAPP_VALIDATION_CREDITS if validate_whatsapp else None
            ),
            n8n_execution_id=execution_id,
        )

    @staticmethod
    def _execution_id_from(body: object) -> str | None:
        """n8n's execution id, if the webhook's response carried one.

        The default "Respond immediately" answer is `{"message": "Workflow was
        started"}` and has no id. A workflow that answers through a "Respond to
        Webhook" node can return `{"execution_id": "{{ $execution.id }}"}` (or
        `executionId`, or n8n's own `{"execution": {"id": ...}}` shape) and the
        run is tied to its execution from the first moment.
        """
        if not isinstance(body, dict):
            return None

        value = body.get("execution_id", body.get("executionId"))
        if value is None and isinstance(body.get("execution"), dict):
            value = body["execution"].get("id")

        if value is None:
            return None

        text = str(value).strip()

        return text[:64] or None

    async def _post(
        self, payload: dict[str, object], headers: dict[str, str]
    ) -> str | None:
        """POST the job to n8n. Returns the execution id when the response
        carried one, None otherwise."""
        url = settings.N8N_WEBHOOK_URL

        try:
            if self._client is not None:
                response = await self._client.post(url, json=payload, headers=headers)
            else:
                async with httpx.AsyncClient(timeout=settings.N8N_TIMEOUT) as client:
                    response = await client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            log.warning("lead_search.timeout", url=url)
            raise UpstreamError(
                "The lead search workflow took too long to respond. Try again."
            ) from exc
        except httpx.HTTPError as exc:
            log.error("lead_search.transport_error", error=str(exc))
            raise UpstreamError("Could not reach the lead search workflow.") from exc

        if response.status_code == 404 and "not registered" in response.text:
            log.warning("lead_search.webhook_not_registered", url=url)
            raise UpstreamError(_TEST_MODE_HINT)

        if response.status_code in (401, 403):
            log.error("lead_search.unauthorized", status=response.status_code)
            raise UpstreamError("The lead search workflow rejected the request.")

        if response.status_code >= 400:
            log.error(
                "lead_search.http_error",
                status=response.status_code,
                body=response.text[:500],
            )
            raise UpstreamError("The lead search workflow returned an error.")

        try:
            body = response.json()
        except ValueError:
            return None

        return self._execution_id_from(body)
