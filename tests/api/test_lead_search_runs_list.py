"""GET /lead-radar/runs — the Searches list, and what "active" means.

This listing is what lets a user leave the page mid-search and come back to
it, so the cases that matter are: a run is only listed to its owner, and a
closed run whose results have not arrived still counts as active.
"""

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.config import settings
from app.models.lead_search import LeadSearchRun
from tests.api.test_auth import PREFIX as AUTH_PREFIX
from tests.api.test_auth import register, signed_in_tokens, verify
from tests.api.test_lead_search_progress import _mock_n8n

PREFIX = settings.API_V1_PREFIX
RUNS = f"{PREFIX}/lead-radar/runs"
SEARCH = f"{PREFIX}/lead-radar/search"

OTHER = {"email": "stranger@leadpilot.io", "password": "an0ther-secret-pw"}


@pytest.fixture(autouse=True)
def _webhook_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "N8N_WEBHOOK_URL", "https://n8n.test/webhook/leads")
    monkeypatch.setattr(settings, "PUBLIC_API_URL", "https://api.leadpilot.test")


async def _fund(client: AsyncClient, headers: dict[str, str]) -> dict[str, str]:
    """The start gate refuses a search the balance could not cover."""
    response = await client.post(
        f"{PREFIX}/billing/subscriptions",
        json={"plan_code": "pro_yearly"},
        headers=headers,
    )
    assert response.status_code == 201

    return headers


async def _owner(client: AsyncClient, db_session: Any) -> dict[str, str]:
    tokens = await signed_in_tokens(client, db_session)

    return await _fund(client, {"Authorization": f"Bearer {tokens['access_token']}"})


async def _stranger(client: AsyncClient, db_session: Any) -> dict[str, str]:
    """A second, unrelated account, to prove runs are not shared."""
    await register(client, **OTHER, full_name="Someone Else")
    await verify(client, db_session, email=OTHER["email"])

    response = await client.post(f"{AUTH_PREFIX}/login", json=OTHER)
    assert response.status_code == 200

    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _start(client: AsyncClient, headers: dict[str, str], keyword: str) -> dict:
    response = await client.post(
        SEARCH,
        json={"original_keyword": keyword, "expanded_keywords": []},
        headers=headers,
    )
    assert response.status_code == 200

    return response.json()


async def test_lists_own_runs_newest_first(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_n8n(monkeypatch)
    headers = await _owner(client, db_session)

    await _start(client, headers, "LED screen")
    second = await _start(client, headers, "Digital signage")

    response = await client.get(RUNS, headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert body["page"] == 1
    # Newest first, so the Searches list opens on the most recent search.
    assert body["items"][0]["id"] == second["run_id"]
    assert body["items"][0]["original_keyword"] == "Digital signage"


async def test_run_is_private_to_its_owner(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_n8n(monkeypatch)

    owner = await _owner(client, db_session)
    await _start(client, owner, "LED screen")

    stranger = await _stranger(client, db_session)
    response = await client.get(RUNS, headers=stranger)

    assert response.status_code == 200
    assert response.json()["total"] == 0


async def test_active_only_includes_a_run_awaiting_its_results(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run can close its last checkpoint seconds before the workflow posts
    the results. Until those land the user is still waiting, so it stays
    active -- that is what keeps the badge on and the page reattaching."""
    _mock_n8n(monkeypatch)
    headers = await _owner(client, db_session)

    started = await _start(client, headers, "LED screen")
    run_id = started["run_id"]

    # Still running.
    response = await client.get(RUNS, params={"active_only": True}, headers=headers)
    assert response.json()["total"] == 1

    result = await db_session.execute(
        select(LeadSearchRun).where(LeadSearchRun.id == run_id)
    )
    token = result.scalar_one().callback_token

    # Close it via the workflow callback, without ever sending results.
    completed = await client.post(
        f"{RUNS}/{run_id}/progress",
        json={"stage": "completed", "status": "completed"},
        headers={"X-LeadPilot-Run-Token": token},
    )
    assert completed.status_code == 202

    # Closed, but the results never arrived: the user is still waiting.
    response = await client.get(RUNS, params={"active_only": True}, headers=headers)
    assert response.json()["total"] == 1


async def test_active_only_drops_a_run_once_its_results_land(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_n8n(monkeypatch)
    headers = await _owner(client, db_session)

    started = await _start(client, headers, "LED screen")
    run_id = started["run_id"]

    result = await db_session.execute(
        select(LeadSearchRun).where(LeadSearchRun.id == run_id)
    )
    token = result.scalar_one().callback_token

    results = await client.post(
        f"{PREFIX}/lead-radar/results",
        json={
            "run_id": run_id,
            "companies": [
                {
                    "company_name": "Acme Displays",
                    "website": "https://acme-displays.test",
                    "decision_makers": [],
                }
            ],
        },
        headers={"X-LeadPilot-Run-Token": token},
    )
    assert results.status_code == 201

    response = await client.get(RUNS, params={"active_only": True}, headers=headers)
    assert response.json()["total"] == 0

    # Still in the full listing: it happened, it is just no longer pending.
    response = await client.get(RUNS, headers=headers)
    assert response.json()["total"] == 1
