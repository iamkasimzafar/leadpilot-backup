"""Charging for a lead search.

The rules under test:

  - no run fee, no per-company charge: finding a company costs nothing
  - 10 credits per decision maker found with a verified email
    (email_status == "valid" and an address present)
  - +5 credits on top of that, only for a verified contact whose number came
    back Active on WhatsApp (only if validation was requested)
  - nothing is taken at dispatch; the bill is settled once, when the results
    callback reports success
  - a failed run, or one with no verified email at all, costs nothing
  - the results POST can be retried, so the charge must happen exactly once
  - the wallet cannot go negative: a bill the balance cannot cover is charged
    as far as it goes and the remainder recorded as a shortfall
  - a search the balance could not cover (against the estimate) is refused
    up front with 402
"""

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.config import settings
from app.models.lead_search import LeadSearchRun
from app.services.billing_catalog import (
    BASE_CONTACT_CREDIT,
    DEFAULT_COMPANIES_PER_KEYWORD,
    DEFAULT_CONTACTS_PER_COMPANY,
    WHATSAPP_VALIDATION_CREDITS,
)
from app.services.search_pricing import cost_lines, total_credits
from tests.api.test_auth import signed_in_tokens
from tests.api.test_lead_results import _mock_n8n, _post_results

PREFIX = settings.API_V1_PREFIX
SEARCH = f"{PREFIX}/lead-radar/search"
QUOTE = f"{PREFIX}/lead-radar/quote"
BILLING = f"{PREFIX}/billing"

# What the two plans grant: $49 and $499 at 10 credits per dollar.
STARTER_CREDITS = 490
PRO_CREDITS = 4990


@pytest.fixture(autouse=True)
def _webhook_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "N8N_WEBHOOK_URL", "https://n8n.test/webhook/leads")


async def _headers(client: AsyncClient, db_session: Any) -> dict[str, str]:
    tokens = await signed_in_tokens(client, db_session)
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def _fund(client: AsyncClient, headers: dict[str, str], plan: str) -> None:
    response = await client.post(
        f"{BILLING}/subscriptions", json={"plan_code": plan}, headers=headers
    )
    assert response.status_code == 201


async def _balance(client: AsyncClient, headers: dict[str, str]) -> int:
    response = await client.get(f"{BILLING}/overview", headers=headers)
    assert response.status_code == 200
    return int(response.json()["balance"])


async def _usage_lines(client: AsyncClient, headers: dict[str, str]) -> list[dict]:
    """USAGE ledger entries, oldest first."""
    response = await client.get(f"{BILLING}/transactions", headers=headers)
    assert response.status_code == 200
    items = [t for t in response.json()["items"] if t["kind"] == "usage"]
    return list(reversed(items))


async def _start(
    client: AsyncClient,
    headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    *,
    expanded: list[str] | None = None,
    validate_whatsapp: bool = False,
) -> tuple[str, str]:
    """Dispatch a run; returns (run_id, progress_token)."""
    captured = _mock_n8n(monkeypatch)
    response = await client.post(
        SEARCH,
        json={
            "original_keyword": "LED screen",
            "expanded_keywords": expanded or [],
            "validate_whatsapp": validate_whatsapp,
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["run_id"], captured["progress_token"]


async def _results(client: AsyncClient, run_id: str, token: str, **payload: Any) -> None:
    """Post results and require them accepted (the endpoint answers 201)."""
    response = await _post_results(client, run_id, token, **payload)
    assert response.status_code == 201, response.text


async def _notification_titles(client: AsyncClient, headers: dict[str, str]) -> list[str]:
    response = await client.get(f"{PREFIX}/notifications", headers=headers)
    assert response.status_code == 200
    return [n["title"] for n in response.json()["items"]]


async def _quote(
    client: AsyncClient, headers: dict[str, str], *, keywords: int, whatsapp: bool = False
) -> Any:
    """A quote for `keywords` search terms: the original plus keywords-1 expansions."""
    return await client.post(
        QUOTE,
        json={
            "original_keyword": "LED screen",
            "expanded_keywords": [f"Term {i}" for i in range(keywords - 1)],
            "validate_whatsapp": whatsapp,
        },
        headers=headers,
    )


async def _run(client: AsyncClient, headers: dict[str, str], run_id: str) -> dict:
    response = await client.get(f"{PREFIX}/lead-radar/runs/{run_id}", headers=headers)
    assert response.status_code == 200
    return response.json()


async def _db_run(db_session: Any, run_id: str) -> LeadSearchRun:
    db_session.expire_all()
    return (
        await db_session.execute(select(LeadSearchRun).where(LeadSearchRun.id == run_id))
    ).scalar_one()


def _contact(
    i: int, *, whatsapp_status: str | None = None, valid_email: bool = True
) -> dict:
    """One decision maker. `valid_email=False` gives it no billable email at
    all (what a company with a non-verified contact looks like)."""
    person: dict[str, Any] = {
        "full_name": f"Person {i}",
        "phone_number": f"+49 30 {1000 + i}",
    }
    if valid_email:
        person["verified_email"] = f"person{i}@example.com"
        person["email_status"] = "valid"
    if whatsapp_status is not None:
        person["whatsapp_status"] = whatsapp_status
    return person


def _companies(count: int, *, statuses: list[str | None] | None = None) -> list[dict]:
    """`count` distinct companies, each with one verified-email contact.

    `statuses`, if given, replaces the whole set with a single company
    carrying one contact per status instead -- `count` is then ignored, so
    the caller's own count of verified contacts (`len(statuses)`) is exactly
    what gets billed."""
    if statuses is not None:
        return [
            {
                "company_name": "Company 0",
                "website": "company-0.example.com",
                "decision_makers": [
                    _contact(i, whatsapp_status=status)
                    for i, status in enumerate(statuses)
                ],
            }
        ]

    return [
        {
            "company_name": f"Company {i}",
            "website": f"company-{i}.example.com",
            "decision_makers": [_contact(i)],
        }
        for i in range(count)
    ]


# --- The maths ---------------------------------------------------------------


def test_the_rates() -> None:
    assert BASE_CONTACT_CREDIT == 10
    assert WHATSAPP_VALIDATION_CREDITS == 5


def test_a_bill_is_contacts_plus_whatsapp() -> None:
    lines = cost_lines(verified_contacts=3, active_whatsapp=2)

    assert [line.key for line in lines] == ["contacts", "whatsapp"]
    assert total_credits(lines) == 3 * 10 + 2 * 5


def test_no_whatsapp_line_when_nothing_came_back_active() -> None:
    lines = cost_lines(verified_contacts=3, active_whatsapp=0)

    assert [line.key for line in lines] == ["contacts"]
    assert total_credits(lines) == 30


def test_no_verified_contacts_is_free() -> None:
    lines = cost_lines(verified_contacts=0, active_whatsapp=0)

    assert lines == []
    assert total_credits(lines) == 0


# --- The quote ---------------------------------------------------------------


async def test_quote_uses_defaults_for_a_new_account(
    client: AsyncClient, db_session
) -> None:
    headers = await _headers(client, db_session)

    response = await _quote(client, headers, keywords=1)

    assert response.status_code == 200
    body = response.json()
    assert body["based_on_history"] is False
    assert body["runs_sampled"] == 0
    assert body["estimated_companies"] == DEFAULT_COMPANIES_PER_KEYWORD
    expected_contacts = round(
        DEFAULT_COMPANIES_PER_KEYWORD * DEFAULT_CONTACTS_PER_COMPANY
    )
    assert body["total"] == expected_contacts * 10
    assert body["balance"] == 0
    assert body["affordable"] is False
    assert body["shortfall"] == body["total"]
    # The rates ride along so the UI never hardcodes a price.
    assert body["contact_credits"] == 10
    assert body["whatsapp_credits"] == 5


async def test_quote_adds_whatsapp_when_requested(
    client: AsyncClient, db_session
) -> None:
    headers = await _headers(client, db_session)

    without = (await _quote(client, headers, keywords=2)).json()
    with_checks = (await _quote(client, headers, keywords=2, whatsapp=True)).json()

    expected_checks = round(
        with_checks["estimated_companies"] * DEFAULT_CONTACTS_PER_COMPANY
    )
    assert with_checks["estimated_whatsapp_checks"] == expected_checks
    assert with_checks["total"] == without["total"] + expected_checks * 5
    assert [line["key"] for line in with_checks["lines"]] == ["contacts", "whatsapp"]


async def test_quote_learns_from_the_accounts_own_runs(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    """After a completed run, the estimate reflects its real yield."""
    headers = await _headers(client, db_session)
    await _fund(client, headers, "pro_yearly")

    # One keyword that turned up 4 companies with 2 contacts each.
    run_id, token = await _start(client, headers, monkeypatch)
    companies = _companies(4)
    for company in companies:
        company["decision_makers"] = [
            {"full_name": "A"}, {"full_name": "B"}
        ]
    await _results(client, run_id, token, companies=companies)

    body = (await _quote(client, headers, keywords=3, whatsapp=True)).json()

    assert body["based_on_history"] is True
    assert body["runs_sampled"] == 1
    assert body["estimated_companies"] == 12  # 3 keywords x 4 per keyword
    assert body["estimated_whatsapp_checks"] == 24  # 12 companies x 2 contacts


async def test_quote_requires_auth(client: AsyncClient) -> None:
    response = await client.post(QUOTE, json={"original_keyword": "LED screen"})

    assert response.status_code == 401


# --- The start gate ----------------------------------------------------------


async def test_a_search_the_balance_cannot_cover_is_refused(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    headers = await _headers(client, db_session)
    _mock_n8n(monkeypatch)

    response = await client.post(
        SEARCH,
        json={"original_keyword": "LED screen", "expanded_keywords": []},
        headers=headers,
    )

    assert response.status_code == 402
    error = response.json()["error"]
    assert error["code"] == "insufficient_credits"
    assert error["details"]["balance"] == 0
    expected_contacts = round(
        DEFAULT_COMPANIES_PER_KEYWORD * DEFAULT_CONTACTS_PER_COMPANY
    )
    assert error["details"]["required"] == expected_contacts * 10
    assert error["details"]["shortfall"] == error["details"]["required"]

    # Refused means refused: no run row was opened.
    assert (await db_session.execute(select(LeadSearchRun))).scalars().all() == []


async def test_nothing_is_charged_at_dispatch(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    headers = await _headers(client, db_session)
    await _fund(client, headers, "pro_yearly")

    await _start(client, headers, monkeypatch)

    assert await _balance(client, headers) == PRO_CREDITS
    assert await _usage_lines(client, headers) == []


# --- Settlement --------------------------------------------------------------


async def test_a_successful_run_is_charged_for_its_verified_contacts(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    headers = await _headers(client, db_session)
    await _fund(client, headers, "pro_yearly")
    run_id, token = await _start(client, headers, monkeypatch)

    await _results(client, run_id, token, companies=_companies(3))

    expected = 3 * 10
    assert await _balance(client, headers) == PRO_CREDITS - expected

    run = await _run(client, headers, run_id)
    assert run["credits_charged"] == expected
    assert run["credits_shortfall"] == 0
    assert run["whatsapp_checks"] == 0
    assert run["credits_charged_at"] is not None

    lines = await _usage_lines(client, headers)
    assert [line["amount"] for line in lines] == [-30]
    assert "3 verified emails" in lines[0]["description"]
    assert all(line["reference_id"] == run_id for line in lines)


async def test_a_company_with_no_verified_contact_is_free(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    """Finding a company shell costs nothing on its own."""
    headers = await _headers(client, db_session)
    await _fund(client, headers, "pro_yearly")
    run_id, token = await _start(client, headers, monkeypatch)

    companies = [
        {"company_name": "No Contact Co", "website": "no-contact.example.com"},
        {
            "company_name": "Unverified Co",
            "website": "unverified.example.com",
            "decision_makers": [_contact(0, valid_email=False)],
        },
    ]
    await _results(client, run_id, token, companies=companies)

    assert await _balance(client, headers) == PRO_CREDITS
    run = await _run(client, headers, run_id)
    assert run["credits_charged"] == 0
    assert await _usage_lines(client, headers) == []


async def test_only_active_whatsapp_is_billed(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    """Not Active and null both cost nothing extra; only Active does."""
    headers = await _headers(client, db_session)
    await _fund(client, headers, "pro_yearly")
    run_id, token = await _start(client, headers, monkeypatch, validate_whatsapp=True)

    companies = _companies(2, statuses=["Active", "Not Active", None, "Active"])
    await _results(client, run_id, token, companies=companies)

    verified = 4
    active = 2
    expected = verified * 10 + active * 5
    assert await _balance(client, headers) == PRO_CREDITS - expected

    run = await _run(client, headers, run_id)
    assert run["whatsapp_checks"] == active
    assert run["credits_charged"] == expected

    lines = await _usage_lines(client, headers)
    assert [line["amount"] for line in lines] == [-40, -10]
    assert "2 active WhatsApps" in lines[1]["description"]


async def test_whatsapp_is_free_when_validation_was_off(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    """The workflow reporting Active is not the same as the user asking."""
    headers = await _headers(client, db_session)
    await _fund(client, headers, "pro_yearly")
    run_id, token = await _start(client, headers, monkeypatch, validate_whatsapp=False)

    companies = _companies(2, statuses=["Active", "Not Active"])
    await _results(client, run_id, token, companies=companies)

    verified = 2
    assert await _balance(client, headers) == PRO_CREDITS - verified * 10
    run = await _run(client, headers, run_id)
    assert run["whatsapp_checks"] == 0
    assert [line["amount"] for line in await _usage_lines(client, headers)] == [
        -verified * 10
    ]


async def test_the_contact_line_counts_what_was_saved(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    """Billed on rows saved, so a duplicate in the payload is not paid twice."""
    headers = await _headers(client, db_session)
    await _fund(client, headers, "pro_yearly")
    run_id, token = await _start(client, headers, monkeypatch)

    companies = _companies(3)
    companies.append(dict(companies[0]))  # the same company twice
    await _results(client, run_id, token, companies=companies)

    run = await _run(client, headers, run_id)
    assert run["credits_charged"] == 3 * 10
    assert run["companies_found"] == 3


# --- No charge on failure ----------------------------------------------------


async def test_a_failed_result_costs_nothing(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    headers = await _headers(client, db_session)
    await _fund(client, headers, "pro_yearly")
    run_id, token = await _start(client, headers, monkeypatch)

    await _results(
        client, run_id, token, status="failed", error="Snov.io quota exhausted"
    )
    assert await _balance(client, headers) == PRO_CREDITS
    assert await _usage_lines(client, headers) == []

    run = await _run(client, headers, run_id)
    assert run["status"] == "failed"
    assert run["credits_charged"] == 0
    assert run["credits_charged_at"] is None


async def test_results_after_an_error_are_not_charged(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    """A run the progress callback already failed stays unbilled even if the
    results POST lands afterwards with companies in it."""
    headers = await _headers(client, db_session)
    await _fund(client, headers, "pro_yearly")
    run_id, token = await _start(client, headers, monkeypatch)

    failed = await client.post(
        f"{PREFIX}/lead-radar/runs/{run_id}/progress",
        json={"stage": "finding_emails", "error": "boom"},
        headers={"X-LeadPilot-Run-Token": token},
    )
    assert failed.status_code == 202

    await _results(client, run_id, token, companies=_companies(5))

    assert await _balance(client, headers) == PRO_CREDITS
    run = await _run(client, headers, run_id)
    assert run["status"] == "failed"
    assert run["credits_charged"] == 0


# --- Exactly once ------------------------------------------------------------


async def test_a_retried_results_post_is_charged_once(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    """n8n's HTTP node retries, and can post the same results several times."""
    headers = await _headers(client, db_session)
    await _fund(client, headers, "pro_yearly")
    run_id, token = await _start(client, headers, monkeypatch)

    companies = _companies(3)
    for _ in range(3):
        await _results(client, run_id, token, companies=companies)

    expected = 3 * 10
    assert await _balance(client, headers) == PRO_CREDITS - expected
    assert len(await _usage_lines(client, headers)) == 1
    assert (await _run(client, headers, run_id))["credits_charged"] == expected


# --- Shortfall ---------------------------------------------------------------


async def test_a_bill_beyond_the_balance_is_charged_as_far_as_it_goes(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    """The estimate passed the gate, but the workflow found far more verified
    contacts than expected. Results are kept, the balance stops at zero, and
    the rest is recorded rather than lost."""
    headers = await _headers(client, db_session)
    await _fund(client, headers, "starter_monthly")  # 490 credits
    run_id, token = await _start(client, headers, monkeypatch)

    found = 60  # 60 x 10 = 600, well over 490
    await _results(client, run_id, token, companies=_companies(found))

    bill = found * 10
    assert await _balance(client, headers) == 0

    run = await _run(client, headers, run_id)
    assert run["companies_found"] == found, "results are still saved in full"
    assert run["credits_charged"] == STARTER_CREDITS
    assert run["credits_shortfall"] == bill - STARTER_CREDITS

    lines = await _usage_lines(client, headers)
    assert lines[0]["amount"] == -STARTER_CREDITS
    assert "partial" in lines[0]["description"]


async def test_a_shortfall_raises_a_notification(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    headers = await _headers(client, db_session)
    await _fund(client, headers, "starter_monthly")
    run_id, token = await _start(client, headers, monkeypatch)
    await _results(client, run_id, token, companies=_companies(60))

    response = await client.get(f"{PREFIX}/notifications", headers=headers)
    titles = [n["title"] for n in response.json()["items"]]

    assert any("could not be charged" in title for title in titles)
    notice = next(
        n for n in response.json()["items"] if "could not be charged" in n["title"]
    )
    assert notice["kind"] == "credits"
    assert notice["link"] == "/wallet"


async def test_a_shortfall_notification_ignores_the_credits_preference(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    """Money owed is not something the user can opt out of hearing about."""
    headers = await _headers(client, db_session)
    await _fund(client, headers, "starter_monthly")
    muted = await client.patch(
        f"{PREFIX}/notification-preferences", json={"credits": False}, headers=headers
    )
    assert muted.status_code == 200

    run_id, token = await _start(client, headers, monkeypatch)
    await _results(client, run_id, token, companies=_companies(60))

    titles = await _notification_titles(client, headers)
    assert any("could not be charged" in title for title in titles)


async def test_no_shortfall_no_notification(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    headers = await _headers(client, db_session)
    await _fund(client, headers, "pro_yearly")
    run_id, token = await _start(client, headers, monkeypatch)
    await _results(client, run_id, token, companies=_companies(3))

    titles = await _notification_titles(client, headers)
    assert not any("could not be charged" in title for title in titles)


# --- Persistence -------------------------------------------------------------


async def test_settlement_is_stored_on_the_run(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    headers = await _headers(client, db_session)
    await _fund(client, headers, "pro_yearly")
    run_id, token = await _start(client, headers, monkeypatch, validate_whatsapp=True)
    companies = _companies(2, statuses=["Active", "Not Active"])
    await _results(client, run_id, token, companies=companies)

    run = await _db_run(db_session, run_id)

    assert run.credits_charged == 2 * 10 + 1 * 5
    assert run.credits_shortfall == 0
    assert run.whatsapp_checks == 1
    assert run.credits_charged_at is not None
