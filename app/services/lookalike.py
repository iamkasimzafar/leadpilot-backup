"""Lookalike Companies: a two-step search the user controls between steps.

Step 1 (discovery) turns a competitor's domain into a list of similar company
websites. No Company or DecisionMaker rows are created and no per-item
billing happens -- there is nothing yet to size a bigger bill on. A flat
discovery fee is charged the moment the list lands (LOOKALIKE_DISCOVERY_CREDITS),
same call-and-forget shape as AI keyword expansion. The user then picks which
of those domains are worth spending real credits on.

Step 2 (find contacts for selected) takes exactly the domains the user
checked and dispatches them to a different n8n webhook, which runs them
through the same Snov.io chain a b2b search uses. Its results land on the
ordinary /lead-radar/results endpoint and are billed exactly like a b2b run
-- ingestion and settlement are LeadResultsService's job, unchanged.

Both steps reuse LeadSearchRun (search_type="lookalike_discovery" /
"lookalike_contacts") and the same progress-callback machinery every other
search type already has: one dispatcher, two webhooks, one settlement path.
"""

import json
from datetime import UTC, datetime

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import (
    InsufficientCreditsError,
    ServiceUnavailableError,
    UpstreamError,
)
from app.core.logging import get_logger
from app.models.lead_search import LeadSearchRun, RunStatus, SearchStage
from app.models.notification import NotificationKind
from app.schemas.lead_radar import (
    LookalikeDiscoveryResultsRequest,
    LookalikeDiscoveryResultsResponse,
    StartLookalikeContactsRequest,
    StartLookalikeContactsResponse,
    StartLookalikeDiscoveryResponse,
)
from app.services.base import BaseService
from app.services.billing import BillingService
from app.services.billing_catalog import (
    LOOKALIKE_DISCOVERY_CREDITS,
    WHATSAPP_VALIDATION_CREDITS,
)
from app.services.lead_search_progress import LeadSearchProgressService
from app.services.notification import NotificationService
from app.services.progress_stream import progress_stream

log = get_logger(__name__)

_TEST_MODE_HINT = (
    "The n8n workflow is not listening. Open the workflow and click "
    "'Execute workflow', or activate it and switch the webhook URL to the "
    "/webhook/ path."
)


class LookalikeService(BaseService):
    def __init__(self, db: AsyncSession, client: httpx.AsyncClient | None = None) -> None:
        super().__init__(db)
        self._client = client
        self.progress = LeadSearchProgressService(db)
        self.billing = BillingService(db)
        self.notifications = NotificationService(db)

    # --- Step 1: discovery ---------------------------------------------------
    async def start_discovery(
        self, user_id: str, domain: str
    ) -> StartLookalikeDiscoveryResponse:
        webhook_url = settings.N8N_LOOKALIKE_DISCOVERY_WEBHOOK_URL
        if not webhook_url:
            log.warning(
                "lookalike.disabled", reason="N8N_LOOKALIKE_DISCOVERY_WEBHOOK_URL not set"
            )
            raise ServiceUnavailableError(
                "Lookalike company search is not configured on this server."
            )

        run = await self.progress.create_run(
            user_id,
            domain,
            [domain],
            auto_add_to_leads=False,
            search_type="lookalike_discovery",
        )

        payload = self._payload(
            run.id,
            run.callback_token,
            results_path="/lead-radar/lookalike/discover/results",
            extra={"domain": domain},
        )

        try:
            await self._post(webhook_url, payload)
        except (UpstreamError, ServiceUnavailableError) as exc:
            await self.progress.mark_dispatch_failed(run, str(exc))
            raise

        log.info("lookalike.discovery_dispatched", run_id=run.id, domain=domain)

        return StartLookalikeDiscoveryResponse(
            dispatched=True, run_id=run.id, domain=domain
        )

    async def ingest_discovery_results(
        self, run: LeadSearchRun, payload: LookalikeDiscoveryResultsRequest
    ) -> LookalikeDiscoveryResultsResponse:
        """Store the domain list a discovery run found, and bill the flat fee.

        No Company or DecisionMaker rows are created here -- that only ever
        happens in step 2, once the user has picked which domains to spend
        credits on. Idempotent the same way results ingestion always is here:
        a repeat POST (an n8n HTTP-node retry) overwrites the same list rather
        than duplicating it, and is never charged twice for the same run.
        """
        failed = payload.is_failed
        domains = [] if failed else payload.domains
        now = datetime.now(UTC)

        run.discovered_domains_json = json.dumps(
            [d.model_dump(exclude_none=True) for d in domains], ensure_ascii=False
        )

        first_results = run.results_received_at is None
        if first_results:
            run.results_received_at = now

        if failed:
            run.status = RunStatus.FAILED.value
            run.error = (payload.error or payload.error_text or "The workflow failed.")[
                :500
            ]
            run.error_reason = payload.reason
        elif run.status == RunStatus.RUNNING.value:
            run.status = RunStatus.COMPLETED.value
            run.stage = SearchStage.COMPLETED.value

        run.finished_at = run.finished_at or now

        # Flat discovery fee, charged once, only when there is a list worth
        # charging for. A balance too low to cover it still keeps the domain
        # list -- the search already ran; only the charge is skipped -- and
        # leaves credits_charged_at unset so a later retry can still try.
        if not failed and run.credits_charged_at is None and domains:
            try:
                await self.billing.spend(
                    run.user_id,
                    LOOKALIKE_DISCOVERY_CREDITS,
                    f"Lookalike companies — {run.original_keyword}",
                    reference_type="lead_search_run",
                    reference_id=run.id,
                )
                run.credits_charged = LOOKALIKE_DISCOVERY_CREDITS
                run.credits_charged_at = now
            except InsufficientCreditsError:
                log.warning("lookalike.discovery_charge_skipped", run_id=run.id)

        notification = None
        if first_results:
            notification = await self._finish_notification(run, len(domains))

        await self.commit()
        progress_stream.publish(run.user_id, run.id)
        if notification is not None:
            NotificationService.publish(run.user_id, notification.id)

        log.info(
            "lookalike.discovery_ingested",
            run_id=run.id,
            domains_found=len(domains),
            failed=failed,
        )

        return LookalikeDiscoveryResultsResponse(
            run_id=run.id, status=run.status, domains_found=len(domains)
        )

    async def _finish_notification(self, run: LeadSearchRun, domains_found: int):  # type: ignore[no-untyped-def]
        if run.status == RunStatus.FAILED.value:
            return await self.notifications.create(
                run.user_id,
                kind=NotificationKind.MONITOR,
                title=f'Lookalike search for "{run.original_keyword}" failed',
                subtitle=run.error or "The lookalike workflow reported an error.",
                link="/lead-radar",
                commit=False,
            )

        return await self.notifications.create(
            run.user_id,
            kind=NotificationKind.LEAD,
            title=f'Similar companies for "{run.original_keyword}" are ready',
            subtitle=(
                f"{domains_found:,} similar companies found. Review the list "
                "and pick which ones to find contacts for."
            ),
            link="/lead-radar",
            commit=False,
        )

    # --- Step 2: find contacts for selected -----------------------------------
    async def start_contacts(
        self, user_id: str, payload: StartLookalikeContactsRequest
    ) -> StartLookalikeContactsResponse:
        webhook_url = settings.N8N_LOOKALIKE_CONTACTS_WEBHOOK_URL
        if not webhook_url:
            log.warning(
                "lookalike.disabled", reason="N8N_LOOKALIKE_CONTACTS_WEBHOOK_URL not set"
            )
            raise ServiceUnavailableError(
                "Lookalike contact search is not configured on this server."
            )

        source = await self.progress.get_for_user(user_id, payload.source_run_id)

        run = await self.progress.create_run(
            user_id,
            f"Lookalike: {source.original_keyword}",
            payload.domains,
            auto_add_to_leads=True,
            validate_whatsapp=payload.validate_whatsapp,
            search_type="lookalike_contacts",
        )
        run.source_run_id = source.id
        await self.commit()

        n8n_payload = self._payload(
            run.id,
            run.callback_token,
            # Step 2's output is a plain b2b-shaped result (companies +
            # decision makers), so it lands on the ordinary results endpoint
            # LeadResultsService already ingests and bills -- no new route.
            results_path="/lead-radar/results",
            extra={
                "domains": payload.domains,
                "validate_whatsapp": payload.validate_whatsapp,
                "whatsapp_credits_per_check": (
                    WHATSAPP_VALIDATION_CREDITS if payload.validate_whatsapp else None
                ),
            },
        )

        try:
            await self._post(webhook_url, n8n_payload)
        except (UpstreamError, ServiceUnavailableError) as exc:
            await self.progress.mark_dispatch_failed(run, str(exc))
            raise

        log.info(
            "lookalike.contacts_dispatched",
            run_id=run.id,
            source_run_id=source.id,
            domain_count=len(payload.domains),
        )

        return StartLookalikeContactsResponse(
            dispatched=True,
            run_id=run.id,
            source_run_id=source.id,
            domain_count=len(payload.domains),
        )

    # --- Shared ----------------------------------------------------------------
    def _payload(
        self,
        run_id: str,
        callback_token: str,
        *,
        results_path: str,
        extra: dict[str, object],
    ) -> dict[str, object]:
        base = settings.PUBLIC_API_URL.rstrip("/")
        prefix = f"{base}{settings.API_V1_PREFIX}"

        return {
            "run_id": run_id,
            "progress_url": f"{prefix}/lead-radar/runs/{run_id}/progress",
            "results_url": f"{prefix}{results_path}",
            "progress_token": callback_token,
            **extra,
        }

    async def _post(self, url: str, payload: dict[str, object]) -> None:
        headers = {}
        if settings.N8N_WEBHOOK_SECRET:
            headers[settings.N8N_WEBHOOK_HEADER] = settings.N8N_WEBHOOK_SECRET

        try:
            if self._client is not None:
                response = await self._client.post(url, json=payload, headers=headers)
            else:
                async with httpx.AsyncClient(timeout=settings.N8N_TIMEOUT) as client:
                    response = await client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise UpstreamError(
                "The lookalike workflow took too long to respond. Try again."
            ) from exc
        except httpx.HTTPError as exc:
            raise UpstreamError("Could not reach the lookalike workflow.") from exc

        if response.status_code == 404 and "not registered" in response.text:
            raise UpstreamError(_TEST_MODE_HINT)

        if response.status_code in (401, 403):
            raise UpstreamError("The lookalike workflow rejected the request.")

        if response.status_code >= 400:
            raise UpstreamError("The lookalike workflow returned an error.")


__all__ = ["LookalikeService"]
