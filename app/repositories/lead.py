"""Data access for companies and their decision makers."""

from datetime import datetime

from sqlalchemy import Select, func, or_, select
from sqlalchemy.sql.elements import ColumnElement

from app.models.lead import Company, DecisionMaker, LeadStatus
from app.repositories.base import BaseRepository

# More words than this adds nothing a user would notice, and each one is
# another pass over the contacts table.
MAX_SEARCH_TERMS = 6

# Everything a user can see on a lead's row or in its expanded contacts, so
# whatever they remember about a lead is something they can search by.
_COMPANY_SEARCH_COLUMNS = (
    Company.company_name,
    Company.website,
    Company.location,
    Company.industry,
    Company.hq_phone,
    Company.notes,
)
_CONTACT_SEARCH_COLUMNS = (
    DecisionMaker.full_name,
    DecisionMaker.job_title,
    DecisionMaker.verified_email,
    DecisionMaker.phone_number,
)


class CompanyRepository(BaseRepository[Company]):
    model = Company

    # --- Ownership ----------------------------------------------------------
    async def get_owned(self, user_id: str, company_id: str) -> Company | None:
        """Scoped by user, so one account can never touch another's rows."""
        return await self.find_one_by(id=company_id, user_id=user_id)

    async def list_owned_by_ids(self, user_id: str, ids: list[str]) -> list[Company]:
        """Only the ids that belong to this user come back; the rest are
        silently dropped rather than erroring, so a forged id reveals nothing."""
        if not ids:
            return []

        stmt = select(Company).where(Company.user_id == user_id, Company.id.in_(ids))
        result = await self.db.execute(stmt)

        return list(result.scalars().all())

    # --- My Leads -----------------------------------------------------------
    @staticmethod
    def _search_clauses(search: str | None) -> list[ColumnElement[bool]]:
        """What "this lead matches the search box" means, as SQL.

        The text is split into words and EVERY word has to match somewhere --
        in the company's own fields or in any of its contacts -- so "acme
        berlin" finds Acme in Berlin, and "jane acme" finds the Acme lead that
        has a Jane among its contacts. Matching is a case-insensitive
        "contains".

        The contacts are reached with EXISTS rather than a JOIN: a join would
        return a company once per matching contact, which breaks both the page
        size and the total.
        """
        clauses: list[ColumnElement[bool]] = []

        for term in (search or "").split()[:MAX_SEARCH_TERMS]:
            # % and _ are LIKE wildcards; a user typing "50%" means the text.
            escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"

            in_contacts = (
                select(DecisionMaker.id)
                .where(
                    DecisionMaker.company_id == Company.id,
                    or_(
                        *(
                            column.ilike(pattern, escape="\\")
                            for column in _CONTACT_SEARCH_COLUMNS
                        )
                    ),
                )
                .exists()
            )

            clauses.append(
                or_(
                    *(
                        column.ilike(pattern, escape="\\")
                        for column in _COMPANY_SEARCH_COLUMNS
                    ),
                    in_contacts,
                )
            )

        return clauses

    def _leads_query(
        self, user_id: str, status: str | None, search: str | None = None
    ) -> Select[tuple[Company]]:
        stmt = select(Company).where(
            Company.user_id == user_id, Company.added_to_leads_at.is_not(None)
        )
        if status:
            stmt = stmt.where(Company.status == status)

        for clause in self._search_clauses(search):
            stmt = stmt.where(clause)

        return stmt

    async def list_leads(
        self,
        user_id: str,
        *,
        status: str | None = None,
        search: str | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> list[Company]:
        """Most recently added first. `search` runs in the database, over every
        lead the user has, before the page is cut -- not over one page."""
        stmt = (
            self._leads_query(user_id, status, search)
            .order_by(Company.added_to_leads_at.desc(), Company.id)
            .offset(offset)
            .limit(limit)
        )
        result = await self.db.execute(stmt)

        return list(result.scalars().all())

    async def list_leads_for_export(
        self,
        user_id: str,
        *,
        status: str | None = None,
        ids: list[str] | None = None,
        search: str | None = None,
    ) -> list[Company]:
        """Every matching lead, newest-added first, with no page limit.

        `ids` narrows to a hand-picked set. It is still scoped to the user's
        own leads, so an id from another account simply does not appear
        rather than leaking.
        """
        stmt = self._leads_query(user_id, status, search)
        if ids:
            stmt = stmt.where(Company.id.in_(ids))

        stmt = stmt.order_by(Company.added_to_leads_at.desc(), Company.id)
        result = await self.db.execute(stmt)

        return list(result.scalars().unique().all())

    async def count_leads(
        self, user_id: str, *, status: str | None = None, search: str | None = None
    ) -> int:
        stmt = select(func.count()).select_from(
            self._leads_query(user_id, status, search).subquery()
        )
        result = await self.db.execute(stmt)

        return result.scalar_one()

    async def count_added_since(self, user_id: str, since: datetime) -> int:
        """Leads added to My Leads at or after `since` (the dashboard's "today")."""
        stmt = (
            select(func.count())
            .select_from(Company)
            .where(Company.user_id == user_id, Company.added_to_leads_at >= since)
        )
        result = await self.db.execute(stmt)

        return result.scalar_one()

    async def counts_by_status(
        self, user_id: str, *, search: str | None = None
    ) -> dict[str, int]:
        """One number per My Leads tab, in a single query.

        With a search, the numbers are how many matches sit in each tab, so the
        tabs tell the user where their results are instead of contradicting
        the list underneath.
        """
        stmt = select(Company.status, func.count()).where(
            Company.user_id == user_id, Company.added_to_leads_at.is_not(None)
        )
        for clause in self._search_clauses(search):
            stmt = stmt.where(clause)

        stmt = stmt.group_by(Company.status)
        result = await self.db.execute(stmt)

        counts = {status.value: 0 for status in LeadStatus}
        for status, count in result.all():
            counts[status] = count
        counts["all"] = sum(counts[s.value] for s in LeadStatus)

        return counts

    async def list_for_user(
        self, user_id: str, *, offset: int = 0, limit: int = 20
    ) -> list[Company]:
        """Newest first. Decision makers come along via the selectin relationship."""
        stmt = (
            select(Company)
            .where(Company.user_id == user_id)
            .order_by(Company.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        result = await self.db.execute(stmt)

        return list(result.scalars().all())

    async def list_for_run(self, run_id: str) -> list[Company]:
        stmt = (
            select(Company)
            .where(Company.run_id == run_id)
            .order_by(Company.created_at.asc())
        )
        result = await self.db.execute(stmt)

        return list(result.scalars().all())

    async def count_for_run(self, run_id: str) -> int:
        stmt = select(func.count()).select_from(Company).where(Company.run_id == run_id)
        result = await self.db.execute(stmt)

        return result.scalar_one()

    async def find_existing(
        self, user_id: str, website: str | None, company_name: str
    ) -> Company | None:
        """Look for the same company already saved for this user.

        Matched on website when there is one (the reliable key), falling back to
        an exact name match. Used to make the results callback idempotent, so an
        n8n retry does not duplicate every company.
        """
        stmt = select(Company).where(Company.user_id == user_id)

        if website:
            stmt = stmt.where(Company.website == website)
        else:
            stmt = stmt.where(
                Company.company_name == company_name, Company.website.is_(None)
            )

        result = await self.db.execute(stmt.limit(1))

        return result.scalar_one_or_none()


class DecisionMakerRepository(BaseRepository[DecisionMaker]):
    model = DecisionMaker

    async def get_for_company(
        self, company_id: str, contact_id: str
    ) -> DecisionMaker | None:
        return await self.find_one_by(id=contact_id, company_id=company_id)

    async def count_found_since(self, user_id: str, since: datetime) -> int:
        """Decision makers discovered for this user at or after `since`."""
        stmt = (
            select(func.count())
            .select_from(DecisionMaker)
            .join(Company, Company.id == DecisionMaker.company_id)
            .where(Company.user_id == user_id, DecisionMaker.created_at >= since)
        )
        result = await self.db.execute(stmt)

        return result.scalar_one()

    async def count_for_run(self, run_id: str) -> int:
        stmt = (
            select(func.count())
            .select_from(DecisionMaker)
            .join(Company, Company.id == DecisionMaker.company_id)
            .where(Company.run_id == run_id)
        )
        result = await self.db.execute(stmt)

        return result.scalar_one()

    async def count_whatsapp_checked_for_run(self, run_id: str) -> int:
        """Contacts the workflow actually ran a WhatsApp check on.

        A null status means no check happened -- no number to check, or
        validation was off -- so those rows are not counted and not charged.
        "Not Active" is a check that was made and is billed like any other.
        """
        stmt = (
            select(func.count())
            .select_from(DecisionMaker)
            .join(Company, Company.id == DecisionMaker.company_id)
            .where(Company.run_id == run_id, DecisionMaker.whatsapp_status.is_not(None))
        )
        result = await self.db.execute(stmt)

        return result.scalar_one()
