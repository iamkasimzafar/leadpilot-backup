"""My Leads: adding search results to the list, and managing them after.

A lead is a Company row with `added_to_leads_at` set. Everything here is
scoped to the signed-in user: a company id that belongs to someone else is
treated as if it did not exist.
"""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.models.lead import Company, DecisionMaker, LeadStatus
from app.repositories.lead import CompanyRepository, DecisionMakerRepository
from app.repositories.lead_search import LeadSearchRunRepository
from app.schemas.lead import (
    AddToLeadsResponse,
    DecisionMakerUpdate,
    LeadCounts,
    LeadUpdate,
)
from app.services.base import BaseService

log = get_logger(__name__)


class LeadService(BaseService):
    def __init__(self, db: AsyncSession) -> None:
        super().__init__(db)
        self.companies = CompanyRepository(db)
        self.contacts = DecisionMakerRepository(db)
        self.runs = LeadSearchRunRepository(db)

    # --- Reads --------------------------------------------------------------
    async def list_leads(
        self,
        user_id: str,
        *,
        status: str | None,
        offset: int,
        limit: int,
        search: str | None = None,
    ) -> tuple[list[Company], int, LeadCounts]:
        """One page, plus the total and the per-tab counts for the same search,
        so the three can never disagree."""
        search = " ".join((search or "").split()) or None

        items = await self.companies.list_leads(
            user_id, status=status, search=search, offset=offset, limit=limit
        )
        total = await self.companies.count_leads(user_id, status=status, search=search)
        counts = await self.counts(user_id, search=search)

        return items, total, counts

    async def counts(self, user_id: str, *, search: str | None = None) -> LeadCounts:
        return LeadCounts(**await self.companies.counts_by_status(user_id, search=search))

    async def get(self, user_id: str, lead_id: str) -> Company:
        company = await self.companies.get_owned(user_id, lead_id)
        if company is None:
            raise NotFoundError("No lead with that id.")

        return company

    # --- Adding -------------------------------------------------------------
    async def add(
        self, user_id: str, *, company_ids: list[str], run_id: str | None
    ) -> AddToLeadsResponse:
        """Put companies into My Leads. Already-added ones are left alone, so
        clicking "add all" twice is harmless."""
        candidates: list[Company] = []

        if run_id:
            run = await self.runs.get_for_user(user_id, run_id)
            if run is None:
                raise NotFoundError("No search run with that id.")
            candidates.extend(await self.companies.list_for_run(run.id))

        if company_ids:
            owned = await self.companies.list_owned_by_ids(user_id, company_ids)
            candidates.extend(owned)

        added = 0
        already = 0
        seen: set[str] = set()
        now = datetime.now(UTC)

        for company in candidates:
            if company.id in seen:
                continue
            seen.add(company.id)

            if company.in_leads:
                already += 1
                continue

            company.added_to_leads_at = now
            company.status = LeadStatus.NEW.value
            added += 1

        await self.commit()

        total = await self.companies.count_leads(user_id)
        log.info("leads.added", user_id=user_id, added=added, already=already)

        return AddToLeadsResponse(
            added=added, already_in_leads=already, total_in_leads=total
        )

    @staticmethod
    def mark_added(company: Company, at: datetime) -> None:
        """Flag a company as a lead inside someone else's transaction (results
        ingest with auto-add on). Does not commit."""
        if not company.in_leads:
            company.added_to_leads_at = at
            company.status = LeadStatus.NEW.value

    # --- Editing ------------------------------------------------------------
    async def update(self, user_id: str, lead_id: str, changes: LeadUpdate) -> Company:
        company = await self.get(user_id, lead_id)

        for field, value in changes.model_dump(exclude_unset=True).items():
            setattr(company, field, value)

        await self.commit()

        return company

    async def set_status(self, user_id: str, ids: list[str], status: str) -> int:
        companies = await self.companies.list_owned_by_ids(user_id, ids)
        for company in companies:
            company.status = status

        await self.commit()
        log.info("leads.status", user_id=user_id, status=status, count=len(companies))

        return len(companies)

    async def delete(self, user_id: str, ids: list[str]) -> int:
        """Remove leads entirely. Their decision makers go with them (cascade)."""
        companies = await self.companies.list_owned_by_ids(user_id, ids)
        for company in companies:
            await self.db.delete(company)

        await self.commit()
        log.info("leads.deleted", user_id=user_id, count=len(companies))

        return len(companies)

    # --- Contacts -----------------------------------------------------------
    async def update_contact(
        self, user_id: str, lead_id: str, contact_id: str, changes: DecisionMakerUpdate
    ) -> DecisionMaker:
        company = await self.get(user_id, lead_id)
        contact = await self.contacts.get_for_company(company.id, contact_id)
        if contact is None:
            raise NotFoundError("No contact with that id on this lead.")

        for field, value in changes.model_dump(exclude_unset=True).items():
            setattr(contact, field, value)

        await self.commit()
        # Keep the parent's loaded collection truthful for any later read.
        await self.db.refresh(company, attribute_names=["decision_makers"])

        return contact

    async def delete_contact(self, user_id: str, lead_id: str, contact_id: str) -> None:
        company = await self.get(user_id, lead_id)
        contact = await self.contacts.get_for_company(company.id, contact_id)
        if contact is None:
            raise NotFoundError("No contact with that id on this lead.")

        await self.db.delete(contact)
        await self.commit()
        # The row is gone; the company's in-memory list must not still show it.
        await self.db.refresh(company, attribute_names=["decision_makers"])


__all__ = ["LeadService"]
