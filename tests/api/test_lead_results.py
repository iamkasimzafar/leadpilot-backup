"""The final results callback: the payload n8n posts when a search finishes."""

from typing import Any

import httpx
import pytest
from httpx import AsyncClient

from app.core.config import settings
from app.services import lead_search as lead_search_module
from tests.api.test_auth import signed_in_tokens

PREFIX = settings.API_V1_PREFIX
SEARCH = f"{PREFIX}/lead-radar/search"
RESULTS = f"{PREFIX}/lead-radar/results"

# The shape from the user's n8n workflow, verbatim.
SAMPLE_COMPANY = {
    "company_name": "DVSLEDSYSTEMS",
    "website": "https://dvsledsystems.com",
    "location": "N/A",
    "industry": "Electrical & Electronic Manufacturing",
    "company_size": "11-50",
    "hq_phone": "8135638005",
    "decision_makers": [
        {
            "full_name": "Cruze Hutcheson",
            "job_title": "Director Of Operations",
            "verified_email": "cruze@dvsledsystems.com",
            "email_status": "valid",
            "linkedin_url": "https://www.linkedin.com/in/cruze-hutcheson-790775162",
            "phone_number": "8135638005",
            "whatsapp_status": "Not Active",
        },
        {
            "full_name": "Joshua Carroll",
            "job_title": "Senior Project Manager",
            "verified_email": "jcarroll@dvsledsystems.com",
            "email_status": "valid",
            "linkedin_url": "https://www.linkedin.com/in/joshua-carroll-04132942",
            "phone_number": "8135638005",
            "whatsapp_status": "Not Active",
        },
    ],
}


@pytest.fixture(autouse=True)
def _webhook_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "N8N_WEBHOOK_URL", "https://n8n.test/webhook/leads")
    monkeypatch.setattr(settings, "PUBLIC_API_URL", "https://api.leadpilot.test")


def _mock_n8n(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    original_init = lead_search_module.LeadSearchService.__init__

    def patched_init(self: Any, client: httpx.AsyncClient | None = None) -> None:
        original_init(self, httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    monkeypatch.setattr(lead_search_module.LeadSearchService, "__init__", patched_init)

    return captured


async def _fund(client: AsyncClient, headers: dict[str, str]) -> None:
    """Put credits in the wallet: the start gate refuses a search the balance
    could not cover. Pro yearly grants 4,990, enough for any run here."""
    response = await client.post(
        f"{PREFIX}/billing/subscriptions",
        json={"plan_code": "pro_yearly"},
        headers=headers,
    )
    assert response.status_code == 201


async def _setup(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, str], str, str]:
    """Sign in, fund the wallet, start a run, and return
    (headers, run_id, progress_token)."""
    captured = _mock_n8n(monkeypatch)
    tokens = await signed_in_tokens(client, db_session)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    await _fund(client, headers)

    response = await client.post(
        SEARCH,
        json={"original_keyword": "LED screen", "expanded_keywords": []},
        headers=headers,
    )
    assert response.status_code == 200

    return headers, response.json()["run_id"], captured["progress_token"]


async def _post_results(
    client: AsyncClient, run_id: str, token: str, **payload: Any
) -> httpx.Response:
    body = {"run_id": run_id, "status": "completed", **payload}

    return await client.post(RESULTS, json=body, headers={"X-LeadPilot-Run-Token": token})


# --- Happy path --------------------------------------------------------------


async def test_results_are_saved_with_their_decision_makers(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers, run_id, token = await _setup(client, db_session, monkeypatch)

    response = await _post_results(
        client, run_id, token, total_companies=1, companies=[SAMPLE_COMPANY]
    )

    assert response.status_code == 201
    body = response.json()
    assert body["companies_saved"] == 1
    assert body["companies_updated"] == 0
    assert body["contacts_saved"] == 2
    assert body["total_companies"] == 1
    assert body["status"] == "completed"

    listed = await client.get(
        f"{PREFIX}/lead-radar/runs/{run_id}/results", headers=headers
    )
    companies = listed.json()
    assert len(companies) == 1
    company = companies[0]
    assert company["company_name"] == "DVSLEDSYSTEMS"
    assert company["website"] == "https://dvsledsystems.com"
    assert company["industry"] == "Electrical & Electronic Manufacturing"
    # "N/A" from the workflow is stored as null, not shown to the user.
    assert company["location"] is None

    names = [p["full_name"] for p in company["decision_makers"]]
    assert names == ["Cruze Hutcheson", "Joshua Carroll"]
    assert company["decision_makers"][0]["email_status"] == "valid"


async def test_posting_results_completes_the_run_and_notifies(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers, run_id, token = await _setup(client, db_session, monkeypatch)

    await _post_results(client, run_id, token, companies=[SAMPLE_COMPANY])

    run = (await client.get(f"{PREFIX}/lead-radar/runs/{run_id}", headers=headers)).json()
    assert run["status"] == "completed"
    assert run["stage"] == "completed"
    assert run["companies_found"] == 1
    assert run["contacts_found"] == 2
    assert run["finished_at"] is not None

    notifications = await client.get(f"{PREFIX}/notifications", headers=headers)
    newest = notifications.json()["items"][0]
    assert "1 companies added to My Leads" in newest["title"]
    assert newest["link"] == "/my-leads"


async def test_an_empty_result_set_is_accepted(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A search that found nothing must still close cleanly."""
    headers, run_id, token = await _setup(client, db_session, monkeypatch)

    response = await _post_results(client, run_id, token, total_companies=0, companies=[])

    assert response.status_code == 201
    assert response.json()["total_companies"] == 0

    run = (await client.get(f"{PREFIX}/lead-radar/runs/{run_id}", headers=headers)).json()
    assert run["status"] == "completed"


async def test_results_are_acknowledged_even_when_progress_closed_the_run_first(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real sequence: the last progress checkpoint sends status=completed,
    and the results POST lands a few seconds later. The run is already closed
    by then -- the results must still be recorded, flagged as received, and
    announced, or the UI waits forever on "collecting results"."""
    headers, run_id, token = await _setup(client, db_session, monkeypatch)

    closed = await client.post(
        f"{PREFIX}/lead-radar/runs/{run_id}/progress",
        json={"stage": "verifying_contacts", "status": "completed"},
        headers={"X-LeadPilot-Run-Token": token},
    )
    assert closed.status_code == 202
    before = (
        await client.get(f"{PREFIX}/lead-radar/runs/{run_id}", headers=headers)
    ).json()
    assert before["status"] == "completed"
    assert before["results_received_at"] is None

    response = await _post_results(client, run_id, token, companies=[SAMPLE_COMPANY])

    assert response.status_code == 201
    after = (
        await client.get(f"{PREFIX}/lead-radar/runs/{run_id}", headers=headers)
    ).json()
    assert after["results_received_at"] is not None
    assert after["companies_found"] == 1
    assert sum(1 for e in after["events"] if e["stage"] == "completed") == 1

    listed = await client.get(
        f"{PREFIX}/lead-radar/runs/{run_id}/results", headers=headers
    )
    assert len(listed.json()) == 1

    notifications = await client.get(f"{PREFIX}/notifications", headers=headers)
    titles = [n["title"] for n in notifications.json()["items"]]
    assert any("added to My Leads" in t for t in titles)


# --- Idempotency -------------------------------------------------------------


async def test_reposting_the_same_results_does_not_duplicate(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """n8n retries on failure, so the same payload may arrive twice."""
    headers, run_id, token = await _setup(client, db_session, monkeypatch)

    await _post_results(client, run_id, token, companies=[SAMPLE_COMPANY])
    second = await _post_results(client, run_id, token, companies=[SAMPLE_COMPANY])

    assert second.status_code == 201
    body = second.json()
    assert body["companies_saved"] == 0
    assert body["companies_updated"] == 1
    assert body["total_companies"] == 1

    listed = await client.get(
        f"{PREFIX}/lead-radar/runs/{run_id}/results", headers=headers
    )
    companies = listed.json()
    assert len(companies) == 1
    # Contacts are replaced, not appended.
    assert len(companies[0]["decision_makers"]) == 2


# --- Tolerating the workflow's real-world output ------------------------------


async def test_placeholder_values_become_null(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers, run_id, token = await _setup(client, db_session, monkeypatch)

    await _post_results(
        client,
        run_id,
        token,
        companies=[
            {
                "company_name": "Placeholder Co",
                "website": "",
                "location": "N/A",
                "industry": "-",
                "company_size": "unknown",
                "hq_phone": "  ",
                "decision_makers": [],
            }
        ],
    )

    listed = await client.get(
        f"{PREFIX}/lead-radar/runs/{run_id}/results", headers=headers
    )
    company = listed.json()[0]
    assert company["company_name"] == "Placeholder Co"
    for field in ("website", "location", "industry", "company_size", "hq_phone"):
        assert company[field] is None


async def test_a_company_without_contacts_is_accepted(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers, run_id, token = await _setup(client, db_session, monkeypatch)

    response = await _post_results(
        client,
        run_id,
        token,
        companies=[{"company_name": "Solo Ltd", "decision_makers": None}],
    )

    assert response.status_code == 201
    assert response.json()["contacts_saved"] == 0

    listed = await client.get(
        f"{PREFIX}/lead-radar/runs/{run_id}/results", headers=headers
    )
    assert listed.json()[0]["decision_makers"] == []


async def test_unknown_fields_do_not_break_the_endpoint(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new field in the n8n workflow must never 422 the whole payload."""
    headers, run_id, token = await _setup(client, db_session, monkeypatch)

    response = await _post_results(
        client,
        run_id,
        token,
        companies=[
            {
                "company_name": "Future Corp",
                "revenue_band": "10M-50M",
                "decision_makers": [
                    {"full_name": "Ada Lovelace", "seniority": "C-level"}
                ],
            }
        ],
    )

    assert response.status_code == 201
    listed = await client.get(
        f"{PREFIX}/lead-radar/runs/{run_id}/results", headers=headers
    )
    assert listed.json()[0]["decision_makers"][0]["full_name"] == "Ada Lovelace"


async def test_a_failed_status_fails_the_run(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers, run_id, token = await _setup(client, db_session, monkeypatch)

    response = await client.post(
        RESULTS,
        json={
            "run_id": run_id,
            "status": "failed",
            "error": "Apollo quota exhausted.",
            "companies": [],
        },
        headers={"X-LeadPilot-Run-Token": token},
    )

    assert response.status_code == 201
    run = (await client.get(f"{PREFIX}/lead-radar/runs/{run_id}", headers=headers)).json()
    assert run["status"] == "failed"
    assert run["error"] == "Apollo quota exhausted."


# --- Concurrency ----------------------------------------------------------------


async def test_repeated_results_posts_finish_the_run_only_once(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An n8n HTTP node runs once per input item, so the results POST can land
    many times, each carrying the full company list. Companies are refreshed,
    but the run closes -- and the user is told -- exactly once.

    (True parallelism cannot be exercised here: the test client shares one
    session across requests. The live-server burst in the scratchpad covers
    that; this pins the finish-once logic deterministically.)
    """
    headers, run_id, token = await _setup(client, db_session, monkeypatch)

    for _ in range(5):
        response = await _post_results(client, run_id, token, companies=[SAMPLE_COMPANY])
        assert response.status_code == 201

    listed = await client.get(
        f"{PREFIX}/lead-radar/runs/{run_id}/results", headers=headers
    )
    companies = listed.json()
    assert len(companies) == 1
    assert len(companies[0]["decision_makers"]) == 2

    run = (await client.get(f"{PREFIX}/lead-radar/runs/{run_id}", headers=headers)).json()
    assert run["status"] == "completed"
    assert run["companies_found"] == 1
    # One completion event, not five.
    assert sum(1 for e in run["events"] if e["stage"] == "completed") == 1

    notifications = await client.get(f"{PREFIX}/notifications", headers=headers)
    finished = [
        n for n in notifications.json()["items"] if "added to My Leads" in n["title"]
    ]
    assert len(finished) == 1


async def test_a_mysql_deadlock_is_retried(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MySQL error 1213 rolls the transaction back and asks for a retry; the
    route must oblige rather than surface a 500 to n8n."""
    from sqlalchemy.exc import OperationalError

    from app.services import lead_results as lead_results_module

    headers, run_id, token = await _setup(client, db_session, monkeypatch)

    class _FakeDeadlockError(Exception):
        args = (
            1213,
            "Deadlock found when trying to get lock; try restarting transaction",
        )

    attempts = {"n": 0}
    real_ingest = lead_results_module.LeadResultsService.ingest

    async def flaky_ingest(self: Any, run: Any, payload: Any) -> Any:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise OperationalError("UPDATE company ...", {}, _FakeDeadlockError())
        return await real_ingest(self, run, payload)

    monkeypatch.setattr(lead_results_module.LeadResultsService, "ingest", flaky_ingest)

    response = await _post_results(client, run_id, token, companies=[SAMPLE_COMPANY])

    assert response.status_code == 201
    assert attempts["n"] == 3
    listed = await client.get(
        f"{PREFIX}/lead-radar/runs/{run_id}/results", headers=headers
    )
    assert len(listed.json()) == 1


def test_is_deadlock_recognises_only_mysql_1213() -> None:
    from sqlalchemy.exc import OperationalError

    from app.services.lead_results import is_deadlock

    class _OrigError(Exception):
        def __init__(self, code: int) -> None:
            super().__init__(code, "msg")

    assert is_deadlock(OperationalError("stmt", {}, _OrigError(1213)))
    assert not is_deadlock(OperationalError("stmt", {}, _OrigError(1054)))
    assert not is_deadlock(ValueError("not a db error"))


# --- Authentication ----------------------------------------------------------


async def test_a_wrong_token_is_refused(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers, run_id, _ = await _setup(client, db_session, monkeypatch)

    response = await _post_results(
        client, run_id, "not-the-token", companies=[SAMPLE_COMPANY]
    )

    assert response.status_code == 401

    listed = await client.get(
        f"{PREFIX}/lead-radar/runs/{run_id}/results", headers=headers
    )
    assert listed.json() == []


async def test_a_missing_token_is_refused(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, run_id, _ = await _setup(client, db_session, monkeypatch)

    response = await client.post(
        RESULTS, json={"run_id": run_id, "status": "completed", "companies": []}
    )

    assert response.status_code == 422


async def test_results_are_not_readable_by_another_account(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.auth import AuthService

    _, run_id, token = await _setup(client, db_session, monkeypatch)
    await _post_results(client, run_id, token, companies=[SAMPLE_COMPANY])

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
        f"{PREFIX}/lead-radar/runs/{run_id}/results", headers=other_headers
    )

    assert response.status_code == 404
