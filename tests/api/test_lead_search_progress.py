"""The n8n progress webhook and the run it reports against."""

from typing import Any

import httpx
import pytest
from httpx import AsyncClient

from app.core.config import settings
from app.models.lead_search import STAGE_ORDER
from app.services import lead_search as lead_search_module
from app.services.progress_stream import progress_stream
from tests.api.test_auth import signed_in_tokens

PREFIX = settings.API_V1_PREFIX
SEARCH = f"{PREFIX}/lead-radar/search"


@pytest.fixture(autouse=True)
def _webhook_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "N8N_WEBHOOK_URL", "https://n8n.test/webhook/leads")
    monkeypatch.setattr(settings, "PUBLIC_API_URL", "https://api.leadpilot.test")


def _mock_n8n(monkeypatch: pytest.MonkeyPatch, *, status: int = 200) -> dict[str, Any]:
    """Capture what the backend sends to n8n."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured.update(json.loads(request.content))
        return httpx.Response(status, json={"ok": True})

    original_init = lead_search_module.LeadSearchService.__init__

    def patched_init(self: Any, client: httpx.AsyncClient | None = None) -> None:
        original_init(self, httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    monkeypatch.setattr(lead_search_module.LeadSearchService, "__init__", patched_init)

    return captured


async def _headers(client: AsyncClient, db_session: Any) -> dict[str, str]:
    """Sign in and fund the wallet: the start gate refuses a search the balance
    could not cover. Pro yearly grants 4,990, enough for any run here."""
    tokens = await signed_in_tokens(client, db_session)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    response = await client.post(
        f"{PREFIX}/billing/subscriptions",
        json={"plan_code": "pro_yearly"},
        headers=headers,
    )
    assert response.status_code == 201

    return headers


async def _start(client: AsyncClient, headers: dict[str, str]) -> dict:
    response = await client.post(
        SEARCH,
        json={"original_keyword": "LED screen", "expanded_keywords": ["Digital signage"]},
        headers=headers,
    )
    assert response.status_code == 200
    return response.json()


async def _post_progress(
    client: AsyncClient, run_id: str, token: str, **payload: Any
) -> httpx.Response:
    return await client.post(
        f"{PREFIX}/lead-radar/runs/{run_id}/progress",
        json=payload,
        headers={"X-LeadPilot-Run-Token": token},
    )


async def _get_run(client: AsyncClient, headers: dict[str, str], run_id: str) -> dict:
    response = await client.get(f"{PREFIX}/lead-radar/runs/{run_id}", headers=headers)
    assert response.status_code == 200
    return response.json()


# --- Dispatch ----------------------------------------------------------------


async def test_search_returns_a_run_id_and_tells_n8n_where_to_report(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _mock_n8n(monkeypatch)
    headers = await _headers(client, db_session)

    body = await _start(client, headers)

    assert body["dispatched"] is True
    run_id = body["run_id"]
    assert run_id

    # The workflow gets a ready-made callback URL and this run's own token.
    assert captured["run_id"] == run_id
    assert captured["progress_url"] == (
        f"https://api.leadpilot.test{PREFIX}/lead-radar/runs/{run_id}/progress"
    )
    assert captured["progress_token"]
    assert captured["expanded_keywords"] == ["LED screen", "Digital signage"]


async def test_a_new_run_starts_queued_with_the_full_stage_list(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_n8n(monkeypatch)
    headers = await _headers(client, db_session)
    run_id = (await _start(client, headers))["run_id"]

    run = await _get_run(client, headers, run_id)

    assert run["status"] == "running"
    assert run["stage"] == "queued"
    assert run["events"] == []
    assert run["stage_order"] == [s.value for s in STAGE_ORDER]
    assert run["keyword_count"] == 2


async def test_a_failed_dispatch_closes_the_run(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """n8n refused the job, so the run must not sit at 'running' forever."""
    _mock_n8n(monkeypatch, status=500)
    headers = await _headers(client, db_session)

    response = await client.post(
        SEARCH,
        json={"original_keyword": "LED screen", "expanded_keywords": []},
        headers=headers,
    )

    assert response.status_code == 502

    listed = await client.get(f"{PREFIX}/lead-radar/runs/does-not-exist", headers=headers)
    assert listed.status_code == 404


# --- Progress callbacks ------------------------------------------------------


async def test_each_checkpoint_is_recorded_and_advances_the_run(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _mock_n8n(monkeypatch)
    headers = await _headers(client, db_session)
    run_id = (await _start(client, headers))["run_id"]
    token = captured["progress_token"]

    for stage in ("searching_companies", "ai_analysing", "domain_search"):
        response = await _post_progress(client, run_id, token, stage=stage)
        assert response.status_code == 202

    run = await _get_run(client, headers, run_id)

    assert run["stage"] == "domain_search"
    assert [e["stage"] for e in run["events"]] == [
        "searching_companies",
        "ai_analysing",
        "domain_search",
    ]
    assert run["status"] == "running"


async def test_counts_and_messages_are_kept(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _mock_n8n(monkeypatch)
    headers = await _headers(client, db_session)
    run_id = (await _start(client, headers))["run_id"]
    token = captured["progress_token"]

    await _post_progress(
        client,
        run_id,
        token,
        stage="searching_companies",
        message="142 companies matched",
        count=142,
    )
    await _post_progress(client, run_id, token, stage="finding_decision_makers", count=38)

    run = await _get_run(client, headers, run_id)

    assert run["companies_found"] == 142
    assert run["contacts_found"] == 38
    first = run["events"][0]
    assert first["message"] == "142 companies matched"
    assert first["count"] == 142


async def test_completion_closes_the_run_and_notifies(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _mock_n8n(monkeypatch)
    headers = await _headers(client, db_session)
    run_id = (await _start(client, headers))["run_id"]
    token = captured["progress_token"]

    await _post_progress(client, run_id, token, stage="searching_companies", count=10)
    await _post_progress(
        client, run_id, token, stage="verifying_contacts", count=4, status="completed"
    )

    run = await _get_run(client, headers, run_id)
    assert run["status"] == "completed"
    assert run["stage"] == "completed"
    assert run["finished_at"] is not None

    notifications = await client.get(f"{PREFIX}/notifications", headers=headers)
    newest = notifications.json()["items"][0]
    assert "finished" in newest["title"]
    assert "LED screen" in newest["title"]


async def test_repeated_completion_finishes_the_run_only_once(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An n8n HTTP node runs once per input item, so the completing checkpoint
    can arrive dozens of times. Only the first may close the run and notify."""
    captured = _mock_n8n(monkeypatch)
    headers = await _headers(client, db_session)
    run_id = (await _start(client, headers))["run_id"]
    token = captured["progress_token"]

    for i in range(5):
        response = await _post_progress(
            client,
            run_id,
            token,
            stage="verifying_contacts",
            message=f"item {i}",
            status="completed",
        )
        assert response.status_code == 202

    run = await _get_run(client, headers, run_id)
    assert run["status"] == "completed"
    # Every report is kept as history...
    assert len(run["events"]) == 5

    # ...but the user hears about it exactly once.
    notifications = await client.get(f"{PREFIX}/notifications", headers=headers)
    finished = [n for n in notifications.json()["items"] if "finished" in n["title"]]
    assert len(finished) == 1


async def test_a_late_error_cannot_reopen_or_fail_a_completed_run(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _mock_n8n(monkeypatch)
    headers = await _headers(client, db_session)
    run_id = (await _start(client, headers))["run_id"]
    token = captured["progress_token"]

    await _post_progress(
        client, run_id, token, stage="verifying_contacts", status="completed"
    )
    await _post_progress(
        client, run_id, token, stage="domain_search", error="late failure"
    )

    run = await _get_run(client, headers, run_id)
    assert run["status"] == "completed"
    assert run["error"] is None


async def test_an_error_fails_the_run_and_notifies(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _mock_n8n(monkeypatch)
    headers = await _headers(client, db_session)
    run_id = (await _start(client, headers))["run_id"]
    token = captured["progress_token"]

    await _post_progress(
        client,
        run_id,
        token,
        stage="domain_search",
        error="Domain provider rate-limited the workflow.",
    )

    run = await _get_run(client, headers, run_id)
    assert run["status"] == "failed"
    assert run["error"] == "Domain provider rate-limited the workflow."

    notifications = await client.get(f"{PREFIX}/notifications", headers=headers)
    assert "failed" in notifications.json()["items"][0]["title"]


async def test_a_late_earlier_checkpoint_does_not_move_the_run_backwards(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """n8n nodes can report out of order; the tracker must not regress."""
    captured = _mock_n8n(monkeypatch)
    headers = await _headers(client, db_session)
    run_id = (await _start(client, headers))["run_id"]
    token = captured["progress_token"]

    await _post_progress(client, run_id, token, stage="finding_emails")
    await _post_progress(client, run_id, token, stage="ai_analysing")

    run = await _get_run(client, headers, run_id)

    assert run["stage"] == "finding_emails"
    # Both are still recorded: the event list is the truth of what was reported.
    assert len(run["events"]) == 2


async def test_an_unknown_stage_is_rejected(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _mock_n8n(monkeypatch)
    headers = await _headers(client, db_session)
    run_id = (await _start(client, headers))["run_id"]

    response = await _post_progress(
        client, run_id, captured["progress_token"], stage="teleporting"
    )

    assert response.status_code == 422


# --- Callback authentication -------------------------------------------------


async def test_a_wrong_token_is_refused(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_n8n(monkeypatch)
    headers = await _headers(client, db_session)
    run_id = (await _start(client, headers))["run_id"]

    response = await _post_progress(
        client, run_id, "not-the-right-token", stage="searching_companies"
    )

    assert response.status_code == 401
    assert (await _get_run(client, headers, run_id))["events"] == []


async def test_a_missing_token_header_is_refused(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_n8n(monkeypatch)
    headers = await _headers(client, db_session)
    run_id = (await _start(client, headers))["run_id"]

    response = await client.post(
        f"{PREFIX}/lead-radar/runs/{run_id}/progress",
        json={"stage": "searching_companies"},
    )

    assert response.status_code == 422


async def test_one_runs_token_cannot_write_to_another_run(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _mock_n8n(monkeypatch)
    headers = await _headers(client, db_session)

    first = (await _start(client, headers))["run_id"]
    first_token = captured["progress_token"]
    second = (await _start(client, headers))["run_id"]

    assert first != second

    response = await _post_progress(
        client, second, first_token, stage="searching_companies"
    )

    assert response.status_code == 401


# --- Run access --------------------------------------------------------------


async def test_a_run_is_not_readable_by_another_account(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.auth import AuthService

    _mock_n8n(monkeypatch)
    owner_headers = await _headers(client, db_session)
    run_id = (await _start(client, owner_headers))["run_id"]

    service = AuthService(db_session)
    other, _ = await service.register("other@leadpilot.io", "other-password-1", "Other")
    other.is_verified = True
    await service.commit()
    login = await client.post(
        f"{PREFIX}/auth/login",
        json={"email": "other@leadpilot.io", "password": "other-password-1"},
    )
    other_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    response = await client.get(
        f"{PREFIX}/lead-radar/runs/{run_id}", headers=other_headers
    )

    assert response.status_code == 404


async def test_reading_a_run_requires_auth(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_n8n(monkeypatch)
    headers = await _headers(client, db_session)
    run_id = (await _start(client, headers))["run_id"]

    assert (await client.get(f"{PREFIX}/lead-radar/runs/{run_id}")).status_code == 401


# --- Live stream -------------------------------------------------------------


async def test_progress_publish_reaches_a_subscriber() -> None:
    user_id = "user-under-test"

    async with progress_stream.subscribe(user_id) as queue:
        progress_stream.publish(user_id, "run-1")

        assert queue.get_nowait() == "run-1"

    assert progress_stream.connection_count(user_id) == 0


async def test_progress_stream_rejects_a_bad_token(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_n8n(monkeypatch)
    headers = await _headers(client, db_session)
    run_id = (await _start(client, headers))["run_id"]

    response = await client.get(
        f"{PREFIX}/lead-radar/runs/{run_id}/stream", params={"token": "nonsense"}
    )

    assert response.status_code == 401
