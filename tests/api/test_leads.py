"""My Leads: auto-add on results, manual add, tabs, editing, deleting."""

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
LEADS = f"{PREFIX}/leads"


def _company(name: str, website: str, contacts: int = 1) -> dict[str, Any]:
    return {
        "company_name": name,
        "website": website,
        "industry": "Manufacturing",
        "company_size": "11-50",
        "decision_makers": [
            {
                "full_name": f"{name} Person {i}",
                "job_title": "Buyer",
                "verified_email": f"p{i}@{website.split('//')[1]}",
                "email_status": "valid",
            }
            for i in range(contacts)
        ],
    }


COMPANIES = [
    _company("Alpha Ltd", "https://alpha.test", 2),
    _company("Beta GmbH", "https://beta.test", 1),
    _company("Gamma Inc", "https://gamma.test", 3),
]


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


async def _run_search(
    client: AsyncClient,
    db_session: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    auto_add: bool,
    headers: dict[str, str] | None = None,
) -> tuple[dict[str, str], str]:
    """Sign in (unless headers given), start a search, post results.
    Returns (headers, run_id)."""
    captured = _mock_n8n(monkeypatch)

    if headers is None:
        tokens = await signed_in_tokens(client, db_session)
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}

        # The start gate refuses a search the balance could not cover. Pro
        # yearly grants 4,990 credits, enough for any run here.
        funded = await client.post(
            f"{PREFIX}/billing/subscriptions",
            json={"plan_code": "pro_yearly"},
            headers=headers,
        )
        assert funded.status_code == 201

    started = await client.post(
        SEARCH,
        json={
            "original_keyword": "LED screen",
            "expanded_keywords": [],
            "auto_add_to_leads": auto_add,
        },
        headers=headers,
    )
    assert started.status_code == 200
    run_id = started.json()["run_id"]

    posted = await client.post(
        RESULTS,
        json={"run_id": run_id, "status": "completed", "companies": COMPANIES},
        headers={"X-LeadPilot-Run-Token": captured["progress_token"]},
    )
    assert posted.status_code == 201

    return headers, run_id


async def _leads(client: AsyncClient, headers: dict[str, str], **params: Any) -> dict:
    response = await client.get(LEADS, headers=headers, params=params)
    assert response.status_code == 200
    return response.json()


async def _results(client: AsyncClient, headers: dict[str, str], run_id: str) -> list:
    response = await client.get(
        f"{PREFIX}/lead-radar/runs/{run_id}/results", headers=headers
    )
    assert response.status_code == 200
    return response.json()


# --- Auto-add ------------------------------------------------------------------


async def test_auto_add_puts_every_result_in_my_leads(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers, run_id = await _run_search(client, db_session, monkeypatch, auto_add=True)

    page = await _leads(client, headers)

    assert page["total"] == 3
    assert page["counts"] == {"all": 3, "new": 3, "contacted": 0, "interested": 0}
    assert all(item["in_leads"] for item in page["items"])
    assert all(item["status"] == "new" for item in page["items"])

    # The results view reflects it too.
    assert all(c["in_leads"] for c in await _results(client, headers, run_id))


async def test_auto_add_off_leaves_results_out_of_my_leads(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers, run_id = await _run_search(client, db_session, monkeypatch, auto_add=False)

    page = await _leads(client, headers)
    assert page["total"] == 0
    assert page["counts"]["all"] == 0

    results = await _results(client, headers, run_id)
    assert len(results) == 3
    assert not any(c["in_leads"] for c in results)


async def test_the_run_records_the_auto_add_choice(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers, run_id = await _run_search(client, db_session, monkeypatch, auto_add=False)

    run = (await client.get(f"{PREFIX}/lead-radar/runs/{run_id}", headers=headers)).json()

    assert run["auto_add_to_leads"] is False


# --- Manual add ----------------------------------------------------------------


async def test_adding_selected_companies(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers, run_id = await _run_search(client, db_session, monkeypatch, auto_add=False)
    results = await _results(client, headers, run_id)
    chosen = [results[0]["id"], results[2]["id"]]

    response = await client.post(
        f"{LEADS}/add", json={"company_ids": chosen}, headers=headers
    )

    assert response.status_code == 200
    assert response.json() == {"added": 2, "already_in_leads": 0, "total_in_leads": 2}

    page = await _leads(client, headers)
    assert sorted(item["id"] for item in page["items"]) == sorted(chosen)


async def test_adding_every_result_from_a_run(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers, run_id = await _run_search(client, db_session, monkeypatch, auto_add=False)

    response = await client.post(f"{LEADS}/add", json={"run_id": run_id}, headers=headers)

    assert response.status_code == 200
    assert response.json()["added"] == 3
    assert (await _leads(client, headers))["total"] == 3


async def test_adding_twice_does_not_duplicate(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers, run_id = await _run_search(client, db_session, monkeypatch, auto_add=False)

    await client.post(f"{LEADS}/add", json={"run_id": run_id}, headers=headers)
    second = await client.post(f"{LEADS}/add", json={"run_id": run_id}, headers=headers)

    assert second.json() == {"added": 0, "already_in_leads": 3, "total_in_leads": 3}


async def test_add_requires_ids_or_run(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers, _ = await _run_search(client, db_session, monkeypatch, auto_add=False)

    response = await client.post(f"{LEADS}/add", json={}, headers=headers)

    assert response.status_code == 422


# --- Tabs ------------------------------------------------------------------------


async def test_tabs_filter_by_status_and_counts_stay_account_wide(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers, _ = await _run_search(client, db_session, monkeypatch, auto_add=True)
    ids = [item["id"] for item in (await _leads(client, headers))["items"]]

    await client.post(
        f"{LEADS}/bulk-status",
        json={"ids": ids[:1], "status": "contacted"},
        headers=headers,
    )
    await client.post(
        f"{LEADS}/bulk-status",
        json={"ids": ids[1:2], "status": "interested"},
        headers=headers,
    )

    contacted = await _leads(client, headers, status="contacted")
    assert contacted["total"] == 1
    assert contacted["items"][0]["status"] == "contacted"
    # Counts describe the whole account, not the filtered page.
    assert contacted["counts"] == {"all": 3, "new": 1, "contacted": 1, "interested": 1}

    assert (await _leads(client, headers, status="new"))["total"] == 1
    assert (await _leads(client, headers, status="interested"))["total"] == 1
    assert (await _leads(client, headers))["total"] == 3


async def test_counts_endpoint(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers, _ = await _run_search(client, db_session, monkeypatch, auto_add=True)

    response = await client.get(f"{LEADS}/counts", headers=headers)

    assert response.status_code == 200
    assert response.json() == {"all": 3, "new": 3, "contacted": 0, "interested": 0}


# --- Editing -------------------------------------------------------------------


async def test_updating_a_lead(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers, _ = await _run_search(client, db_session, monkeypatch, auto_add=True)
    lead_id = (await _leads(client, headers))["items"][0]["id"]

    response = await client.patch(
        f"{LEADS}/{lead_id}",
        json={
            "company_name": "Renamed Co",
            "location": "Berlin",
            "status": "interested",
            "notes": "Met at trade fair.",
            "hq_phone": "N/A",
        },
        headers=headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["company_name"] == "Renamed Co"
    assert body["location"] == "Berlin"
    assert body["status"] == "interested"
    assert body["notes"] == "Met at trade fair."
    # Placeholder values are cleaned on edit too.
    assert body["hq_phone"] is None
    # Untouched fields survive a partial update.
    assert body["industry"] == "Manufacturing"


async def test_updating_a_contact(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers, _ = await _run_search(client, db_session, monkeypatch, auto_add=True)
    lead = (await _leads(client, headers))["items"][0]
    contact = lead["decision_makers"][0]

    response = await client.patch(
        f"{LEADS}/{lead['id']}/contacts/{contact['id']}",
        json={"job_title": "CEO", "phone_number": "+1 555 0100"},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["job_title"] == "CEO"
    assert response.json()["phone_number"] == "+1 555 0100"
    assert response.json()["full_name"] == contact["full_name"]


async def test_deleting_a_contact(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers, _ = await _run_search(client, db_session, monkeypatch, auto_add=True)
    page = await _leads(client, headers)
    lead = next(item for item in page["items"] if len(item["decision_makers"]) == 3)
    contact_id = lead["decision_makers"][0]["id"]

    response = await client.delete(
        f"{LEADS}/{lead['id']}/contacts/{contact_id}", headers=headers
    )

    assert response.status_code == 204
    refreshed = (await client.get(f"{LEADS}/{lead['id']}", headers=headers)).json()
    assert len(refreshed["decision_makers"]) == 2


# --- Deleting ------------------------------------------------------------------


async def test_deleting_one_lead(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers, _ = await _run_search(client, db_session, monkeypatch, auto_add=True)
    lead_id = (await _leads(client, headers))["items"][0]["id"]

    response = await client.delete(f"{LEADS}/{lead_id}", headers=headers)

    assert response.status_code == 204
    assert (await _leads(client, headers))["total"] == 2
    assert (await client.get(f"{LEADS}/{lead_id}", headers=headers)).status_code == 404


async def test_bulk_delete(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers, _ = await _run_search(client, db_session, monkeypatch, auto_add=True)
    ids = [item["id"] for item in (await _leads(client, headers))["items"]]

    response = await client.post(
        f"{LEADS}/bulk-delete", json={"ids": ids[:2]}, headers=headers
    )

    assert response.status_code == 200
    assert response.json() == {"affected": 2}
    assert (await _leads(client, headers))["total"] == 1


# --- Isolation ------------------------------------------------------------------


async def test_leads_are_scoped_to_their_owner(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.auth import AuthService

    owner_headers, _ = await _run_search(client, db_session, monkeypatch, auto_add=True)
    owner_ids = [item["id"] for item in (await _leads(client, owner_headers))["items"]]

    service = AuthService(db_session)
    other, _ = await service.register("other@leadpilot.io", "other-password-1", "Other")
    other.is_verified = True
    await service.commit()
    login = await client.post(
        f"{PREFIX}/auth/login",
        json={"email": "other@leadpilot.io", "password": "other-password-1"},
    )
    other_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    # Sees nothing...
    assert (await _leads(client, other_headers))["total"] == 0

    # ...cannot read, edit, re-status, or delete the owner's leads...
    assert (
        await client.get(f"{LEADS}/{owner_ids[0]}", headers=other_headers)
    ).status_code == 404
    assert (
        await client.patch(
            f"{LEADS}/{owner_ids[0]}", json={"notes": "hijack"}, headers=other_headers
        )
    ).status_code == 404
    bulk = await client.post(
        f"{LEADS}/bulk-status",
        json={"ids": owner_ids, "status": "contacted"},
        headers=other_headers,
    )
    assert bulk.json() == {"affected": 0}
    gone = await client.post(
        f"{LEADS}/bulk-delete", json={"ids": owner_ids}, headers=other_headers
    )
    assert gone.json() == {"affected": 0}

    # ...and cannot add them to their own list.
    added = await client.post(
        f"{LEADS}/add", json={"company_ids": owner_ids}, headers=other_headers
    )
    assert added.json()["added"] == 0

    # The owner's leads are untouched.
    owner_page = await _leads(client, owner_headers)
    assert owner_page["total"] == 3
    assert all(item["status"] == "new" for item in owner_page["items"])


async def test_leads_require_auth(client: AsyncClient) -> None:
    assert (await client.get(LEADS)).status_code == 401
