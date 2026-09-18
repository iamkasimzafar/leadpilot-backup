"""WhatsApp validation: the opt-in flag and its per-check price.

The flag has to reach the workflow, and it has to default to off: the check is
billed per number, so a run that did not ask for it must never trigger one.
"""

import json
from typing import Any

import httpx
from httpx import AsyncClient
from sqlalchemy import select

from app.core.config import settings
from app.models.lead_search import LeadSearchRun
from app.schemas.lead_radar import StartSearchRequest
from app.services.billing_catalog import WHATSAPP_VALIDATION_CREDITS
from app.services.lead_search import LeadSearchService
from tests.api.test_auth import signed_in_tokens

PREFIX = f"{settings.API_V1_PREFIX}/lead-radar"


async def _headers(client: AsyncClient, db_session: Any) -> dict[str, str]:
    """Sign in and fund the wallet.

    The start gate refuses a search the balance could not cover, so an
    unfunded account gets 402 before any of these assertions are reached.
    Pro yearly grants 4,990 credits, more than any run here needs.
    """
    tokens = await signed_in_tokens(client, db_session)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    response = await client.post(
        f"{settings.API_V1_PREFIX}/billing/subscriptions",
        json={"plan_code": "pro_yearly"},
        headers=headers,
    )
    assert response.status_code == 201

    return headers


def _capturing_client(sink: list[dict]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        sink.append(json.loads(request.content))

        return httpx.Response(200, json={"ok": True})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _dispatch(**kwargs: Any) -> tuple[dict, Any]:
    sent: list[dict] = []
    async with _capturing_client(sent) as http:
        result = await LeadSearchService(http).start(
            "led screen",
            [],
            user_id="user-1",
            run_id="run-1",
            callback_token="token-1",
            **kwargs,
        )

    return sent[0], result


def _patched_start(monkeypatch, sink: list[dict]) -> None:
    """Route the real dispatch through a mock webhook."""
    real_start = LeadSearchService.start

    async def _start(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        async with _capturing_client(sink) as http:
            self._client = http
            return await real_start(self, *args, **kwargs)

    monkeypatch.setattr(LeadSearchService, "start", _start)


# --- The price ---------------------------------------------------------------


def test_a_check_costs_eight_credits() -> None:
    """The n8n workflow bills per lookup at this rate."""
    assert WHATSAPP_VALIDATION_CREDITS == 8


# --- Request validation ------------------------------------------------------


def test_validation_is_off_by_default() -> None:
    """Opt-in: a charged check must never happen without being asked for."""
    assert StartSearchRequest(original_keyword="led screen").validate_whatsapp is False


def test_validation_can_be_requested() -> None:
    payload = StartSearchRequest(original_keyword="led screen", validate_whatsapp=True)

    assert payload.validate_whatsapp is True


# --- Dispatch to n8n ---------------------------------------------------------


async def test_flag_and_price_reach_the_webhook(monkeypatch) -> None:
    monkeypatch.setattr(settings, "N8N_WEBHOOK_URL", "https://n8n.test/webhook/leads")

    payload, result = await _dispatch(validate_whatsapp=True)

    assert payload["validate_whatsapp"] is True
    # The price travels with the flag so the workflow can report the charge.
    assert payload["whatsapp_credits_per_check"] == 8
    assert result.validate_whatsapp is True
    assert result.whatsapp_credits_per_check == 8


async def test_opting_out_sends_false_and_no_price(monkeypatch) -> None:
    """No price means the workflow has nothing to bill against."""
    monkeypatch.setattr(settings, "N8N_WEBHOOK_URL", "https://n8n.test/webhook/leads")

    payload, result = await _dispatch()

    assert payload["validate_whatsapp"] is False
    assert payload["whatsapp_credits_per_check"] is None
    assert result.validate_whatsapp is False
    assert result.whatsapp_credits_per_check is None


async def test_whatsapp_travels_with_the_other_targeting(monkeypatch) -> None:
    monkeypatch.setattr(settings, "N8N_WEBHOOK_URL", "https://n8n.test/webhook/leads")

    payload, _ = await _dispatch(
        country="de",
        company_type="distributor",
        contact_role="c_level",
        company_size="11_50",
        validate_whatsapp=True,
    )

    assert payload["country"] == "de"
    assert payload["contact_role"] == "c_level"
    assert payload["validate_whatsapp"] is True
    assert payload["whatsapp_credits_per_check"] == 8


# --- Persistence -------------------------------------------------------------


async def test_flag_is_stored_on_the_run(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "N8N_WEBHOOK_URL", "https://n8n.test/webhook/leads")
    sent: list[dict] = []
    _patched_start(monkeypatch, sent)

    headers = await _headers(client, db_session)
    response = await client.post(
        f"{PREFIX}/search",
        json={"original_keyword": "led screen", "validate_whatsapp": True},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["validate_whatsapp"] is True
    assert response.json()["whatsapp_credits_per_check"] == 8

    run = (await db_session.execute(select(LeadSearchRun))).scalars().first()
    assert run.validate_whatsapp is True


async def test_a_run_without_the_flag_stores_false(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "N8N_WEBHOOK_URL", "https://n8n.test/webhook/leads")
    sent: list[dict] = []
    _patched_start(monkeypatch, sent)

    headers = await _headers(client, db_session)
    response = await client.post(
        f"{PREFIX}/search",
        json={"original_keyword": "led screen"},
        headers=headers,
    )

    assert response.status_code == 200, response.text

    run = (await db_session.execute(select(LeadSearchRun))).scalars().first()
    assert run.validate_whatsapp is False
