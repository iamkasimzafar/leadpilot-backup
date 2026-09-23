"""Radar monitors: the saved searches n8n re-runs on a schedule.

Three jobs live here.

CRUD
    What the Radar Monitors panel calls. A monitor is inert data until the
    scheduler picks it up.

The due list
    n8n's monitor workflow wakes on its own cron, asks for whatever is due,
    and runs each one. Handing out a monitor also claims it, so an overlapping
    pass cannot dispatch the same monitor twice.

Results ingest
    The billing-critical half. Every contact the run found is checked against
    the emails the user already has; only genuinely new ones are inserted,
    counted and charged. A contact the user already had costs nothing, however
    many times a directory site re-surfaces it.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.models.lead import Company, DecisionMaker
from app.models.lead_search import (
    LeadSearchRun,
    RunStatus,
    SearchStage,
    SearchType,
)
from app.models.radar_monitor import (
    MonitorFrequency,
    MonitorSearchType,
    MonitorStatus,
    RadarMonitor,
)
from app.repositories.lead import CompanyRepository
from app.repositories.radar_monitor import (
    ContactEmailRepository,
    RadarMonitorRepository,
)
from app.schemas.lead import CompanyIn, DecisionMakerIn
from app.schemas.radar_monitor import (
    DueMonitor,
    MonitorCreateRequest,
    MonitorFilters,
    MonitorResultsRequest,
    MonitorResultsResponse,
    MonitorUpdateRequest,
)
from app.services.base import BaseService
from app.services.billing import BillingService
from app.services.billing_catalog import (
    BASE_CONTACT_CREDIT,
    WHATSAPP_VALIDATION_CREDITS,
)
from app.services.leads import LeadService
from app.services.search_targeting import role_titles_for, size_bounds_for

log = get_logger(__name__)

# A verified email means the provider's own check passed. Only these are
# billable, matching search_pricing.py.
_EMAIL_VALID = "valid"

_WHATSAPP_ACTIVE = "active"


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class IngestTally:
    """What one monitor run actually produced."""

    companies_received: int
    companies_saved: int

    # Companies this run wrote leads into, new or already held. What the
    # results list shows, so it is what the run's company count must report.
    companies_touched: int

    contacts_added: int
    duplicates_skipped: int
    billable_contacts: int
    active_whatsapp: int


class RadarMonitorService(BaseService):
    def __init__(self, db: AsyncSession) -> None:
        super().__init__(db)
        self.monitors = RadarMonitorRepository(db)
        self.companies = CompanyRepository(db)
        self.contacts = ContactEmailRepository(db)
        self.billing = BillingService(db)
        self.leads = LeadService(db)

    # --- CRUD ---------------------------------------------------------------

    async def create(self, user_id: str, payload: MonitorCreateRequest) -> RadarMonitor:
        monitor = await self.monitors.create(
            user_id=user_id,
            name=payload.name,
            search_type=payload.search_type,
            search_value=payload.search_value,
            search_label=payload.search_label,
            filters_json=json.dumps(
                payload.filters.model_dump(exclude_none=True), ensure_ascii=False
            ),
            frequency=payload.frequency,
            limit_per_run=payload.limit_per_run,
            status=MonitorStatus.RUNNING.value,
            # Due immediately: a monitor the user just saved should not wait a
            # whole day to prove it works.
            next_run_at=None,
        )
        await self.commit()

        log.info(
            "radar_monitor.created",
            monitor_id=monitor.id,
            user_id=user_id,
            search_type=payload.search_type,
            frequency=payload.frequency,
        )

        return monitor

    async def list_for_user(
        self, user_id: str, *, offset: int = 0, limit: int = 50
    ) -> tuple[list[RadarMonitor], int]:
        items = await self.monitors.list_for_user(user_id, offset=offset, limit=limit)
        total = await self.monitors.count_for_user(user_id)

        return items, total

    async def get(self, monitor_id: str, user_id: str) -> RadarMonitor:
        monitor = await self.monitors.get_for_user(monitor_id, user_id)
        if monitor is None:
            raise NotFoundError("Monitor not found.")

        return monitor

    async def update(
        self, monitor_id: str, user_id: str, payload: MonitorUpdateRequest
    ) -> RadarMonitor:
        monitor = await self.get(monitor_id, user_id)

        if payload.name is not None:
            monitor.name = payload.name.strip()

        # Changing what the monitor searches restarts its paging.
        #
        # `serper_offset` counts pages through one particular query. Carried
        # over to a different term it would put the next run on page 3 of a
        # search nobody has read page 1 of, so the monitor would skip its own
        # first -- and best -- results. Only an actual change resets it: a
        # save that leaves the term alone must not rewind the paging and make
        # the monitor re-find leads the user already has.
        new_type = payload.search_type or monitor.search_type
        new_value = payload.search_value or monitor.search_value

        if new_type != monitor.search_type or new_value != monitor.search_value:
            monitor.search_type = new_type
            monitor.search_value = new_value
            monitor.serper_offset = 0

            log.info(
                "radar_monitor.target_changed",
                monitor_id=monitor.id,
                search_type=new_type,
                reset_offset=True,
            )

        # The label travels with the value, so it is set either way: an
        # HS-code monitor needs its wording refreshed even when only the
        # description changed, and a keyword monitor has no label at all.
        if payload.search_label is not None or payload.search_type == "keyword":
            monitor.search_label = (
                payload.search_label if new_type == MonitorSearchType.HS_CODE else None
            )

        if payload.filters is not None:
            monitor.filters_json = json.dumps(
                payload.filters.model_dump(exclude_none=True), ensure_ascii=False
            )

        if payload.frequency is not None:
            monitor.frequency = payload.frequency

        if payload.limit_per_run is not None:
            monitor.limit_per_run = payload.limit_per_run

        if payload.status is not None and payload.status != monitor.status:
            monitor.status = payload.status

            # Clearing the schedule means "due as soon as it is eligible".
            # Resuming should run promptly rather than wait out the rest of
            # yesterday's interval; a paused monitor is excluded by status, so
            # the field is moot until it is resumed.
            monitor.next_run_at = None

        await self.commit()

        log.info("radar_monitor.updated", monitor_id=monitor.id, user_id=user_id)

        return monitor

    async def delete(self, monitor_id: str, user_id: str) -> None:
        monitor = await self.get(monitor_id, user_id)
        await self.db.delete(monitor)
        await self.commit()

        log.info("radar_monitor.deleted", monitor_id=monitor_id, user_id=user_id)

    # --- The scheduler contract ---------------------------------------------

    async def claim_due(self, *, limit: int = 100) -> list[DueMonitor]:
        """Monitors ready to run, claimed as they are handed out.

        Claiming inside the same call is what makes this safe to poll: if the
        workflow retries, or two passes overlap, the conditional UPDATE lets
        exactly one of them take each monitor. A monitor that loses the race
        is simply left out of the response.
        """
        now = _utcnow()
        due = await self.monitors.due(now, limit=limit)

        base = settings.PUBLIC_API_URL.rstrip("/")
        results_url = f"{base}{settings.API_V1_PREFIX}/lead-radar/monitors/results"

        claimed: list[DueMonitor] = []

        for monitor in due:
            interval = MonitorFrequency(monitor.frequency).interval_days
            next_run = now + timedelta(days=interval)

            if not await self.monitors.claim(monitor, now, next_run):
                log.info("radar_monitor.claim_lost", monitor_id=monitor.id)
                continue

            if monitor.started_at is None:
                monitor.started_at = now

            filters = MonitorFilters(**monitor.filters)

            # An HS-code monitor searches the product wording, not the digits:
            # almost nobody publishes an HS code on their site, so searching
            # the code returns tariff pages instead of buyers.
            is_hs = monitor.search_type == MonitorSearchType.HS_CODE.value
            search_term = (
                monitor.search_label or monitor.search_value
                if is_hs
                else monitor.search_value
            )

            # Resolve the catalogue codes here, so the workflow filters on the
            # real Snov.io titles and employee bounds without carrying a copy
            # of the catalogue itself.
            role = filters.contact_role
            size = filters.company_size
            titles = role_titles_for(role) if role else None
            bounds = size_bounds_for(size) if size else None

            # Open a run for this dispatch, so the monitor's work shows up in
            # Your Searches alongside the user's own searches rather than
            # happening invisibly overnight.
            run = LeadSearchRun(
                user_id=monitor.user_id,
                search_type=SearchType.MONITOR.value,
                original_keyword=monitor.name,
                keyword_count=1,
                auto_add_to_leads=True,
                country=filters.country,
                contact_role=filters.contact_role,
                company_size=filters.company_size,
                validate_whatsapp=False,
                monitor_id=monitor.id,
                status=RunStatus.RUNNING.value,
                stage=SearchStage.SEARCHING_COMPANIES.value,
            )
            self.db.add(run)
            await self.db.flush()

            claimed.append(
                DueMonitor(
                    monitor_id=monitor.id,
                    run_id=run.id,
                    user_id=monitor.user_id,
                    search_type=monitor.search_type,
                    search_term=search_term,
                    original_keyword=search_term,
                    hs_code=monitor.search_value if is_hs else None,
                    country=filters.country,
                    company_size=filters.company_size,
                    contact_role=filters.contact_role,
                    contact_role_titles=titles or [],
                    company_size_min=bounds[0] if bounds else None,
                    company_size_max=bounds[1] if bounds else None,
                    validate_whatsapp=False,
                    limit_per_run=monitor.limit_per_run,
                    serper_offset=monitor.serper_offset,
                    # The workflow asks Serper for limit_per_run results per
                    # page, and the offset advances by that same amount after
                    # each run, so the two stay in step: offset 100 at 50 a
                    # page is page 3. Dividing by the current limit also keeps
                    # it right if the user changes the limit mid-life.
                    serper_page=(monitor.serper_offset // monitor.limit_per_run) + 1,
                    results_url=results_url,
                    progress_token=monitor.callback_token,
                )
            )

        await self.commit()

        log.info("radar_monitor.due_claimed", count=len(claimed), considered=len(due))

        return claimed

    # --- Results ingest -----------------------------------------------------

    async def ingest(
        self, monitor: RadarMonitor, payload: MonitorResultsRequest
    ) -> MonitorResultsResponse:
        """Save a run's results, billing only what is genuinely new.

        The dedupe is the whole point. A background monitor will re-surface
        the same directory site over a month; without this the user pays again
        every time. An email the user already has is discarded outright: not
        inserted, not counted, not charged.
        """
        if payload.status == "failed":
            return await self._fail(monitor, payload.error)

        now = _utcnow()

        # Every candidate email in this payload, in one lookup rather than one
        # query per contact.
        candidates = [
            person.verified_email
            for company in payload.companies
            for person in company.decision_makers
            if person.verified_email
        ]
        already_have = await self.contacts.existing_emails(monitor.user_id, candidates)

        # Emails seen earlier in *this* payload. The run itself can carry the
        # same contact under two companies, and that must not bill twice
        # either.
        seen: set[str] = set()

        companies_saved = 0
        companies_touched = 0
        contacts_added = 0
        duplicates_skipped = 0
        billable = 0
        active_whatsapp = 0

        # The per-run cap the user chose. Enforced here, on the billing path,
        # rather than trusted to the workflow: it is the one promise that
        # bounds what a background run can cost, so it cannot depend on an
        # upstream node sizing its output correctly.
        cap = monitor.limit_per_run
        capped_out = 0

        # The run this dispatch opened, so the companies it saves are listed
        # under it in Your Searches.
        open_run = await self._latest_run(monitor)
        run_id = open_run.id if open_run is not None else None

        for incoming in payload.companies:
            fresh: list[DecisionMakerIn] = []

            for person in incoming.decision_makers:
                email = (person.verified_email or "").strip().lower()

                if not email:
                    # No address is nothing to bill and nothing to dedupe on.
                    continue

                if email in already_have or email in seen:
                    duplicates_skipped += 1
                    continue

                if contacts_added + len(fresh) >= cap:
                    # Over the cap: not inserted, not counted, not charged.
                    # Deliberately not recorded as "seen" either, so it is
                    # still fair game if a later run surfaces it again.
                    capped_out += 1
                    continue

                seen.add(email)
                fresh.append(person)

            # A company whose contacts the user already had adds nothing.
            if not fresh:
                continue

            company, created = await self._upsert_company(
                monitor, incoming, now, run_id
            )
            companies_touched += 1
            if created:
                companies_saved += 1

            for person in fresh:
                self.db.add(
                    DecisionMaker(
                        company_id=company.id,
                        user_id=monitor.user_id,
                        full_name=person.full_name,
                        job_title=person.job_title,
                        verified_email=person.verified_email,
                        email_status=person.email_status,
                        linkedin_url=person.linkedin_url,
                        phone_number=person.phone_number,
                        whatsapp_status=person.whatsapp_status,
                        extra_json="{}",
                    )
                )
                contacts_added += 1

                # Billable on the same terms as a manual search: a verified
                # address, and the WhatsApp premium only when Active.
                if (person.email_status or "").strip().lower() == _EMAIL_VALID:
                    billable += 1

                    if (person.whatsapp_status or "").strip().lower() == _WHATSAPP_ACTIVE:
                        active_whatsapp += 1

            await self.db.flush()

        tally = IngestTally(
            companies_received=len(payload.companies),
            companies_saved=companies_saved,
            companies_touched=companies_touched,
            contacts_added=contacts_added,
            duplicates_skipped=duplicates_skipped,
            billable_contacts=billable,
            active_whatsapp=active_whatsapp,
        )

        charged, shortfall = await self._bill(monitor, tally)

        # Advancing the offset is what makes tomorrow's run find anything.
        # Only a completed run advances it: a failed one would skip a page of
        # results nobody ever looked at.
        monitor.serper_offset += monitor.limit_per_run
        monitor.total_leads_generated += contacts_added
        monitor.last_completed_at = now

        # Close the run this dispatch opened, so Your Searches shows what the
        # monitor did: what it found, what was new, and what it skipped as
        # already-owned.
        await self._close_run(
            monitor,
            tally,
            charged=charged,
            shortfall=shortfall,
            at=now,
        )
        monitor.last_error = None

        await self.commit()

        log.info(
            "radar_monitor.ingested",
            monitor_id=monitor.id,
            user_id=monitor.user_id,
            companies_received=tally.companies_received,
            contacts_added=contacts_added,
            duplicates_skipped=duplicates_skipped,
            capped_out=capped_out,
            charged=charged,
            shortfall=shortfall,
            serper_offset=monitor.serper_offset,
        )

        return MonitorResultsResponse(
            monitor_id=monitor.id,
            companies_received=tally.companies_received,
            companies_saved=companies_saved,
            companies_touched=companies_touched,
            contacts_added=contacts_added,
            duplicates_skipped=duplicates_skipped,
            credits_charged=charged,
            credits_shortfall=shortfall,
            total_leads_generated=monitor.total_leads_generated,
            serper_offset=monitor.serper_offset,
        )

    async def _latest_run(self, monitor: RadarMonitor) -> LeadSearchRun | None:
        """The run this monitor's most recent dispatch opened.

        Matched by monitor rather than carried on the payload, so a workflow
        that loses the run id still closes the right row.
        """
        result = await self.db.execute(
            select(LeadSearchRun)
            .where(
                LeadSearchRun.monitor_id == monitor.id,
                LeadSearchRun.status == RunStatus.RUNNING.value,
            )
            .order_by(LeadSearchRun.created_at.desc())
            .limit(1)
        )

        return result.scalar_one_or_none()

    async def _close_run(
        self,
        monitor: RadarMonitor,
        tally: IngestTally,
        *,
        charged: int,
        shortfall: int,
        at: datetime,
    ) -> None:
        """Mark this monitor's run complete, with what it actually produced."""
        run = await self._latest_run(monitor)
        if run is None:
            return

        run.status = RunStatus.COMPLETED.value
        run.stage = SearchStage.COMPLETED.value
        # Every company this run put leads into, not just the ones it created.
        # The results list below shows all of them, so counting only new rows
        # here would contradict what the user is looking at.
        run.companies_found = tally.companies_touched
        run.contacts_found = tally.contacts_added + tally.duplicates_skipped
        run.contacts_added = tally.contacts_added
        run.contacts_duplicate = tally.duplicates_skipped
        run.credits_charged = charged
        run.credits_shortfall = shortfall
        run.credits_charged_at = at
        run.finished_at = at
        run.results_received_at = at

    async def _fail_run(self, monitor: RadarMonitor, error: str, at: datetime) -> None:
        """Mark this monitor's run failed. Nothing is billed for it."""
        run = await self._latest_run(monitor)
        if run is None:
            return

        run.status = RunStatus.FAILED.value
        run.error = error
        run.finished_at = at

    async def _upsert_company(
        self,
        monitor: RadarMonitor,
        incoming: CompanyIn,
        now: datetime,
        run_id: str | None,
    ) -> tuple[Company, bool]:
        """Find the user's existing row for this company, or make one.

        Reuses the same (user_id, website) identity the manual search uses, so
        a monitor finding a company the user already has attaches the new
        contacts to it rather than creating a second copy.
        """
        existing = await self.companies.find_existing(
            monitor.user_id, incoming.website, incoming.company_name
        )

        if existing is not None:
            # A monitor's whole purpose is to put leads in My Leads, so a
            # company it re-surfaced counts as added if it was not already.
            if existing.added_to_leads_at is None:
                self.leads.mark_added(existing, now)

            # Record that this run found it too, without moving the company's
            # own run pointer off whichever search first turned it up.
            if run_id is not None:
                await self.companies.link_to_run(run_id, existing.id)

            return existing, False

        company = await self.companies.create(
            user_id=monitor.user_id,
            run_id=run_id,
            company_name=incoming.company_name,
            website=incoming.website,
            location=incoming.location,
            industry=incoming.industry,
            company_size=incoming.company_size,
            hq_phone=incoming.hq_phone,
            extra_json="{}",
        )
        self.leads.mark_added(company, now)
        await self.db.flush()

        if run_id is not None:
            await self.companies.link_to_run(run_id, company.id)

        return company, True

    async def _bill(self, monitor: RadarMonitor, tally: IngestTally) -> tuple[int, int]:
        """Charge for the contacts this run actually added.

        Same rates as a manual search, so a lead costs the same however it was
        found. Charged capped rather than refused: the work is already done
        and the wallet cannot go negative, so whatever the balance could not
        cover is reported as a shortfall.
        """
        if tally.billable_contacts <= 0:
            return 0, 0

        lines: list[tuple[str, int]] = [
            (
                f"Radar monitor leads ({tally.billable_contacts} verified contacts)",
                tally.billable_contacts * BASE_CONTACT_CREDIT,
            )
        ]

        if tally.active_whatsapp > 0:
            lines.append(
                (
                    f"WhatsApp validation ({tally.active_whatsapp} active)",
                    tally.active_whatsapp * WHATSAPP_VALIDATION_CREDITS,
                )
            )

        return await self.billing.charge_capped(
            monitor.user_id,
            lines,
            reference_type="radar_monitor",
            reference_id=monitor.id,
        )

    async def _fail(
        self, monitor: RadarMonitor, error: str | None
    ) -> MonitorResultsResponse:
        """Record a failed run without billing or advancing the offset."""
        monitor.last_error = (error or "Monitor run failed.")[:500]

        await self._fail_run(monitor, monitor.last_error, _utcnow())

        await self.commit()

        log.warning(
            "radar_monitor.run_failed", monitor_id=monitor.id, error=monitor.last_error
        )

        return MonitorResultsResponse(
            monitor_id=monitor.id,
            companies_received=0,
            companies_saved=0,
            contacts_added=0,
            duplicates_skipped=0,
            credits_charged=0,
            credits_shortfall=0,
            total_leads_generated=monitor.total_leads_generated,
            serper_offset=monitor.serper_offset,
        )
