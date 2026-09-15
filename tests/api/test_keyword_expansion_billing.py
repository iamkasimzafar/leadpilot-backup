"""The credit charge attached to the AI keyword-expansion endpoint.

These drive the real HTTP route with DeepSeek's transport mocked, so they cover
the thing the service-level tests cannot: who pays, and when.
"""

import json
from typing import Any

import httpx
import pytest
from httpx import AsyncClient

from app.core.config import settings
from app.services import keyword_expansion as keyword_expansion_module
from app.services.billing_catalog import AI_KEYWORD_EXPANSION_CREDITS
from tests.api.test_auth import signed_in_tokens

PREFIX = settings.API_V1_PREFIX
EXPAND = f"{PREFIX}/lead-radar/expand"
BILLING = f"{PREFIX}/billing"

VALID_REPLY = {
    "valid": True,
    "normalized_keyword": "LED screen",
    "synonyms": ["Digital signage", "LED display"],
    "scenarios": ["Stadium display"],
    "translations": [{"lang": "ES", "term": "Pantalla LED"}],
}

INVALID_REPLY = {
    "valid": False,
    "reason": "That looks like random characters, not a product.",
    "suggestions": ["LED screen", "Solar panel", "Hydraulic pump", "Steel pipe"],
}


@pytest.fixture(autouse=True)
def _api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "test-key")


def _mock_deepseek(
    monkeypatch: pytest.MonkeyPatch, *, status: int = 200, reply: Any = None
) -> dict[str, int]:
    """Point KeywordExpansionService at a fake DeepSeek.

    Returns a dict whose "calls" entry counts how many times it was hit, so a
    test can assert the API was never reached.
    """
    counter = {"calls": 0}
    body: Any = (
        {"choices": [{"message": {"content": json.dumps(reply)}}]}
        if reply is not None
        else {"error": "upstream is down"}
    )

    def handler(request: httpx.Request) -> httpx.Response:
        counter["calls"] += 1
        return httpx.Response(status, json=body)

    original_init = keyword_expansion_module.KeywordExpansionService.__init__

    def patched_init(self: Any, client: httpx.AsyncClient | None = None) -> None:
        original_init(self, httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    monkeypatch.setattr(
        keyword_expansion_module.KeywordExpansionService, "__init__", patched_init
    )

    return counter


async def _headers(client: AsyncClient, db_session: Any) -> dict[str, str]:
    tokens = await signed_in_tokens(client, db_session)
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def _subscribe(client: AsyncClient, headers: dict[str, str]) -> None:
    """Starter plan: $49 at 10 credits per dollar = 490, and the only way to
    have any balance."""
    response = await client.post(
        f"{BILLING}/subscriptions", json={"plan_code": "starter_monthly"}, headers=headers
    )
    assert response.status_code == 201


async def _balance(client: AsyncClient, headers: dict[str, str]) -> int:
    response = await client.get(f"{BILLING}/overview", headers=headers)
    assert response.status_code == 200
    return int(response.json()["balance"])


# --- Charging ----------------------------------------------------------------


async def test_successful_expansion_charges_five_credits(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_deepseek(monkeypatch, reply=VALID_REPLY)
    headers = await _headers(client, db_session)
    await _subscribe(client, headers)

    response = await client.post(EXPAND, json={"keyword": "LED screen"}, headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is True
    assert body["credits_charged"] == AI_KEYWORD_EXPANSION_CREDITS == 5
    assert body["balance_after"] == 485
    assert await _balance(client, headers) == 485


async def test_each_expansion_charges_again(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_deepseek(monkeypatch, reply=VALID_REPLY)
    headers = await _headers(client, db_session)
    await _subscribe(client, headers)

    for expected in (485, 480, 475):
        response = await client.post(
            EXPAND, json={"keyword": "LED screen"}, headers=headers
        )
        assert response.json()["balance_after"] == expected

    assert await _balance(client, headers) == 475


async def test_the_charge_appears_in_credit_history(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_deepseek(monkeypatch, reply=VALID_REPLY)
    headers = await _headers(client, db_session)
    await _subscribe(client, headers)
    await client.post(EXPAND, json={"keyword": "LED screen"}, headers=headers)

    response = await client.get(f"{BILLING}/transactions", headers=headers)

    newest = response.json()["items"][0]
    assert newest["kind"] == "usage"
    assert newest["amount"] == -5
    assert newest["balance_after"] == 485
    assert "AI keyword expansion" in newest["description"]
    assert "LED screen" in newest["description"]


# --- When nothing is charged -------------------------------------------------


async def test_a_rejected_keyword_is_not_charged(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_deepseek(monkeypatch, reply=INVALID_REPLY)
    headers = await _headers(client, db_session)
    await _subscribe(client, headers)

    response = await client.post(EXPAND, json={"keyword": "asdfghjkl"}, headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert body["credits_charged"] == 0
    assert body["balance_after"] is None
    assert await _balance(client, headers) == 490


async def test_an_upstream_failure_is_not_charged(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_deepseek(monkeypatch, status=500)
    headers = await _headers(client, db_session)
    await _subscribe(client, headers)

    response = await client.post(EXPAND, json={"keyword": "LED screen"}, headers=headers)

    assert response.status_code == 502
    assert await _balance(client, headers) == 490


async def test_input_without_letters_is_not_charged(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The local guard answers before any API call, so there is nothing to bill."""
    calls = _mock_deepseek(monkeypatch, reply=VALID_REPLY)
    headers = await _headers(client, db_session)
    await _subscribe(client, headers)

    response = await client.post(EXPAND, json={"keyword": "12345 !!!"}, headers=headers)

    assert response.status_code == 200
    assert response.json()["valid"] is False
    assert calls["calls"] == 0
    assert await _balance(client, headers) == 490


# --- Not enough credits ------------------------------------------------------


async def test_expansion_is_refused_with_an_empty_wallet(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No subscription means no credits: 402 before DeepSeek is ever called."""
    calls = _mock_deepseek(monkeypatch, reply=VALID_REPLY)
    headers = await _headers(client, db_session)

    response = await client.post(EXPAND, json={"keyword": "LED screen"}, headers=headers)

    assert response.status_code == 402
    body = response.json()
    assert body["error"]["code"] == "insufficient_credits"
    assert body["error"]["details"] == {"balance": 0, "required": 5}
    assert calls["calls"] == 0


async def test_expansion_is_refused_when_the_balance_is_short(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _mock_deepseek(monkeypatch, reply=VALID_REPLY)
    headers = await _headers(client, db_session)
    await _subscribe(client, headers)

    # Spend down to 3 credits — two short of the 5 an expansion costs.
    from sqlalchemy import select

    from app.models.billing import Subscription
    from app.services.billing import BillingService

    subscription = (await db_session.execute(select(Subscription))).scalar_one()
    await BillingService(db_session).spend(subscription.user_id, 487, "Test drain")
    assert await _balance(client, headers) == 3

    response = await client.post(EXPAND, json={"keyword": "LED screen"}, headers=headers)

    assert response.status_code == 402
    assert response.json()["error"]["details"] == {"balance": 3, "required": 5}
    assert calls["calls"] == 0
    assert await _balance(client, headers) == 3


async def test_expansion_requires_auth(client: AsyncClient) -> None:
    response = await client.post(EXPAND, json={"keyword": "LED screen"})

    assert response.status_code == 401
