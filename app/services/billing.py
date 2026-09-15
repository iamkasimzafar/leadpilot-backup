"""Subscription and credit business rules.

The one rule that matters most lives here, not in the UI: a credit top-up is
refused unless the user holds an active base subscription. The frontend greys
the packs out for the same reason, but this check is what actually enforces it.

There is no payment provider yet, so purchases complete the moment they are
requested and subscriptions do not renew: when a period ends the plan lapses
and the user picks a plan again.
"""

import calendar
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    ConflictError,
    InsufficientCreditsError,
    SubscriptionRequiredError,
    ValidationError,
)
from app.core.logging import get_logger
from app.models.billing import (
    CreditPurchase,
    CreditTransaction,
    PurchaseStatus,
    Subscription,
    SubscriptionStatus,
    TransactionKind,
)
from app.models.notification import NotificationKind
from app.repositories.billing import (
    CreditPurchaseRepository,
    CreditTransactionRepository,
    SubscriptionRepository,
    WalletRepository,
)
from app.schemas.billing import (
    BillingOverview,
    CreditPackRead,
    PlanRead,
    SubscriptionRead,
)
from app.services.base import BaseService
from app.services.billing_catalog import PACKS, PLANS, BillingInterval
from app.services.notification import NotificationService

log = get_logger(__name__)


def _add_interval(start: datetime, interval: BillingInterval) -> datetime:
    """Advance by one calendar month or year, clamping the day (Jan 31 -> Feb 28)."""
    if interval == "year":
        year, month = start.year + 1, start.month
    else:
        year = start.year + start.month // 12
        month = start.month % 12 + 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return start.replace(year=year, month=month, day=day)


def _as_utc(value: datetime) -> datetime:
    """SQLite hands back naive datetimes even for timezone=True columns."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class BillingService(BaseService):
    def __init__(self, db: AsyncSession) -> None:
        super().__init__(db)
        self.wallets = WalletRepository(db)
        self.subscriptions = SubscriptionRepository(db)
        self.purchases = CreditPurchaseRepository(db)
        self.transactions = CreditTransactionRepository(db)
        self.notifications = NotificationService(db)

    # --- Reads --------------------------------------------------------------
    async def active_subscription(self, user_id: str) -> Subscription | None:
        """The user's live plan, or None.

        A plan whose period has ended is expired here, on read, rather than by
        a scheduler: with no renewal there is nothing else to do at period end.
        """
        subscription = await self.subscriptions.get_current(user_id)
        if subscription is None:
            return None

        if _as_utc(subscription.current_period_end) <= datetime.now(UTC):
            subscription.status = SubscriptionStatus.EXPIRED.value
            await self.db.flush()
            log.info("billing.subscription_expired", user_id=user_id)
            return None

        return subscription

    async def overview(self, user_id: str) -> BillingOverview:
        wallet = await self.wallets.get_or_create(user_id)
        subscription = await self.active_subscription(user_id)
        # A first read may have created the wallet or expired a lapsed plan.
        await self.commit()

        return BillingOverview(
            balance=wallet.balance,
            subscription=(
                SubscriptionRead.model_validate(subscription) if subscription else None
            ),
            can_top_up=subscription is not None,
            plans=[PlanRead.model_validate(plan) for plan in PLANS.values()],
            packs=[CreditPackRead.model_validate(pack) for pack in PACKS.values()],
        )

    async def list_transactions(
        self, user_id: str, *, offset: int, limit: int
    ) -> tuple[list[CreditTransaction], int]:
        items = await self.transactions.list_for_user(user_id, offset=offset, limit=limit)
        total = await self.transactions.count(user_id=user_id)
        return items, total

    # --- Purchases ----------------------------------------------------------
    async def subscribe(self, user_id: str, plan_code: str) -> Subscription:
        """Start a base plan and grant its included credits."""
        plan = PLANS.get(plan_code)
        if plan is None:
            raise ValidationError("Unknown plan.")

        if await self.active_subscription(user_id) is not None:
            raise ConflictError("You already have an active subscription.")

        now = datetime.now(UTC)
        subscription = await self.subscriptions.create(
            user_id=user_id,
            plan_code=plan.code,
            plan_name=plan.name,
            billing_interval=plan.interval,
            price_cents=plan.price_cents,
            credits_included=plan.credits,
            status=SubscriptionStatus.ACTIVE.value,
            current_period_start=now,
            current_period_end=_add_interval(now, plan.interval),
        )
        await self._post(
            user_id,
            plan.credits,
            kind=TransactionKind.SUBSCRIPTION_GRANT,
            description=f"{plan.name} plan — {plan.credits:,} credits included",
            reference_type="subscription",
            reference_id=subscription.id,
        )

        # commit=False: the notification joins this transaction, so it cannot
        # survive a failed subscription (or go missing after a successful one).
        notification = await self.notifications.create(
            user_id,
            kind=NotificationKind.CREDITS,
            title=f"{plan.name} plan activated",
            subtitle=(
                f"{plan.credits:,} credits added to your wallet. "
                f"Credit top-ups are now unlocked."
            ),
            link="/wallet",
            commit=False,
        )

        await self.commit()

        # Announced only once the row is durable.
        NotificationService.publish(user_id, notification.id)

        log.info("billing.subscribed", user_id=user_id, plan=plan.code)
        return subscription

    async def top_up(self, user_id: str, pack_code: str) -> CreditPurchase:
        """Buy a credit pack. Gated on an active subscription."""
        pack = PACKS.get(pack_code)
        if pack is None:
            raise ValidationError("Unknown credit pack.")

        # The gate. Enforced here so a hand-crafted request cannot bypass the UI.
        if await self.active_subscription(user_id) is None:
            raise SubscriptionRequiredError()

        purchase = await self.purchases.create(
            user_id=user_id,
            pack_code=pack.code,
            pack_name=pack.name,
            credits=pack.credits,
            price_cents=pack.price_cents,
            status=PurchaseStatus.COMPLETED.value,
        )
        transaction = await self._post(
            user_id,
            pack.credits,
            kind=TransactionKind.TOP_UP,
            description=f"{pack.name} — {pack.credits:,} credits",
            reference_type="credit_purchase",
            reference_id=purchase.id,
        )

        notification = await self.notifications.create(
            user_id,
            kind=NotificationKind.CREDITS,
            title=f"{pack.credits:,} credits added",
            subtitle=(
                f"{pack.name} purchased. "
                f"Your balance is now {transaction.balance_after:,} credits."
            ),
            link="/wallet",
            commit=False,
        )

        await self.commit()

        NotificationService.publish(user_id, notification.id)

        log.info("billing.topped_up", user_id=user_id, pack=pack.code)
        return purchase

    # --- Spending -----------------------------------------------------------
    async def ensure_can_afford(self, user_id: str, amount: int) -> int:
        """Check the balance covers `amount` before doing expensive work.

        Raises InsufficientCreditsError if not. Returns the current balance.
        This only reads: the debit still happens in `spend`, so a caller that
        fails midway never leaves the user short.
        """
        wallet = await self.wallets.get_or_create(user_id)

        # A first read may have created the wallet row.
        await self.commit()

        if wallet.balance < amount:
            raise InsufficientCreditsError(
                details={"balance": wallet.balance, "required": amount}
            )

        return wallet.balance

    async def spend(
        self,
        user_id: str,
        amount: int,
        description: str,
        *,
        reference_type: str | None = None,
        reference_id: str | None = None,
    ) -> CreditTransaction:
        """Deduct credits for a feature. Refused if the balance is too low."""
        if amount <= 0:
            raise ValueError("amount must be positive")

        wallet = await self.wallets.get_for_update(user_id)
        if wallet.balance < amount:
            raise InsufficientCreditsError(
                details={"balance": wallet.balance, "required": amount}
            )

        transaction = await self._post(
            user_id,
            -amount,
            kind=TransactionKind.USAGE,
            description=description,
            reference_type=reference_type,
            reference_id=reference_id,
        )
        await self.commit()

        log.info("billing.spent", user_id=user_id, amount=amount)
        return transaction

    # --- Ledger -------------------------------------------------------------
    async def _post(
        self,
        user_id: str,
        amount: int,
        *,
        kind: TransactionKind,
        description: str,
        reference_type: str | None = None,
        reference_id: str | None = None,
    ) -> CreditTransaction:
        """Append a ledger entry and move the wallet balance by `amount`.

        Does not commit: callers group it with the purchase row it belongs to.
        """
        wallet = await self.wallets.get_for_update(user_id)
        wallet.balance += amount

        return await self.transactions.create(
            user_id=user_id,
            kind=kind.value,
            amount=amount,
            balance_after=wallet.balance,
            description=description,
            reference_type=reference_type,
            reference_id=reference_id,
        )


__all__ = ["BillingService"]
