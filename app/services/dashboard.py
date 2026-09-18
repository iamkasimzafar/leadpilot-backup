"""Dashboard figures.

Everything here is a real count over the user's own rows. Nothing is
estimated or padded: if a number is not derivable from the database it is not
on the dashboard.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.lead import LeadStatus
from app.repositories.billing import WalletRepository
from app.repositories.lead import CompanyRepository, DecisionMakerRepository
from app.repositories.lead_search import LeadSearchRunRepository
from app.schemas.dashboard import DashboardRun, DashboardSummary
from app.services.base import BaseService

# How many recent searches the dashboard lists.
RECENT_RUNS = 5


def local_day_start_utc(tz_offset_minutes: int, now: datetime | None = None) -> datetime:
    """The UTC instant at which the user's current local day began.

    `tz_offset_minutes` follows JavaScript's `Date.getTimezoneOffset()`: the
    minutes to ADD to local time to reach UTC, so UTC+5 is -300. Doing the
    arithmetic here rather than trusting the server clock means a user in
    Karachi sees Karachi's today, not the server's.
    """
    now = now or datetime.now(UTC)
    local_now = now - timedelta(minutes=tz_offset_minutes)
    local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)

    return local_start + timedelta(minutes=tz_offset_minutes)


class DashboardService(BaseService):
    def __init__(self, db: AsyncSession) -> None:
        super().__init__(db)
        self.companies = CompanyRepository(db)
        self.contacts = DecisionMakerRepository(db)
        self.runs = LeadSearchRunRepository(db)
        self.wallets = WalletRepository(db)

    async def summary(self, user_id: str, *, tz_offset_minutes: int) -> DashboardSummary:
        since = local_day_start_utc(tz_offset_minutes)

        wallet = await self.wallets.get_or_create(user_id)
        # A first visit may have created the wallet row.
        await self.commit()

        recent = await self.runs.list_for_user(user_id, offset=0, limit=RECENT_RUNS)

        return DashboardSummary(
            new_leads_today=await self.companies.count_added_since(user_id, since),
            awaiting_contact=await self.companies.count_leads(
                user_id, status=LeadStatus.NEW.value
            ),
            searches_today=await self.runs.count_created_since(user_id, since),
            contacts_found_today=await self.contacts.count_found_since(user_id, since),
            credits_left=wallet.balance,
            leads_total=await self.companies.count_leads(user_id),
            day_start=since,
            recent_runs=[DashboardRun.model_validate(run) for run in recent],
        )


__all__ = ["DashboardService", "local_day_start_utc"]
