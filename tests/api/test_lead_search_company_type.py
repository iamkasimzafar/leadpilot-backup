"""Company-type targeting: validation, the DeepSeek prompt, and dispatch.

Two things matter here. The type must reach the n8n webhook payload, and it must
reach the DeepSeek prompt -- the second is the whole point of the feature, since
a distributor and an installer search for the same product in different words.
"""

import json
from typing import Any

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.config import settings
from app.models.lead_search import LeadSearchRun
from app.schemas.lead_radar import ExpandKeywordRequest, StartSearchRequest
from app.services.company_types import COMPANY_TYPES, is_valid, name_for, prompt_hint_for
from app.services.keyword_expansion import KeywordExpansionService
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


def _deepseek_client(sink: list[dict], reply: dict) -> httpx.AsyncClient:
    """Stands in for DeepSeek, recording the request body it was sent."""

    def handler(request: httpx.Request) -> httpx.Response:
        sink.append(json.loads(request.content))

        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(reply)}}]},
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


VALID_REPLY = {
    "valid": True,
    "normalized_keyword": "LED screen",
    "reason": None,
    "suggestions": [],
    "synonyms": ["LED display", "Video wall"],
    "scenarios": ["Stadium display"],
    "translations": [{"lang": "ES", "term": "Pantalla LED"}],
}


# --- The catalogue -----------------------------------------------------------


def test_the_four_types_are_defined() -> None:
    assert set(COMPANY_TYPES) == {"distributor", "brand_oem", "retailer", "installer"}


def test_every_type_has_a_name_and_a_prompt_hint() -> None:
    for code in COMPANY_TYPES:
        assert name_for(code)
        assert prompt_hint_for(code)


def test_unknown_types_are_rejected() -> None:
    assert not is_valid("manufacturer")
    assert not is_valid("")
    assert prompt_hint_for("nope") is None


# --- Request validation ------------------------------------------------------


def test_search_request_accepts_a_known_type() -> None:
    payload = StartSearchRequest(original_keyword="led screen", company_type="retailer")

    assert payload.company_type == "retailer"


def test_search_request_normalises_case() -> None:
    payload = StartSearchRequest(original_keyword="led screen", company_type="RETAILER")

    assert payload.company_type == "retailer"


def test_search_request_defaults_to_any_type() -> None:
    assert StartSearchRequest(original_keyword="led screen").company_type is None


def test_search_request_rejects_an_unknown_type() -> None:
    with pytest.raises(ValueError, match="Unknown company type"):
        StartSearchRequest(original_keyword="led screen", company_type="farmer")


def test_expand_request_validates_the_type_too() -> None:
    assert ExpandKeywordRequest(keyword="led screen").company_type is None
    assert (
        ExpandKeywordRequest(keyword="led screen", company_type="brand_oem").company_type
        == "brand_oem"
    )

    with pytest.raises(ValueError, match="Unknown company type"):
        ExpandKeywordRequest(keyword="led screen", company_type="farmer")


async def test_api_rejects_an_unknown_type(client: AsyncClient, db_session) -> None:
    headers = await _headers(client, db_session)

    response = await client.post(
        f"{PREFIX}/search",
        json={"original_keyword": "led screen", "company_type": "farmer"},
        headers=headers,
    )

    assert response.status_code == 422


# --- The DeepSeek prompt (the technical requirement) -------------------------


async def test_company_type_reaches_the_deepseek_prompt(monkeypatch) -> None:
    monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "test-key")
    sent: list[dict] = []

    async with _deepseek_client(sent, VALID_REPLY) as http:
        await KeywordExpansionService(http).expand("led screen", "distributor")

    user_message = sent[0]["messages"][1]["content"]

    assert "Target company type" in user_message
    assert "Distributor / Wholesaler" in user_message
    # The description, not just the code: the model needs to know what the
    # buyer actually is to write terms they would search for.
    assert "wholesalers" in user_message


async def test_each_type_sends_its_own_description(monkeypatch) -> None:
    monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "test-key")

    for code in COMPANY_TYPES:
        sent: list[dict] = []
        async with _deepseek_client(sent, VALID_REPLY) as http:
            await KeywordExpansionService(http).expand("led screen", code)

        message = sent[0]["messages"][1]["content"]
        assert name_for(code) in message
        assert prompt_hint_for(code) in message


async def test_no_company_type_leaves_the_prompt_unchanged(monkeypatch) -> None:
    monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "test-key")
    sent: list[dict] = []

    async with _deepseek_client(sent, VALID_REPLY) as http:
        await KeywordExpansionService(http).expand("led screen")

    user_message = sent[0]["messages"][1]["content"]

    assert user_message == "Keyword: led screen"
    assert "Target company type" not in user_message


async def test_system_prompt_explains_how_to_use_the_target(monkeypatch) -> None:
    """The instruction has to exist, or the extra line means nothing."""
    monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "test-key")
    sent: list[dict] = []

    async with _deepseek_client(sent, VALID_REPLY) as http:
        await KeywordExpansionService(http).expand("led screen", "retailer")

    system_message = sent[0]["messages"][0]["content"]

    assert "TARGET BUYER" in system_message


async def test_expansion_still_works_with_a_type(monkeypatch) -> None:
    monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "test-key")
    sent: list[dict] = []

    async with _deepseek_client(sent, VALID_REPLY) as http:
        result = await KeywordExpansionService(http).expand("led screen", "installer")

    assert result.valid
    assert [term.term for term in result.terms][:2] == ["LED display", "Video wall"]


# --- Dispatch to n8n ---------------------------------------------------------


async def test_company_type_reaches_the_webhook(monkeypatch) -> None:
    monkeypatch.setattr(settings, "N8N_WEBHOOK_URL", "https://n8n.test/webhook/leads")
    sent: list[dict] = []

    async with _capturing_client(sent) as http:
        await LeadSearchService(http).start(
            "led screen",
            [],
            user_id="user-1",
            run_id="run-1",
            callback_token="token-1",
            company_type="brand_oem",
        )

    assert sent[0]["company_type"] == "brand_oem"
    assert sent[0]["company_type_name"] == "Brand / OEM"


async def test_any_type_sends_null(monkeypatch) -> None:
    monkeypatch.setattr(settings, "N8N_WEBHOOK_URL", "https://n8n.test/webhook/leads")
    sent: list[dict] = []

    async with _capturing_client(sent) as http:
        await LeadSearchService(http).start(
            "led screen",
            [],
            user_id="user-1",
            run_id="run-1",
            callback_token="token-1",
        )

    assert sent[0]["company_type"] is None
    assert sent[0]["company_type_name"] is None


async def test_country_and_type_travel_together(monkeypatch) -> None:
    monkeypatch.setattr(settings, "N8N_WEBHOOK_URL", "https://n8n.test/webhook/leads")
    sent: list[dict] = []

    async with _capturing_client(sent) as http:
        result = await LeadSearchService(http).start(
            "led screen",
            [],
            user_id="user-1",
            run_id="run-1",
            callback_token="token-1",
            country="de",
            company_type="installer",
        )

    assert sent[0]["country"] == "de"
    assert sent[0]["company_type"] == "installer"
    assert result.company_type_name == "Service / Installer"


# --- Persistence -------------------------------------------------------------


async def test_company_type_is_stored_on_the_run(
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
        json={"original_keyword": "led screen", "company_type": "distributor"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["company_type"] == "distributor"
    assert response.json()["company_type_name"] == "Distributor / Wholesaler"

    run = (await db_session.execute(select(LeadSearchRun))).scalars().first()
    assert run.company_type == "distributor"


async def test_any_type_stores_null(
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
        json={"original_keyword": "led screen"},
        headers=headers,
    )

    assert response.status_code == 200, response.text

    run = (await db_session.execute(select(LeadSearchRun))).scalars().first()
    assert run.company_type is None
