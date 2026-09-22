"""The plans and credit packs on sale.

Kept in code rather than a table: there are five products, they change with a
deploy, and every purchase snapshots its price and credits onto the row it
creates -- so editing a price here never rewrites history. Codes are the
stable identifiers the frontend sends back; names and prices are display data.
"""

from dataclasses import dataclass
from typing import Literal

BillingInterval = Literal["month", "year"]


@dataclass(frozen=True)
class Plan:
    """A base subscription. Holding one unlocks credit top-ups."""

    code: str
    name: str
    interval: BillingInterval
    price_cents: int
    credits: int
    popular: bool = False


@dataclass(frozen=True)
class CreditPack:
    """A one-off credit top-up. Only purchasable with an active plan."""

    code: str
    name: str
    price_cents: int
    credits: int
    popular: bool = False


# --- Feature pricing ---------------------------------------------------------
# What each AI/search action costs a user. Kept beside the plans so every price
# in the product is declared in one file.
# Charged immediately when the user clicks AI Expansion: they get the keyword
# list back instantly, so this is billed at the click rather than settled
# later like the search itself.
AI_KEYWORD_EXPANSION_CREDITS = 5

# --- Lead search -------------------------------------------------------------
# There is no run fee and no per-company charge: finding a company shell costs
# nothing. A search is billed once, when the workflow's results arrive
# successfully, purely on the decision makers it actually turned up:
#
#   base fee    BASE_CONTACT_CREDIT x every decision maker with a verified
#               email (email_status == "valid" and an email address present)
#   WhatsApp    WHATSAPP_VALIDATION_CREDITS x every one of those contacts
#               whose number came back Active on WhatsApp -- never charged
#               for a number that was checked and came back not active, or
#               never checked at all
#
# A run that fails, never reports back, or returns no verified email at all,
# costs nothing. The maths lives in services/search_pricing.py.
BASE_CONTACT_CREDIT = 10
WHATSAPP_VALIDATION_CREDITS = 5

# Starting assumptions for the pre-dispatch estimate, used until an account has
# completed runs of its own to learn from.
DEFAULT_COMPANIES_PER_KEYWORD = 10
DEFAULT_CONTACTS_PER_COMPANY = 3

# The exchange rate every product is priced at: $1 buys this many credits.
# Applies to plans and packs alike, so value per dollar is identical whichever
# a user buys. Derive `credits` from it rather than hand-writing a number, and
# the two can never drift apart.
CREDITS_PER_DOLLAR = 10


def _credits_for(price_cents: int) -> int:
    return price_cents // 100 * CREDITS_PER_DOLLAR


PLANS: dict[str, Plan] = {
    plan.code: plan
    for plan in (
        Plan("starter_monthly", "Starter", "month", 49_00, _credits_for(49_00)),
        Plan("pro_yearly", "Pro", "year", 499_00, _credits_for(499_00), popular=True),
    )
}

PACKS: dict[str, CreditPack] = {
    pack.code: pack
    for pack in (
        CreditPack("basic", "Basic Pack", 39_00, _credits_for(39_00)),
        CreditPack("popular", "Popular Pack", 99_00, _credits_for(99_00), popular=True),
        CreditPack("enterprise", "Enterprise Pack", 249_00, _credits_for(249_00)),
    )
}

__all__ = [
    "AI_KEYWORD_EXPANSION_CREDITS",
    "BASE_CONTACT_CREDIT",
    "CREDITS_PER_DOLLAR",
    "DEFAULT_COMPANIES_PER_KEYWORD",
    "DEFAULT_CONTACTS_PER_COMPANY",
    "PACKS",
    "PLANS",
    "WHATSAPP_VALIDATION_CREDITS",
    "BillingInterval",
    "CreditPack",
    "Plan",
]
