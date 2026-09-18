"""Ingests the finished results a lead-search workflow posts back.

Writing results also closes the run: this is the last thing n8n sends, so it
doubles as the completion signal. Safe to call twice -- a company already saved
for this user is updated rather than duplicated, which matters because the
workflow's HTTP node may retry.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.lead import Company, DecisionMaker
from app.models.lead_search import LeadSearchRun, RunStatus, SearchStage
from app.models.notification import NotificationKind
from app.repositories.lead import CompanyRepository, DecisionMakerRepository
from app.repositories.lead_search import LeadSearchEventRepository
from app.schemas.lead import CompanyIn, SearchResultsRequest, SearchResultsResponse
from app.services.base import BaseService
from app.services.billing import BillingService
from app.services.leads import LeadService
from app.services.notification import NotificationService
from app.services.progress_stream import progress_stream
from app.services.search_pricing import cost_lines, total_credits

log = get_logger(__name__)

# Keys that have their own column; everything else goes to extra_json.
_COMPANY_KNOWN = {
    "company_name",
    "website",
    "location",
    "industry",
    "company_size",
    "hq_phone",
    "decision_makers",
}
_CONTACT_KNOWN = {
    "full_name",
    "job_title",
    "verified_email",
    "email_status",
    "linkedin_url",
    "phone_number",
    "whatsapp_status",
}


# MySQL's "Deadlock found when trying to get lock; try restarting transaction".
# The engine has already rolled the losing transaction back; the documented
# response is to retry it.
MYSQL_DEADLOCK = 1213


def is_deadlock(exc: BaseException) -> bool:
    """True for a MySQL deadlock, however the driver wrapped it."""
    if not isinstance(exc, OperationalError):
        return False

    orig = getattr(exc, "orig", None)
    args: tuple[Any, ...] = tuple(getattr(orig, "args", ()))

    return bool(args) and args[0] == MYSQL_DEADLOCK


def _extras(model: Any, known: set[str]) -> str:
    """JSON of any field the workflow sent that we have no column for."""
    extra = {
        key: value for key, value in (model.model_extra or {}).items() if key not in known
    }

    return json.dumps(extra, ensure_ascii=False, default=str) if extra else "{}"


@dataclass(frozen=True)
class Settlement:
    """What a completed run was billed."""

    charged: int
    shortfall: int
    whatsapp_checks: int

    # The full bill: charged + shortfall.
    billed: int


class LeadResultsService(BaseService):
    def __init__(self, db: AsyncSession) -> None:
        super().__init__(db)
        self.companies = CompanyRepository(db)
        self.contacts = DecisionMakerRepository(db)
        self.events = LeadSearchEventRepository(db)
        self.notifications = NotificationService(db)
        self.billing = BillingService(db)

    async def ingest(
        self, run: LeadSearchRun, payload: SearchResultsRequest
    ) -> SearchResultsResponse:
        # Serialise ingests for this run. An n8n HTTP node runs once per input
        # item, so the results POST can arrive many times at once, each with
        # the full company list. Without this lock those transactions rewrite
        # the same company rows in different orders and InnoDB deadlocks. With
        # it, the second waits for the first to commit, then finds every
        # company already saved and simply refreshes it.
        #
        # SQLite ignores FOR UPDATE (it serialises writers on its own).
        locked = await self.db.execute(
            select(LeadSearchRun).where(LeadSearchRun.id == run.id).with_for_update()
        )
        run = locked.scalar_one()

        saved = 0
        updated = 0
        contacts_saved = 0
        now = datetime.now(UTC)

        for incoming in payload.companies:
            company, is_new = await self._upsert_company(run, incoming)

            if is_new:
                saved += 1
            else:
                updated += 1

            contacts_saved += await self._replace_contacts(company, incoming)

            # "Auto-add to My Leads" was ticked at dispatch: the result is a
            # lead straight away, no review step.
            if run.auto_add_to_leads:
                LeadService.mark_added(company, now)

        total = await self.companies.count_for_run(run.id)
        contact_total = await self.contacts.count_for_run(run.id)

        failed = payload.status == "failed" or payload.error is not None

        # Two separate facts, each decided by its own conditional UPDATE so
        # they hold under real concurrency (every parallel request reads the
        # same pre-commit state, so only the database can say who was first):
        #
        #  1. "results received" -- the first ingest for this run, whoever
        #     closed it. Drives the results event and the user's notification.
        #     Keyed on results_received_at so that a run the last progress
        #     checkpoint already closed still gets its results acknowledged.
        #  2. "run finished" -- flips status once, if still running. Usually
        #     the progress callback already did this seconds earlier.
        await self.db.flush()
        now = datetime.now(UTC)

        received = await self.db.execute(
            update(LeadSearchRun)
            .where(
                LeadSearchRun.id == run.id,
                LeadSearchRun.results_received_at.is_(None),
            )
            .values(
                results_received_at=now,
                companies_found=total,
                contacts_found=contact_total,
            )
        )
        first_results = received.rowcount == 1  # type: ignore[attr-defined]

        if not first_results:
            # A repeat post (n8n retry or per-item burst): refresh totals only.
            await self.db.execute(
                update(LeadSearchRun)
                .where(LeadSearchRun.id == run.id)
                .values(companies_found=total, contacts_found=contact_total)
            )

        closing: dict[str, object] = {"finished_at": now}
        if failed:
            closing["status"] = RunStatus.FAILED.value
            closing["error"] = (payload.error or "The workflow reported a failure.")[:500]
        else:
            closing["status"] = RunStatus.COMPLETED.value
            closing["stage"] = SearchStage.COMPLETED.value

        await self.db.execute(
            update(LeadSearchRun)
            .where(
                LeadSearchRun.id == run.id,
                LeadSearchRun.status == RunStatus.RUNNING.value,
            )
            .values(**closing)
        )

        await self.db.refresh(run)

        # --- Settlement ---------------------------------------------------
        # Charged once, only for a run that completed successfully, and only
        # by whichever ingest claims it: n8n can post the same results several
        # times at once, and the conditional UPDATE succeeds for exactly one
        # of them. A failed run -- or one the error trigger already closed --
        # is never billed: nothing is taken until the results are good.
        settled: Settlement | None = None
        if not failed and run.status == RunStatus.COMPLETED.value:
            claimed = await self.db.execute(
                update(LeadSearchRun)
                .where(
                    LeadSearchRun.id == run.id,
                    LeadSearchRun.credits_charged_at.is_(None),
                )
                .values(credits_charged_at=now)
            )
            if claimed.rowcount == 1:  # type: ignore[attr-defined]
                settled = await self._settle(run, companies=total)
                await self.db.refresh(run)

        notification = None
        shortfall_notice = None
        if first_results:
            # Recorded as an event too, so the run's history shows results landing.
            summary = (
                run.error
                if failed
                else f"{total:,} companies and {contact_total:,} contacts saved"
            )
            if settled is not None:
                summary = f"{summary} · {settled.charged:,} credits charged"

            await self.events.create(
                run_id=run.id,
                stage=SearchStage.COMPLETED.value,
                message=summary,
                count=total,
            )

            notification = await self._finish_notification(run, failed)

        if settled is not None and settled.shortfall > 0:
            shortfall_notice = await self._shortfall_notification(run, settled)

        await self.commit()

        progress_stream.publish(run.user_id, run.id)

        # None when the user has this notification kind switched off.
        for notice in (notification, shortfall_notice):
            if notice is not None:
                NotificationService.publish(run.user_id, notice.id)

        log.info(
            "lead_results.ingested",
            run_id=run.id,
            saved=saved,
            updated=updated,
            contacts=contacts_saved,
            charged=settled.charged if settled else None,
        )

        return SearchResultsResponse(
            run_id=run.id,
            status=run.status,
            companies_saved=saved,
            companies_updated=updated,
            contacts_saved=contacts_saved,
            total_companies=total,
        )

    # --- Writing ------------------------------------------------------------
    async def _upsert_company(
        self, run: LeadSearchRun, incoming: CompanyIn
    ) -> tuple[Company, bool]:
        """Create the company, or refresh the one already saved for this user."""
        existing = await self.companies.find_existing(
            run.user_id, incoming.website, incoming.company_name
        )

        values = {
            "company_name": incoming.company_name,
            "website": incoming.website,
            "location": incoming.location,
            "industry": incoming.industry,
            "company_size": incoming.company_size,
            "hq_phone": incoming.hq_phone,
            "extra_json": _extras(incoming, _COMPANY_KNOWN),
        }

        if existing is not None:
            return await self._refresh_company(existing, run, values), False

        # Insert inside a savepoint: if a concurrent request inserted the same
        # website a moment ago, the unique index rejects ours, the savepoint
        # rolls back just this statement, and we adopt the winner's row.
        try:
            async with self.db.begin_nested():
                company = await self.companies.create(
                    user_id=run.user_id, run_id=run.id, **values
                )
        except IntegrityError:
            log.info("lead_results.company_race_resolved", website=incoming.website)
            existing = await self.companies.find_existing(
                run.user_id, incoming.website, incoming.company_name
            )
            if existing is None:  # pragma: no cover - would need a third writer
                raise

            return await self._refresh_company(existing, run, values), False

        return company, True

    async def _refresh_company(
        self, existing: Company, run: LeadSearchRun, values: dict[str, Any]
    ) -> Company:
        """Update a company we already hold with what the workflow sent now.
        Keeps whatever we had when the new payload omits a field."""
        for key, value in values.items():
            if value not in (None, "{}"):
                setattr(existing, key, value)
        existing.run_id = run.id
        await self.db.flush()

        return existing

    async def _replace_contacts(self, company: Company, incoming: CompanyIn) -> int:
        """Set the company's decision makers to what the payload carries.

        Replacing rather than appending keeps a retry idempotent: sending the
        same company twice leaves one copy of each contact, not two.
        """
        if not incoming.decision_makers:
            return 0

        for existing in list(company.decision_makers):
            await self.db.delete(existing)
        await self.db.flush()

        created = 0
        for person in incoming.decision_makers:
            self.db.add(
                DecisionMaker(
                    company_id=company.id,
                    full_name=person.full_name,
                    job_title=person.job_title,
                    verified_email=person.verified_email,
                    email_status=person.email_status,
                    linkedin_url=person.linkedin_url,
                    phone_number=person.phone_number,
                    whatsapp_status=person.whatsapp_status,
                    extra_json=_extras(person, _CONTACT_KNOWN),
                )
            )
            created += 1

        await self.db.flush()

        # The rows were written directly, so the company's loaded collection
        # still describes the old contacts. Reload it before anyone reads it.
        await self.db.refresh(company, attribute_names=["decision_makers"])

        return created

    async def _settle(self, run: LeadSearchRun, *, companies: int) -> Settlement:
        """Bill the run from what it actually returned.

        Run fee, then companies, then WhatsApp checks, charged in that order
        until the balance runs out; whatever is left over is recorded on the
        run as a shortfall rather than refused, because the workflow has
        already done the work. Does not commit: it joins the ingest
        transaction, so the results and their charge land together or not at
        all.
        """
        # Only numbers the workflow actually checked, and only if the user
        # asked for validation. Charged whether the number was active or not.
        checks = (
            await self.contacts.count_whatsapp_checked_for_run(run.id)
            if run.validate_whatsapp
            else 0
        )

        lines = cost_lines(
            companies=companies,
            whatsapp_checks=checks,
            validate_whatsapp=run.validate_whatsapp,
        )

        charged, shortfall = await self.billing.charge_capped(
            run.user_id,
            [(line.description, line.credits) for line in lines],
            reference_type="lead_search_run",
            reference_id=run.id,
        )

        await self.db.execute(
            update(LeadSearchRun)
            .where(LeadSearchRun.id == run.id)
            .values(
                credits_charged=charged,
                credits_shortfall=shortfall,
                whatsapp_checks=checks,
            )
        )

        log.info(
            "lead_search.settled",
            run_id=run.id,
            user_id=run.user_id,
            companies=companies,
            whatsapp_checks=checks,
            charged=charged,
            shortfall=shortfall,
        )

        return Settlement(
            charged=charged,
            shortfall=shortfall,
            whatsapp_checks=checks,
            billed=total_credits(lines),
        )

    async def _shortfall_notification(self, run: LeadSearchRun, settled: Settlement):  # type: ignore[no-untyped-def]
        """Tell the user part of the bill could not be taken.

        Forced past the credits notification preference: this is money they
        owe, not news they can opt out of.
        """
        return await self.notifications.create(
            run.user_id,
            kind=NotificationKind.CREDITS,
            title=f"{settled.shortfall:,} credits could not be charged",
            subtitle=(
                f'Your search for "{run.original_keyword}" cost {settled.billed:,} '
                f"credits but your balance only covered {settled.charged:,}. "
                f"Top up to keep searching."
            ),
            link="/wallet",
            commit=False,
            force=True,
        )

    async def _finish_notification(self, run: LeadSearchRun, failed: bool):  # type: ignore[no-untyped-def]
        if failed:
            return await self.notifications.create(
                run.user_id,
                kind=NotificationKind.MONITOR,
                title=f'Search for "{run.original_keyword}" failed',
                subtitle=run.error or "The lead search workflow reported an error.",
                link="/lead-radar",
                commit=False,
            )

        if run.auto_add_to_leads:
            return await self.notifications.create(
                run.user_id,
                kind=NotificationKind.LEAD,
                title=f"{run.companies_found:,} companies added to My Leads",
                subtitle=(
                    f'Search for "{run.original_keyword}" finished with '
                    f"{run.contacts_found:,} decision makers."
                ),
                link="/my-leads",
                commit=False,
            )

        # Auto-add was off: the results are waiting to be reviewed.
        return await self.notifications.create(
            run.user_id,
            kind=NotificationKind.LEAD,
            title=f'Search for "{run.original_keyword}" finished',
            subtitle=(
                f"{run.companies_found:,} companies and {run.contacts_found:,} "
                f"decision makers found. Review them and add the ones you want."
            ),
            link="/lead-radar",
            commit=False,
        )


__all__ = ["LeadResultsService"]
