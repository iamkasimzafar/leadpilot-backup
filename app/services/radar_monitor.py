"""Radar monitors: saved searches re-run in the background.

Three jobs live here.

CRUD
    What the Radar Monitors panel calls. A monitor is inert data until the
    dispatcher picks it up.

Dispatch
    Called by the Celery beat task every few minutes. Each running monitor
    carries the random second it next wakes (`next_run_at`); whichever have
    come round are claimed under a conditional UPDATE and POSTed to the n8n
    workflow, a few at a time, so an overlapping tick cannot dispatch the
    same monitor twice and n8n never sees a spike.

Results ingest
    The billing-critical half. Every contact the run found is checked against
    the emails the user already has; only genuinely new ones are inserted,
    counted and charged. A contact the user already had costs nothing, however
    many times a directory site re-surfaces it.
"""

import asyncio
import json
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import NotFoundError, UpstreamError
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


# Seconds in a day: the width of the window a daily monitor's slot is drawn
# from.
_DAY = 24 * 60 * 60

# Module-level so tests can seed it. SystemRandom would be overkill: the slot
# only has to be spread, not unguessable.
_rng = random.Random()


def first_run_slot(now: datetime, rng: random.Random | None = None) -> datetime:
    """When a brand-new or just-resumed monitor first runs: within the next
    MONITOR_FIRST_RUN_DELAY_MINUTES, so the user sees it work.

    Never sooner than 30 seconds, so a create-then-pause in quick succession
    has a moment to land before the dispatcher could pick it up.
    """
    ceiling = max(settings.MONITOR_FIRST_RUN_DELAY_MINUTES * 60, 30)
    delay = (rng or _rng).randint(30, ceiling)

    return now + timedelta(seconds=delay)


def next_slot(
    now: datetime, frequency: str, rng: random.Random | None = None
) -> datetime:
    """The random moment a monitor next wakes after running at `now`.

    A daily monitor gets a random second of the next UTC calendar day; a
    weekly one, of the same weekday next week. Anchoring to the calendar day
    is what makes "daily" mean daily: a slot chosen as "now plus 24h plus
    random" would drift later every run, and a monitor that ran at 02:00
    could wait until 23:59 the day after next. This way every day of a daily
    monitor's life sees exactly one run, just never at a predictable time.
    """
    interval = MonitorFrequency(frequency).interval_days
    day_start = datetime(now.year, now.month, now.day, tzinfo=UTC) + timedelta(
        days=interval
    )

    return day_start + timedelta(seconds=(rng or _rng).randrange(_DAY))


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

    @classmethod
    def empty(cls) -> "IngestTally":
        """A report that produced nothing: a failure, or an ignored repeat."""
        return cls(0, 0, 0, 0, 0, 0, 0)


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
            # Its first run lands within minutes, so the user sees it work
            # rather than waiting up to a day for the first random slot.
            next_run_at=first_run_slot(_utcnow()),
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

            # Resuming runs promptly rather than waiting out whatever slot was
            # left over from before the pause. Pausing leaves the slot alone:
            # status already keeps it out of the dispatcher's query.
            if payload.status == MonitorStatus.RUNNING.value:
                monitor.next_run_at = first_run_slot(_utcnow())

        await self.commit()

        log.info("radar_monitor.updated", monitor_id=monitor.id, user_id=user_id)

        return monitor

    async def delete(self, monitor_id: str, user_id: str) -> None:
        monitor = await self.get(monitor_id, user_id)
        await self.db.delete(monitor)
        await self.commit()

        log.info("radar_monitor.deleted", monitor_id=monitor_id, user_id=user_id)

    # --- Dispatch -----------------------------------------------------------

    async def dispatch_due(
        self,
        *,
        now: datetime | None = None,
        limit: int | None = None,
        spacing: float | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> list[DueMonitor]:
        """Send every monitor whose slot has come round to n8n, one POST each.

        Called by the Celery beat task every few minutes. The schedule itself
        is `next_run_at`: each monitor carries the exact random second it is
        next meant to wake, so this only has to ask "whose time is it?".

        Claiming happens before the POST, under a conditional UPDATE, and is
        committed on its own. Two things follow. If two ticks ever overlap,
        exactly one of them takes each monitor. And if the process dies
        between the claim and the POST, the monitor is not dispatched twice
        -- it is simply skipped until its next slot, which is the cheaper
        mistake: a missed day costs nothing, a doubled one bills twice.

        `spacing` seconds are slept between POSTs, and at most `limit`
        monitors go per call, so a large account's monitors reach n8n as a
        steady trickle rather than a spike.
        """
        if not settings.N8N_MONITOR_WEBHOOK_URL:
            log.warning("radar_monitor.dispatch_disabled", reason="no webhook url")

            return []

        now = now or _utcnow()
        limit = limit if limit is not None else settings.MONITOR_DISPATCH_BATCH
        spacing = (
            spacing if spacing is not None else settings.MONITOR_DISPATCH_SPACING_SECONDS
        )

        due = await self.monitors.due(now, limit=limit)
        dispatched: list[DueMonitor] = []

        for index, monitor in enumerate(due):
            if index and spacing > 0:
                await asyncio.sleep(spacing)

            payload = await self._claim(monitor, now)
            if payload is None:
                continue

            try:
                await self._post(payload, client)
            except UpstreamError as exc:
                # Give it another go soon rather than losing the whole day to
                # a blip. The run opened for it is discarded, not failed: n8n
                # never started anything, and a stream of "failed" searches
                # the user did not start -- one per retry while n8n is down --
                # would only be noise. The reason stays on the monitor.
                monitor.last_error = str(exc)[:500]
                monitor.next_run_at = now + timedelta(
                    minutes=settings.MONITOR_RETRY_MINUTES
                )
                unsent = await self.db.get(LeadSearchRun, payload.run_id)
                if unsent is not None:
                    await self.db.delete(unsent)
                await self.commit()

                log.warning(
                    "radar_monitor.dispatch_failed",
                    monitor_id=monitor.id,
                    error=monitor.last_error,
                )
                continue

            monitor.last_error = None
            await self.commit()
            dispatched.append(payload)

            log.info(
                "radar_monitor.dispatched",
                monitor_id=monitor.id,
                run_id=payload.run_id,
                next_run_at=monitor.next_run_at,
            )

        return dispatched

    async def _claim(self, monitor: RadarMonitor, now: datetime) -> DueMonitor | None:
        """Take the monitor for this run and open its search, or None if
        another tick got there first. Commits, so the claim survives whatever
        happens to the POST that follows."""
        slot = next_slot(now, monitor.frequency)

        if not await self.monitors.claim(monitor, now, slot):
            log.info("radar_monitor.claim_lost", monitor_id=monitor.id)

            return None

        # The ORM object still holds the pre-claim values; line it up with the
        # row so later writes in this session do not undo the claim.
        monitor.last_run_at = now
        monitor.next_run_at = slot

        if monitor.started_at is None:
            monitor.started_at = now

        filters = MonitorFilters(**monitor.filters)

        # An HS-code monitor searches the product wording, not the digits:
        # almost nobody publishes an HS code on their site, so searching the
        # code returns tariff pages instead of buyers.
        is_hs = monitor.search_type == MonitorSearchType.HS_CODE.value
        search_term = (
            monitor.search_label or monitor.search_value
            if is_hs
            else monitor.search_value
        )

        # Resolve the catalogue codes here, so the workflow filters on the real
        # Snov.io titles and employee bounds without carrying a copy of the
        # catalogue itself.
        role = filters.contact_role
        size = filters.company_size
        titles = role_titles_for(role) if role else None
        bounds = size_bounds_for(size) if size else None

        # Open a run for this dispatch, so the monitor's work shows up in Your
        # Searches alongside the user's own searches rather than happening
        # invisibly in the background.
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
        await self.commit()

        base = settings.PUBLIC_API_URL.rstrip("/")

        return DueMonitor(
            monitor_id=monitor.id,
            run_id=run.id,
            user_id=monitor.user_id,
            search_type=monitor.search_type,
            search_term=search_term,
            original_keyword=search_term,
            # The pipeline's first node splits on this list, one Serper query
            # per entry. A monitor runs exactly one.
            expanded_keywords=[search_term],
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
            # The workflow asks Serper for limit_per_run results per page, and
            # the offset advances by that same amount after each run, so the
            # two stay in step: offset 100 at 50 a page is page 3. Dividing by
            # the current limit also keeps it right if the user changes the
            # limit mid-life.
            serper_page=(monitor.serper_offset // monitor.limit_per_run) + 1,
            results_url=f"{base}{settings.API_V1_PREFIX}/lead-radar/monitors/results",
            progress_token=monitor.callback_token,
        )

    async def _post(self, payload: DueMonitor, client: httpx.AsyncClient | None) -> None:
        """Hand one monitor to the n8n workflow. Raises UpstreamError when n8n
        could not be reached or refused the job."""
        url = settings.N8N_MONITOR_WEBHOOK_URL
        body = payload.model_dump(mode="json")

        headers: dict[str, str] = {}
        if settings.N8N_WEBHOOK_SECRET:
            headers[settings.N8N_WEBHOOK_HEADER] = settings.N8N_WEBHOOK_SECRET

        try:
            if client is not None:
                response = await client.post(url, json=body, headers=headers)
            else:
                async with httpx.AsyncClient(timeout=settings.N8N_TIMEOUT) as owned:
                    response = await owned.post(url, json=body, headers=headers)
        except httpx.TimeoutException as exc:
            raise UpstreamError("The monitor workflow took too long to respond.") from exc
        except httpx.HTTPError as exc:
            raise UpstreamError("Could not reach the monitor workflow.") from exc

        if response.status_code == 404 and "not registered" in response.text:
            # n8n answers this when the workflow is not active: the production
            # webhook path only exists once it has been switched on.
            raise UpstreamError(
                "The monitor workflow is not active in n8n; activate it to "
                "register its webhook."
            )

        if response.status_code >= 400:
            raise UpstreamError(
                f"The monitor workflow returned HTTP {response.status_code}."
            )

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
        run = await self._run_for(monitor, payload.run_id)

        # Whether this is the run's first report. n8n can post more than once
        # for one execution -- a node that runs twice fires the results node
        # twice -- and the offset must move exactly once per run, or the
        # monitor skips a page of results it never read. Keyed on
        # results_received_at rather than status, so a slow run the stale
        # sweeper already failed still counts its late results as the first.
        first_report = run is None or run.results_received_at is None

        if payload.status == "failed":
            if not first_report:
                # The run already reported results; a later failure from the
                # same execution changes nothing.
                return self._response(monitor, IngestTally.empty(), 0, 0)

            return await self._fail(monitor, payload.error, run)

        if not first_report and not payload.companies:
            # A repeat with nothing in it: the spurious second post. No-op.
            log.info("radar_monitor.repeat_report_ignored", monitor_id=monitor.id)

            return self._response(monitor, IngestTally.empty(), 0, 0)

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
        run_id = run.id if run is not None else None

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

            company, created = await self._upsert_company(monitor, incoming, now, run_id)
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
        # Only a completed run advances it -- a failed one would skip a page
        # of results nobody ever looked at -- and only on the run's first
        # report, so a repeat post from the same execution cannot skip one
        # either.
        if first_report:
            monitor.serper_offset += monitor.limit_per_run
            monitor.last_completed_at = now

        # New contacts count whichever report brought them: the dedupe above
        # has already made sure none of them is counted or billed twice.
        monitor.total_leads_generated += contacts_added

        # Close the run this dispatch opened, so Your Searches shows what the
        # monitor did: what it found, what was new, and what it skipped as
        # already-owned.
        if run is not None:
            self._close_run(
                run,
                tally,
                charged=charged,
                shortfall=shortfall,
                at=now,
                first=first_report,
            )
        monitor.last_error = None

        await self.commit()

        log.info(
            "radar_monitor.ingested",
            monitor_id=monitor.id,
            user_id=monitor.user_id,
            run_id=run_id,
            first_report=first_report,
            companies_received=tally.companies_received,
            contacts_added=contacts_added,
            duplicates_skipped=duplicates_skipped,
            capped_out=capped_out,
            charged=charged,
            shortfall=shortfall,
            serper_offset=monitor.serper_offset,
        )

        return self._response(monitor, tally, charged, shortfall)

    @staticmethod
    def _response(
        monitor: RadarMonitor, tally: IngestTally, charged: int, shortfall: int
    ) -> MonitorResultsResponse:
        return MonitorResultsResponse(
            monitor_id=monitor.id,
            companies_received=tally.companies_received,
            companies_saved=tally.companies_saved,
            companies_touched=tally.companies_touched,
            contacts_added=tally.contacts_added,
            duplicates_skipped=tally.duplicates_skipped,
            credits_charged=charged,
            credits_shortfall=shortfall,
            total_leads_generated=monitor.total_leads_generated,
            serper_offset=monitor.serper_offset,
        )

    async def _run_for(
        self, monitor: RadarMonitor, run_id: str | None
    ) -> LeadSearchRun | None:
        """The run a results post belongs to.

        By the id the dispatcher sent, whatever its status: a slow run the
        stale sweeper failed must still be found when its results arrive. A
        payload without an id falls back to the monitor's open run.
        """
        if run_id:
            result = await self.db.execute(
                select(LeadSearchRun).where(
                    LeadSearchRun.id == run_id,
                    LeadSearchRun.monitor_id == monitor.id,
                )
            )
            found = result.scalar_one_or_none()
            if found is not None:
                return found

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

    @staticmethod
    def _close_run(
        run: LeadSearchRun,
        tally: IngestTally,
        *,
        charged: int,
        shortfall: int,
        at: datetime,
        first: bool,
    ) -> None:
        """Mark the run complete with what it produced.

        A first report sets the totals; a repeat adds to them, since whatever
        it brought was genuinely new (the dedupe saw to that).
        """
        run.status = RunStatus.COMPLETED.value
        run.stage = SearchStage.COMPLETED.value
        # A sweeper that closed it as stale was wrong: results did arrive.
        run.error = None

        found = tally.contacts_added + tally.duplicates_skipped

        if first:
            # Every company this run put leads into, not just the ones it
            # created: the results list shows all of them, so counting only
            # new rows would contradict what the user is looking at.
            run.companies_found = tally.companies_touched
            run.contacts_found = found
            run.contacts_added = tally.contacts_added
            run.contacts_duplicate = tally.duplicates_skipped
            run.credits_charged = charged
            run.credits_shortfall = shortfall
            run.credits_charged_at = at
            run.finished_at = at
            run.results_received_at = at
        else:
            run.companies_found += tally.companies_touched
            run.contacts_found += found
            run.contacts_added += tally.contacts_added
            run.contacts_duplicate += tally.duplicates_skipped
            run.credits_charged += charged
            run.credits_shortfall += shortfall

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
        self, monitor: RadarMonitor, error: str | None, run: LeadSearchRun | None
    ) -> MonitorResultsResponse:
        """Record a failed run without billing or advancing the offset."""
        monitor.last_error = (error or "Monitor run failed.")[:500]

        if run is not None:
            run.status = RunStatus.FAILED.value
            run.error = monitor.last_error
            run.finished_at = _utcnow()

        await self.commit()

        log.warning(
            "radar_monitor.run_failed", monitor_id=monitor.id, error=monitor.last_error
        )

        return self._response(monitor, IngestTally.empty(), 0, 0)
