"""The failure shape of the results callback.

When the workflow finds companies but not one verifiable email, it POSTs

    {"status": "failed", "reason": "no_valid_emails_found",
     "message": "Zero valid emails were found for any decision makers ..."}

with no run_id in the body and no `error` key. That has to fail the run,
keep the reason so failures can be counted by cause, charge nothing, and
tell the user -- without the workflow having to rebuild the success payload.
"""

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.config import settings
from app.models.lead_search import LeadSearchRun
from app.schemas.lead import SearchResultsRequest
from app.services.workflow_errors import REASONS, explain
from tests.api.test_auth import signed_in_tokens
from tests.api.test_lead_results import _mock_n8n

PREFIX = settings.API_V1_PREFIX
SEARCH = f"{PREFIX}/lead-radar/search"
RESULTS = f"{PREFIX}/lead-radar/results"
BILLING = f"{PREFIX}/billing"

PRO_CREDITS = 4990

NO_EMAILS = {
    "status": "failed",
    "reason": "no_valid_emails_found",
    "message": (
        "Zero valid emails were found for any decision makers in this search batch."
    ),
}


@pytest.fixture(autouse=True)
def _webhook_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "N8N_WEBHOOK_URL", "https://n8n.test/webhook/leads")


async def _headers(client: AsyncClient, db_session: Any) -> dict[str, str]:
    tokens = await signed_in_tokens(client, db_session)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    # Funded so the start gate lets the search through.
    funded = await client.post(
        f"{BILLING}/subscriptions", json={"plan_code": "pro_yearly"}, headers=headers
    )
    assert funded.status_code == 201

    return headers


async def _start(
    client: AsyncClient, headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> tuple[str, str]:
    """Dispatch a run; returns (run_id, progress_token)."""
    captured = _mock_n8n(monkeypatch)
    response = await client.post(
        SEARCH,
        json={"original_keyword": "LED screen", "expanded_keywords": []},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["run_id"], captured["progress_token"]


async def _post(
    client: AsyncClient, token: str, body: dict, *, run_id_header: str | None = None
) -> Any:
    headers = {"X-LeadPilot-Run-Token": token}
    if run_id_header:
        headers["X-LeadPilot-Run-Id"] = run_id_header

    return await client.post(RESULTS, json=body, headers=headers)


async def _run(client: AsyncClient, headers: dict[str, str], run_id: str) -> dict:
    response = await client.get(f"{PREFIX}/lead-radar/runs/{run_id}", headers=headers)
    assert response.status_code == 200
    return response.json()


async def _balance(client: AsyncClient, headers: dict[str, str]) -> int:
    response = await client.get(f"{BILLING}/overview", headers=headers)
    return int(response.json()["balance"])


# --- Parsing the shape -------------------------------------------------------


def test_the_failure_payload_parses_without_a_run_id() -> None:
    payload = SearchResultsRequest.model_validate(NO_EMAILS)

    assert payload.run_id is None
    assert payload.is_failed
    assert payload.reason == "no_valid_emails_found"
    assert payload.error_text == NO_EMAILS["message"]
    assert payload.companies == []


def test_message_and_error_are_the_same_thing() -> None:
    by_error = SearchResultsRequest.model_validate({"status": "failed", "error": "boom"})
    by_message = SearchResultsRequest.model_validate(
        {"status": "failed", "message": "boom"}
    )

    assert by_error.error_text == "boom"
    assert by_message.error_text == "boom"


def test_status_is_case_insensitive() -> None:
    for spelling in ("failed", "FAILED", "Failed", " failed "):
        assert SearchResultsRequest.model_validate({"status": spelling}).is_failed


def test_a_reason_alone_means_failure() -> None:
    """A reason is only ever sent for a failure, even if status was left out."""
    payload = SearchResultsRequest.model_validate({"reason": "no_valid_emails_found"})

    assert payload.is_failed


def test_the_reason_is_normalised_to_a_code() -> None:
    payload = SearchResultsRequest.model_validate(
        {"status": "failed", "reason": "No Valid Emails Found!"}
    )

    assert payload.reason == "no_valid_emails_found"


def test_success_is_still_success() -> None:
    payload = SearchResultsRequest.model_validate({"run_id": "r", "status": "completed"})

    assert not payload.is_failed
    assert payload.error_text is None


def test_the_text_classifier_knows_this_failure() -> None:
    """A workflow that sends only the message still gets the right reason."""
    assert explain(NO_EMAILS["message"]) == "no_valid_emails_found"
    assert "no_valid_emails_found" in REASONS


# --- The endpoint ------------------------------------------------------------


async def test_no_valid_emails_fails_the_run_with_its_reason(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    headers = await _headers(client, db_session)
    run_id, token = await _start(client, headers, monkeypatch)

    response = await _post(client, token, NO_EMAILS, run_id_header=run_id)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["run_id"] == run_id
    assert body["status"] == "failed"
    assert body["companies_saved"] == 0

    run = await _run(client, headers, run_id)
    assert run["status"] == "failed"
    assert run["error_reason"] == "no_valid_emails_found"
    assert run["error"] == NO_EMAILS["message"]
    assert run["results_received_at"] is not None
    assert run["finished_at"] is not None


async def test_the_reason_is_stored_on_the_row(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    """A column, so failures can be counted by cause."""
    headers = await _headers(client, db_session)
    run_id, token = await _start(client, headers, monkeypatch)
    await _post(client, token, NO_EMAILS, run_id_header=run_id)

    row = (
        await db_session.execute(select(LeadSearchRun).where(LeadSearchRun.id == run_id))
    ).scalar_one()

    assert row.error_reason == "no_valid_emails_found"
    assert row.error == NO_EMAILS["message"]


async def test_run_id_in_the_body_still_works(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    headers = await _headers(client, db_session)
    run_id, token = await _start(client, headers, monkeypatch)

    response = await _post(client, token, {**NO_EMAILS, "run_id": run_id})

    assert response.status_code == 201
    run = await _run(client, headers, run_id)
    assert run["error_reason"] == "no_valid_emails_found"


async def test_no_run_id_anywhere_is_a_clear_error(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    headers = await _headers(client, db_session)
    _, token = await _start(client, headers, monkeypatch)

    response = await _post(client, token, NO_EMAILS)

    assert response.status_code == 422
    assert "run_id" in response.json()["error"]["message"]


async def test_a_wrong_token_is_refused(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    headers = await _headers(client, db_session)
    run_id, _ = await _start(client, headers, monkeypatch)

    response = await _post(client, "not-the-token", NO_EMAILS, run_id_header=run_id)

    assert response.status_code == 401


# --- Nothing is charged ------------------------------------------------------


async def test_a_no_emails_failure_costs_nothing(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    headers = await _headers(client, db_session)
    run_id, token = await _start(client, headers, monkeypatch)

    await _post(client, token, NO_EMAILS, run_id_header=run_id)

    assert await _balance(client, headers) == PRO_CREDITS
    run = await _run(client, headers, run_id)
    assert run["credits_charged"] == 0
    assert run["credits_charged_at"] is None


async def test_a_failure_after_the_completed_checkpoint_still_fails_the_run(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    """The final progress checkpoint often says "completed" seconds before the
    results POST. The results are the workflow's last word: a failure there
    must win, and must still charge nothing."""
    headers = await _headers(client, db_session)
    run_id, token = await _start(client, headers, monkeypatch)

    done = await client.post(
        f"{PREFIX}/lead-radar/runs/{run_id}/progress",
        json={"stage": "verifying_contacts", "status": "completed"},
        headers={"X-LeadPilot-Run-Token": token},
    )
    assert done.status_code == 202
    assert (await _run(client, headers, run_id))["status"] == "completed"

    await _post(client, token, NO_EMAILS, run_id_header=run_id)

    run = await _run(client, headers, run_id)
    assert run["status"] == "failed"
    assert run["error_reason"] == "no_valid_emails_found"
    assert run["credits_charged"] == 0
    assert await _balance(client, headers) == PRO_CREDITS


async def test_a_repeated_failure_post_is_harmless(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    headers = await _headers(client, db_session)
    run_id, token = await _start(client, headers, monkeypatch)

    for _ in range(3):
        repeat = await _post(client, token, NO_EMAILS, run_id_header=run_id)
        assert repeat.status_code == 201

    run = await _run(client, headers, run_id)
    assert run["status"] == "failed"
    assert run["error_reason"] == "no_valid_emails_found"
    assert await _balance(client, headers) == PRO_CREDITS


# --- The user hears about it -------------------------------------------------


async def test_the_user_is_notified_with_the_message(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    headers = await _headers(client, db_session)
    run_id, token = await _start(client, headers, monkeypatch)

    await _post(client, token, NO_EMAILS, run_id_header=run_id)

    response = await client.get(f"{PREFIX}/notifications", headers=headers)
    failed = [n for n in response.json()["items"] if "failed" in n["title"]]

    assert len(failed) == 1
    assert failed[0]["subtitle"] == NO_EMAILS["message"]


async def test_a_message_only_failure_is_classified_from_its_text(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    """A workflow that forgot `reason` still lands on the right cause."""
    headers = await _headers(client, db_session)
    run_id, token = await _start(client, headers, monkeypatch)

    await _post(
        client,
        token,
        {"status": "failed", "message": "Zero valid emails were found this time."},
        run_id_header=run_id,
    )

    run = await _run(client, headers, run_id)
    assert run["error_reason"] == "no_valid_emails_found"


async def test_the_dispatch_payload_tells_n8n_where_to_post_results(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    headers = await _headers(client, db_session)
    captured = _mock_n8n(monkeypatch)
    await client.post(
        SEARCH,
        json={"original_keyword": "LED screen", "expanded_keywords": []},
        headers=headers,
    )

    assert captured["results_url"].endswith("/lead-radar/results")
    assert captured["progress_token"]
