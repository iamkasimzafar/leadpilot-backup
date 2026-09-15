"""Billing request and response bodies."""

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import BaseSchema


# --- Catalogue ---------------------------------------------------------------
class PlanRead(BaseSchema):
    code: str
    name: str
    interval: str = Field(description='"month" or "year".')
    price_cents: int
    credits: int
    popular: bool


class CreditPackRead(BaseSchema):
    code: str
    name: str
    price_cents: int
    credits: int
    popular: bool


# --- Records -----------------------------------------------------------------
class SubscriptionRead(BaseSchema):
    id: str
    plan_code: str
    plan_name: str
    status: str
    billing_interval: str
    price_cents: int
    credits_included: int
    current_period_start: datetime
    current_period_end: datetime


class CreditPurchaseRead(BaseSchema):
    id: str
    pack_code: str
    pack_name: str
    credits: int
    price_cents: int
    status: str
    created_at: datetime


class CreditTransactionRead(BaseSchema):
    id: str
    kind: str
    amount: int = Field(description="Signed: positive adds credits, negative spends.")
    balance_after: int
    description: str
    created_at: datetime


# --- Overview ----------------------------------------------------------------
class BillingOverview(BaseModel):
    """Everything the wallet page needs in one round trip."""

    balance: int
    subscription: SubscriptionRead | None
    can_top_up: bool = Field(
        description="False until the user holds an active subscription."
    )
    plans: list[PlanRead]
    packs: list[CreditPackRead]


# --- Requests ----------------------------------------------------------------
class SubscribeRequest(BaseModel):
    plan_code: str = Field(min_length=1, max_length=32)


class TopUpRequest(BaseModel):
    pack_code: str = Field(min_length=1, max_length=32)
