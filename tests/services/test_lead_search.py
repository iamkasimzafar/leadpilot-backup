"""LeadSearchService against a mocked n8n webhook."""

import json
from typing import Any

import httpx
import pytest

from app.core.config import settings
from app.core.exceptions import ServiceUnavailableError, UpstreamError
from app.services.lead_search import LeadSearchService

WEBHOOK_URL = "http://n8n.test/webhook-test/lead-search-v1"


@pytest.fixture(autouse=True)
def _webhook_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "N8N_WEBHOOK_URL", WEBHOOK_URL)
    monkeypatch.setattr(settings, "N8N_WEBHOOK_SECRET", "")


def _capturing_client(
    sink: dict[str, Any], status: int = 200, body: Any = None
) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        sink["url"] = str(request.url)
        sink["json"] = json.loads(request.content)
        sink["headers"] = dict(request.headers)
        return httpx.Response(status, json=body if body is not None else {"ok": True})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_payload_matches_the_workflow_contract() -> None:
    sink: dict[str, Any] = {}
    service = LeadSearchService(_capturing_client(sink))

    result = await service.start(
        "US furniture importer",
        ["furniture distributor USA", "wholesale furniture importer US"],
    )

    assert sink["url"] == WEBHOOK_URL
    assert sink["json"]["original_keyword"] == "US furniture importer"
    assert sink["json"]["expanded_keywords"] == [
        "US furniture importer",
        "furniture distributor USA",
        "wholesale furniture importer US",
    ]
    assert result.dispatched is True
    assert result.expanded_keywords == sink["json"]["expanded_keywords"]


async def test_original_keyword_leads_and_duplicates_are_dropped() -> None:
    sink: dict[str, Any] = {}
    service = LeadSearchService(_capturing_client(sink))

    await service.start(
        "  LED   screen ",
        ["Digital signage", "led screen", "  ", "Digital Signage", "LED video wall"],
    )

    assert sink["json"]["expanded_keywords"] == [
        "LED screen",
        "Digital signage",
        "LED video wall",
    ]


async def test_secret_is_sent_as_header_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "N8N_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setattr(settings, "N8N_WEBHOOK_HEADER", "X-LeadPilot-Token")
    sink: dict[str, Any] = {}
    service = LeadSearchService(_capturing_client(sink))

    await service.start("LED screen", [])

    assert sink["headers"]["x-leadpilot-token"] == "s3cret"


async def test_no_secret_means_no_header() -> None:
    sink: dict[str, Any] = {}
    service = LeadSearchService(_capturing_client(sink))

    await service.start("LED screen", [])

    assert "x-leadpilot-token" not in sink["headers"]


async def test_unconfigured_webhook_gives_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "N8N_WEBHOOK_URL", "")

    with pytest.raises(ServiceUnavailableError):
        await LeadSearchService().start("LED screen", [])


async def test_unregistered_test_webhook_explains_how_to_arm_it() -> None:
    body = {"code": 404, "message": 'The requested webhook "x" is not registered.'}
    sink: dict[str, Any] = {}
    service = LeadSearchService(_capturing_client(sink, status=404, body=body))

    with pytest.raises(UpstreamError) as exc:
        await service.start("LED screen", [])

    assert "Execute workflow" in exc.value.message


async def test_workflow_error_becomes_502() -> None:
    sink: dict[str, Any] = {}
    service = LeadSearchService(_capturing_client(sink, status=500, body={"e": 1}))

    with pytest.raises(UpstreamError):
        await service.start("LED screen", [])
