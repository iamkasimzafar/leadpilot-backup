"""Reports figures.

Like the dashboard, every number is a real count over the user's own rows.
Nothing is estimated, extrapolated or padded out to make a chart look fuller
than the data behind it.

The window is a whole number of the user's local days ending today, so "last
30 days" means the 30 calendar days they would count on a calendar, not a
rolling 720 hours measured from the server's clock.
"""

from collections import defaultdict
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.lead import LeadStatus
from app.models.lead_search import RunStatus
from app.repositories.reports import ReportsRepository
from app.schemas.reports import (
    BreakdownSlice,
    FunnelStage,
    ReportSummary,
    ReportTotals,
    SearchOutcomes,
    TrendPoint,
)
from app.services.base import BaseService
from app.services.dashboard import local_day_start_utc

# Windows the page offers. Bounded so one request can never ask the database
# to roll up an unbounded history.
MIN_DAYS = 7
MAX_DAYS = 365

# How many rows a "top N" breakdown returns.
TOP_N = 8

# Ledger kinds that represent spending, in the order the page lists them.
# `subscription_grant` and `top_up` are credits in, so they never appear here.
SPEND_KINDS = ("usage", "adjustment")


def _round1(value: float) -> float:
    return round(value, 1)


def _rate(part: int, whole: int) -> float:
    """A percentage, 0-100, one decimal. Zero when there is nothing to divide."""
    if whole <= 0:
        return 0.0

    return _round1(part * 100 / whole)


def _country_of(location: str) -> str:
    """Fold a free-text location down to its country.

    The workflow writes locations as "Hamburg, Germany" or just "Germany", so
    the last comma-separated part is the country. Imperfect by nature -- this
    is unnormalised text from a third party -- but it turns a scatter of cities
    into a chart that answers "where are my leads?".
    """
    parts = [part.strip() for part in location.split(",") if part.strip()]

    return parts[-1] if parts else location.strip()


class ReportsService(BaseService):
    def __init__(self, db: AsyncSession) -> None:
        super().__init__(db)
        self.reports = ReportsRepository(db)

    async def summary(
        self, user_id: str, *, days: int, tz_offset_minutes: int
    ) -> ReportSummary:
        days = max(MIN_DAYS, min(MAX_DAYS, days))

        now = datetime.now(UTC)
        today_start = local_day_start_utc(tz_offset_minutes, now)

        # The window covers `days` local days ending with today, so a 7-day
        # window is today plus the six before it.
        start = today_start - timedelta(days=days - 1)
        end = today_start + timedelta(days=1)

        trend = await self._trend(
            user_id, start, end, days=days, tz_offset_minutes=tz_offset_minutes
        )

        found, with_email, with_phone, on_whatsapp = await self.reports.contact_quality(
            user_id, start, end
        )

        leads_added = sum(point.leads for point in trend)
        searches_run = sum(point.searches for point in trend)
        credits_spent = sum(point.credits_spent for point in trend)

        totals = ReportTotals(
            leads_added=leads_added,
            contacts_found=found,
            searches_run=searches_run,
            credits_spent=credits_spent,
            contacts_with_email=with_email,
            contacts_with_phone=with_phone,
            contacts_on_whatsapp=on_whatsapp,
            email_rate=_rate(with_email, found),
            contacts_per_search=(
                _round1(found / searches_run) if searches_run else 0.0
            ),
            credits_per_lead=(
                _round1(credits_spent / leads_added) if leads_added else 0.0
            ),
        )

        outcomes = await self._outcomes(user_id, start, end)
        funnel = await self._funnel(user_id, start, end)
        countries = await self._top_countries(user_id, start, end)
        keywords = await self._top_keywords(user_id, start, end)
        spend = await self._spend_by_kind(user_id, start, end)

        # "Nothing here yet" is about activity, not about a single metric: an
        # account that ran a search which found nothing has still done
        # something, and should see its searches rather than an empty state.
        is_empty = not (leads_added or found or searches_run or credits_spent)

        return ReportSummary(
            start_date=_local_date_key(start, tz_offset_minutes),
            end_date=_local_date_key(today_start, tz_offset_minutes),
            days=days,
            totals=totals,
            outcomes=outcomes,
            trend=trend,
            funnel=funnel,
            top_countries=countries,
            top_keywords=keywords,
            spend_by_kind=spend,
            is_empty=is_empty,
            window_start=start,
        )

    # --- Pieces -------------------------------------------------------------
    async def _trend(
        self,
        user_id: str,
        start: datetime,
        end: datetime,
        *,
        days: int,
        tz_offset_minutes: int,
    ) -> list[TrendPoint]:
        """One point per local day, including days with nothing on them.

        The empty days matter: a line chart that silently skips them would
        compress a quiet fortnight into a slope that never happened.
        """
        leads = await self.reports.lead_added_times(user_id, start, end)
        contacts = await self.reports.contact_found_times(user_id, start, end)
        searches = await self.reports.search_started_times(user_id, start, end)
        spends = await self.reports.spend_entries(user_id, start, end)

        lead_by_day = _bucket(leads, tz_offset_minutes)
        contact_by_day = _bucket(contacts, tz_offset_minutes)
        search_by_day = _bucket(searches, tz_offset_minutes)

        spend_by_day: dict[str, int] = defaultdict(int)
        for when, amount, _kind in spends:
            spend_by_day[_local_date_key(when, tz_offset_minutes)] += amount

        points: list[TrendPoint] = []
        for offset in range(days):
            day = start + timedelta(days=offset)
            key = _local_date_key(day, tz_offset_minutes)
            points.append(
                TrendPoint(
                    date=key,
                    leads=lead_by_day.get(key, 0),
                    contacts=contact_by_day.get(key, 0),
                    searches=search_by_day.get(key, 0),
                    credits_spent=spend_by_day.get(key, 0),
                )
            )

        return points

    async def _outcomes(
        self, user_id: str, start: datetime, end: datetime
    ) -> SearchOutcomes:
        counts = await self.reports.run_status_counts(user_id, start, end)

        completed = counts.get(RunStatus.COMPLETED.value, 0)
        failed = counts.get(RunStatus.FAILED.value, 0)
        running = counts.get(RunStatus.RUNNING.value, 0)

        # A search still running has not succeeded or failed yet, so it is left
        # out of the rate rather than counted against it.
        return SearchOutcomes(
            completed=completed,
            failed=failed,
            running=running,
            success_rate=_rate(completed, completed + failed),
        )

    async def _funnel(
        self, user_id: str, start: datetime, end: datetime
    ) -> list[FunnelStage]:
        """Found -> in My Leads -> contacted -> interested.

        Each stage is a subset of the one before it, so the bars only ever
        narrow. "Contacted" therefore includes leads now marked interested:
        you cannot become interested without having been contacted.
        """
        found = await self.reports.companies_found(user_id, start, end)
        by_status = await self.reports.lead_status_counts(user_id, start, end)

        new = by_status.get(LeadStatus.NEW.value, 0)
        contacted = by_status.get(LeadStatus.CONTACTED.value, 0)
        interested = by_status.get(LeadStatus.INTERESTED.value, 0)

        in_leads = new + contacted + interested

        return [
            FunnelStage(key="found", count=found),
            FunnelStage(key="in_leads", count=in_leads),
            FunnelStage(key="contacted", count=contacted + interested),
            FunnelStage(key="interested", count=interested),
        ]

    async def _top_countries(
        self, user_id: str, start: datetime, end: datetime
    ) -> list[BreakdownSlice]:
        rows = await self.reports.top_locations(user_id, start, end, limit=TOP_N)

        totals: dict[str, int] = defaultdict(int)
        for location, count in rows:
            totals[_country_of(location)] += count

        ranked = sorted(totals.items(), key=lambda item: (-item[1], item[0]))

        return [
            BreakdownSlice(label=label, count=count) for label, count in ranked[:TOP_N]
        ]

    async def _top_keywords(
        self, user_id: str, start: datetime, end: datetime
    ) -> list[BreakdownSlice]:
        rows = await self.reports.top_keywords(user_id, start, end, limit=TOP_N)

        return [
            BreakdownSlice(label=keyword, count=count)
            for keyword, count in rows
            if count > 0
        ]

    async def _spend_by_kind(
        self, user_id: str, start: datetime, end: datetime
    ) -> list[BreakdownSlice]:
        entries = await self.reports.spend_entries(user_id, start, end)

        totals: dict[str, int] = defaultdict(int)
        for _when, amount, kind in entries:
            totals[kind] += amount

        # Keep the catalogue's order where we know it, then anything new the
        # ledger grows later, so an unrecognised kind is still shown.
        ordered = [kind for kind in SPEND_KINDS if totals.get(kind)]
        ordered += sorted(kind for kind in totals if kind not in SPEND_KINDS)

        return [BreakdownSlice(label=kind, count=totals[kind]) for kind in ordered]


def _local_date_key(moment: datetime, tz_offset_minutes: int) -> str:
    """The user's local calendar day for a UTC instant, as YYYY-MM-DD."""
    local = moment - timedelta(minutes=tz_offset_minutes)

    return local.date().isoformat()


def _bucket(moments: list[datetime], tz_offset_minutes: int) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for moment in moments:
        counts[_local_date_key(moment, tz_offset_minutes)] += 1

    return counts


__all__ = ["ReportsService"]
