"""Aggregate queries behind the Reports page.

These are read-only rollups over rows the user already owns. They live apart
from the per-entity repositories because they cut across companies, contacts,
runs and the credit ledger, and because every one of them is grouped or
filtered by a time window.

Daily buckets are built in Python rather than with a SQL date function. The
bucket has to be the user's local calendar day, and the two databases in play
spell that differently (MySQL `DATE(CONVERT_TZ(...))` vs SQLite
`date(..., '+N minutes')`). Fetching timestamps and bucketing them here keeps
one implementation that behaves identically on both, and the row counts
involved are per-user and window-bounded, so there is nothing to gain from
pushing it into the engine.
"""

from datetime import datetime

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing import CreditTransaction
from app.models.lead import Company, DecisionMaker
from app.models.lead_search import LeadSearchRun, RunStatus


class ReportsRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # --- Timestamps for the daily trend -------------------------------------
    async def lead_added_times(
        self, user_id: str, start: datetime, end: datetime
    ) -> list[datetime]:
        """When each company became a lead, within the window."""
        stmt = select(Company.added_to_leads_at).where(
            Company.user_id == user_id,
            Company.added_to_leads_at.is_not(None),
            Company.added_to_leads_at >= start,
            Company.added_to_leads_at < end,
        )
        result = await self.db.execute(stmt)

        return [row for row in result.scalars().all() if row is not None]

    async def contact_found_times(
        self, user_id: str, start: datetime, end: datetime
    ) -> list[datetime]:
        stmt = (
            select(DecisionMaker.created_at)
            .join(Company, Company.id == DecisionMaker.company_id)
            .where(
                Company.user_id == user_id,
                DecisionMaker.created_at >= start,
                DecisionMaker.created_at < end,
            )
        )
        result = await self.db.execute(stmt)

        return list(result.scalars().all())

    async def search_started_times(
        self, user_id: str, start: datetime, end: datetime
    ) -> list[datetime]:
        stmt = select(LeadSearchRun.created_at).where(
            LeadSearchRun.user_id == user_id,
            LeadSearchRun.created_at >= start,
            LeadSearchRun.created_at < end,
        )
        result = await self.db.execute(stmt)

        return list(result.scalars().all())

    async def spend_entries(
        self, user_id: str, start: datetime, end: datetime
    ) -> list[tuple[datetime, int, str]]:
        """Every credit-spending ledger row in the window: (when, amount, kind).

        Only negative amounts, because this is what was *spent*; a top-up or a
        plan grant is money in, and mixing the two would make the spend line
        meaningless. Amounts come back positive for display.
        """
        stmt = select(
            CreditTransaction.created_at,
            CreditTransaction.amount,
            CreditTransaction.kind,
        ).where(
            CreditTransaction.user_id == user_id,
            CreditTransaction.amount < 0,
            CreditTransaction.created_at >= start,
            CreditTransaction.created_at < end,
        )
        result = await self.db.execute(stmt)

        return [(when, -amount, kind) for when, amount, kind in result.all()]

    # --- Contact quality ----------------------------------------------------
    async def contact_quality(
        self, user_id: str, start: datetime, end: datetime
    ) -> tuple[int, int, int, int]:
        """(found, with_email, with_phone, on_whatsapp) over the window.

        One pass, so the four numbers can never disagree with each other.
        "On WhatsApp" counts only a positive check: a null status means no
        check was made, and a negative one means the number is not on it.
        """
        stmt = (
            select(
                func.count(),
                func.sum(case((DecisionMaker.verified_email.is_not(None), 1), else_=0)),
                func.sum(case((DecisionMaker.phone_number.is_not(None), 1), else_=0)),
                # Results are normalised to "Active" on the way in (see
                # normalise_whatsapp_status). "valid" is accepted too, for
                # rows stored before that existed.
                func.sum(
                    case(
                        (
                            func.lower(
                                func.coalesce(DecisionMaker.whatsapp_status, "")
                            ).in_(["active", "valid"]),
                            1,
                        ),
                        else_=0,
                    )
                ),
            )
            .select_from(DecisionMaker)
            .join(Company, Company.id == DecisionMaker.company_id)
            .where(
                Company.user_id == user_id,
                DecisionMaker.created_at >= start,
                DecisionMaker.created_at < end,
            )
        )
        result = await self.db.execute(stmt)
        found, email, phone, whatsapp = result.one()

        return int(found or 0), int(email or 0), int(phone or 0), int(whatsapp or 0)

    # --- Funnel -------------------------------------------------------------
    async def companies_found(self, user_id: str, start: datetime, end: datetime) -> int:
        """Every company the workflow returned in the window, whether or not it
        was ever added to My Leads. The top of the funnel."""
        stmt = (
            select(func.count())
            .select_from(Company)
            .where(
                Company.user_id == user_id,
                Company.created_at >= start,
                Company.created_at < end,
            )
        )
        result = await self.db.execute(stmt)

        return int(result.scalar_one())

    async def lead_status_counts(
        self, user_id: str, start: datetime, end: datetime
    ) -> dict[str, int]:
        """Leads added in the window, grouped by their CURRENT status.

        Note the two different clocks: membership is decided by when the lead
        was added, but the status is today's. A lead added last week and
        contacted this morning counts as contacted.
        """
        stmt = (
            select(Company.status, func.count())
            .where(
                Company.user_id == user_id,
                Company.added_to_leads_at.is_not(None),
                Company.added_to_leads_at >= start,
                Company.added_to_leads_at < end,
            )
            .group_by(Company.status)
        )
        result = await self.db.execute(stmt)

        return {status: int(count) for status, count in result.all()}

    # --- Breakdowns ---------------------------------------------------------
    async def top_locations(
        self, user_id: str, start: datetime, end: datetime, *, limit: int = 8
    ) -> list[tuple[str, int]]:
        """Companies per location, biggest first. Rows without a location are
        skipped rather than bucketed as "Unknown": a blank is missing data, not
        a place."""
        stmt = (
            select(Company.location, func.count())
            .where(
                Company.user_id == user_id,
                Company.location.is_not(None),
                Company.location != "",
                Company.created_at >= start,
                Company.created_at < end,
            )
            .group_by(Company.location)
            .order_by(func.count().desc())
            # Over-fetch: locations are free text ("Hamburg, Germany") and get
            # folded to their country in the service, which can merge rows.
            .limit(limit * 6)
        )
        result = await self.db.execute(stmt)

        return [(location, int(count)) for location, count in result.all()]

    async def top_keywords(
        self, user_id: str, start: datetime, end: datetime, *, limit: int = 8
    ) -> list[tuple[str, int]]:
        """Which searches actually produced companies, best first.

        Counted from the companies each run returned rather than from the run
        rows, so a keyword searched twice for nothing does not outrank one that
        searched once and found thirty.
        """
        stmt = (
            select(LeadSearchRun.original_keyword, func.count(Company.id))
            .join(Company, Company.run_id == LeadSearchRun.id)
            .where(
                LeadSearchRun.user_id == user_id,
                LeadSearchRun.created_at >= start,
                LeadSearchRun.created_at < end,
            )
            .group_by(LeadSearchRun.original_keyword)
            .order_by(func.count(Company.id).desc())
            .limit(limit)
        )
        result = await self.db.execute(stmt)

        return [(keyword, int(count)) for keyword, count in result.all()]

    # --- Search reliability -------------------------------------------------
    async def run_status_counts(
        self, user_id: str, start: datetime, end: datetime
    ) -> dict[str, int]:
        stmt = (
            select(LeadSearchRun.status, func.count())
            .where(
                LeadSearchRun.user_id == user_id,
                LeadSearchRun.created_at >= start,
                LeadSearchRun.created_at < end,
            )
            .group_by(LeadSearchRun.status)
        )
        result = await self.db.execute(stmt)

        counts = {status.value: 0 for status in RunStatus}
        for status, count in result.all():
            counts[status] = int(count)

        return counts
