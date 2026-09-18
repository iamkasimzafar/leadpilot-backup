"""Country scoping for a lead search: validation, persistence, and dispatch.

The dispatch assertions are the point: the country has to reach the n8n webhook
payload, because that is what the workflow passes to SerpApi's `gl` parameter.
"""

from typing import Any

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.config import settings
from app.models.lead_search import LeadSearchRun
from app.schemas.lead_radar import StartSearchRequest
from app.services.countries import COUNTRIES, is_valid, name_for
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
    """An httpx client that records the webhook payload instead of sending it."""

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        sink.append(json.loads(request.content))

        return httpx.Response(200, json={"ok": True})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- The country list --------------------------------------------------------


def test_country_list_is_populated() -> None:
    assert len(COUNTRIES) > 200
    assert name_for("de") == "Germany"
    assert name_for("us") == "United States"


def test_united_kingdom_uses_the_iso_code() -> None:
    """SerpApi lists the UK twice (uk and gb); we keep the ISO 3166-1 code."""
    assert is_valid("gb")
    assert not is_valid("uk")


def test_unknown_codes_are_rejected() -> None:
    assert not is_valid("xx")
    assert not is_valid("")


# --- Request validation ------------------------------------------------------


def test_request_accepts_a_known_country() -> None:
    payload = StartSearchRequest(original_keyword="led display", country="de")

    assert payload.country == "de"


def test_request_normalises_case() -> None:
    payload = StartSearchRequest(original_keyword="led display", country="DE")

    assert payload.country == "de"


def test_request_defaults_to_no_country() -> None:
    payload = StartSearchRequest(original_keyword="led display")

    assert payload.country is None


def test_request_treats_blank_as_worldwide() -> None:
    payload = StartSearchRequest(original_keyword="led display", country="  ")

    assert payload.country is None


def test_request_rejects_an_unknown_country() -> None:
    with pytest.raises(ValueError, match="Unknown country code"):
        StartSearchRequest(original_keyword="led display", country="zz")


async def test_api_rejects_an_unknown_country(client: AsyncClient, db_session) -> None:
    headers = await _headers(client, db_session)

    response = await client.post(
        f"{PREFIX}/search",
        json={"original_keyword": "led display", "country": "zz"},
        headers=headers,
    )

    assert response.status_code == 422


# --- Dispatch to n8n ---------------------------------------------------------


async def test_country_reaches_the_webhook_payload(monkeypatch) -> None:
    monkeypatch.setattr(settings, "N8N_WEBHOOK_URL", "https://n8n.test/webhook/leads")
    sent: list[dict] = []

    async with _capturing_client(sent) as http:
        await LeadSearchService(http).start(
            "led display",
            ["led screen"],
            user_id="user-1",
            run_id="run-1",
            callback_token="token-1",
            country="de",
        )

    assert len(sent) == 1
    assert sent[0]["country"] == "de"
    assert sent[0]["country_name"] == "Germany"


async def test_worldwide_sends_null_country(monkeypatch) -> None:
    """The workflow reads null as 'no country filter'."""
    monkeypatch.setattr(settings, "N8N_WEBHOOK_URL", "https://n8n.test/webhook/leads")
    sent: list[dict] = []

    async with _capturing_client(sent) as http:
        await LeadSearchService(http).start(
            "led display",
            [],
            user_id="user-1",
            run_id="run-1",
            callback_token="token-1",
            country=None,
        )

    assert sent[0]["country"] is None
    assert sent[0]["country_name"] is None


async def test_response_echoes_the_country(monkeypatch) -> None:
    monkeypatch.setattr(settings, "N8N_WEBHOOK_URL", "https://n8n.test/webhook/leads")
    sent: list[dict] = []

    async with _capturing_client(sent) as http:
        result = await LeadSearchService(http).start(
            "led display",
            [],
            user_id="user-1",
            run_id="run-1",
            callback_token="token-1",
            country="jp",
        )

    assert result.country == "jp"
    assert result.country_name == "Japan"


# --- Persistence -------------------------------------------------------------


async def test_country_is_stored_on_the_run(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "N8N_WEBHOOK_URL", "https://n8n.test/webhook/leads")

    sent: list[dict] = []
    real_start = LeadSearchService.start

    async def _start(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        async with _capturing_client(sent) as http:
            self._client = http
            return await real_start(self, *args, **kwargs)

    monkeypatch.setattr(LeadSearchService, "start", _start)

    headers = await _headers(client, db_session)
    response = await client.post(
        f"{PREFIX}/search",
        json={"original_keyword": "led display", "country": "fr"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["country"] == "fr"

    run = (await db_session.execute(select(LeadSearchRun))).scalars().first()
    assert run.country == "fr"


async def test_worldwide_search_stores_null(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    """A search with no country must stay as broad as it was before."""
    monkeypatch.setattr(settings, "N8N_WEBHOOK_URL", "https://n8n.test/webhook/leads")

    sent: list[dict] = []
    real_start = LeadSearchService.start

    async def _start(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        async with _capturing_client(sent) as http:
            self._client = http
            return await real_start(self, *args, **kwargs)

    monkeypatch.setattr(LeadSearchService, "start", _start)

    headers = await _headers(client, db_session)
    response = await client.post(
        f"{PREFIX}/search",
        json={"original_keyword": "led display"},
        headers=headers,
    )

    assert response.status_code == 200, response.text

    run = (await db_session.execute(select(LeadSearchRun))).scalars().first()
    assert run.country is None
