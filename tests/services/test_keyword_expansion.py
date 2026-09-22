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
        # Deliberately out of DE/ES/FR order, and ES duplicated: the required
        # trio must still come through, reordered ahead of anything else.
        "translations": [
            {"lang": "IT", "term": "Schermo LED"},
            {"lang": "es", "term": "Pantalla LED"},
            {"lang": "ES", "term": "Pantalla LED grande"},
            {"lang": "FR", "term": "Ecran LED"},
            {"lang": "DE", "term": "LED-Anzeige"},
        ],
    }
    service = KeywordExpansionService(_client_returning(200, _completion(reply)))

    result = await service.expand(["  LED screen "])

    assert result.valid is True
    assert result.normalized_keyword == "LED screen"
    assert [t.term for t in result.terms] == [
        "Digital signage",
        "OLED billboard",
        "Stadium display",
        "Mall video wall",
        "LED-Anzeige",
        "Pantalla LED",
        "Ecran LED",
        "Schermo LED",
    ]
    assert all(t.selected for t in result.terms if t.type != "lang")
    assert all(not t.selected for t in result.terms if t.type == "lang")
    # DE, ES, FR always lead, ahead of the model's own order for the rest.
    assert [t.lang for t in result.terms if t.type == "lang"] == [
        "DE",
        "ES",
        "FR",
        "IT",
    ]


async def test_non_english_keyword_keeps_english_translation() -> None:
    reply = {
        "valid": True,
        "normalized_keyword": "Hydraulic pump",
        "synonyms": ["Hydraulic gear pump"],
        "translations": [
            {"lang": "EN", "term": "Hydraulic pump"},
            {"lang": "DE", "term": "Hydraulikpumpe"},
            {"lang": "ES", "term": "Bomba hidraulica"},
            {"lang": "FR", "term": "Pompe hydraulique"},
        ],
    }
    service = KeywordExpansionService(_client_returning(200, _completion(reply)))

    result = await service.expand(["液压泵"])

    assert result.normalized_keyword == "Hydraulic pump"
    # EN is kept (it is a real translation for a non-English input), but the
    # required DE/ES/FR trio still sorts ahead of it.
    assert [(t.lang, t.term) for t in result.terms if t.type == "lang"] == [
        ("DE", "Hydraulikpumpe"),
        ("ES", "Bomba hidraulica"),
        ("FR", "Pompe hydraulique"),
        ("EN", "Hydraulic pump"),
    ]


async def test_missing_required_language_still_succeeds() -> None:
    """A model reply that skips one of DE/ES/FR should not fail the request --
    English (or whatever it did return) is still a usable expansion. (The
    drift from the prompt's hard requirement is logged separately, at
    keyword_expansion.missing_required_langs, so it stays visible without
    breaking the user's search.)"""
    reply = {
        "valid": True,
        "normalized_keyword": "Solar panel",
        "synonyms": ["PV module"],
        "translations": [
            {"lang": "DE", "term": "Solarmodul"},
            {"lang": "ES", "term": "Panel solar"},
            # FR omitted.
        ],
    }
    service = KeywordExpansionService(_client_returning(200, _completion(reply)))

    result = await service.expand(["solar panel"])

    assert result.valid is True
    assert [t.lang for t in result.terms if t.type == "lang"] == ["DE", "ES"]


async def test_generous_reply_is_not_capped_back_to_the_old_limits() -> None:
    """Search now takes several keyword tags at once, each needing its own
    thorough expansion, so the per-category caps were raised well past the
    old ~15-term total -- this pins that a full-size reply actually gets
    through rather than being trimmed back to the previous handful."""
    synonyms = [f"Synonym {i}" for i in range(10)]
    scenarios = [f"Scenario {i}" for i in range(10)]
    translations = [
        {"lang": lang, "term": f"Term {lang}"}
        for lang in ["DE", "ES", "FR", "IT", "PT", "NL", "PL", "TR", "AR", "RU"]
    ]
    reply = {
        "valid": True,
        "normalized_keyword": "LED display",
        "synonyms": synonyms,
        "scenarios": scenarios,
        "translations": translations,
    }
    service = KeywordExpansionService(_client_returning(200, _completion(reply)))

    result = await service.expand(["LED display"])

    assert len([t for t in result.terms if t.type == "synonym"]) == 10
    assert len([t for t in result.terms if t.type == "scenario"]) == 10
    assert len([t for t in result.terms if t.type == "lang"]) == 10
    assert len(result.terms) == 30


async def test_chatty_reply_is_hard_capped_at_30_terms() -> None:
    """The 25-30 target is a prompt instruction, not something the model can
    be trusted to obey -- a reply that ignores it and returns far more must
    still be trimmed to exactly 30 (10 synonyms + 10 scenarios + 10
    languages) here, in code, so the UI can never be flooded regardless of
    how the model behaves."""
    synonyms = [f"Synonym {i}" for i in range(50)]
    scenarios = [f"Scenario {i}" for i in range(50)]
    translations = [
        {"lang": lang, "term": f"Term {lang}"}
        for lang in [
            "DE", "ES", "FR", "IT", "PT", "NL", "PL", "TR", "AR", "RU",
            "JA", "KO", "VI", "ID", "TH",
        ]
    ]
    reply = {
        "valid": True,
        "normalized_keyword": "LED display",
        "synonyms": synonyms,
        "scenarios": scenarios,
        "translations": translations,
    }
    service = KeywordExpansionService(_client_returning(200, _completion(reply)))

    result = await service.expand(["LED display"])

    assert len([t for t in result.terms if t.type == "synonym"]) == 10
    assert len([t for t in result.terms if t.type == "scenario"]) == 10
    assert len([t for t in result.terms if t.type == "lang"]) == 10
    assert len(result.terms) == 30


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
    result = await service.expand([junk])

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

    result = await service.expand(["12345 !!!"])

    assert result.valid is False


async def test_missing_api_key_gives_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "")

    with pytest.raises(ServiceUnavailableError):
        await KeywordExpansionService().expand(["LED screen"])


async def test_upstream_http_error_becomes_502() -> None:
    service = KeywordExpansionService(_client_returning(401, {"error": "bad key"}))

    with pytest.raises(UpstreamError):
        await service.expand(["LED screen"])


async def test_unparseable_model_reply_becomes_502() -> None:
    body = {"choices": [{"message": {"content": "not json at all"}}]}
    service = KeywordExpansionService(_client_returning(200, body))

    with pytest.raises(UpstreamError):
        await service.expand(["LED screen"])


async def test_fenced_json_is_tolerated() -> None:
    reply = {
        "valid": True,
        "normalized_keyword": "Solar panel",
        "synonyms": ["PV module"],
    }
    body = {"choices": [{"message": {"content": f"```json\n{json.dumps(reply)}\n```"}}]}
    service = KeywordExpansionService(_client_returning(200, body))

    result = await service.expand(["solar panel"])

    assert result.valid is True
    assert [t.term for t in result.terms] == ["PV module"]


# --- Several keyword tags expanded together -----------------------------------


async def test_two_keywords_share_one_30_term_budget() -> None:
    """The whole point: several tags submitted together get ONE combined
    30-term ceiling, not 30 each."""
    reply = {
        "valid": True,
        "normalized_keyword": "LED display, Digital signage",
        "synonyms": [f"Synonym {i}" for i in range(10)],
        "scenarios": [f"Scenario {i}" for i in range(10)],
        "translations": [
            {"lang": lang, "term": f"Term {lang}"}
            for lang in ["DE", "ES", "FR", "IT", "PT", "NL", "PL", "TR", "AR", "RU"]
        ],
    }
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=_completion(reply))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = KeywordExpansionService(client)

    result = await service.expand(["LED display", "Digital signage"])

    # One API call for both tags together, not one per tag.
    assert len(sent) == 1
    user_message = sent[0]["messages"][1]["content"]
    assert "LED display" in user_message
    assert "Digital signage" in user_message

    assert result.valid is True
    assert len(result.terms) == 30
    assert result.normalized_keyword == "LED display, Digital signage"


async def test_one_bad_tag_among_several_is_dropped_not_fatal() -> None:
    """A junk tag mixed in with real ones should not block the whole search --
    it is reported back so the UI can tell the user, but the good tags still
    expand normally."""
    reply = {
        "valid": True,
        "normalized_keyword": "LED display",
        "rejected_keywords": [
            {"keyword": "asdfgh", "reason": "That is not a real product keyword."}
        ],
        "synonyms": ["Digital signage"],
        "scenarios": ["Stadium display"],
        "translations": [
            {"lang": "DE", "term": "LED-Anzeige"},
            {"lang": "ES", "term": "Pantalla LED"},
            {"lang": "FR", "term": "Ecran LED"},
        ],
    }
    service = KeywordExpansionService(_client_returning(200, _completion(reply)))

    result = await service.expand(["LED display", "asdfgh"])

    assert result.valid is True
    assert result.rejected_keywords == ["asdfgh"]
    assert [t.term for t in result.terms] == [
        "Digital signage",
        "Stadium display",
        "LED-Anzeige",
        "Pantalla LED",
        "Ecran LED",
    ]


async def test_every_tag_rejected_fails_the_whole_batch() -> None:
    reply = {
        "valid": False,
        "reason": "Neither of those looks like a product keyword.",
        "suggestions": ["LED screen", "Solar panel", "Hydraulic pump", "Steel pipe"],
        "rejected_keywords": [
            {"keyword": "asdfgh", "reason": "Random characters."},
            {"keyword": "qwerty", "reason": "Random characters."},
        ],
    }
    service = KeywordExpansionService(_client_returning(200, _completion(reply)))

    result = await service.expand(["asdfgh", "qwerty"])

    assert result.valid is False
    assert result.reason == "Neither of those looks like a product keyword"
    assert result.terms == []


async def test_locally_rejected_tag_is_reported_alongside_model_rejects() -> None:
    """A tag with no letters at all never reaches the model (the cheap local
    guard catches it), but it still needs to show up as rejected -- not just
    silently vanish -- when at least one other tag is usable."""
    reply = {
        "valid": True,
        "normalized_keyword": "LED display",
        "synonyms": ["Digital signage"],
    }
    service = KeywordExpansionService(_client_returning(200, _completion(reply)))

    result = await service.expand(["LED display", "12345"])

    assert result.valid is True
    assert result.rejected_keywords == ["12345"]
