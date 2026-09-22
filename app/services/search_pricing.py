"""What a lead search costs, and how the estimate is made.

The bill has up to two lines (rates in billing_catalog):

  base fee    BASE_CONTACT_CREDIT x every decision maker with a verified
              email -- a verified email means email_status == "valid" and an
              actual address; nothing else is billable
  WhatsApp    WHATSAPP_VALIDATION_CREDITS x every one of those contacts whose
              number came back Active on WhatsApp. Never charged for a number
              that was checked and found not active, nor for one never
              checked at all -- only a positive result is a premium worth
              paying for.

There is no run fee and no per-company charge: a company with no verified
contact costs nothing, however many the workflow had to look through to find
it. Nothing is taken at dispatch. The whole amount is charged once, when the
results callback reports success -- see LeadResultsService.ingest. A run that
fails, or whose results never arrive, costs nothing.

Both lines depend on what the workflow finds, so the amount is not known
until the end. The quote produced here is the pre-dispatch estimate: it lets
the user see the likely cost, and it is what the start gate checks the
balance against, so nobody launches a search their balance could not cover.
"""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.lead_search import LeadSearchRunRepository
from app.schemas.lead_radar import QuoteLine, SearchQuote
from app.services.base import BaseService
from app.services.billing_catalog import (
    BASE_CONTACT_CREDIT,
    DEFAULT_COMPANIES_PER_KEYWORD,
    DEFAULT_CONTACTS_PER_COMPANY,
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


def cost_lines(*, verified_contacts: int, active_whatsapp: int) -> list[CostLine]:
    """The bill for a run that found `verified_contacts` decision makers with
    a verified email, `active_whatsapp` of which came back Active on
    WhatsApp. Used for both the estimate and the real settlement, so the two
    can never drift apart."""
    lines: list[CostLine] = []

    if verified_contacts > 0:
        lines.append(
            CostLine(
                "contacts",
                f"Lead search — {verified_contacts:,} verified emails found",
                verified_contacts,
                BASE_CONTACT_CREDIT,
            )
        )

    if active_whatsapp > 0:
        lines.append(
            CostLine(
                "whatsapp",
                f"Lead search — {active_whatsapp:,} active WhatsApps found",
                active_whatsapp,
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
        """The pre-dispatch estimate for a search of `keyword_count` terms.

        Companies and contacts are still estimated from history to size the
        run for the user, but only verified-email contacts are billable, and
        the estimate has no way to know in advance how many of those will
        turn out valid or Active on WhatsApp. It assumes every estimated
        contact will carry a verified email (the same "assume the best case
        that still bounds the bill" approach the old per-company estimate
        used), and, when validation was requested, that all of them come back
        Active -- the true upper bound the start gate must be able to cover.
        """
        assumptions = await self.assumptions_for(user_id)

        # At least one company, or the estimate would read as free.
        estimated_companies = (
            max(1, round(keyword_count * assumptions.companies_per_keyword))
            if keyword_count > 0
            else 0
        )

        estimated_contacts = (
            round(estimated_companies * assumptions.contacts_per_company)
            if estimated_companies > 0
            else 0
        )

        # Every contact is assumed to carry a number and come back Active,
        # so this is an upper bound; the real count is whatever the workflow
        # actually found.
        estimated_active_whatsapp = estimated_contacts if validate_whatsapp else 0

        lines = cost_lines(
            verified_contacts=estimated_contacts,
            active_whatsapp=estimated_active_whatsapp,
        )
        total = total_credits(lines)

        return SearchQuote(
            keyword_count=keyword_count,
            validate_whatsapp=validate_whatsapp,
            estimated_companies=estimated_companies,
            estimated_whatsapp_checks=estimated_active_whatsapp,
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
            contact_credits=BASE_CONTACT_CREDIT,
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
