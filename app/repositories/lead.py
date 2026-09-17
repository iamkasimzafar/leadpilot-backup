"""Data access for companies and their decision makers."""

from sqlalchemy import Select, func, select

from app.models.lead import Company, DecisionMaker, LeadStatus
from app.repositories.base import BaseRepository


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
    def _leads_query(self, user_id: str, status: str | None) -> Select[tuple[Company]]:
        stmt = select(Company).where(
            Company.user_id == user_id, Company.added_to_leads_at.is_not(None)
        )
        if status:
            stmt = stmt.where(Company.status == status)

        return stmt

    async def list_leads(
        self, user_id: str, *, status: str | None = None, offset: int = 0, limit: int = 20
    ) -> list[Company]:
        """Most recently added first."""
        stmt = (
            self._leads_query(user_id, status)
            .order_by(Company.added_to_leads_at.desc())
            .offset(offset)
            .limit(limit)
        )
        result = await self.db.execute(stmt)

        return list(result.scalars().all())

    async def count_leads(self, user_id: str, *, status: str | None = None) -> int:
        stmt = select(func.count()).select_from(
            self._leads_query(user_id, status).subquery()
        )
        result = await self.db.execute(stmt)

        return result.scalar_one()

    async def counts_by_status(self, user_id: str) -> dict[str, int]:
        """One number per My Leads tab, in a single query."""
        stmt = (
            select(Company.status, func.count())
            .where(Company.user_id == user_id, Company.added_to_leads_at.is_not(None))
            .group_by(Company.status)
        )
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

    async def count_for_run(self, run_id: str) -> int:
        stmt = (
            select(func.count())
            .select_from(DecisionMaker)
            .join(Company, Company.id == DecisionMaker.company_id)
            .where(Company.run_id == run_id)
        )
        result = await self.db.execute(stmt)

        return result.scalar_one()
