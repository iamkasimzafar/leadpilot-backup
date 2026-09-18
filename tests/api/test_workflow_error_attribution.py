"""How an n8n Error Trigger report finds the run it belongs to.

The Error Trigger's payload is exactly:

    {"workflow_id", "execution_id", "error_message", "last_node_executed"}

-- no run id, no token. The run must be found from the execution id the
progress callbacks (or the webhook's reply) taught us, or, failing that, from
there being only one search in flight.
"""

import json
from typing import Any

import httpx
import pytest
from httpx import AsyncClient

from app.core.config import settings
from app.services import lead_search as lead_search_module
from tests.api.test_auth import signed_in_tokens

PREFIX = settings.API_V1_PREFIX
SEARCH = f"{PREFIX}/lead-radar/search"
ERROR = f"{PREFIX}/lead-radar/error"

# The body the user's Error Trigger workflow sends, with the expressions
# resolved. Nothing else is added to it in these tests.
N8N_ERROR = {
    "workflow_id": "wf-lead-search",
    "execution_id": "4021",
    "error_message": "429 Too Many Requests from provider",
    "last_node_executed": "Get prospect profiles",
}


@pytest.fixture(autouse=True)
def _webhook_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "N8N_WEBHOOK_URL", "https://n8n.test/webhook/leads")
    monkeypatch.setattr(settings, "PUBLIC_API_URL", "https://api.leadpilot.test")
    monkeypatch.setattr(settings, "N8N_WEBHOOK_SECRET", "")


def _mock_n8n(
    monkeypatch: pytest.MonkeyPatch, *, reply: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Capture what the backend sends to n8n, answering with `reply`."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=reply or {"message": "Workflow was started"})

    original_init = lead_search_module.LeadSearchService.__init__

    def patched_init(self: Any, client: httpx.AsyncClient | None = None) -> None:
        original_init(self, httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    monkeypatch.setattr(lead_search_module.LeadSearchService, "__init__", patched_init)

    return captured


async def _signed_in(client: AsyncClient, db_session: Any) -> dict[str, str]:
    tokens = await signed_in_tokens(client, db_session)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    # The start gate refuses a search the balance could not cover.
    funded = await client.post(
        f"{PREFIX}/billing/subscriptions",
        json={"plan_code": "pro_yearly"},
        headers=headers,
    )
    assert funded.status_code == 201

    return headers


async def _start(
    client: AsyncClient, headers: dict[str, str], captured: dict[str, Any], keyword: str
) -> tuple[str, str]:
    """Start a run; returns (run_id, progress_token)."""
    response = await client.post(
        SEARCH,
        json={"original_keyword": keyword, "expanded_keywords": []},
        headers=headers,
    )
    assert response.status_code == 200

    return response.json()["run_id"], captured["progress_token"]


async def _progress(
    client: AsyncClient, run_id: str, token: str, **payload: Any
) -> httpx.Response:
    return await client.post(
        f"{PREFIX}/lead-radar/runs/{run_id}/progress",
        json=payload,
        headers={"X-LeadPilot-Run-Token": token},
    )


async def _run(client: AsyncClient, headers: dict[str, str], run_id: str) -> dict:
    response = await client.get(f"{PREFIX}/lead-radar/runs/{run_id}", headers=headers)
    assert response.status_code == 200
    return response.json()


# --- By execution id ---------------------------------------------------------


async def test_a_progress_callback_teaches_the_run_its_execution_id(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _mock_n8n(monkeypatch)
    headers = await _signed_in(client, db_session)
    run_id, token = await _start(client, headers, captured, "LED screen")

    assert (await _run(client, headers, run_id))["n8n_execution_id"] is None

    response = await _progress(
        client, run_id, token, stage="searching_companies", execution_id="4021"
    )
    assert response.status_code == 202

    assert (await _run(client, headers, run_id))["n8n_execution_id"] == "4021"


async def test_the_bare_error_payload_is_attributed_by_execution_id(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two searches in flight, so only the execution id can tell them apart."""
    captured = _mock_n8n(monkeypatch)
    headers = await _signed_in(client, db_session)
    other_id, other_token = await _start(client, headers, captured, "Solar panels")
    run_id, token = await _start(client, headers, captured, "LED screen")

    await _progress(
        client, other_id, other_token, stage="searching_companies", execution_id="4020"
    )
    await _progress(
        client, run_id, token, stage="searching_companies", execution_id="4021"
    )

    response = await client.post(ERROR, json=N8N_ERROR)

    assert response.status_code == 202
    body = response.json()
    assert body["attributed"] is True
    assert body["attributed_by"] == "execution_id"
    assert body["reason"] == "rate_limited"

    failed = await _run(client, headers, run_id)
    assert failed["status"] == "failed"
    assert failed["error_reason"] == "rate_limited"
    assert failed["error_node"] == "Get prospect profiles"

    # The other search is untouched.
    assert (await _run(client, headers, other_id))["status"] == "running"

    notifications = await client.get(f"{PREFIX}/notifications", headers=headers)
    newest = notifications.json()["items"][0]
    assert "LED screen" in newest["title"]
    assert "failed" in newest["title"]


async def test_the_webhook_reply_can_carry_the_execution_id(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A "Respond to Webhook" node answering with `{{ $execution.id }}` ties the
    run to its execution before any checkpoint arrives."""
    captured = _mock_n8n(monkeypatch, reply={"execution_id": 9001})
    headers = await _signed_in(client, db_session)

    started = await client.post(
        SEARCH,
        json={"original_keyword": "LED screen", "expanded_keywords": []},
        headers=headers,
    )
    assert started.status_code == 200
    assert started.json()["n8n_execution_id"] == "9001"
    run_id = started.json()["run_id"]
    assert captured["run_id"] == run_id

    assert (await _run(client, headers, run_id))["n8n_execution_id"] == "9001"

    response = await client.post(ERROR, json={**N8N_ERROR, "execution_id": 9001})
    assert response.json()["attributed_by"] == "execution_id"
    assert (await _run(client, headers, run_id))["status"] == "failed"


async def test_a_numeric_execution_id_matches_the_string_one(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """n8n's $execution.id is a string in expressions but may arrive as a
    number from a JSON body; both must land on the same run."""
    captured = _mock_n8n(monkeypatch)
    headers = await _signed_in(client, db_session)
    await _start(client, headers, captured, "Solar panels")
    run_id, token = await _start(client, headers, captured, "LED screen")

    await _progress(client, run_id, token, stage="ai_analysing", execution_id=4021)

    response = await client.post(ERROR, json={**N8N_ERROR, "execution_id": "4021"})
    assert response.json()["attributed_by"] == "execution_id"
    assert (await _run(client, headers, run_id))["status"] == "failed"


# --- By the only run in flight -----------------------------------------------


async def test_with_one_search_running_the_bare_payload_still_reaches_it(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The workflow never sent an execution id, but there is only one search
    it could be."""
    captured = _mock_n8n(monkeypatch)
    headers = await _signed_in(client, db_session)
    run_id, _ = await _start(client, headers, captured, "LED screen")

    response = await client.post(ERROR, json=N8N_ERROR)

    body = response.json()
    assert body["attributed"] is True
    assert body["attributed_by"] == "sole_running_run"

    failed = await _run(client, headers, run_id)
    assert failed["status"] == "failed"
    assert failed["error_node"] == "Get prospect profiles"
    # The report also taught the run its execution id.
    assert failed["n8n_execution_id"] == "4021"


async def test_with_two_searches_running_an_anonymous_failure_is_not_guessed(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failing the wrong user's search would be worse than failing none: the
    sweeper closes whichever one really died."""
    captured = _mock_n8n(monkeypatch)
    headers = await _signed_in(client, db_session)
    first, _ = await _start(client, headers, captured, "Solar panels")
    second, _ = await _start(client, headers, captured, "LED screen")

    response = await client.post(ERROR, json=N8N_ERROR)

    assert response.status_code == 202
    assert response.json()["attributed"] is False
    assert response.json()["attributed_by"] is None
    assert (await _run(client, headers, first))["status"] == "running"
    assert (await _run(client, headers, second))["status"] == "running"


async def test_the_sole_run_is_not_blamed_for_another_execution(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One run in flight, already known to be execution 4021; a failure from
    execution 7777 (a manual test run on the canvas, say) is not its."""
    captured = _mock_n8n(monkeypatch)
    headers = await _signed_in(client, db_session)
    run_id, token = await _start(client, headers, captured, "LED screen")
    await _progress(
        client, run_id, token, stage="searching_companies", execution_id="4021"
    )

    response = await client.post(ERROR, json={**N8N_ERROR, "execution_id": "7777"})

    assert response.json()["attributed"] is False
    assert (await _run(client, headers, run_id))["status"] == "running"


async def test_a_wrong_token_does_not_fall_through_to_a_guess(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run id with the wrong token is a bad caller, not a missing id: the
    sole-run fallback must not rescue it."""
    captured = _mock_n8n(monkeypatch)
    headers = await _signed_in(client, db_session)
    run_id, _ = await _start(client, headers, captured, "LED screen")

    response = await client.post(
        ERROR,
        json={**N8N_ERROR, "run_id": run_id, "progress_token": "not-the-token"},
    )

    assert response.status_code == 202
    assert response.json()["attributed"] is False
    assert (await _run(client, headers, run_id))["status"] == "running"


async def test_unresolved_expressions_count_as_absent(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """n8n sends the raw `{{ ... }}` when a field cannot be resolved. That
    must not be stored as an id, nor block the sole-run fallback."""
    captured = _mock_n8n(monkeypatch)
    headers = await _signed_in(client, db_session)
    run_id, _ = await _start(client, headers, captured, "LED screen")

    response = await client.post(
        ERROR,
        json={
            "workflow_id": "{{ $json.workflow.id }}",
            "execution_id": "{{ $json.execution.id }}",
            "error_message": "socket hang up",
            "last_node_executed": "{{ $json.execution.lastNodeExecuted }}",
        },
    )

    assert response.json()["attributed_by"] == "sole_running_run"
    failed = await _run(client, headers, run_id)
    assert failed["status"] == "failed"
    assert failed["n8n_execution_id"] is None
    assert failed["error_node"] is None
    assert failed["error_reason"] == "unreachable"


# --- Shared secret -----------------------------------------------------------


async def test_the_error_endpoint_requires_the_secret_when_one_is_set(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "N8N_WEBHOOK_SECRET", "s3cret")

    refused = await client.post(ERROR, json=N8N_ERROR)
    assert refused.status_code == 401

    wrong = await client.post(
        ERROR, json=N8N_ERROR, headers={"X-LeadPilot-Token": "nope"}
    )
    assert wrong.status_code == 401

    accepted = await client.post(
        ERROR, json=N8N_ERROR, headers={"X-LeadPilot-Token": "s3cret"}
    )
    assert accepted.status_code == 202


async def test_the_error_endpoint_stays_open_without_a_secret(
    client: AsyncClient,
) -> None:
    response = await client.post(ERROR, json=N8N_ERROR)

    assert response.status_code == 202
    assert response.json()["recorded"] is True
