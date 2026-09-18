"""The watchdog that closes runs the workflow never finished."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import update

from app.core.config import settings
from app.models.lead_search import LeadSearchRun
from app.services import lead_search as lead_search_module
from app.services.lead_search_sweeper import (
    NO_RESULTS_REASON,
    LeadSearchSweeper,
)
from tests.api.test_auth import signed_in_tokens

PREFIX = settings.API_V1_PREFIX


@pytest.fixture(autouse=True)
def _webhook_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "N8N_WEBHOOK_URL", "https://n8n.test/webhook/leads")
    monkeypatch.setattr(settings, "PUBLIC_API_URL", "https://api.leadpilot.test")
    monkeypatch.setattr(settings, "LEAD_SEARCH_STALE_MINUTES", 30)
    monkeypatch.setattr(settings, "LEAD_SEARCH_RESULTS_TIMEOUT_MINUTES", 15)


def _mock_n8n(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"message": "Workflow was started"})

    original_init = lead_search_module.LeadSearchService.__init__

    def patched_init(self: Any, client: httpx.AsyncClient | None = None) -> None:
        original_init(self, httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    monkeypatch.setattr(lead_search_module.LeadSearchService, "__init__", patched_init)

    return captured


async def _running_run(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, str], str, str]:
    captured = _mock_n8n(monkeypatch)
    tokens = await signed_in_tokens(client, db_session)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

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


async def _age(db_session: Any, run_id: str, **columns: datetime) -> None:
    """Backdate timestamps directly: the sweeper judges by them."""
    await db_session.execute(
        update(LeadSearchRun).where(LeadSearchRun.id == run_id).values(**columns)
    )
    await db_session.commit()


async def _run(client: AsyncClient, headers: dict[str, str], run_id: str) -> dict:
    response = await client.get(f"{PREFIX}/lead-radar/runs/{run_id}", headers=headers)
    assert response.status_code == 200
    return response.json()


# --- Stale running runs --------------------------------------------------------


async def test_a_run_with_no_checkpoint_for_too_long_is_failed_and_the_user_told(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers, run_id, _ = await _running_run(client, db_session, monkeypatch)
    await _age(db_session, run_id, updated_at=datetime.now(UTC) - timedelta(hours=2))

    report = await LeadSearchSweeper(db_session).sweep()

    assert report.stale == [run_id]
    assert report.without_results == []

    run = await _run(client, headers, run_id)
    assert run["status"] == "failed"
    assert "No progress" in run["error"]
    assert run["finished_at"] is not None

    notifications = await client.get(f"{PREFIX}/notifications", headers=headers)
    newest = notifications.json()["items"][0]
    assert "LED screen" in newest["title"]
    assert "failed" in newest["title"]
    assert "No progress" in newest["subtitle"]

    # It no longer counts as active in the Searches list.
    active = await client.get(
        f"{PREFIX}/lead-radar/runs", params={"active_only": "true"}, headers=headers
    )
    assert active.json()["total"] == 0


async def test_a_fresh_run_is_left_alone(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers, run_id, _ = await _running_run(client, db_session, monkeypatch)

    report = await LeadSearchSweeper(db_session).sweep()

    assert report.closed == 0
    assert (await _run(client, headers, run_id))["status"] == "running"


async def test_a_checkpoint_counts_as_activity(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An old run that just reported progress is alive, whatever its age."""
    headers, run_id, token = await _running_run(client, db_session, monkeypatch)
    await _age(
        db_session,
        run_id,
        created_at=datetime.now(UTC) - timedelta(hours=3),
        updated_at=datetime.now(UTC) - timedelta(hours=3),
    )

    reported = await client.post(
        f"{PREFIX}/lead-radar/runs/{run_id}/progress",
        json={"stage": "finding_emails", "count": 12},
        headers={"X-LeadPilot-Run-Token": token},
    )
    assert reported.status_code == 202

    report = await LeadSearchSweeper(db_session).sweep()

    assert report.closed == 0
    assert (await _run(client, headers, run_id))["status"] == "running"


async def test_sweeping_twice_notifies_once(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers, run_id, _ = await _running_run(client, db_session, monkeypatch)
    await _age(db_session, run_id, updated_at=datetime.now(UTC) - timedelta(hours=2))

    first = await LeadSearchSweeper(db_session).sweep()
    second = await LeadSearchSweeper(db_session).sweep()

    assert first.closed == 1
    assert second.closed == 0

    notifications = await client.get(f"{PREFIX}/notifications", headers=headers)
    failures = [n for n in notifications.json()["items"] if "failed" in n["title"]]
    assert len(failures) == 1


# --- Completed but results never arrived -------------------------------------


async def test_a_completed_run_whose_results_never_came_is_failed(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers, run_id, token = await _running_run(client, db_session, monkeypatch)
    closed = await client.post(
        f"{PREFIX}/lead-radar/runs/{run_id}/progress",
        json={"stage": "verifying_contacts", "status": "completed"},
        headers={"X-LeadPilot-Run-Token": token},
    )
    assert closed.status_code == 202
    assert (await _run(client, headers, run_id))["status"] == "completed"

    await _age(db_session, run_id, finished_at=datetime.now(UTC) - timedelta(hours=1))

    report = await LeadSearchSweeper(db_session).sweep()

    assert report.without_results == [run_id]
    run = await _run(client, headers, run_id)
    assert run["status"] == "failed"
    assert run["error"] == NO_RESULTS_REASON
    assert run["results_received_at"] is None
    assert run["credits_charged"] == 0


async def test_a_completed_run_still_within_the_results_grace_is_left_alone(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers, run_id, token = await _running_run(client, db_session, monkeypatch)
    await client.post(
        f"{PREFIX}/lead-radar/runs/{run_id}/progress",
        json={"stage": "verifying_contacts", "status": "completed"},
        headers={"X-LeadPilot-Run-Token": token},
    )

    report = await LeadSearchSweeper(db_session).sweep()

    assert report.closed == 0
    assert (await _run(client, headers, run_id))["status"] == "completed"


async def test_a_run_whose_results_arrived_is_never_touched(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers, run_id, token = await _running_run(client, db_session, monkeypatch)
    results = await client.post(
        f"{PREFIX}/lead-radar/results",
        json={
            "run_id": run_id,
            "companies": [
                {"company_name": "Acme Displays", "website": "https://acme.test"}
            ],
        },
        headers={"X-LeadPilot-Run-Token": token},
    )
    assert results.status_code == 201

    await _age(
        db_session,
        run_id,
        finished_at=datetime.now(UTC) - timedelta(days=1),
        updated_at=datetime.now(UTC) - timedelta(days=1),
    )

    report = await LeadSearchSweeper(db_session).sweep()

    assert report.closed == 0
    run = await _run(client, headers, run_id)
    assert run["status"] == "completed"
    assert run["results_received_at"] is not None
