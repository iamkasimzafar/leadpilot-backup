"""The dashboard summary: every figure must be a real count of the user's rows."""

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from httpx import AsyncClient

from app.core.config import settings
from app.services import lead_search as lead_search_module
from app.services.billing_catalog import BASE_CONTACT_CREDIT
from app.services.dashboard import local_day_start_utc
from tests.api.test_auth import signed_in_tokens

PREFIX = settings.API_V1_PREFIX
SUMMARY = f"{PREFIX}/dashboard/summary"


def _company(name: str, website: str, contacts: int) -> dict[str, Any]:
    return {
        "company_name": name,
        "website": website,
        "decision_makers": [
            {
                "full_name": f"{name} {i}",
                "verified_email": f"p{i}@{website[8:]}",
                "email_status": "valid",
            }
            for i in range(contacts)
        ],
    }


@pytest.fixture(autouse=True)
def _webhook_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "N8N_WEBHOOK_URL", "https://n8n.test/webhook/leads")
    monkeypatch.setattr(settings, "PUBLIC_API_URL", "https://api.leadpilot.test")


def _mock_n8n(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Point the dispatcher at a fake n8n and return the dict it fills in.

    Idempotent within a test: a second call returns the same dict instead of
    wrapping the already-patched constructor (which would leave the newest
    dict empty). monkeypatch undoes the patch after each test.
    """
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


async def _headers(client: AsyncClient, db_session: Any) -> dict[str, str]:
    """Sign in, leaving the wallet empty.

    Some of these tests assert on a fresh account's zeros, so funding is done
    per test with `_subscribe` rather than here.
    """
    tokens = await signed_in_tokens(client, db_session)

    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def _subscribe(
    client: AsyncClient, headers: dict[str, str], plan: str = "pro_yearly"
) -> None:
    """Fund the wallet. The start gate refuses a search the balance could not
    cover, so any test that runs a search has to subscribe first."""
    response = await client.post(
        f"{settings.API_V1_PREFIX}/billing/subscriptions",
        json={"plan_code": plan},
        headers=headers,
    )
    assert response.status_code == 201


async def _summary(client: AsyncClient, headers: dict[str, str], **params: Any) -> dict:
    response = await client.get(SUMMARY, headers=headers, params=params)
    assert response.status_code == 200
    return response.json()


async def _search_with_results(
    client: AsyncClient,
    headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    *,
    keyword: str,
    companies: list[dict[str, Any]],
    auto_add: bool = True,
) -> str:
    captured = _mock_n8n(monkeypatch)
    started = await client.post(
        f"{PREFIX}/lead-radar/search",
        json={
            "original_keyword": keyword,
            "expanded_keywords": [],
            "auto_add_to_leads": auto_add,
        },
        headers=headers,
    )
    assert started.status_code == 200
    run_id = started.json()["run_id"]

    posted = await client.post(
        f"{PREFIX}/lead-radar/results",
        json={"run_id": run_id, "status": "completed", "companies": companies},
        headers={"X-LeadPilot-Run-Token": captured["progress_token"]},
    )
    assert posted.status_code == 201

    return run_id


# --- Figures -----------------------------------------------------------------


async def test_a_fresh_account_shows_zeros_not_placeholders(
    client: AsyncClient, db_session
) -> None:
    headers = await _headers(client, db_session)

    body = await _summary(client, headers)

    assert body["new_leads_today"] == 0
    assert body["awaiting_contact"] == 0
    assert body["searches_today"] == 0
    assert body["contacts_found_today"] == 0
    assert body["credits_left"] == 0
    assert body["leads_total"] == 0
    assert body["recent_runs"] == []


async def test_figures_reflect_real_activity(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = await _headers(client, db_session)

    # Credits come from the wallet, not a constant.
    subscribed = await client.post(
        f"{PREFIX}/billing/subscriptions",
        json={"plan_code": "starter_monthly"},
        headers=headers,
    )
    assert subscribed.status_code == 201

    run_id = await _search_with_results(
        client,
        headers,
        monkeypatch,
        keyword="LED screen",
        companies=[
            _company("Alpha", "https://alpha.test", 2),
            _company("Beta", "https://beta.test", 1),
            _company("Gamma", "https://gamma.test", 3),
        ],
    )

    body = await _summary(client, headers)

    assert body["new_leads_today"] == 3
    assert body["awaiting_contact"] == 3
    assert body["searches_today"] == 1
    assert body["contacts_found_today"] == 6
    # Starter grants 490. The search is then settled from what came back:
    # 10 credits per verified-email contact, no run fee or company charge.
    expected_charge = 6 * BASE_CONTACT_CREDIT
    assert body["credits_left"] == 490 - expected_charge
    assert body["leads_total"] == 3

    recent = body["recent_runs"]
    assert len(recent) == 1
    assert recent[0]["id"] == run_id
    assert recent[0]["original_keyword"] == "LED screen"
    assert recent[0]["status"] == "completed"
    assert recent[0]["companies_found"] == 3
    assert recent[0]["contacts_found"] == 6
    assert recent[0]["results_received_at"] is not None


async def test_contacting_a_lead_lowers_awaiting_contact(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = await _headers(client, db_session)
    await _subscribe(client, headers)
    await _search_with_results(
        client,
        headers,
        monkeypatch,
        keyword="LED screen",
        companies=[
            _company("Alpha", "https://alpha.test", 1),
            _company("Beta", "https://beta.test", 1),
        ],
    )
    lead_id = (await client.get(f"{PREFIX}/leads", headers=headers)).json()["items"][0][
        "id"
    ]

    await client.patch(
        f"{PREFIX}/leads/{lead_id}", json={"status": "contacted"}, headers=headers
    )

    body = await _summary(client, headers)
    assert body["awaiting_contact"] == 1
    # Still added today; contacting it does not un-add it.
    assert body["new_leads_today"] == 2


async def test_results_not_added_to_leads_do_not_count_as_new_leads(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = await _headers(client, db_session)
    await _subscribe(client, headers)
    await _search_with_results(
        client,
        headers,
        monkeypatch,
        keyword="LED screen",
        companies=[_company("Alpha", "https://alpha.test", 2)],
        auto_add=False,
    )

    body = await _summary(client, headers)

    assert body["new_leads_today"] == 0
    assert body["awaiting_contact"] == 0
    # The search and its contacts still happened today.
    assert body["searches_today"] == 1
    assert body["contacts_found_today"] == 2


async def test_recent_runs_are_newest_first_and_capped(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = await _headers(client, db_session)
    await _subscribe(client, headers)
    for i in range(7):
        await _search_with_results(
            client, headers, monkeypatch, keyword=f"keyword {i}", companies=[]
        )

    recent = (await _summary(client, headers))["recent_runs"]

    assert len(recent) == 5
    assert [r["original_keyword"] for r in recent] == [
        f"keyword {i}" for i in (6, 5, 4, 3, 2)
    ]


# --- Scope & validation ------------------------------------------------------


async def test_figures_are_scoped_to_the_user(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.auth import AuthService

    owner = await _headers(client, db_session)
    await _subscribe(client, owner)
    await _search_with_results(
        client,
        owner,
        monkeypatch,
        keyword="LED screen",
        companies=[_company("Alpha", "https://alpha.test", 2)],
    )

    service = AuthService(db_session)
    other, _ = await service.register("other@leadpilot.io", "other-password-1", "Other")
    other.is_verified = True
    await service.commit()
    login = await client.post(
        f"{PREFIX}/auth/login",
        json={"email": "other@leadpilot.io", "password": "other-password-1"},
    )
    other_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    body = await _summary(client, other_headers)
    assert body["new_leads_today"] == 0
    assert body["searches_today"] == 0
    assert body["contacts_found_today"] == 0
    assert body["recent_runs"] == []


async def test_timezone_offset_is_validated(client: AsyncClient, db_session) -> None:
    headers = await _headers(client, db_session)

    assert (
        await client.get(SUMMARY, headers=headers, params={"tz_offset": 9999})
    ).status_code == 422
    assert (
        await client.get(SUMMARY, headers=headers, params={"tz_offset": -300})
    ).status_code == 200


async def test_summary_requires_auth(client: AsyncClient) -> None:
    assert (await client.get(SUMMARY)).status_code == 401


# --- Day boundary ------------------------------------------------------------


def test_local_day_start_follows_the_users_timezone() -> None:
    now = datetime(2026, 9, 17, 1, 0, tzinfo=UTC)  # 01:00 UTC

    # UTC: today began an hour ago.
    assert local_day_start_utc(0, now) == datetime(2026, 9, 17, 0, 0, tzinfo=UTC)

    # Karachi (UTC+5, offset -300): it is 06:00 there; local midnight was
    # 19:00 UTC the previous evening.
    assert local_day_start_utc(-300, now) == datetime(2026, 9, 16, 19, 0, tzinfo=UTC)

    # New York (UTC-4, offset 240): it is still 21:00 on the 16th there;
    # local midnight was 04:00 UTC on the 16th.
    assert local_day_start_utc(240, now) == datetime(2026, 9, 16, 4, 0, tzinfo=UTC)
