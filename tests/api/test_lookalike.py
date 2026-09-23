"""Lookalike Companies: discovery (step 1) and find-contacts (step 2).

The rules under test:

  - discovery dispatches to its own webhook, creates a lookalike_discovery
    run, and charges nothing until results land
  - discovery results are stored on the run and billed a flat fee, once,
    regardless of how many domains were found
  - a failed discovery costs nothing and stores no domains
  - contacts dispatches to a different webhook, seeded with exactly the
    domains the user selected, and links back to the discovery run
  - contacts results land on the ordinary /lead-radar/results endpoint and
    are billed exactly like a b2b search (per verified-email contact)
"""

import json
from typing import Any

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.config import settings
from app.models.lead_search import LeadSearchRun
from app.services import lookalike as lookalike_module
from app.services.billing_catalog import BASE_CONTACT_CREDIT, LOOKALIKE_DISCOVERY_CREDITS
from tests.api.test_auth import signed_in_tokens
from tests.api.test_lead_results import _post_results

PREFIX = settings.API_V1_PREFIX
DISCOVER = f"{PREFIX}/lead-radar/lookalike/discover"
DISCOVER_RESULTS = f"{PREFIX}/lead-radar/lookalike/discover/results"
CONTACTS = f"{PREFIX}/lead-radar/lookalike/contacts"
BILLING = f"{PREFIX}/billing"

PRO_CREDITS = 4990


@pytest.fixture(autouse=True)
def _webhooks_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings,
        "N8N_LOOKALIKE_DISCOVERY_WEBHOOK_URL",
        "https://n8n.test/webhook/lookalike-d",
    )
    monkeypatch.setattr(
        settings,
        "N8N_LOOKALIKE_CONTACTS_WEBHOOK_URL",
        "https://n8n.test/webhook/lookalike-c",
    )
    monkeypatch.setattr(settings, "PUBLIC_API_URL", "https://api.leadpilot.test")


# Captured once, true and unpatched, so a test that calls _mock_n8n twice
# (discovery, then contacts) always re-patches from the real __init__ rather
# than from whatever the previous _mock_n8n call left behind -- otherwise the
# second mock's client never actually gets used.
_REAL_INIT = lookalike_module.LookalikeService.__init__


def _mock_n8n(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Point LookalikeService at a fake n8n and return the dict it fills in."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    def patched_init(self: Any, db: Any, client: httpx.AsyncClient | None = None) -> None:
        _REAL_INIT(self, db, httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    monkeypatch.setattr(lookalike_module.LookalikeService, "__init__", patched_init)

    return captured


async def _headers(client: AsyncClient, db_session: Any) -> dict[str, str]:
    tokens = await signed_in_tokens(client, db_session)
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def _fund(
    client: AsyncClient, headers: dict[str, str], plan: str = "pro_yearly"
) -> None:
    response = await client.post(
        f"{BILLING}/subscriptions", json={"plan_code": plan}, headers=headers
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


async def _db_run(db_session: Any, run_id: str) -> LeadSearchRun:
    db_session.expire_all()
    return (
        await db_session.execute(select(LeadSearchRun).where(LeadSearchRun.id == run_id))
    ).scalar_one()


async def _discover(
    client: AsyncClient,
    headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    *,
    domain: str,
) -> tuple[str, str, dict]:
    """Dispatch discovery; returns (run_id, progress_token, captured n8n payload)."""
    captured = _mock_n8n(monkeypatch)
    response = await client.post(DISCOVER, json={"domain": domain}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["run_id"], captured["progress_token"], captured


def _domains(count: int) -> list[dict]:
    return [
        {
            "domain": f"similar-{i}.example.com",
            "url": f"https://similar-{i}.example.com",
            "title": f"Similar Co {i}",
            "snippet": "A similar company.",
        }
        for i in range(count)
    ]


# --- Discovery: dispatch -----------------------------------------------------


async def test_discovery_dispatches_and_creates_a_run(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    headers = await _headers(client, db_session)

    run_id, token, payload = await _discover(
        client, headers, monkeypatch, domain="competitor.com"
    )

    assert payload["domain"] == "competitor.com"
    assert payload["run_id"] == run_id
    assert payload["progress_token"] == token
    assert payload["results_url"].endswith("/lead-radar/lookalike/discover/results")

    run = await _run(client, headers, run_id)
    assert run["search_type"] == "lookalike_discovery"
    assert run["status"] == "running"


async def test_domain_is_normalised_before_dispatch(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    headers = await _headers(client, db_session)

    _run_id, _token, payload = await _discover(
        client, headers, monkeypatch, domain="https://www.Competitor.com/products?x=1"
    )

    assert payload["domain"] == "competitor.com"


async def test_discovery_requires_auth(client: AsyncClient) -> None:
    response = await client.post(DISCOVER, json={"domain": "competitor.com"})
    assert response.status_code == 401


async def test_discovery_is_refused_without_a_configured_webhook(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "N8N_LOOKALIKE_DISCOVERY_WEBHOOK_URL", "")
    headers = await _headers(client, db_session)

    response = await client.post(
        DISCOVER, json={"domain": "competitor.com"}, headers=headers
    )

    assert response.status_code == 503


async def test_nothing_is_charged_at_discovery_dispatch(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    headers = await _headers(client, db_session)
    await _fund(client, headers)

    await _discover(client, headers, monkeypatch, domain="competitor.com")

    assert await _balance(client, headers) == PRO_CREDITS


# --- Discovery: results -------------------------------------------------------


async def test_discovery_results_are_stored_and_billed_flat(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    headers = await _headers(client, db_session)
    await _fund(client, headers)
    run_id, token, _ = await _discover(
        client, headers, monkeypatch, domain="competitor.com"
    )

    response = await client.post(
        DISCOVER_RESULTS,
        json={"run_id": run_id, "domains": _domains(23)},
        headers={"X-LeadPilot-Run-Token": token},
    )
    assert response.status_code == 201, response.text
    assert response.json()["domains_found"] == 23

    run = await _run(client, headers, run_id)
    assert run["status"] == "completed"
    assert len(run["discovered_domains"]) == 23
    assert run["discovered_domains"][0]["domain"] == "similar-0.example.com"
    assert run["credits_charged"] == LOOKALIKE_DISCOVERY_CREDITS

    assert await _balance(client, headers) == PRO_CREDITS - LOOKALIKE_DISCOVERY_CREDITS


async def test_discovery_charge_is_flat_regardless_of_domain_count(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    """1 domain costs the same as 50: the fee is for running the search, not
    sized by what it returned."""
    headers = await _headers(client, db_session)
    await _fund(client, headers)
    run_id, token, _ = await _discover(
        client, headers, monkeypatch, domain="competitor.com"
    )

    await client.post(
        DISCOVER_RESULTS,
        json={"run_id": run_id, "domains": _domains(1)},
        headers={"X-LeadPilot-Run-Token": token},
    )

    assert await _balance(client, headers) == PRO_CREDITS - LOOKALIKE_DISCOVERY_CREDITS


async def test_a_repeat_results_post_is_charged_once(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    headers = await _headers(client, db_session)
    await _fund(client, headers)
    run_id, token, _ = await _discover(
        client, headers, monkeypatch, domain="competitor.com"
    )

    for _ in range(3):
        response = await client.post(
            DISCOVER_RESULTS,
            json={"run_id": run_id, "domains": _domains(5)},
            headers={"X-LeadPilot-Run-Token": token},
        )
        assert response.status_code == 201

    assert await _balance(client, headers) == PRO_CREDITS - LOOKALIKE_DISCOVERY_CREDITS


async def test_a_failed_discovery_costs_nothing_and_stores_no_domains(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    headers = await _headers(client, db_session)
    await _fund(client, headers)
    run_id, token, _ = await _discover(
        client, headers, monkeypatch, domain="competitor.com"
    )

    response = await client.post(
        DISCOVER_RESULTS,
        json={"run_id": run_id, "status": "failed", "error": "could_not_analyse_site"},
        headers={"X-LeadPilot-Run-Token": token},
    )
    assert response.status_code == 201

    run = await _run(client, headers, run_id)
    assert run["status"] == "failed"
    assert run["discovered_domains"] == []
    assert run["credits_charged"] == 0
    assert await _balance(client, headers) == PRO_CREDITS


async def test_no_domains_found_is_not_charged(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    """A "completed" run that found nothing is not the same as a failure, but
    there is still nothing worth charging for."""
    headers = await _headers(client, db_session)
    await _fund(client, headers)
    run_id, token, _ = await _discover(
        client, headers, monkeypatch, domain="competitor.com"
    )

    response = await client.post(
        DISCOVER_RESULTS,
        json={"run_id": run_id, "domains": []},
        headers={"X-LeadPilot-Run-Token": token},
    )
    assert response.status_code == 201

    assert await _balance(client, headers) == PRO_CREDITS


async def test_discovery_results_require_a_valid_token(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    headers = await _headers(client, db_session)
    run_id, _token, _ = await _discover(
        client, headers, monkeypatch, domain="competitor.com"
    )

    response = await client.post(
        DISCOVER_RESULTS,
        json={"run_id": run_id, "domains": _domains(3)},
        headers={"X-LeadPilot-Run-Token": "wrong-token"},
    )

    assert response.status_code == 401


async def test_discovery_charge_is_skipped_when_balance_is_too_low(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    """The domain list is real work already done: it is kept even when the
    account cannot afford the flat fee."""
    headers = await _headers(client, db_session)  # unfunded: balance 0
    run_id, token, _ = await _discover(
        client, headers, monkeypatch, domain="competitor.com"
    )

    response = await client.post(
        DISCOVER_RESULTS,
        json={"run_id": run_id, "domains": _domains(5)},
        headers={"X-LeadPilot-Run-Token": token},
    )
    assert response.status_code == 201

    run = await _run(client, headers, run_id)
    assert len(run["discovered_domains"]) == 5
    assert run["credits_charged"] == 0
    assert run["credits_charged_at"] is None


# --- Contacts: dispatch --------------------------------------------------------


async def test_contacts_dispatches_seeded_with_selected_domains(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    headers = await _headers(client, db_session)
    await _fund(client, headers)
    discovery_run_id, token, _ = await _discover(
        client, headers, monkeypatch, domain="competitor.com"
    )
    await client.post(
        DISCOVER_RESULTS,
        json={"run_id": discovery_run_id, "domains": _domains(10)},
        headers={"X-LeadPilot-Run-Token": token},
    )

    captured = _mock_n8n(monkeypatch)
    response = await client.post(
        CONTACTS,
        json={
            "source_run_id": discovery_run_id,
            "domains": ["similar-0.example.com", "similar-1.example.com"],
        },
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source_run_id"] == discovery_run_id
    assert body["domain_count"] == 2

    assert captured["domains"] == ["similar-0.example.com", "similar-1.example.com"]
    assert captured["results_url"].endswith("/lead-radar/results")

    run = await _run(client, headers, body["run_id"])
    assert run["search_type"] == "lookalike_contacts"
    assert run["source_run_id"] == discovery_run_id


async def test_contacts_deduplicates_selected_domains(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    headers = await _headers(client, db_session)
    await _fund(client, headers)
    discovery_run_id, token, _ = await _discover(
        client, headers, monkeypatch, domain="competitor.com"
    )
    await client.post(
        DISCOVER_RESULTS,
        json={"run_id": discovery_run_id, "domains": _domains(3)},
        headers={"X-LeadPilot-Run-Token": token},
    )

    captured = _mock_n8n(monkeypatch)
    response = await client.post(
        CONTACTS,
        json={
            "source_run_id": discovery_run_id,
            "domains": [
                "similar-0.example.com",
                "similar-0.example.com",
                "Similar-1.example.com",
            ],
        },
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert captured["domains"] == ["similar-0.example.com", "similar-1.example.com"]


async def test_contacts_requires_at_least_one_domain(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    headers = await _headers(client, db_session)
    await _fund(client, headers)
    discovery_run_id, token, _ = await _discover(
        client, headers, monkeypatch, domain="competitor.com"
    )
    await client.post(
        DISCOVER_RESULTS,
        json={"run_id": discovery_run_id, "domains": _domains(1)},
        headers={"X-LeadPilot-Run-Token": token},
    )

    response = await client.post(
        CONTACTS,
        json={"source_run_id": discovery_run_id, "domains": []},
        headers=headers,
    )

    assert response.status_code == 422


async def test_contacts_is_refused_without_a_configured_webhook(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "N8N_LOOKALIKE_CONTACTS_WEBHOOK_URL", "")
    headers = await _headers(client, db_session)
    await _fund(client, headers)
    discovery_run_id, token, _ = await _discover(
        client, headers, monkeypatch, domain="competitor.com"
    )
    await client.post(
        DISCOVER_RESULTS,
        json={"run_id": discovery_run_id, "domains": _domains(1)},
        headers={"X-LeadPilot-Run-Token": token},
    )

    response = await client.post(
        CONTACTS,
        json={"source_run_id": discovery_run_id, "domains": ["similar-0.example.com"]},
        headers=headers,
    )

    assert response.status_code == 503


async def test_contacts_rejects_an_unknown_source_run(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    headers = await _headers(client, db_session)
    await _fund(client, headers)

    response = await client.post(
        CONTACTS,
        json={"source_run_id": "not-a-real-run", "domains": ["similar-0.example.com"]},
        headers=headers,
    )

    assert response.status_code == 404


# --- Contacts: results (reuses the ordinary results endpoint) -----------------


async def test_contacts_results_are_billed_like_a_b2b_search(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    headers = await _headers(client, db_session)
    await _fund(client, headers)
    discovery_run_id, dtoken, _ = await _discover(
        client, headers, monkeypatch, domain="competitor.com"
    )
    await client.post(
        DISCOVER_RESULTS,
        json={"run_id": discovery_run_id, "domains": _domains(2)},
        headers={"X-LeadPilot-Run-Token": dtoken},
    )
    balance_after_discovery = await _balance(client, headers)

    captured = _mock_n8n(monkeypatch)
    started = await client.post(
        CONTACTS,
        json={
            "source_run_id": discovery_run_id,
            "domains": ["similar-0.example.com", "similar-1.example.com"],
        },
        headers=headers,
    )
    assert started.status_code == 200
    contacts_run_id = started.json()["run_id"]

    companies = [
        {
            "company_name": "Similar Co 0",
            "website": "similar-0.example.com",
            "decision_makers": [
                {
                    "full_name": "Alex Buyer",
                    "verified_email": "alex@similar-0.example.com",
                    "email_status": "valid",
                }
            ],
        },
        {
            "company_name": "Similar Co 1",
            "website": "similar-1.example.com",
            "decision_makers": [
                {
                    "full_name": "Sam Buyer",
                    "verified_email": "sam@similar-1.example.com",
                    "email_status": "valid",
                }
            ],
        },
    ]
    response = await _post_results(
        client, contacts_run_id, captured["progress_token"], companies=companies
    )
    assert response.status_code == 201, response.text

    expected_charge = 2 * BASE_CONTACT_CREDIT
    assert await _balance(client, headers) == balance_after_discovery - expected_charge

    run = await _run(client, headers, contacts_run_id)
    assert run["credits_charged"] == expected_charge
    assert run["companies_found"] == 2
    assert run["contacts_found"] == 2
