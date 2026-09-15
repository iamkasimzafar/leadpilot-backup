"""Data access for wallets, subscriptions, purchases and the credit ledger."""

from sqlalchemy import select

from app.models.billing import (
    CreditPurchase,
    CreditTransaction,
    Subscription,
    SubscriptionStatus,
    Wallet,
)
from app.repositories.base import BaseRepository


class WalletRepository(BaseRepository[Wallet]):
    model = Wallet

    async def get_or_create(self, user_id: str) -> Wallet:
        wallet = await self.get(user_id)
        if wallet is None:
            wallet = await self.create(user_id=user_id, balance=0)
        return wallet

    async def get_for_update(self, user_id: str) -> Wallet:
        """Load the wallet with a row lock, so two concurrent purchases (or a
        purchase racing a spend) cannot both read the same balance and lose an
        update. SQLite ignores FOR UPDATE; MySQL honours it."""
        stmt = select(Wallet).where(Wallet.user_id == user_id).with_for_update()
        result = await self.db.execute(stmt)
        wallet = result.scalar_one_or_none()
        if wallet is None:
            wallet = await self.create(user_id=user_id, balance=0)
        return wallet


class SubscriptionRepository(BaseRepository[Subscription]):
    model = Subscription

    async def get_current(self, user_id: str) -> Subscription | None:
        """The newest subscription still marked active, if any.

        Whether it has actually lapsed is the service's call: it compares
        `current_period_end` against now and expires it lazily.
        """
        stmt = (
            select(Subscription)
            .where(
                Subscription.user_id == user_id,
                Subscription.status == SubscriptionStatus.ACTIVE.value,
            )
            .order_by(Subscription.current_period_end.desc())
            .limit(1)
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()


class CreditPurchaseRepository(BaseRepository[CreditPurchase]):
    model = CreditPurchase


class CreditTransactionRepository(BaseRepository[CreditTransaction]):
    model = CreditTransaction

    async def list_for_user(
        self, user_id: str, *, offset: int = 0, limit: int = 20
    ) -> list[CreditTransaction]:
        """Newest first."""
        stmt = (
            select(CreditTransaction)
            .where(CreditTransaction.user_id == user_id)
            .order_by(CreditTransaction.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())
