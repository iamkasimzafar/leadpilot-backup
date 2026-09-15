"""Subscription and credit-wallet ORM models.

Credits are an append-only ledger (CreditTransaction) with the running total
denormalised onto Wallet, so reading a balance is one primary-key lookup.
Every ledger row records `balance_after`, so history is auditable without
replaying the ledger.

Statuses and kinds are stored as plain strings rather than a database ENUM:
portable across MySQL and the SQLite used by the test suite, and adding a
value never needs a migration.
"""

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


def _uuid_str() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(UTC)


class SubscriptionStatus(StrEnum):
    ACTIVE = "active"
    CANCELED = "canceled"
    EXPIRED = "expired"


class PurchaseStatus(StrEnum):
    # There is no payment step yet, so purchases complete immediately. The
    # column exists so a pending -> paid flow can be added without a migration.
    COMPLETED = "completed"


class TransactionKind(StrEnum):
    SUBSCRIPTION_GRANT = "subscription_grant"
    TOP_UP = "top_up"
    USAGE = "usage"
    ADJUSTMENT = "adjustment"


class Wallet(Base, TimestampMixin):
    """A user's credit balance. One row per user, created on first use."""

    __table_args__ = (
        CheckConstraint("balance >= 0", name="ck_wallet_balance_non_negative"),
    )

    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("user.id", ondelete="CASCADE"), primary_key=True
    )
    balance: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Wallet user_id={self.user_id} balance={self.balance}>"


class Subscription(Base, TimestampMixin):
    """A base plan held by a user.

    Plan details are snapshotted at purchase (name, price, credits) so the
    record stays truthful if the catalogue changes later. A user has at most
    one ACTIVE row at a time; the service enforces that.
    """

    __table_args__ = (Index("ix_subscription_user_id_status", "user_id", "status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid_str)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    )

    plan_code: Mapped[str] = mapped_column(String(32), nullable=False)
    plan_name: Mapped[str] = mapped_column(String(64), nullable=False)
    billing_interval: Mapped[str] = mapped_column(String(8), nullable=False)
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    credits_included: Mapped[int] = mapped_column(Integer, nullable=False)

    status: Mapped[str] = mapped_column(
        String(16), default=SubscriptionStatus.ACTIVE.value, nullable=False
    )
    current_period_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    current_period_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    canceled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Subscription user_id={self.user_id} {self.plan_code} {self.status}>"


class CreditPurchase(Base):
    """A credit top-up order. Only possible while a subscription is active."""

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid_str)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("user.id", ondelete="CASCADE"), index=True, nullable=False
    )

    pack_code: Mapped[str] = mapped_column(String(32), nullable=False)
    pack_name: Mapped[str] = mapped_column(String(64), nullable=False)
    credits: Mapped[int] = mapped_column(Integer, nullable=False)
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default=PurchaseStatus.COMPLETED.value, nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        server_default=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<CreditPurchase user_id={self.user_id} pack={self.pack_code}>"


class CreditTransaction(Base):
    """One movement of credits. Positive amounts add, negative amounts spend."""

    __table_args__ = (
        Index("ix_credit_transaction_user_id_created_at", "user_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid_str)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    )

    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(String(255), nullable=False)

    # What caused the movement: ("subscription", id), ("credit_purchase", id),
    # or later ("radar_task", id). Free-form so new sources need no migration.
    reference_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reference_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # Python-side default (microsecond precision) rather than only the server
    # default, so two entries written in the same second still order correctly.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        server_default=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<CreditTransaction user_id={self.user_id} {self.kind} {self.amount:+}>"
