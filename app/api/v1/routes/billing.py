"""Billing endpoints: wallet balance, base subscriptions and credit top-ups.

There is no payment step yet -- a purchase completes as soon as it is posted.
"""

from fastapi import APIRouter, status

from app.api.deps import CurrentUser, DbSession, Pagination
from app.schemas.billing import (
    BillingOverview,
    CreditPurchaseRead,
    CreditTransactionRead,
    SubscribeRequest,
    SubscriptionRead,
    TopUpRequest,
)
from app.schemas.common import Page
from app.services.billing import BillingService

router = APIRouter()


@router.get("/overview", response_model=BillingOverview, summary="Wallet overview")
async def get_overview(db: DbSession, current_user: CurrentUser) -> BillingOverview:
    """Balance, active plan (if any), the catalogue, and whether top-ups are unlocked."""
    return await BillingService(db).overview(current_user.id)


@router.get(
    "/transactions",
    response_model=Page[CreditTransactionRead],
    summary="Credit history",
)
async def list_transactions(
    db: DbSession, pagination: Pagination, current_user: CurrentUser
) -> Page[CreditTransactionRead]:
    """Every credit movement for the signed-in user, newest first."""
    items, total = await BillingService(db).list_transactions(
        current_user.id, offset=pagination.offset, limit=pagination.limit
    )

    return Page.create(
        items=[CreditTransactionRead.model_validate(t) for t in items],
        total=total,
        page=pagination.page,
        per_page=pagination.per_page,
    )


@router.post(
    "/subscriptions",
    response_model=SubscriptionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Subscribe to a base plan",
)
async def subscribe(
    payload: SubscribeRequest, db: DbSession, current_user: CurrentUser
) -> SubscriptionRead:
    """Start a plan and credit its included allowance. 409 if one is already active."""
    subscription = await BillingService(db).subscribe(current_user.id, payload.plan_code)
    return SubscriptionRead.model_validate(subscription)


@router.post(
    "/top-ups",
    response_model=CreditPurchaseRead,
    status_code=status.HTTP_201_CREATED,
    summary="Buy a credit pack",
)
async def top_up(
    payload: TopUpRequest, db: DbSession, current_user: CurrentUser
) -> CreditPurchaseRead:
    """Add a credit pack to the wallet.

    Answers 403 `subscription_required` unless the user holds an active plan.
    """
    purchase = await BillingService(db).top_up(current_user.id, payload.pack_code)
    return CreditPurchaseRead.model_validate(purchase)
