"""One vocabulary for a WhatsApp check, whatever the workflow called it."""

import json
from typing import Any

import httpx
import pytest
from httpx import AsyncClient

from app.core.config import settings
from app.schemas.lead import DecisionMakerIn, normalise_whatsapp_status
from app.services import lead_search as lead_search_module
from tests.api.test_auth import signed_in_tokens

PREFIX = settings.API_V1_PREFIX


@pytest.mark.parametrize(
    ("sent", "stored"),
    [
        # What the validator API itself says.
        ("valid", "Active"),
        ("invalid", "Not Active"),
        # What the workflows translate it to.
        ("Active", "Active"),
        ("Not Active", "Not Active"),
        ("  not   ACTIVE ", "Not Active"),
        # JavaScript booleans.
        (True, "Active"),
        (False, "Not Active"),
        ("exists", "Active"),
        ("not found", "Not Active"),
        # A descriptive label is read by its leading phrase, negatives first.
        ("Active on WhatsApp Business", "Active"),
        ("Not active on WhatsApp", "Not Active"),
        ("invalid: number not registered", "Not Active"),
        # ...but only whole words, and never from a bare "no" / "yes".
        ("validation failed", None),
        ("no response from the API", None),
        # No check was made: never reachable, never billed.
        (None, None),
        ("", None),
        ("Not Checked", None),
        ("unknown", None),
        ("N/A", None),
        ("The service was not able to process your request", None),
        (404, None),
    ],
)
def test_every_wording_lands_on_one_of_three_values(
    sent: Any, stored: str | None
) -> None:
    assert normalise_whatsapp_status(sent) == stored
    assert (
        DecisionMakerIn(full_name="Ana", whatsapp_status=sent).whatsapp_status == stored
    )


# --- Through the results callback ------------------------------------------------


@pytest.fixture(autouse=True)
def _webhooks_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "N8N_WEBHOOK_URL", "https://n8n.test/webhook/leads")
    monkeypatch.setattr(
        settings, "N8N_LOCAL_WEBHOOK_URL", "https://n8n.test/webhook/local"
    )
    monkeypatch.setattr(settings, "PUBLIC_API_URL", "https://api.leadpilot.test")


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


def _contact(name: str, status: Any) -> dict[str, Any]:
    return {"full_name": name, "phone_number": "+17185550100", "whatsapp_status": status}


async def test_unchecked_numbers_are_neither_shown_as_reachable_nor_billed(
    client: AsyncClient, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bug this guards: a workflow that could not read its validator's
    output labelled every contact "Not Checked". That is not null, so the UI
    showed all of them as on WhatsApp and the run was billed a check each."""
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
        json={
            "search_type": "local",
            "original_keyword": "supermarket",
            "location": "Brooklyn, New York",
            "validate_whatsapp": True,
        },
        headers=headers,
    )
    assert started.status_code == 200
    run_id = started.json()["run_id"]

    saved = await client.post(
        f"{PREFIX}/lead-radar/results",
        json={
            "run_id": run_id,
            "companies": [
                {
                    "company_name": "On It",
                    "decision_makers": [_contact("On It", "valid")],
                },
                {
                    "company_name": "Not On It",
                    "decision_makers": [_contact("Not On It", "invalid")],
                },
                {
                    "company_name": "Errored",
                    "decision_makers": [_contact("Errored", "Not Checked")],
                },
                {
                    "company_name": "Skipped",
                    "decision_makers": [_contact("Skipped", None)],
                },
            ],
        },
        headers={"X-LeadPilot-Run-Token": captured["progress_token"]},
    )
    assert saved.status_code == 201

    results = await client.get(
        f"{PREFIX}/lead-radar/runs/{run_id}/results", headers=headers
    )
    stored = {
        company["company_name"]: company["decision_makers"][0]["whatsapp_status"]
        for company in results.json()
    }
    assert stored == {
        "On It": "Active",
        "Not On It": "Not Active",
        "Errored": None,
        "Skipped": None,
    }

    # Two checks were really made, so two are billed -- not four.
    run = await client.get(f"{PREFIX}/lead-radar/runs/{run_id}", headers=headers)
    assert run.json()["whatsapp_checks"] == 2
