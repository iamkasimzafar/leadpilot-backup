"""Contact-role and company-size filters.

These decide what Snov.io extracts, which is what the run is charged for, so the
resolved filter values (job titles, employee bounds) have to reach the workflow
payload -- a code alone would leave n8n to invent its own mapping.
"""

import json
from typing import Any

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.config import settings
from app.models.lead_search import LeadSearchRun
from app.schemas.lead_radar import StartSearchRequest
from app.services.lead_search import LeadSearchService
from app.services.search_targeting import (
    COMPANY_SIZES,
    CONTACT_ROLES,
    MAX_ROLE_TITLES,
    is_valid_role,
    is_valid_size,
    role_titles_for,
    size_bounds_for,
)
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
    """Run a dispatch against a mock webhook, returning (payload, response)."""
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


# --- The catalogues ----------------------------------------------------------


def test_the_five_roles_are_defined() -> None:
    """"Any" is not a role: it is the absence of a filter."""
    assert set(CONTACT_ROLES) == {
        "c_level",
        "procurement",
        "marketing",
        "sales",
        "engineering",
    }
    assert not is_valid_role("any")


def test_the_four_size_bands_are_defined() -> None:
    assert set(COMPANY_SIZES) == {"1_10", "11_50", "51_200", "201_plus"}


def test_every_role_carries_job_titles() -> None:
    for code in CONTACT_ROLES:
        titles = role_titles_for(code)
        assert titles and len(titles) >= 3


def test_no_role_exceeds_the_n8n_title_limit() -> None:
    """The n8n extraction filter cannot handle more than ten titles at once."""
    assert MAX_ROLE_TITLES < 10

    for code in CONTACT_ROLES:
        titles = role_titles_for(code) or []
        assert len(titles) <= MAX_ROLE_TITLES, f"{code} sends {len(titles)} titles"


def test_titles_are_unique_within_a_role() -> None:
    """A duplicate would waste one of the few slots available."""
    for code in CONTACT_ROLES:
        titles = role_titles_for(code) or []
        assert len(titles) == len(set(titles)), f"{code} repeats a title"


def test_c_level_covers_owner_titles() -> None:
    """The titles that actually buy survived the trim to fit the n8n cap."""
    titles = role_titles_for("c_level") or []

    assert "CEO" in titles
    assert "Owner" in titles
    assert "Founder" in titles
    assert "Managing Director" in titles


def test_procurement_covers_buyer_titles() -> None:
    titles = role_titles_for("procurement") or []

    assert "Buyer" in titles
    assert any("Procurement" in title for title in titles)


def test_size_bands_are_contiguous_and_open_ended() -> None:
    assert size_bounds_for("1_10") == (1, 10)
    assert size_bounds_for("11_50") == (11, 50)
    assert size_bounds_for("51_200") == (51, 200)
    # 200+ has no upper bound.
    assert size_bounds_for("201_plus") == (201, None)


def test_unknown_codes_are_rejected() -> None:
    assert not is_valid_role("intern")
    assert not is_valid_size("500_plus")
    assert role_titles_for("intern") is None
    assert size_bounds_for("500_plus") is None


# --- Request validation ------------------------------------------------------


def test_request_accepts_known_values() -> None:
    payload = StartSearchRequest(
        original_keyword="led screen", contact_role="c_level", company_size="11_50"
    )

    assert payload.contact_role == "c_level"
    assert payload.company_size == "11_50"


def test_request_defaults_to_no_filters() -> None:
    payload = StartSearchRequest(original_keyword="led screen")

    assert payload.contact_role is None
    assert payload.company_size is None


def test_any_is_treated_as_no_filter() -> None:
    """The UI sentinel must not reach the workflow as a literal filter."""
    payload = StartSearchRequest(
        original_keyword="led screen", contact_role="any", company_size="any"
    )

    assert payload.contact_role is None
    assert payload.company_size is None


def test_request_rejects_an_unknown_role() -> None:
    with pytest.raises(ValueError, match="Unknown contact role"):
        StartSearchRequest(original_keyword="led screen", contact_role="intern")


def test_request_rejects_an_unknown_size() -> None:
    with pytest.raises(ValueError, match="Unknown company size"):
        StartSearchRequest(original_keyword="led screen", company_size="500_plus")


async def test_api_rejects_unknown_values(client: AsyncClient, db_session) -> None:
    headers = await _headers(client, db_session)

    for body in (
        {"original_keyword": "led screen", "contact_role": "intern"},
        {"original_keyword": "led screen", "company_size": "500_plus"},
    ):
        response = await client.post(f"{PREFIX}/search", json=body, headers=headers)
        assert response.status_code == 422


# --- Dispatch to n8n ---------------------------------------------------------


async def test_role_reaches_the_webhook_with_its_titles(monkeypatch) -> None:
    """The titles are the actual Snov.io filter, so they must be forwarded."""
    monkeypatch.setattr(settings, "N8N_WEBHOOK_URL", "https://n8n.test/webhook/leads")

    payload, result = await _dispatch(contact_role="procurement")

    assert payload["contact_role"] == "procurement"
    assert payload["contact_role_name"] == "Procurement / Buyer"
    assert "Buyer" in payload["contact_role_titles"]
    assert result.contact_role_name == "Procurement / Buyer"


async def test_size_reaches_the_webhook_with_its_bounds(monkeypatch) -> None:
    monkeypatch.setattr(settings, "N8N_WEBHOOK_URL", "https://n8n.test/webhook/leads")

    payload, result = await _dispatch(company_size="51_200")

    assert payload["company_size"] == "51_200"
    assert payload["company_size_min"] == 51
    assert payload["company_size_max"] == 200
    assert result.company_size_name == "51-200 employees"


async def test_open_ended_band_sends_a_null_maximum(monkeypatch) -> None:
    monkeypatch.setattr(settings, "N8N_WEBHOOK_URL", "https://n8n.test/webhook/leads")

    payload, _ = await _dispatch(company_size="201_plus")

    assert payload["company_size_min"] == 201
    assert payload["company_size_max"] is None


async def test_no_filters_send_nulls(monkeypatch) -> None:
    """Unfiltered must stay unfiltered: extract every decision-maker."""
    monkeypatch.setattr(settings, "N8N_WEBHOOK_URL", "https://n8n.test/webhook/leads")

    payload, _ = await _dispatch()

    for key in (
        "contact_role",
        "contact_role_name",
        "contact_role_titles",
        "company_size",
        "company_size_name",
        "company_size_min",
        "company_size_max",
    ):
        assert payload[key] is None, key


async def test_every_targeting_field_travels_together(monkeypatch) -> None:
    monkeypatch.setattr(settings, "N8N_WEBHOOK_URL", "https://n8n.test/webhook/leads")

    payload, _ = await _dispatch(
        country="de",
        company_type="distributor",
        contact_role="c_level",
        company_size="11_50",
    )

    assert payload["country"] == "de"
    assert payload["company_type"] == "distributor"
    assert payload["contact_role"] == "c_level"
    assert payload["company_size"] == "11_50"
    assert payload["company_size_min"] == 11


# --- Persistence -------------------------------------------------------------


async def test_filters_are_stored_on_the_run(
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
        json={
            "original_keyword": "led screen",
            "contact_role": "marketing",
            "company_size": "1_10",
        },
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["contact_role"] == "marketing"
    assert response.json()["company_size_name"] == "1-10 employees"

    run = (await db_session.execute(select(LeadSearchRun))).scalars().first()
    assert run.contact_role == "marketing"
    assert run.company_size == "1_10"


async def test_unfiltered_run_stores_nulls(
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
        f"{PREFIX}/search", json={"original_keyword": "led screen"}, headers=headers
    )

    assert response.status_code == 200, response.text

    run = (await db_session.execute(select(LeadSearchRun))).scalars().first()
    assert run.contact_role is None
    assert run.company_size is None
