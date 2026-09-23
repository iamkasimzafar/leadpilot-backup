"""HS Code search (Advanced Filters): the free AI translation step, and the
confirmed-code dispatch + billing.

The rules under test:

  - translation is free: no wallet touched, whatever the model answers
  - translation returns up to 3 candidates with a 6-digit code + description
  - a rejected description returns valid=false with a reason, no candidates
  - the confirmed search creates an hs_code run and dispatches with the
    country resolved to its ccTLD and the Snov.io extraction filters
  - a search the balance cannot cover is refused up front (402), same gate
    as the keyword flow
  - results land on the ordinary /lead-radar/results endpoint and are billed
    exactly like a b2b search (per verified-email contact)
"""

import json
from typing import Any

import httpx
import pytest
from httpx import AsyncClient

from app.core.config import settings
from app.services import hs_code as hs_code_module
from app.services import hs_code_search as hs_code_search_module
from app.services.billing_catalog import BASE_CONTACT_CREDIT
from tests.api.test_auth import signed_in_tokens
from tests.api.test_lead_results import _post_results

PREFIX = settings.API_V1_PREFIX
TRANSLATE = f"{PREFIX}/lead-radar/hs-code/translate"
SEARCH = f"{PREFIX}/lead-radar/hs-code/search"
BILLING = f"{PREFIX}/billing"

PRO_CREDITS = 4990

VALID_REPLY = {
    "valid": True,
    "candidates": [
        {"code": "852859", "description": "Other monitors and projectors"},
        {"code": "854231", "description": "Electronic integrated circuits"},
    ],
}

INVALID_REPLY = {
    "valid": False,
    "reason": "That doesn't look like a product description.",
}


@pytest.fixture(autouse=True)
def _configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(settings, "N8N_HS_CODE_WEBHOOK_URL", "https://n8n.test/webhook/hs")
    monkeypatch.setattr(settings, "PUBLIC_API_URL", "https://api.leadpilot.test")


def _mock_deepseek(
    monkeypatch: pytest.MonkeyPatch, *, status: int = 200, reply: Any = None
) -> dict[str, int]:
    counter = {"calls": 0}
    body: Any = (
        {"choices": [{"message": {"content": json.dumps(reply)}}]}
        if reply is not None
        else {"error": "upstream is down"}
    )

    def handler(request: httpx.Request) -> httpx.Response:
        counter["calls"] += 1
        return httpx.Response(status, json=body)

    original_init = hs_code_module.HsCodeService.__init__

    def patched_init(self: Any, client: httpx.AsyncClient | None = None) -> None:
        original_init(self, httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    monkeypatch.setattr(hs_code_module.HsCodeService, "__init__", patched_init)

    return counter


_REAL_SEARCH_INIT = hs_code_search_module.HsCodeSearchService.__init__


def _mock_n8n(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    def patched_init(self: Any, client: httpx.AsyncClient | None = None) -> None:
        _REAL_SEARCH_INIT(self, httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    monkeypatch.setattr(
        hs_code_search_module.HsCodeSearchService, "__init__", patched_init
    )

    return captured


async def _headers(client: AsyncClient, db_session: Any) -> dict[str, str]:
    tokens = await signed_in_tokens(client, db_session)
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def _fund(client: AsyncClient, headers: dict[str, str]) -> None:
    response = await client.post(
        f"{BILLING}/subscriptions", json={"plan_code": "pro_yearly"}, headers=headers
    )
    assert response.status_code == 201


async def _balance(client: AsyncClient, headers: dict[str, str]) -> int:
    response = await client.get(f"{BILLING}/overview", headers=headers)
    assert response.status_code == 200
    return int(response.json()["balance"])


async def _run(client: AsyncClient, headers: dict[str, str], run_id: str) -> dict:
    response = await client.get(f"{PREFIX}/lead-radar/runs/{run_id}", headers=headers)
    assert response.status_code == 200
    return response.json()


# --- Phase 1: translation ------------------------------------------------------


async def test_translate_returns_candidates(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    _mock_deepseek(monkeypatch, reply=VALID_REPLY)
    headers = await _headers(client, db_session)

    response = await client.post(
        TRANSLATE, json={"text": "LED screen"}, headers=headers
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["valid"] is True
    assert len(body["candidates"]) == 2
    assert body["candidates"][0] == {
        "code": "852859",
        "description": "Other monitors and projectors",
    }


async def test_translation_is_free(client: AsyncClient, db_session, monkeypatch) -> None:
    """Free even for an unfunded account: nothing is charged at this step."""
    _mock_deepseek(monkeypatch, reply=VALID_REPLY)
    headers = await _headers(client, db_session)

    response = await client.post(
        TRANSLATE, json={"text": "LED screen"}, headers=headers
    )

    assert response.status_code == 200
    assert await _balance(client, headers) == 0


async def test_translation_is_free_even_when_funded(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    _mock_deepseek(monkeypatch, reply=VALID_REPLY)
    headers = await _headers(client, db_session)
    await _fund(client, headers)

    await client.post(TRANSLATE, json={"text": "LED screen"}, headers=headers)

    assert await _balance(client, headers) == PRO_CREDITS


async def test_a_rejected_description_has_no_candidates(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    _mock_deepseek(monkeypatch, reply=INVALID_REPLY)
    headers = await _headers(client, db_session)

    response = await client.post(
        TRANSLATE, json={"text": "asdfghjkl"}, headers=headers
    )

    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert body["candidates"] == []
    assert body["reason"]


async def test_translate_requires_auth(client: AsyncClient) -> None:
    response = await client.post(TRANSLATE, json={"text": "LED screen"})
    assert response.status_code == 401


async def test_translate_is_refused_without_a_configured_key(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "")
    headers = await _headers(client, db_session)

    response = await client.post(
        TRANSLATE, json={"text": "LED screen"}, headers=headers
    )

    assert response.status_code == 503


# --- Phase 2: confirmed search ---------------------------------------------------


async def test_search_dispatches_with_resolved_country_and_filters(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    headers = await _headers(client, db_session)
    await _fund(client, headers)

    captured = _mock_n8n(monkeypatch)
    response = await client.post(
        SEARCH,
        json={
            "hs_code": "852859",
            "hs_description": "Other monitors and projectors",
            "country": "gb",
            "contact_role": "procurement",
            "company_size": "11_50",
            "result_limit": 100,
        },
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dispatched"] is True
    assert body["hs_code"] == "852859"
    assert body["country"] == "gb"

    assert captured["hs_code"] == "852859"
    # gb (SerpApi's code) resolves to the real ccTLD, .uk.
    assert captured["country_tld"] == "uk"
    assert captured["contact_role_titles"]
    assert captured["company_size_min"] == 11
    assert captured["company_size_max"] == 50
    assert captured["result_limit"] == 100
    assert captured["results_url"].endswith("/lead-radar/results")

    run = await _run(client, headers, body["run_id"])
    assert run["search_type"] == "hs_code"


async def test_worldwide_search_has_no_tld(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    headers = await _headers(client, db_session)
    await _fund(client, headers)

    captured = _mock_n8n(monkeypatch)
    response = await client.post(
        SEARCH, json={"hs_code": "852859"}, headers=headers
    )

    assert response.status_code == 200, response.text
    assert captured["country"] is None
    assert captured["country_tld"] is None


async def test_search_rejects_a_non_6_digit_code(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    headers = await _headers(client, db_session)
    await _fund(client, headers)

    response = await client.post(
        SEARCH, json={"hs_code": "8528"}, headers=headers
    )

    assert response.status_code == 422


async def test_search_the_balance_cannot_cover_is_refused(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    headers = await _headers(client, db_session)  # unfunded
    _mock_n8n(monkeypatch)

    response = await client.post(
        SEARCH, json={"hs_code": "852859"}, headers=headers
    )

    assert response.status_code == 402
    error = response.json()["error"]
    assert error["code"] == "insufficient_credits"
    # Refused means refused: no run row opened.
    runs = await client.get(
        f"{PREFIX}/lead-radar/runs", headers=headers, params={"page": 1, "per_page": 10}
    )
    assert runs.json()["total"] == 0


async def test_search_is_refused_without_a_configured_webhook(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "N8N_HS_CODE_WEBHOOK_URL", "")
    headers = await _headers(client, db_session)
    await _fund(client, headers)

    response = await client.post(
        SEARCH, json={"hs_code": "852859"}, headers=headers
    )

    assert response.status_code == 503


async def test_search_requires_auth(client: AsyncClient) -> None:
    response = await client.post(SEARCH, json={"hs_code": "852859"})
    assert response.status_code == 401


# --- Results (reuses the ordinary results endpoint) -----------------------------


async def test_results_are_billed_like_a_b2b_search(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    headers = await _headers(client, db_session)
    await _fund(client, headers)

    captured = _mock_n8n(monkeypatch)
    started = await client.post(
        SEARCH, json={"hs_code": "852859"}, headers=headers
    )
    assert started.status_code == 200
    run_id = started.json()["run_id"]

    companies = [
        {
            "company_name": "Buyer Co",
            "website": "buyer.example.com",
            "decision_makers": [
                {
                    "full_name": "Pat Buyer",
                    "verified_email": "pat@buyer.example.com",
                    "email_status": "valid",
                }
            ],
        }
    ]
    response = await _post_results(
        client, run_id, captured["progress_token"], companies=companies
    )
    assert response.status_code == 201, response.text

    assert await _balance(client, headers) == PRO_CREDITS - BASE_CONTACT_CREDIT

    run = await _run(client, headers, run_id)
    assert run["credits_charged"] == BASE_CONTACT_CREDIT
    assert run["companies_found"] == 1
