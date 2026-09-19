"""The Local Offline Business search: categories, dispatch, and its own stages."""

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
CATEGORIES = f"{PREFIX}/lead-radar/local/categories"
RELATED = f"{PREFIX}/lead-radar/local/categories/related"

B2B_URL = "https://n8n.test/webhook/leads"
LOCAL_URL = "https://n8n.test/webhook/local-leads"

LOCAL_SEARCH = {
    "search_type": "local",
    "original_keyword": "dentist",
    "expanded_keywords": [],
    "location": "  Brooklyn,   New York ",
    "country": "us",
    "business_categories": ["dentist", "Dental clinic"],
    "min_rating": 4.0,
    "max_rating": 5.0,
    "min_reviews": 20,
}


@pytest.fixture(autouse=True)
def _webhooks_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "N8N_WEBHOOK_URL", B2B_URL)
    monkeypatch.setattr(settings, "N8N_LOCAL_WEBHOOK_URL", LOCAL_URL)
    monkeypatch.setattr(settings, "PUBLIC_API_URL", "https://api.leadpilot.test")
    monkeypatch.setattr(settings, "LOCAL_SEARCH_RESULT_LIMIT", 60)


def _mock_n8n(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Capture what the backend sends to n8n, and to which webhook."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"message": "Workflow was started"})

    original_init = lead_search_module.LeadSearchService.__init__

    def patched_init(self: Any, client: httpx.AsyncClient | None = None) -> None:
        original_init(self, httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    monkeypatch.setattr(lead_search_module.LeadSearchService, "__init__", patched_init)

    return captured


async def _signed_in(client: AsyncClient, db_session: Any) -> dict[str, str]:
    tokens = await signed_in_tokens(client, db_session)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    funded = await client.post(
        f"{PREFIX}/billing/subscriptions",
        json={"plan_code": "pro_yearly"},
        headers=headers,
    )
    assert funded.status_code == 201

    return headers


# --- Categories ----------------------------------------------------------------


async def test_categories_need_a_session(client: AsyncClient) -> None:
    assert (await client.get(CATEGORIES, params={"q": "dent"})).status_code == 401


async def test_category_type_ahead_puts_the_best_match_first(
    client: AsyncClient, db_session: Any
) -> None:
    headers = await _signed_in(client, db_session)

    response = await client.get(
        CATEGORIES, params={"q": "dent", "limit": 5}, headers=headers
    )

    assert response.status_code == 200
    body = response.json()
    assert body["items"][0] == "Dentist"
    assert "Dental clinic" in body["items"]
    assert len(body["items"]) == 5
    assert body["total"] > 3000


async def test_related_categories_follow_the_keyword(
    client: AsyncClient, db_session: Any
) -> None:
    headers = await _signed_in(client, db_session)

    response = await client.get(RELATED, params={"keyword": "dentist"}, headers=headers)

    items = response.json()["items"]
    assert "Dental clinic" in items
    assert "Cosmetic dentist" in items


async def test_related_categories_leave_out_the_ones_already_picked(
    client: AsyncClient, db_session: Any
) -> None:
    headers = await _signed_in(client, db_session)

    response = await client.get(
        RELATED,
        params={"keyword": "dentist", "categories": ["Dentist", "Dental clinic"]},
        headers=headers,
    )

    items = response.json()["items"]
    assert "Dentist" not in items
    assert "Dental clinic" not in items
    assert "Cosmetic dentist" in items


async def test_nothing_to_go_on_suggests_nothing(
    client: AsyncClient, db_session: Any
) -> None:
    headers = await _signed_in(client, db_session)

    response = await client.get(RELATED, headers=headers)

    assert response.json()["items"] == []


# --- Dispatch --------------------------------------------------------------------


async def test_a_local_search_goes_to_its_own_workflow_with_the_api_query_ready(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _mock_n8n(monkeypatch)
    headers = await _signed_in(client, db_session)

    response = await client.post(SEARCH, json=LOCAL_SEARCH, headers=headers)

    assert response.status_code == 200
    started = response.json()
    assert started["search_type"] == "local"
    assert started["location"] == "Brooklyn, New York"
    # Returned in Google's own spelling.
    assert started["business_categories"] == ["Dentist", "Dental clinic"]

    assert captured["url"] == LOCAL_URL
    body = captured["body"]
    assert body["search_type"] == "local"
    assert body["run_id"] == started["run_id"]
    assert body["progress_token"]
    assert body["progress_url"].endswith(f"/runs/{started['run_id']}/progress")
    assert body["location"] == "Brooklyn, New York"

    # Shaped as the Local Business Data API's /search query string.
    assert body["local_search"] == {
        "query": "dentist in Brooklyn, New York",
        "queries": ["dentist in Brooklyn, New York"],
        "limit": 60,
        "subtypes": "Dentist,Dental clinic",
        "region": "us",
        "language": "en",
        "business_status": "OPEN",
        "extract_emails_and_contacts": True,
    }

    # The API cannot filter on these; the workflow applies them afterwards.
    assert body["filters"] == {
        "enabled": True,
        "min_rating": 4.0,
        "max_rating": 5.0,
        "min_reviews": 20,
        "max_reviews": None,
    }


async def test_no_categories_and_no_window_is_a_complete_local_search(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _mock_n8n(monkeypatch)
    headers = await _signed_in(client, db_session)

    response = await client.post(
        SEARCH,
        json={
            "search_type": "local",
            "original_keyword": "coffee shop",
            "location": "Lahore",
        },
        headers=headers,
    )

    assert response.status_code == 200
    body = captured["body"]
    assert body["local_search"]["subtypes"] is None
    assert body["local_search"]["region"] is None
    assert body["filters"]["enabled"] is False


async def test_every_selected_keyword_becomes_a_query(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _mock_n8n(monkeypatch)
    headers = await _signed_in(client, db_session)

    await client.post(
        SEARCH,
        json={
            "search_type": "local",
            "original_keyword": "dentist",
            "expanded_keywords": ["orthodontist"],
            "location": "Leeds",
        },
        headers=headers,
    )

    assert captured["body"]["local_search"]["queries"] == [
        "dentist in Leeds",
        "orthodontist in Leeds",
    ]


async def test_b2b_only_filters_are_dropped_from_a_local_search(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _mock_n8n(monkeypatch)
    headers = await _signed_in(client, db_session)

    response = await client.post(
        SEARCH,
        json={**LOCAL_SEARCH, "company_type": "retailer", "contact_role": "c_level"},
        headers=headers,
    )

    assert response.status_code == 200
    assert captured["body"]["company_type"] is None
    assert captured["body"]["contact_role"] is None


async def test_a_b2b_search_is_unchanged_and_ignores_local_fields(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _mock_n8n(monkeypatch)
    headers = await _signed_in(client, db_session)

    response = await client.post(
        SEARCH,
        json={
            "original_keyword": "LED screen",
            "location": "Brooklyn",
            "business_categories": ["Dentist"],
            "min_rating": 4,
        },
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["search_type"] == "b2b"
    assert captured["url"] == B2B_URL
    assert captured["body"]["search_type"] == "b2b"
    assert "local_search" not in captured["body"]
    assert "filters" not in captured["body"]

    run = await client.get(
        f"{PREFIX}/lead-radar/runs/{response.json()['run_id']}", headers=headers
    )
    assert run.json()["search_type"] == "b2b"
    assert run.json()["location"] is None
    assert run.json()["business_categories"] == []


# --- Validation ------------------------------------------------------------------


@pytest.mark.parametrize(
    "override",
    [
        {"location": None},
        {"location": "   "},
        {"business_categories": ["Definitely not a Google category"]},
        {"min_rating": 4.5, "max_rating": 3.0},
        {"min_rating": 0.5},
        {"max_rating": 5.5},
        {"min_reviews": 500, "max_reviews": 10},
        {"min_reviews": -1},
        {"search_type": "regional"},
    ],
)
async def test_a_local_search_that_makes_no_sense_is_refused(
    client: AsyncClient,
    db_session: Any,
    monkeypatch: pytest.MonkeyPatch,
    override: dict[str, Any],
) -> None:
    captured = _mock_n8n(monkeypatch)
    headers = await _signed_in(client, db_session)

    response = await client.post(
        SEARCH, json={**LOCAL_SEARCH, **override}, headers=headers
    )

    assert response.status_code == 422
    assert captured == {}


async def test_local_search_not_configured_fails_the_run_instead_of_stranding_it(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_n8n(monkeypatch)
    monkeypatch.setattr(settings, "N8N_LOCAL_WEBHOOK_URL", "")
    headers = await _signed_in(client, db_session)

    response = await client.post(SEARCH, json=LOCAL_SEARCH, headers=headers)

    assert response.status_code == 503

    runs = await client.get(f"{PREFIX}/lead-radar/runs", headers=headers)
    newest = runs.json()["items"][0]
    assert newest["search_type"] == "local"
    assert newest["status"] == "failed"

    # The B2B search is a separate workflow and is unaffected.
    b2b = await client.post(
        SEARCH, json={"original_keyword": "LED screen"}, headers=headers
    )
    assert b2b.status_code == 200


# --- The run and its own checkpoints ---------------------------------------------


async def test_a_local_run_remembers_its_scope_and_has_its_own_stage_list(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _mock_n8n(monkeypatch)
    headers = await _signed_in(client, db_session)
    run_id = (await client.post(SEARCH, json=LOCAL_SEARCH, headers=headers)).json()[
        "run_id"
    ]
    token = captured["body"]["progress_token"]

    run = (await client.get(f"{PREFIX}/lead-radar/runs/{run_id}", headers=headers)).json()
    assert run["search_type"] == "local"
    assert run["location"] == "Brooklyn, New York"
    assert run["business_categories"] == ["Dentist", "Dental clinic"]
    assert (run["min_rating"], run["max_rating"]) == (4.0, 5.0)
    assert (run["min_reviews"], run["max_reviews"]) == (20, None)
    assert run["stage_order"] == [
        "searching_companies",
        "filtering_results",
        "finding_emails",
        "verifying_contacts",
    ]

    for stage, count in (("searching_companies", 60), ("filtering_results", 41)):
        reported = await client.post(
            f"{PREFIX}/lead-radar/runs/{run_id}/progress",
            json={"stage": stage, "count": count, "execution_id": "500"},
            headers={"X-LeadPilot-Run-Token": token},
        )
        assert reported.status_code == 202

    run = (await client.get(f"{PREFIX}/lead-radar/runs/{run_id}", headers=headers)).json()
    assert run["stage"] == "filtering_results"
    # The count after filtering replaces the raw count from the search.
    assert run["companies_found"] == 41

    # A late, earlier checkpoint cannot drag a local run backwards either.
    await client.post(
        f"{PREFIX}/lead-radar/runs/{run_id}/progress",
        json={"stage": "searching_companies", "count": 60},
        headers={"X-LeadPilot-Run-Token": token},
    )
    run = (await client.get(f"{PREFIX}/lead-radar/runs/{run_id}", headers=headers)).json()
    assert run["stage"] == "filtering_results"


async def test_listings_are_saved_like_any_other_results(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The local workflow maps a Maps listing onto the same results contract;
    the fields with no column of their own (rating, review_count, ...) ride
    along as extras."""
    captured = _mock_n8n(monkeypatch)
    headers = await _signed_in(client, db_session)
    run_id = (await client.post(SEARCH, json=LOCAL_SEARCH, headers=headers)).json()[
        "run_id"
    ]

    saved = await client.post(
        f"{PREFIX}/lead-radar/results",
        json={
            "run_id": run_id,
            "companies": [
                {
                    "company_name": "Brooklyn Smiles Dental",
                    "website": "https://brooklynsmiles.test",
                    "location": "123 Court St, Brooklyn, NY 11201",
                    "industry": "Dentist",
                    "hq_phone": "+1 718 555 0100",
                    "rating": 4.8,
                    "review_count": 212,
                    "place_link": "https://maps.google.com/?cid=1",
                }
            ],
        },
        headers={"X-LeadPilot-Run-Token": captured["body"]["progress_token"]},
    )
    assert saved.status_code == 201

    results = await client.get(
        f"{PREFIX}/lead-radar/runs/{run_id}/results", headers=headers
    )
    company = results.json()[0]
    assert company["company_name"] == "Brooklyn Smiles Dental"
    assert company["industry"] == "Dentist"
    assert company["hq_phone"] == "+1 718 555 0100"
