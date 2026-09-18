"""The /lead-radar/error webhook: n8n reports a failure, the user is told."""

from typing import Any

import httpx
import pytest
from httpx import AsyncClient

from app.core.config import settings
from app.services import lead_search as lead_search_module
from app.services.workflow_errors import explain
from tests.api.test_auth import signed_in_tokens

PREFIX = settings.API_V1_PREFIX
ERROR = f"{PREFIX}/lead-radar/error"


@pytest.fixture(autouse=True)
def _webhook_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "N8N_WEBHOOK_URL", "https://n8n.test/webhook/leads")
    monkeypatch.setattr(settings, "PUBLIC_API_URL", "https://api.leadpilot.test")


def _mock_n8n(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    cls = lead_search_module.LeadSearchService
    existing = getattr(cls.__init__, "_lp_captured", None)
    if existing is not None:
        return existing  # type: ignore[no-any-return]

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    original_init = cls.__init__

    def patched_init(self: Any, client: httpx.AsyncClient | None = None) -> None:
        original_init(self, httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    patched_init._lp_captured = captured  # type: ignore[attr-defined]
    monkeypatch.setattr(cls, "__init__", patched_init)

    return captured


async def _start_run(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, str], str, str]:
    captured = _mock_n8n(monkeypatch)
    tokens = await signed_in_tokens(client, db_session)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    # The start gate refuses a search the balance could not cover, so the run
    # this test needs would never exist on an unfunded account.
    funded = await client.post(
        f"{PREFIX}/billing/subscriptions",
        json={"plan_code": "pro_yearly"},
        headers=headers,
    )
    assert funded.status_code == 201

    started = await client.post(
        f"{PREFIX}/lead-radar/search",
        json={"original_keyword": "LED screen", "expanded_keywords": []},
        headers=headers,
    )
    assert started.status_code == 200

    return headers, started.json()["run_id"], captured["progress_token"]


# --- The payload the user's workflow sends -----------------------------------


async def test_the_bare_n8n_payload_is_accepted(client: AsyncClient) -> None:
    """Exactly the four fields from the user's error-trigger node, no run id."""
    response = await client.post(
        ERROR,
        json={
            "workflow_id": "abc123",
            "execution_id": "4021",
            "error_message": "Error in sub-node 'Message a model'",
            "last_node_executed": "Verifying Contacts",
        },
    )

    assert response.status_code == 202
    body = response.json()
    assert body["recorded"] is True
    # Nothing to attribute it to, so nobody is notified -- but it is kept.
    assert body["attributed"] is False


async def test_an_unresolved_expression_does_not_break_it(client: AsyncClient) -> None:
    """n8n sends the raw expression when a field cannot be resolved."""
    response = await client.post(
        ERROR,
        json={
            "workflow_id": "abc123",
            "execution_id": "4021",
            "error_message": "{{ $json.execution.error.message }}",
            "last_node_executed": "HTTP Request",
        },
    )

    assert response.status_code == 202
    assert response.json()["recorded"] is True


async def test_an_empty_body_is_still_recorded(client: AsyncClient) -> None:
    """A failure must never be lost to a validation error."""
    response = await client.post(ERROR, json={})

    assert response.status_code == 202
    assert response.json()["recorded"] is True


# --- Attributed to a run -----------------------------------------------------


async def test_an_attributed_error_fails_the_run_and_notifies(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers, run_id, token = await _start_run(client, db_session, monkeypatch)

    response = await client.post(
        ERROR,
        json={
            "workflow_id": "abc123",
            "execution_id": "4021",
            "error_message": "429 Too Many Requests from provider",
            "last_node_executed": "Get prospect profiles",
            "run_id": run_id,
            "progress_token": token,
        },
    )

    assert response.status_code == 202
    body = response.json()
    assert body["attributed"] is True
    assert body["reason"] == "rate_limited"

    run = (await client.get(f"{PREFIX}/lead-radar/runs/{run_id}", headers=headers)).json()
    assert run["status"] == "failed"
    assert run["error_reason"] == "rate_limited"
    assert run["error_node"] == "Get prospect profiles"
    assert run["finished_at"] is not None

    notifications = await client.get(f"{PREFIX}/notifications", headers=headers)
    newest = notifications.json()["items"][0]
    assert "failed" in newest["title"]
    assert "LED screen" in newest["title"]
    assert "Get prospect profiles" in newest["subtitle"]


async def test_a_wrong_token_is_not_attributed_but_still_recorded(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bad token must not let one workflow fail another user's run."""
    headers, run_id, _ = await _start_run(client, db_session, monkeypatch)

    response = await client.post(
        ERROR,
        json={
            "error_message": "boom",
            "run_id": run_id,
            "progress_token": "not-the-token",
        },
    )

    assert response.status_code == 202
    assert response.json()["attributed"] is False

    run = (await client.get(f"{PREFIX}/lead-radar/runs/{run_id}", headers=headers)).json()
    assert run["status"] == "running"


async def test_an_error_after_the_results_arrived_does_not_reopen_the_run(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The search delivered: a late error from some trailing node is noise."""
    headers, run_id, token = await _start_run(client, db_session, monkeypatch)
    delivered = await client.post(
        f"{PREFIX}/lead-radar/results",
        json={
            "run_id": run_id,
            "companies": [{"company_name": "Acme", "website": "https://acme.test"}],
        },
        headers={"X-LeadPilot-Run-Token": token},
    )
    assert delivered.status_code == 201

    await client.post(
        ERROR,
        json={"error_message": "late boom", "run_id": run_id, "progress_token": token},
    )

    run = (await client.get(f"{PREFIX}/lead-radar/runs/{run_id}", headers=headers)).json()
    assert run["status"] == "completed"
    assert run["error"] is None


async def test_an_error_after_the_last_checkpoint_but_before_results_fails_the_run(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real workflow says `status: completed` on its last checkpoint, then
    runs a few more nodes before posting results. A crash in those nodes lands
    seconds after the run was closed; no results will ever follow, so the run
    has failed and must say so rather than sit on "collecting results"."""
    headers, run_id, token = await _start_run(client, db_session, monkeypatch)
    await client.post(
        f"{PREFIX}/lead-radar/runs/{run_id}/progress",
        json={
            "stage": "verifying_contacts",
            "status": "completed",
            "execution_id": "135",
        },
        headers={"X-LeadPilot-Run-Token": token},
    )

    response = await client.post(
        ERROR,
        json={
            "workflow_id": "gNUAhBc2Amn0ywf4",
            "execution_id": "135",
            "error_message": (
                "Paired item data for item from node 'Filter & Limit Prospects' "
                "is unavailable."
            ),
            "last_node_executed": "Has Phone Number?",
        },
    )
    assert response.json()["attributed_by"] == "execution_id"

    run = (await client.get(f"{PREFIX}/lead-radar/runs/{run_id}", headers=headers)).json()
    assert run["status"] == "failed"
    assert run["error_node"] == "Has Phone Number?"
    assert "Paired item data" in run["error"]
    assert run["credits_charged"] == 0

    notifications = await client.get(f"{PREFIX}/notifications", headers=headers)
    newest = notifications.json()["items"][0]
    assert "failed" in newest["title"]
    assert "Has Phone Number?" in newest["subtitle"]


# --- Classification ----------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("429 Too Many Requests", "rate_limited"),
        ("You exceeded your current quota", "rate_limited"),
        ("Insufficient credit balance", "provider_credit"),
        ("402 Payment Required", "provider_credit"),
        ("401 Unauthorized: invalid api key", "provider_auth"),
        ("The connection timed out", "timeout"),
        ("ETIMEDOUT", "timeout"),
        ("getaddrinfo ENOTFOUND api.example.com", "unreachable"),
        ("Unexpected token < in JSON at position 0", "bad_response"),
        ("Something nobody predicted", "unknown"),
        ("", "unknown"),
    ],
)
def test_messages_are_classified_for_the_user(message: str, expected: str) -> None:
    assert explain(message) == expected
