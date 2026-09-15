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

    async def start(
        self,
        original_keyword: str,
        expanded_keywords: list[str],
        *,
        auto_add_to_leads: bool = True,
        user_id: str | None = None,
    ) -> StartSearchResponse:
        original = " ".join(original_keyword.split())
        keywords = _merge_keywords(original, expanded_keywords)

        if not settings.N8N_WEBHOOK_URL:
            log.warning("lead_search.disabled", reason="N8N_WEBHOOK_URL not set")
            raise ServiceUnavailableError("Lead search is not configured on this server.")

        # The exact shape the n8n workflow expects. Extra context goes after the
        # two required keys so the workflow can ignore it safely.
        payload = {
            "original_keyword": original,
            "expanded_keywords": keywords,
            "auto_add_to_leads": auto_add_to_leads,
            "requested_by": user_id,
        }

        headers = {}
        if settings.N8N_WEBHOOK_SECRET:
            headers[settings.N8N_WEBHOOK_HEADER] = settings.N8N_WEBHOOK_SECRET

        await self._post(payload, headers)

        log.info(
            "lead_search.dispatched",
            original_keyword=original,
            keyword_count=len(keywords),
        )

        return StartSearchResponse(
            dispatched=True,
            original_keyword=original,
            expanded_keywords=keywords,
        )

    async def _post(self, payload: dict[str, object], headers: dict[str, str]) -> None:
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
