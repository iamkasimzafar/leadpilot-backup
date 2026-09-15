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
AI_KEYWORD_EXPANSION_CREDITS = 5

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
    "CREDITS_PER_DOLLAR",
    "PACKS",
    "PLANS",
    "BillingInterval",
    "CreditPack",
    "Plan",
]
