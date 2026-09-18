"""What a lead search costs, and how the estimate is made.

The bill has three lines (rates in billing_catalog):

  run fee     SEARCH_RUN_CREDITS, flat, once per run
  companies   COMPANY_RESULT_CREDITS x every company the workflow returned
  WhatsApp    WHATSAPP_VALIDATION_CREDITS x every number actually checked,
              only when the run asked for validation, and regardless of
              whether the number turned out to be on WhatsApp

Nothing is taken at dispatch. The whole amount is charged once, when the
results callback reports success -- see LeadResultsService.ingest. A run that
fails, or whose results never arrive, costs nothing.

Two of the three lines depend on what the workflow finds, so the amount is not
known until the end. The quote produced here is the pre-dispatch estimate: it
lets the user see the likely cost, and it is what the start gate checks the
balance against, so nobody launches a search their balance could not cover.
"""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.lead_search import LeadSearchRunRepository
from app.schemas.lead_radar import QuoteLine, SearchQuote
from app.services.base import BaseService
from app.services.billing_catalog import (
    COMPANY_RESULT_CREDITS,
    DEFAULT_COMPANIES_PER_KEYWORD,
    DEFAULT_CONTACTS_PER_COMPANY,
    SEARCH_RUN_CREDITS,
    WHATSAPP_VALIDATION_CREDITS,
)

# How many of the account's latest completed runs the estimate learns from.
HISTORY_RUNS = 10


@dataclass(frozen=True)
class CostLine:
    """One line of a search bill, for the ledger and for the quote."""

    key: str
    description: str
    units: int
    unit_credits: int

    @property
    def credits(self) -> int:
        return self.units * self.unit_credits


def cost_lines(
    *, companies: int, whatsapp_checks: int, validate_whatsapp: bool
) -> list[CostLine]:
    """The bill for a run that found `companies` and checked `whatsapp_checks`
    numbers. Used for both the estimate and the real settlement, so the two
    can never drift apart."""
    lines = [CostLine("run_fee", "Lead search — run fee", 1, SEARCH_RUN_CREDITS)]

    if companies > 0:
        lines.append(
            CostLine(
                "companies",
                f"Lead search — {companies:,} companies found",
                companies,
                COMPANY_RESULT_CREDITS,
            )
        )

    # Only charged when the user asked for validation. A workflow that reports
    # a status it was not asked for is not a reason to bill.
    if validate_whatsapp and whatsapp_checks > 0:
        lines.append(
            CostLine(
                "whatsapp",
                f"Lead search — {whatsapp_checks:,} WhatsApp checks",
                whatsapp_checks,
                WHATSAPP_VALIDATION_CREDITS,
            )
        )

    return lines


def total_credits(lines: list[CostLine]) -> int:
    return sum(line.credits for line in lines)


@dataclass(frozen=True)
class Assumptions:
    """What the estimate multiplies the keyword count by."""

    companies_per_keyword: float
    contacts_per_company: float

    # False when the account has no completed runs and the defaults are in use.
    from_history: bool
    runs_sampled: int


class SearchPricingService(BaseService):
    def __init__(self, db: AsyncSession) -> None:
        super().__init__(db)
        self.runs = LeadSearchRunRepository(db)

    async def assumptions_for(self, user_id: str) -> Assumptions:
        """Learn the per-keyword yield from the account's own recent runs.

        Averaged over the runs' totals rather than per run, so one unusually
        large or small search does not dominate. Falls back to the catalogue
        defaults until there is at least one completed run with results.
        """
        runs = await self.runs.recent_completed(user_id, limit=HISTORY_RUNS)
        usable = [r for r in runs if r.keyword_count > 0 and r.companies_found > 0]

        if not usable:
            return Assumptions(
                DEFAULT_COMPANIES_PER_KEYWORD, DEFAULT_CONTACTS_PER_COMPANY, False, 0
            )

        companies = sum(r.companies_found for r in usable)
        keywords = sum(r.keyword_count for r in usable)
        contacts = sum(r.contacts_found for r in usable)

        return Assumptions(
            companies_per_keyword=companies / keywords,
            contacts_per_company=contacts / companies,
            from_history=True,
            runs_sampled=len(usable),
        )

    async def quote(
        self,
        user_id: str,
        *,
        keyword_count: int,
        validate_whatsapp: bool,
        balance: int,
    ) -> SearchQuote:
        """The pre-dispatch estimate for a search of `keyword_count` terms."""
        assumptions = await self.assumptions_for(user_id)

        # At least one company, or the per-company line would read as free.
        estimated_companies = (
            max(1, round(keyword_count * assumptions.companies_per_keyword))
            if keyword_count > 0
            else 0
        )

        # Every contact is assumed to carry a number, so this is an upper
        # bound; the real count is whatever the workflow actually checked.
        estimated_checks = (
            round(estimated_companies * assumptions.contacts_per_company)
            if validate_whatsapp
            else 0
        )

        lines = cost_lines(
            companies=estimated_companies,
            whatsapp_checks=estimated_checks,
            validate_whatsapp=validate_whatsapp,
        )
        total = total_credits(lines)

        return SearchQuote(
            keyword_count=keyword_count,
            validate_whatsapp=validate_whatsapp,
            estimated_companies=estimated_companies,
            estimated_whatsapp_checks=estimated_checks,
            lines=[
                QuoteLine(
                    key=line.key,
                    units=line.units,
                    unit_credits=line.unit_credits,
                    credits=line.credits,
                )
                for line in lines
            ],
            total=total,
            balance=balance,
            affordable=balance >= total,
            shortfall=max(0, total - balance),
            based_on_history=assumptions.from_history,
            runs_sampled=assumptions.runs_sampled,
            run_fee_credits=SEARCH_RUN_CREDITS,
            company_credits=COMPANY_RESULT_CREDITS,
            whatsapp_credits=WHATSAPP_VALIDATION_CREDITS,
        )


__all__ = [
    "HISTORY_RUNS",
    "Assumptions",
    "CostLine",
    "SearchPricingService",
    "cost_lines",
    "total_credits",
]
