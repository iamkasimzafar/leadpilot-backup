"""Radar monitors: CRUD, the scheduler contract, and the billing guardrail.

The dedupe tests are the important ones. A monitor re-runs the same search for
weeks and will re-surface the same directory site; the promise is that the user
is never billed twice for a contact they already have.
"""

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.config import settings
from app.models.billing import Wallet
from app.models.lead import Company, DecisionMaker
from app.models.radar_monitor import RadarMonitor
from app.services.billing_catalog import BASE_CONTACT_CREDIT
from tests.api.test_auth import register, signed_in_tokens, verify

PREFIX = settings.API_V1_PREFIX
MONITORS = f"{PREFIX}/lead-radar/monitors"
DUE = f"{MONITORS}/due"
RESULTS = f"{MONITORS}/results"

SECRET = "test-workflow-secret"


@pytest.fixture(autouse=True)
def _workflow_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """The due endpoint refuses to hand out tokens without a configured secret."""
    monkeypatch.setattr(settings, "N8N_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(settings, "N8N_WEBHOOK_HEADER", "X-LeadPilot-Token")
    monkeypatch.setattr(settings, "PUBLIC_API_URL", "https://api.leadpilot.test")


def _company(website: str, emails: list[str]) -> dict[str, Any]:
    """One company carrying `emails` as verified decision makers."""
    return {
        "company_name": website.split("//")[-1],
        "website": website,
        "location": "Berlin",
        "industry": "Wholesale",
        "company_size": "11-50",
        "hq_phone": "123456",
        "decision_makers": [
            {
                "full_name": f"Contact {index}",
                "job_title": "Head of Purchasing",
                "verified_email": email,
                "email_status": "valid",
                "linkedin_url": None,
                "phone_number": None,
                "whatsapp_status": "Not Active",
            }
            for index, email in enumerate(emails)
        ],
    }


async def _auth(client: AsyncClient, db_session: Any) -> dict[str, str]:
    tokens = await signed_in_tokens(client, db_session)

    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def _auth_other(client: AsyncClient, db_session: Any) -> dict[str, str]:
    """A second, unrelated account -- for the ownership-scoping test."""
    email = "intruder@leadpilot.io"
    password = "another-secret-pw"

    await register(client, email=email, password=password)
    await verify(client, db_session, email=email)

    response = await client.post(
        f"{PREFIX}/auth/login", json={"email": email, "password": password}
    )
    assert response.status_code == 200, response.text

    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _create_monitor(client: AsyncClient, headers: dict[str, str]) -> dict[str, Any]:
    response = await client.post(
        MONITORS,
        headers=headers,
        json={
            "name": "Hardware Wholesalers - Germany",
            "search_type": "keyword",
            "search_value": "hardware wholesaler",
            "filters": {"country": "de", "company_size": "any", "contact_role": "any"},
            "frequency": "daily",
            "limit_per_run": 50,
        },
    )
    assert response.status_code == 201, response.text

    return response.json()


async def _user_id(db_session: Any) -> str:
    """The id of the account signed_in_tokens creates."""
    from app.models.user import User

    result = await db_session.execute(
        select(User).where(User.email == "founder@leadpilot.io")
    )

    return str(result.scalar_one().id)


async def _fund(db_session: Any, user_id: str, credits: int) -> None:
    """Put credits in the user's wallet so a run can actually be billed."""
    wallet = await db_session.get(Wallet, user_id)
    if wallet is None:
        wallet = Wallet(user_id=user_id, balance=credits)
        db_session.add(wallet)
    else:
        wallet.balance = credits
    await db_session.commit()


# --- CRUD -------------------------------------------------------------------


async def test_create_and_list_monitor(client: AsyncClient, db_session: Any) -> None:
    headers = await _auth(client, db_session)
    created = await _create_monitor(client, headers)

    assert created["status"] == "running"
    assert created["total_leads_generated"] == 0
    assert created["serper_offset"] == 0
    assert created["filters"]["country"] == "de"
    # "any" is stored as no filter at all, not the literal string.
    assert created["filters"]["company_size"] is None

    listed = await client.get(MONITORS, headers=headers)
    assert listed.status_code == 200
    assert listed.json()["total"] == 1


async def test_limit_is_capped(client: AsyncClient, db_session: Any) -> None:
    """Only the three offered caps are accepted, so a monitor cannot be
    hand-edited into draining the balance."""
    headers = await _auth(client, db_session)

    response = await client.post(
        MONITORS,
        headers=headers,
        json={
            "name": "Too greedy",
            "search_type": "keyword",
            "search_value": "widgets",
            "frequency": "daily",
            "limit_per_run": 5000,
        },
    )

    assert response.status_code == 422


async def test_hs_code_monitor_requires_six_digits(
    client: AsyncClient, db_session: Any
) -> None:
    headers = await _auth(client, db_session)

    response = await client.post(
        MONITORS,
        headers=headers,
        json={
            "name": "LED buyers",
            "search_type": "hs_code",
            "search_value": "not-a-code",
            "frequency": "weekly",
            "limit_per_run": 20,
        },
    )

    assert response.status_code == 422


async def test_pause_and_resume(client: AsyncClient, db_session: Any) -> None:
    headers = await _auth(client, db_session)
    monitor = await _create_monitor(client, headers)

    paused = await client.patch(
        f"{MONITORS}/{monitor['id']}", headers=headers, json={"status": "paused"}
    )
    assert paused.status_code == 200
    assert paused.json()["status"] == "paused"

    # A paused monitor is invisible to the scheduler.
    due = await client.get(DUE, headers={"X-LeadPilot-Token": SECRET})
    assert due.json()["total"] == 0


async def test_monitors_are_scoped_to_their_owner(
    client: AsyncClient, db_session: Any
) -> None:
    owner = await _auth(client, db_session)
    monitor = await _create_monitor(client, owner)

    intruder = await _auth_other(client, db_session)
    response = await client.patch(
        f"{MONITORS}/{monitor['id']}", headers=intruder, json={"name": "stolen"}
    )

    assert response.status_code == 404


# --- The scheduler contract -------------------------------------------------


async def test_due_requires_the_shared_secret(
    client: AsyncClient, db_session: Any
) -> None:
    headers = await _auth(client, db_session)
    await _create_monitor(client, headers)

    assert (await client.get(DUE)).status_code == 401
    assert (
        await client.get(DUE, headers={"X-LeadPilot-Token": "wrong"})
    ).status_code == 401


async def test_due_refuses_when_no_secret_is_configured(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It hands out per-monitor callback tokens, so it must never default open."""
    headers = await _auth(client, db_session)
    await _create_monitor(client, headers)

    monkeypatch.setattr(settings, "N8N_WEBHOOK_SECRET", "")

    assert (await client.get(DUE, headers={"X-LeadPilot-Token": ""})).status_code == 401


async def test_due_returns_the_run_plan_and_claims_it(
    client: AsyncClient, db_session: Any
) -> None:
    headers = await _auth(client, db_session)
    await _create_monitor(client, headers)

    first = await client.get(DUE, headers={"X-LeadPilot-Token": SECRET})
    assert first.status_code == 200

    payload = first.json()
    assert payload["total"] == 1

    plan = payload["monitors"][0]
    assert plan["search_term"] == "hardware wholesaler"
    assert plan["country"] == "de"
    assert plan["limit_per_run"] == 50
    assert plan["serper_offset"] == 0
    assert plan["serper_page"] == 1
    assert plan["results_url"].endswith("/lead-radar/monitors/results")
    assert plan["progress_token"]

    # Claimed: a second pass the same night must not dispatch it again, or the
    # user is billed twice for one night's work.
    second = await client.get(DUE, headers={"X-LeadPilot-Token": SECRET})
    assert second.json()["total"] == 0


# --- Results, dedupe and billing --------------------------------------------


async def test_results_reject_a_bad_token(client: AsyncClient, db_session: Any) -> None:
    headers = await _auth(client, db_session)
    monitor = await _create_monitor(client, headers)

    response = await client.post(
        RESULTS,
        headers={"X-LeadPilot-Run-Token": "wrong"},
        json={"monitor_id": monitor["id"], "companies": []},
    )

    assert response.status_code == 401


async def test_run_inserts_leads_bills_and_advances_the_offset(
    client: AsyncClient, db_session: Any
) -> None:
    headers = await _auth(client, db_session)
    monitor = await _create_monitor(client, headers)

    await _fund(db_session, await _user_id(db_session), 1000)

    due = await client.get(DUE, headers={"X-LeadPilot-Token": SECRET})
    token = due.json()["monitors"][0]["progress_token"]

    response = await client.post(
        RESULTS,
        headers={"X-LeadPilot-Run-Token": token},
        json={
            "monitor_id": monitor["id"],
            "status": "completed",
            "companies": [
                _company("https://a.example", ["one@a.example", "two@a.example"])
            ],
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()

    assert body["contacts_added"] == 2
    assert body["duplicates_skipped"] == 0
    assert body["credits_charged"] == 2 * BASE_CONTACT_CREDIT
    assert body["total_leads_generated"] == 2

    # Tomorrow's run must dig past today's results, or it finds nothing new.
    assert body["serper_offset"] == 50

    # The leads are actually in My Leads.
    leads = await client.get(f"{PREFIX}/leads", headers=headers)
    assert leads.json()["total"] == 1


async def test_a_repeat_run_never_bills_the_same_contact_twice(
    client: AsyncClient, db_session: Any
) -> None:
    """The core guardrail.

    A monitor re-surfaces a directory it already scraped. The second run must
    add nothing, count nothing and -- above all -- charge nothing.
    """
    headers = await _auth(client, db_session)
    monitor = await _create_monitor(client, headers)

    user_id = await _user_id(db_session)
    await _fund(db_session, user_id, 1000)

    payload = {
        "monitor_id": monitor["id"],
        "status": "completed",
        "companies": [_company("https://a.example", ["one@a.example", "two@a.example"])],
    }

    due = await client.get(DUE, headers={"X-LeadPilot-Token": SECRET})
    token = due.json()["monitors"][0]["progress_token"]

    first = await client.post(
        RESULTS, headers={"X-LeadPilot-Run-Token": token}, json=payload
    )
    assert first.json()["credits_charged"] == 2 * BASE_CONTACT_CREDIT

    wallet = await db_session.get(Wallet, user_id)
    await db_session.refresh(wallet)
    balance_after_first = wallet.balance

    # The very same contacts come back on the next run.
    second = await client.post(
        RESULTS, headers={"X-LeadPilot-Run-Token": token}, json=payload
    )
    body = second.json()

    assert body["contacts_added"] == 0
    assert body["duplicates_skipped"] == 2
    assert body["credits_charged"] == 0
    assert body["total_leads_generated"] == 2

    await db_session.refresh(wallet)
    assert wallet.balance == balance_after_first

    # And no duplicate rows were written.
    rows = await db_session.execute(
        select(DecisionMaker).where(DecisionMaker.user_id == user_id)
    )
    assert len(list(rows.scalars().all())) == 2


async def test_only_the_new_contacts_in_a_mixed_run_are_billed(
    client: AsyncClient, db_session: Any
) -> None:
    """A run that brings back one known and one new contact bills for one."""
    headers = await _auth(client, db_session)
    monitor = await _create_monitor(client, headers)

    await _fund(db_session, await _user_id(db_session), 1000)

    due = await client.get(DUE, headers={"X-LeadPilot-Token": SECRET})
    token = due.json()["monitors"][0]["progress_token"]

    await client.post(
        RESULTS,
        headers={"X-LeadPilot-Run-Token": token},
        json={
            "monitor_id": monitor["id"],
            "companies": [_company("https://a.example", ["known@a.example"])],
        },
    )

    second = await client.post(
        RESULTS,
        headers={"X-LeadPilot-Run-Token": token},
        json={
            "monitor_id": monitor["id"],
            "companies": [
                _company("https://a.example", ["known@a.example", "fresh@a.example"])
            ],
        },
    )
    body = second.json()

    assert body["contacts_added"] == 1
    assert body["duplicates_skipped"] == 1
    assert body["credits_charged"] == BASE_CONTACT_CREDIT


async def test_a_duplicate_within_one_payload_is_billed_once(
    client: AsyncClient, db_session: Any
) -> None:
    """The same address under two companies in a single run still bills once."""
    headers = await _auth(client, db_session)
    monitor = await _create_monitor(client, headers)

    await _fund(db_session, await _user_id(db_session), 1000)

    due = await client.get(DUE, headers={"X-LeadPilot-Token": SECRET})
    token = due.json()["monitors"][0]["progress_token"]

    response = await client.post(
        RESULTS,
        headers={"X-LeadPilot-Run-Token": token},
        json={
            "monitor_id": monitor["id"],
            "companies": [
                _company("https://a.example", ["same@shared.example"]),
                _company("https://b.example", ["same@shared.example"]),
            ],
        },
    )
    body = response.json()

    assert body["contacts_added"] == 1
    assert body["duplicates_skipped"] == 1
    assert body["credits_charged"] == BASE_CONTACT_CREDIT


async def test_dedupe_sees_leads_found_by_a_manual_search(
    client: AsyncClient, db_session: Any
) -> None:
    """A contact the user already got from a normal search is not billed again
    when a monitor stumbles on it."""
    headers = await _auth(client, db_session)
    monitor = await _create_monitor(client, headers)

    user_id = await _user_id(db_session)
    await _fund(db_session, user_id, 1000)

    # A lead from an earlier manual search.
    company = Company(
        user_id=user_id, company_name="Prior", website="https://prior.example"
    )
    db_session.add(company)
    await db_session.flush()
    db_session.add(
        DecisionMaker(
            company_id=company.id,
            user_id=user_id,
            full_name="Known Person",
            verified_email="known@prior.example",
            email_status="valid",
        )
    )
    await db_session.commit()

    due = await client.get(DUE, headers={"X-LeadPilot-Token": SECRET})
    token = due.json()["monitors"][0]["progress_token"]

    response = await client.post(
        RESULTS,
        headers={"X-LeadPilot-Run-Token": token},
        json={
            "monitor_id": monitor["id"],
            "companies": [_company("https://prior.example", ["known@prior.example"])],
        },
    )
    body = response.json()

    assert body["contacts_added"] == 0
    assert body["duplicates_skipped"] == 1
    assert body["credits_charged"] == 0


async def test_a_failed_run_costs_nothing_and_keeps_the_offset(
    client: AsyncClient, db_session: Any
) -> None:
    """A failed run must not skip a page of results nobody ever looked at."""
    headers = await _auth(client, db_session)
    monitor = await _create_monitor(client, headers)

    due = await client.get(DUE, headers={"X-LeadPilot-Token": SECRET})
    token = due.json()["monitors"][0]["progress_token"]

    response = await client.post(
        RESULTS,
        headers={"X-LeadPilot-Run-Token": token},
        json={
            "monitor_id": monitor["id"],
            "status": "failed",
            "error": "Serper quota exhausted",
            "companies": [],
        },
    )
    body = response.json()

    assert body["credits_charged"] == 0
    assert body["serper_offset"] == 0

    stored = await db_session.get(RadarMonitor, monitor["id"])
    await db_session.refresh(stored)
    assert stored.last_error == "Serper quota exhausted"
