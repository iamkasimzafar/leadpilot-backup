"""Radar monitors: CRUD, the Celery dispatcher, and the billing guardrail.

The dedupe tests are the important ones. A monitor re-runs the same search for
weeks and will re-surface the same directory site; the promise is that the user
is never billed twice for a contact they already have.

The dispatcher is driven directly here, with n8n mocked, rather than through
Celery: the task is a thin `asyncio.run` around `dispatch_due`, and the
behaviour worth testing is all in the service.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.config import settings
from app.models.billing import Wallet
from app.models.lead import Company, DecisionMaker
from app.models.lead_search import LeadSearchRun
from app.models.radar_monitor import RadarMonitor
from app.services.billing_catalog import BASE_CONTACT_CREDIT
from app.services.radar_monitor import RadarMonitorService
from tests.api.test_auth import register, signed_in_tokens, verify

PREFIX = settings.API_V1_PREFIX
MONITORS = f"{PREFIX}/lead-radar/monitors"
RESULTS = f"{MONITORS}/results"

WEBHOOK = "https://n8n.test/webhook/radar-monitor-v1"


@pytest.fixture(autouse=True)
def _monitor_workflow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "N8N_MONITOR_WEBHOOK_URL", WEBHOOK)
    monkeypatch.setattr(settings, "PUBLIC_API_URL", "https://api.leadpilot.test")
    monkeypatch.setattr(settings, "N8N_WEBHOOK_SECRET", "")


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


async def _create_monitor(
    client: AsyncClient, headers: dict[str, str], **overrides: Any
) -> dict[str, Any]:
    body = {
        "name": "Hardware Wholesalers - Germany",
        "search_type": "keyword",
        "search_value": "hardware wholesaler",
        "filters": {"country": "de", "company_size": "any", "contact_role": "any"},
        "frequency": "daily",
        "limit_per_run": 50,
        **overrides,
    }
    response = await client.post(MONITORS, headers=headers, json=body)
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


async def _make_due(db_session: Any, monitor_id: str) -> None:
    """Bring a monitor's slot forward, as the clock reaching it would."""
    monitor = await db_session.get(RadarMonitor, monitor_id)
    monitor.next_run_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()


async def _dispatch(
    db_session: Any,
    *,
    now: datetime | None = None,
    limit: int | None = None,
    respond: int = 200,
) -> list[dict[str, Any]]:
    """Run one dispatcher tick with n8n mocked. Returns the payloads posted."""
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        if respond >= 400:
            return httpx.Response(respond, text="nope")

        return httpx.Response(200, json={"message": "Workflow was started"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as mock:
        await RadarMonitorService(db_session).dispatch_due(
            now=now, limit=limit, spacing=0, client=mock
        )

    return captured


async def _dispatch_one(db_session: Any, monitor_id: str) -> dict[str, Any]:
    """Make one monitor due and dispatch it; return what n8n received."""
    await _make_due(db_session, monitor_id)
    sent = await _dispatch(db_session)
    assert len(sent) == 1, sent

    return sent[0]


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


async def test_a_new_monitor_is_slotted_to_run_within_minutes(
    client: AsyncClient, db_session: Any
) -> None:
    """The user should see it work, not wait a day for its first random slot."""
    headers = await _auth(client, db_session)
    before = datetime.now(UTC)
    created = await _create_monitor(client, headers)

    slot = datetime.fromisoformat(created["next_run_at"])
    if slot.tzinfo is None:
        slot = slot.replace(tzinfo=UTC)

    assert (
        before
        < slot
        <= before + timedelta(minutes=settings.MONITOR_FIRST_RUN_DELAY_MINUTES, seconds=5)
    )


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


async def test_pause_keeps_it_out_of_dispatch_and_resume_reslots_it(
    client: AsyncClient, db_session: Any
) -> None:
    headers = await _auth(client, db_session)
    monitor = await _create_monitor(client, headers)

    paused = await client.patch(
        f"{MONITORS}/{monitor['id']}", headers=headers, json={"status": "paused"}
    )
    assert paused.status_code == 200
    assert paused.json()["status"] == "paused"

    # Due by the clock, but paused: the dispatcher must not touch it.
    await _make_due(db_session, monitor["id"])
    assert await _dispatch(db_session) == []

    before = datetime.now(UTC)
    resumed = await client.patch(
        f"{MONITORS}/{monitor['id']}", headers=headers, json={"status": "running"}
    )
    assert resumed.json()["status"] == "running"

    # Resuming gets a fresh near-term slot rather than the stale one.
    slot = datetime.fromisoformat(resumed.json()["next_run_at"])
    if slot.tzinfo is None:
        slot = slot.replace(tzinfo=UTC)
    assert slot > before


async def test_editing_the_search_term_restarts_the_paging(
    client: AsyncClient, db_session: Any
) -> None:
    """A new term means a new query, and its offset must start over.

    Carried across, the offset would put the next run on page 3 of a search
    nobody has read page 1 of -- so the monitor would skip its own best
    results.
    """
    headers = await _auth(client, db_session)
    monitor = await _create_monitor(client, headers)

    stored = await db_session.get(RadarMonitor, monitor["id"])
    stored.serper_offset = 150
    await db_session.commit()

    response = await client.patch(
        f"{MONITORS}/{monitor['id']}",
        headers=headers,
        json={"search_value": "industrial fasteners"},
    )

    assert response.status_code == 200, response.text
    body = response.json()

    assert body["search_value"] == "industrial fasteners"
    assert body["serper_offset"] == 0


async def test_editing_only_the_schedule_keeps_the_paging(
    client: AsyncClient, db_session: Any
) -> None:
    """The mirror of the above: a save that leaves the term alone must not
    rewind the offset and make the monitor re-find leads the user has."""
    headers = await _auth(client, db_session)
    monitor = await _create_monitor(client, headers)

    stored = await db_session.get(RadarMonitor, monitor["id"])
    stored.serper_offset = 150
    await db_session.commit()

    response = await client.patch(
        f"{MONITORS}/{monitor['id']}",
        headers=headers,
        json={
            "search_value": "hardware wholesaler",
            "frequency": "weekly",
            "limit_per_run": 20,
        },
    )
    body = response.json()

    assert body["frequency"] == "weekly"
    assert body["limit_per_run"] == 20
    assert body["serper_offset"] == 150


async def test_editing_can_switch_a_monitor_to_an_hs_code(
    client: AsyncClient, db_session: Any
) -> None:
    headers = await _auth(client, db_session)
    monitor = await _create_monitor(client, headers)

    response = await client.patch(
        f"{MONITORS}/{monitor['id']}",
        headers=headers,
        json={
            "search_type": "hs_code",
            "search_value": "853120",
            "search_label": "LED indicator panels",
        },
    )
    body = response.json()

    assert body["search_type"] == "hs_code"
    assert body["search_value"] == "853120"
    assert body["search_label"] == "LED indicator panels"

    # The workflow must search the wording, not the digits.
    sent = await _dispatch_one(db_session, monitor["id"])

    assert sent["search_term"] == "LED indicator panels"
    assert sent["expanded_keywords"] == ["LED indicator panels"]
    assert sent["hs_code"] == "853120"


async def test_editing_refuses_a_malformed_hs_code(
    client: AsyncClient, db_session: Any
) -> None:
    headers = await _auth(client, db_session)
    monitor = await _create_monitor(client, headers)

    response = await client.patch(
        f"{MONITORS}/{monitor['id']}",
        headers=headers,
        json={"search_type": "hs_code", "search_value": "nope"},
    )

    assert response.status_code == 422


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


# --- The dispatcher ---------------------------------------------------------


async def test_dispatch_sends_the_run_plan_and_claims_the_monitor(
    client: AsyncClient, db_session: Any
) -> None:
    headers = await _auth(client, db_session)
    monitor = await _create_monitor(client, headers)

    sent = await _dispatch_one(db_session, monitor["id"])

    # The same field names the manual search's webhook body carries, so the
    # shared pipeline nodes run unmodified.
    assert sent["monitor_id"] == monitor["id"]
    assert sent["run_id"]
    assert sent["search_term"] == "hardware wholesaler"
    assert sent["original_keyword"] == "hardware wholesaler"
    assert sent["expanded_keywords"] == ["hardware wholesaler"]
    assert sent["company_type"] is None
    assert sent["country"] == "de"
    assert sent["validate_whatsapp"] is False
    assert sent["limit_per_run"] == 50
    assert sent["serper_offset"] == 0
    assert sent["serper_page"] == 1
    assert sent["results_url"].endswith("/lead-radar/monitors/results")
    assert sent["progress_token"]

    # Claimed: a second tick the same night must not dispatch it again, or the
    # user is billed twice for one night's work.
    assert await _dispatch(db_session) == []

    stored = await db_session.get(RadarMonitor, monitor["id"])
    await db_session.refresh(stored)
    assert stored.started_at is not None
    assert stored.last_run_at is not None
    # Re-slotted into tomorrow, not left due.
    assert stored.next_run_at > datetime.now(UTC).replace(tzinfo=None)


async def test_dispatch_reslots_daily_into_the_next_utc_day(
    client: AsyncClient, db_session: Any
) -> None:
    headers = await _auth(client, db_session)
    monitor = await _create_monitor(client, headers)

    now = datetime(2026, 9, 24, 15, 0, tzinfo=UTC)
    await _make_due(db_session, monitor["id"])
    await _dispatch(db_session, now=now)

    stored = await db_session.get(RadarMonitor, monitor["id"])
    await db_session.refresh(stored)

    slot = stored.next_run_at
    if slot.tzinfo is None:
        slot = slot.replace(tzinfo=UTC)

    tomorrow = datetime(2026, 9, 25, tzinfo=UTC)
    assert tomorrow <= slot < tomorrow + timedelta(days=1)


async def test_dispatch_reslots_weekly_a_week_out(
    client: AsyncClient, db_session: Any
) -> None:
    headers = await _auth(client, db_session)
    monitor = await _create_monitor(client, headers, frequency="weekly")

    now = datetime(2026, 9, 24, 15, 0, tzinfo=UTC)
    await _make_due(db_session, monitor["id"])
    await _dispatch(db_session, now=now)

    stored = await db_session.get(RadarMonitor, monitor["id"])
    await db_session.refresh(stored)

    slot = stored.next_run_at
    if slot.tzinfo is None:
        slot = slot.replace(tzinfo=UTC)

    next_week = datetime(2026, 10, 1, tzinfo=UTC)
    assert next_week <= slot < next_week + timedelta(days=1)


async def test_dispatch_respects_the_batch_limit(
    client: AsyncClient, db_session: Any
) -> None:
    """Rate control: a big account's monitors reach n8n a few at a time."""
    headers = await _auth(client, db_session)
    ids = [
        (await _create_monitor(client, headers, name=f"Monitor {index}"))["id"]
        for index in range(5)
    ]
    for monitor_id in ids:
        await _make_due(db_session, monitor_id)

    first = await _dispatch(db_session, limit=2)
    assert len(first) == 2

    second = await _dispatch(db_session, limit=2)
    assert len(second) == 2

    third = await _dispatch(db_session, limit=2)
    assert len(third) == 1

    assert await _dispatch(db_session, limit=2) == []


async def test_dispatch_does_nothing_without_a_webhook(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unconfigured must mean "wait", not "claim and lose": nothing is taken."""
    headers = await _auth(client, db_session)
    monitor = await _create_monitor(client, headers)
    await _make_due(db_session, monitor["id"])

    monkeypatch.setattr(settings, "N8N_MONITOR_WEBHOOK_URL", "")

    assert await _dispatch(db_session) == []

    stored = await db_session.get(RadarMonitor, monitor["id"])
    await db_session.refresh(stored)
    assert stored.last_run_at is None


async def test_dispatch_opens_a_search_the_user_can_see(
    client: AsyncClient, db_session: Any
) -> None:
    headers = await _auth(client, db_session)
    monitor = await _create_monitor(client, headers)

    sent = await _dispatch_one(db_session, monitor["id"])

    runs = await client.get(f"{PREFIX}/lead-radar/runs", headers=headers)
    assert runs.json()["total"] == 1

    run = runs.json()["items"][0]
    assert run["id"] == sent["run_id"]
    assert run["search_type"] == "monitor"
    assert run["monitor_id"] == monitor["id"]
    assert run["status"] == "running"


async def test_an_unreachable_workflow_retries_soon_and_fails_the_search(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blip must not cost the whole day, and must not leave a run spinning."""
    monkeypatch.setattr(settings, "MONITOR_RETRY_MINUTES", 30)

    headers = await _auth(client, db_session)
    monitor = await _create_monitor(client, headers)
    await _make_due(db_session, monitor["id"])

    now = datetime.now(UTC)
    attempted = await _dispatch(db_session, now=now, respond=502)
    assert len(attempted) == 1

    stored = await db_session.get(RadarMonitor, monitor["id"])
    await db_session.refresh(stored)

    assert stored.last_error
    slot = stored.next_run_at
    if slot.tzinfo is None:
        slot = slot.replace(tzinfo=UTC)
    assert now < slot <= now + timedelta(minutes=30, seconds=1)

    runs = await client.get(f"{PREFIX}/lead-radar/runs", headers=headers)
    assert runs.json()["items"][0]["status"] == "failed"


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

    sent = await _dispatch_one(db_session, monitor["id"])

    response = await client.post(
        RESULTS,
        headers={"X-LeadPilot-Run-Token": sent["progress_token"]},
        json={
            "monitor_id": monitor["id"],
            "run_id": sent["run_id"],
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

    companies = [_company("https://a.example", ["one@a.example", "two@a.example"])]

    sent = await _dispatch_one(db_session, monitor["id"])
    first = await client.post(
        RESULTS,
        headers={"X-LeadPilot-Run-Token": sent["progress_token"]},
        json={
            "monitor_id": monitor["id"],
            "run_id": sent["run_id"],
            "companies": companies,
        },
    )
    assert first.json()["credits_charged"] == 2 * BASE_CONTACT_CREDIT

    wallet = await db_session.get(Wallet, user_id)
    await db_session.refresh(wallet)
    balance_after_first = wallet.balance

    # The very same contacts come back on the next run.
    sent = await _dispatch_one(db_session, monitor["id"])
    second = await client.post(
        RESULTS,
        headers={"X-LeadPilot-Run-Token": sent["progress_token"]},
        json={
            "monitor_id": monitor["id"],
            "run_id": sent["run_id"],
            "companies": companies,
        },
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

    sent = await _dispatch_one(db_session, monitor["id"])
    await client.post(
        RESULTS,
        headers={"X-LeadPilot-Run-Token": sent["progress_token"]},
        json={
            "monitor_id": monitor["id"],
            "run_id": sent["run_id"],
            "companies": [_company("https://a.example", ["known@a.example"])],
        },
    )

    sent = await _dispatch_one(db_session, monitor["id"])
    second = await client.post(
        RESULTS,
        headers={"X-LeadPilot-Run-Token": sent["progress_token"]},
        json={
            "monitor_id": monitor["id"],
            "run_id": sent["run_id"],
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

    sent = await _dispatch_one(db_session, monitor["id"])
    response = await client.post(
        RESULTS,
        headers={"X-LeadPilot-Run-Token": sent["progress_token"]},
        json={
            "monitor_id": monitor["id"],
            "run_id": sent["run_id"],
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

    sent = await _dispatch_one(db_session, monitor["id"])
    response = await client.post(
        RESULTS,
        headers={"X-LeadPilot-Run-Token": sent["progress_token"]},
        json={
            "monitor_id": monitor["id"],
            "run_id": sent["run_id"],
            "companies": [_company("https://prior.example", ["known@prior.example"])],
        },
    )
    body = response.json()

    assert body["contacts_added"] == 0
    assert body["duplicates_skipped"] == 1
    assert body["credits_charged"] == 0


async def test_a_run_never_bills_past_the_per_run_cap(
    client: AsyncClient, db_session: Any
) -> None:
    """The cap is the promise that bounds what a background run can cost, so
    it is enforced on the billing path rather than trusted to the workflow."""
    headers = await _auth(client, db_session)
    monitor = await _create_monitor(client, headers, name="Small cap", limit_per_run=20)
    await _fund(db_session, await _user_id(db_session), 100_000)

    sent = await _dispatch_one(db_session, monitor["id"])

    # The workflow misbehaves and returns 30 contacts against a cap of 20.
    response = await client.post(
        RESULTS,
        headers={"X-LeadPilot-Run-Token": sent["progress_token"]},
        json={
            "monitor_id": monitor["id"],
            "run_id": sent["run_id"],
            "companies": [
                _company(
                    "https://many.example",
                    [f"c{index}@many.example" for index in range(30)],
                )
            ],
        },
    )
    body = response.json()

    assert body["contacts_added"] == 20
    assert body["credits_charged"] == 20 * BASE_CONTACT_CREDIT
    assert body["total_leads_generated"] == 20


async def test_the_serper_page_follows_the_offset(
    client: AsyncClient, db_session: Any
) -> None:
    """Page and offset must stay in step, or the next run re-reads ground the
    last one already covered and the monitor finds nothing new."""
    headers = await _auth(client, db_session)
    monitor = await _create_monitor(client, headers)
    await _fund(db_session, await _user_id(db_session), 100_000)

    sent = await _dispatch_one(db_session, monitor["id"])
    assert sent["serper_offset"] == 0
    assert sent["serper_page"] == 1

    await client.post(
        RESULTS,
        headers={"X-LeadPilot-Run-Token": sent["progress_token"]},
        json={
            "monitor_id": monitor["id"],
            "run_id": sent["run_id"],
            "companies": [_company("https://a.example", ["one@a.example"])],
        },
    )

    sent = await _dispatch_one(db_session, monitor["id"])

    # limit_per_run is 50, so offset 50 is page 2 -- results 51-100.
    assert sent["serper_offset"] == 50
    assert sent["serper_page"] == 2


async def test_a_monitor_run_appears_in_your_searches_with_its_dedupe_split(
    client: AsyncClient, db_session: Any
) -> None:
    """A monitor's work must be visible beside the user's own searches, and
    must explain itself: a run that skipped 2 already-owned contacts and added
    1 should say so, not look like it lost most of what it found."""
    headers = await _auth(client, db_session)
    monitor = await _create_monitor(client, headers)
    await _fund(db_session, await _user_id(db_session), 100_000)

    sent = await _dispatch_one(db_session, monitor["id"])
    await client.post(
        RESULTS,
        headers={"X-LeadPilot-Run-Token": sent["progress_token"]},
        json={
            "monitor_id": monitor["id"],
            "run_id": sent["run_id"],
            "companies": [
                _company("https://a.example", ["one@a.example", "two@a.example"])
            ],
        },
    )

    sent = await _dispatch_one(db_session, monitor["id"])
    await client.post(
        RESULTS,
        headers={"X-LeadPilot-Run-Token": sent["progress_token"]},
        json={
            "monitor_id": monitor["id"],
            "run_id": sent["run_id"],
            "companies": [
                _company(
                    "https://a.example",
                    ["one@a.example", "two@a.example", "three@a.example"],
                )
            ],
        },
    )

    listing = await client.get(f"{PREFIX}/lead-radar/runs", headers=headers)
    items = listing.json()["items"]

    assert listing.json()["total"] == 2

    latest = items[0]
    assert latest["search_type"] == "monitor"
    assert latest["monitor_id"] == monitor["id"]
    assert latest["status"] == "completed"

    # The whole point: 3 came back, 1 was new, 2 were already owned.
    assert latest["contacts_found"] == 3
    assert latest["contacts_added"] == 1
    assert latest["contacts_duplicate"] == 2
    assert latest["companies_found"] == 1

    # Only the new one was billed.
    assert latest["credits_charged"] == BASE_CONTACT_CREDIT


async def test_a_failed_run_costs_nothing_keeps_the_offset_and_marks_the_search(
    client: AsyncClient, db_session: Any
) -> None:
    """A failed run must not skip a page of results nobody ever looked at."""
    headers = await _auth(client, db_session)
    monitor = await _create_monitor(client, headers)

    sent = await _dispatch_one(db_session, monitor["id"])
    response = await client.post(
        RESULTS,
        headers={"X-LeadPilot-Run-Token": sent["progress_token"]},
        json={
            "monitor_id": monitor["id"],
            "run_id": sent["run_id"],
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

    run = await db_session.get(LeadSearchRun, sent["run_id"])
    await db_session.refresh(run)
    assert run.status == "failed"
    assert run.credits_charged == 0


async def test_an_empty_run_still_completes_and_advances_the_offset(
    client: AsyncClient, db_session: Any
) -> None:
    """A quiet night -- every stage came up empty -- is a completed run worth
    zero leads. The page was read, so the offset moves past it; otherwise the
    monitor would re-read the same dead page every night."""
    headers = await _auth(client, db_session)
    monitor = await _create_monitor(client, headers)

    sent = await _dispatch_one(db_session, monitor["id"])
    response = await client.post(
        RESULTS,
        headers={"X-LeadPilot-Run-Token": sent["progress_token"]},
        json={
            "monitor_id": monitor["id"],
            "run_id": sent["run_id"],
            "status": "completed",
            "companies": [],
        },
    )
    body = response.json()

    assert body["contacts_added"] == 0
    assert body["credits_charged"] == 0
    assert body["serper_offset"] == 50

    run = await db_session.get(LeadSearchRun, sent["run_id"])
    await db_session.refresh(run)
    assert run.status == "completed"
