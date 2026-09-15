"""End-to-end tests for subscriptions, the top-up gate, and the credit ledger."""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.config import settings
from app.core.exceptions import InsufficientCreditsError
from app.models.billing import Subscription
from app.services.billing import BillingService, _add_interval
from tests.api.test_auth import signed_in_tokens

PREFIX = f"{settings.API_V1_PREFIX}/billing"


async def auth_headers(client: AsyncClient, db_session) -> dict[str, str]:
    tokens = await signed_in_tokens(client, db_session)
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def overview(client: AsyncClient, headers: dict[str, str]) -> dict:
    response = await client.get(f"{PREFIX}/overview", headers=headers)
    assert response.status_code == 200
    return response.json()


# --- Overview ----------------------------------------------------------------


async def test_overview_starts_empty_and_locked(client: AsyncClient, db_session) -> None:
    headers = await auth_headers(client, db_session)

    body = await overview(client, headers)

    assert body["balance"] == 0
    assert body["subscription"] is None
    assert body["can_top_up"] is False
    assert [p["code"] for p in body["plans"]] == ["starter_monthly", "pro_yearly"]
    assert [p["code"] for p in body["packs"]] == ["basic", "popular", "enterprise"]


async def test_overview_requires_auth(client: AsyncClient) -> None:
    response = await client.get(f"{PREFIX}/overview")

    assert response.status_code == 401


# --- The gate ----------------------------------------------------------------


async def test_top_up_is_refused_without_a_subscription(
    client: AsyncClient, db_session
) -> None:
    headers = await auth_headers(client, db_session)

    response = await client.post(
        f"{PREFIX}/top-ups", json={"pack_code": "basic"}, headers=headers
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "subscription_required"
    assert (await overview(client, headers))["balance"] == 0


async def test_top_up_is_refused_once_the_plan_has_lapsed(
    client: AsyncClient, db_session
) -> None:
    headers = await auth_headers(client, db_session)
    await client.post(
        f"{PREFIX}/subscriptions", json={"plan_code": "starter_monthly"}, headers=headers
    )

    # Wind the clock forward past the period end.
    subscription = (await db_session.execute(select(Subscription))).scalar_one()
    subscription.current_period_end = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()

    response = await client.post(
        f"{PREFIX}/top-ups", json={"pack_code": "basic"}, headers=headers
    )

    assert response.status_code == 403
    body = await overview(client, headers)
    assert body["subscription"] is None
    assert body["can_top_up"] is False
    # The plan's credits stay in the wallet: only the gate closes.
    assert body["balance"] == 490


# --- Subscriptions -----------------------------------------------------------


async def test_subscribing_grants_the_plan_credits_and_unlocks_top_ups(
    client: AsyncClient, db_session
) -> None:
    headers = await auth_headers(client, db_session)

    response = await client.post(
        f"{PREFIX}/subscriptions", json={"plan_code": "pro_yearly"}, headers=headers
    )

    assert response.status_code == 201
    body = response.json()
    assert body["plan_name"] == "Pro"
    assert body["price_cents"] == 499_00
    assert body["status"] == "active"

    summary = await overview(client, headers)
    # $499 at 10 credits per dollar.
    assert summary["balance"] == 4_990
    assert summary["can_top_up"] is True
    assert summary["subscription"]["plan_code"] == "pro_yearly"


async def test_only_one_active_subscription_at_a_time(
    client: AsyncClient, db_session
) -> None:
    headers = await auth_headers(client, db_session)
    await client.post(
        f"{PREFIX}/subscriptions", json={"plan_code": "starter_monthly"}, headers=headers
    )

    response = await client.post(
        f"{PREFIX}/subscriptions", json={"plan_code": "pro_yearly"}, headers=headers
    )

    assert response.status_code == 409
    assert (await overview(client, headers))["balance"] == 490


async def test_unknown_plan_is_rejected(client: AsyncClient, db_session) -> None:
    headers = await auth_headers(client, db_session)

    response = await client.post(
        f"{PREFIX}/subscriptions", json={"plan_code": "platinum"}, headers=headers
    )

    assert response.status_code == 422


# --- Top-ups -----------------------------------------------------------------


async def test_top_up_adds_credits_once_subscribed(
    client: AsyncClient, db_session
) -> None:
    headers = await auth_headers(client, db_session)
    await client.post(
        f"{PREFIX}/subscriptions", json={"plan_code": "starter_monthly"}, headers=headers
    )

    response = await client.post(
        f"{PREFIX}/top-ups", json={"pack_code": "popular"}, headers=headers
    )

    assert response.status_code == 201
    body = response.json()
    # $99 at 10 credits per dollar.
    assert body["credits"] == 990
    assert body["price_cents"] == 99_00
    assert body["status"] == "completed"
    # 490 from the plan + 990 from the pack.
    assert (await overview(client, headers))["balance"] == 1_480


async def test_unknown_pack_is_rejected(client: AsyncClient, db_session) -> None:
    headers = await auth_headers(client, db_session)
    await client.post(
        f"{PREFIX}/subscriptions", json={"plan_code": "starter_monthly"}, headers=headers
    )

    response = await client.post(
        f"{PREFIX}/top-ups", json={"pack_code": "mega"}, headers=headers
    )

    assert response.status_code == 422


# --- Ledger ------------------------------------------------------------------


async def test_transactions_record_every_movement_newest_first(
    client: AsyncClient, db_session
) -> None:
    headers = await auth_headers(client, db_session)
    await client.post(
        f"{PREFIX}/subscriptions", json={"plan_code": "starter_monthly"}, headers=headers
    )
    await client.post(f"{PREFIX}/top-ups", json={"pack_code": "basic"}, headers=headers)

    response = await client.get(f"{PREFIX}/transactions", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    kinds = [item["kind"] for item in body["items"]]
    assert kinds == ["top_up", "subscription_grant"]
    # Starter grants 490, then the Basic pack adds 390.
    assert [item["balance_after"] for item in body["items"]] == [880, 490]


async def test_spending_debits_the_wallet(client: AsyncClient, db_session) -> None:
    headers = await auth_headers(client, db_session)
    await client.post(
        f"{PREFIX}/subscriptions", json={"plan_code": "starter_monthly"}, headers=headers
    )
    subscription = (await db_session.execute(select(Subscription))).scalar_one()

    transaction = await BillingService(db_session).spend(
        subscription.user_id,
        86,
        "Keyword search",
        reference_type="radar_task",
        reference_id="t1",
    )

    assert transaction.amount == -86
    assert transaction.balance_after == 404
    assert (await overview(client, headers))["balance"] == 404


async def test_spending_more_than_the_balance_is_refused(
    client: AsyncClient, db_session
) -> None:
    headers = await auth_headers(client, db_session)
    subscription_response = await client.post(
        f"{PREFIX}/subscriptions", json={"plan_code": "starter_monthly"}, headers=headers
    )
    assert subscription_response.status_code == 201
    subscription = (await db_session.execute(select(Subscription))).scalar_one()

    with pytest.raises(InsufficientCreditsError) as excinfo:
        await BillingService(db_session).spend(subscription.user_id, 491, "Too much")

    assert excinfo.value.details == {"balance": 490, "required": 491}
    assert (await overview(client, headers))["balance"] == 490


# --- Period arithmetic -------------------------------------------------------


def test_add_interval_clamps_to_the_shorter_month() -> None:
    jan_31 = datetime(2026, 1, 31, tzinfo=UTC)

    assert _add_interval(jan_31, "month") == datetime(2026, 2, 28, tzinfo=UTC)
    assert _add_interval(datetime(2026, 12, 15, tzinfo=UTC), "month") == datetime(
        2027, 1, 15, tzinfo=UTC
    )
    assert _add_interval(datetime(2028, 2, 29, tzinfo=UTC), "year") == datetime(
        2029, 2, 28, tzinfo=UTC
    )
