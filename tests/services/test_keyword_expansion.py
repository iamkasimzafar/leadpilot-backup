"""KeywordExpansionService against a mocked DeepSeek endpoint."""

import json
import random
import string
from typing import Any

import httpx
import pytest

from app.core.config import settings
from app.core.exceptions import ServiceUnavailableError, UpstreamError
from app.services.keyword_expansion import KeywordExpansionService


def _client_returning(status: int, body: Any) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-key"
        sent = json.loads(request.content)
        assert sent["response_format"] == {"type": "json_object"}
        return httpx.Response(status, json=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _completion(reply: dict[str, Any]) -> dict[str, Any]:
    return {"choices": [{"message": {"content": json.dumps(reply)}}]}


@pytest.fixture(autouse=True)
def _api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "test-key")


async def test_valid_keyword_is_expanded_and_cleaned() -> None:
    reply = {
        "valid": True,
        "normalized_keyword": "LED screen",
        "synonyms": [
            "Digital signage",
            "digital signage ",
            "LED screen",
            "OLED billboard",
        ],
        "scenarios": ["Stadium display", "Digital Signage", "Mall video wall"],
        "translations": [
            {"lang": "es", "term": "Pantalla LED"},
            {"lang": "ES", "term": "Pantalla LED grande"},
            {"lang": "DE", "term": "LED-Anzeige"},
        ],
    }
    service = KeywordExpansionService(_client_returning(200, _completion(reply)))

    result = await service.expand("  LED screen ")

    assert result.valid is True
    assert result.normalized_keyword == "LED screen"
    assert [t.term for t in result.terms] == [
        "Digital signage",
        "OLED billboard",
        "Stadium display",
        "Mall video wall",
        "Pantalla LED",
        "LED-Anzeige",
    ]
    assert all(t.selected for t in result.terms if t.type != "lang")
    assert all(not t.selected for t in result.terms if t.type == "lang")
    assert [t.lang for t in result.terms if t.type == "lang"] == ["ES", "DE"]


async def test_non_english_keyword_keeps_english_translation() -> None:
    reply = {
        "valid": True,
        "normalized_keyword": "Hydraulic pump",
        "synonyms": ["Hydraulic gear pump"],
        "translations": [
            {"lang": "EN", "term": "Hydraulic pump"},
            {"lang": "DE", "term": "Hydraulikpumpe"},
        ],
    }
    service = KeywordExpansionService(_client_returning(200, _completion(reply)))

    result = await service.expand("液压泵")

    assert result.normalized_keyword == "Hydraulic pump"
    assert [(t.lang, t.term) for t in result.terms if t.type == "lang"] == [
        ("EN", "Hydraulic pump"),
        ("DE", "Hydraulikpumpe"),
    ]


async def test_gibberish_is_rejected_with_reason_and_suggestions() -> None:
    reply = {
        "valid": False,
        "reason": "That looks like random characters, not a product.",
        "suggestions": ["LED screen", "led screen", "Solar panel"],
    }
    service = KeywordExpansionService(_client_returning(200, _completion(reply)))

    # Any junk input: the rejection comes from the model's verdict, not from
    # the service recognising a particular string.
    junk = "".join(random.choices(string.ascii_lowercase, k=12))
    result = await service.expand(junk)

    assert result.valid is False
    assert result.reason == "That looks like random characters, not a product"
    assert result.suggestions == ["LED screen", "Solar panel"]
    assert result.terms == []


async def test_input_without_letters_is_rejected_locally() -> None:
    # No transport at all: the guard must answer before any network call.
    service = KeywordExpansionService(
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: pytest.fail("network call not expected")  # type: ignore[arg-type]
            )
        )
    )

    result = await service.expand("12345 !!!")

    assert result.valid is False


async def test_missing_api_key_gives_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "")

    with pytest.raises(ServiceUnavailableError):
        await KeywordExpansionService().expand("LED screen")


async def test_upstream_http_error_becomes_502() -> None:
    service = KeywordExpansionService(_client_returning(401, {"error": "bad key"}))

    with pytest.raises(UpstreamError):
        await service.expand("LED screen")


async def test_unparseable_model_reply_becomes_502() -> None:
    body = {"choices": [{"message": {"content": "not json at all"}}]}
    service = KeywordExpansionService(_client_returning(200, body))

    with pytest.raises(UpstreamError):
        await service.expand("LED screen")


async def test_fenced_json_is_tolerated() -> None:
    reply = {
        "valid": True,
        "normalized_keyword": "Solar panel",
        "synonyms": ["PV module"],
    }
    body = {"choices": [{"message": {"content": f"```json\n{json.dumps(reply)}\n```"}}]}
    service = KeywordExpansionService(_client_returning(200, body))

    result = await service.expand("solar panel")

    assert result.valid is True
    assert [t.term for t in result.terms] == ["PV module"]
