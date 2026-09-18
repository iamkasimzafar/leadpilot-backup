"""AI keyword expansion for Lead Radar, backed by DeepSeek.

One chat completion does two jobs: it decides whether the input is a real
product / industry keyword at all (random characters, chat messages and the like
are turned away with an explanation instead of producing nonsense), and for valid input it
returns synonyms, buyer scenarios and translations. The model is forced into
JSON mode and its answer is validated with pydantic before anything reaches the
client, so a malformed reply becomes a clean 502 rather than a crash.
"""

import json
import re
from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError

from app.core.config import settings
from app.core.exceptions import ServiceUnavailableError, UpstreamError
from app.core.logging import get_logger
from app.schemas.lead_radar import ExpandedTerm, ExpandKeywordResponse
from app.services.company_types import name_for, prompt_hint_for

log = get_logger(__name__)

# Hard caps so a chatty model can't flood the UI.
MAX_SYNONYMS = 6
MAX_SCENARIOS = 5
MAX_TRANSLATIONS = 6
MAX_SUGGESTIONS = 4
MAX_TERM_LENGTH = 60

SYSTEM_PROMPT = """You are a B2B export lead-generation keyword expert. A user of a \
lead-finding tool types a product, service or industry keyword. Your job has two steps.

STEP 1 - VALIDATE. Decide whether the input is a genuine product, service, material, \
equipment or industry keyword that a sales team could use to find buyer companies. \
Accept keywords in any language (English and Chinese are most common), including \
technical terms, HS-code style descriptions, and common misspellings of real products \
(treat "LED screena" as "LED screen"). REJECT the input when it is:
- random, repeated or keyboard-mashed characters that form no real word in any language
- a greeting, question, sentence, command or chat message
- a person's name, a single pronoun, a number, or a generic word with no product meaning
- offensive content or something unrelated to commerce
When rejecting, explain in one short, friendly sentence why it cannot be used, and \
ALWAYS fill "suggestions" with exactly 4 real product keywords the user could search \
instead. If the input resembles a real product (a typo, a partial word, a vague \
category), suggest the closest matching products. Otherwise suggest 4 varied, \
commonly traded products from different industries as examples. Never return an \
empty "suggestions" array for invalid input.

STEP 2 - EXPAND (only when valid). Produce search terms a lead-generation engine \
would use to find companies that BUY or USE the product:
- "normalized_keyword": the keyword in clean English (fix typos, translate if needed).
- "synonyms": 4 to 6 alternative names for the same product as used in international \
trade and product catalogues. Real, commonly used terms only.
- "scenarios": 3 to 5 application, use-case or buyer-segment phrases describing WHERE \
the product is used or WHO buys it (e.g. for "LED screen": "Stadium display", \
"Mall video wall", "Outdoor advertising screen", "Indoor LED screen buyer").
- "translations": the core keyword in Spanish (ES), German (DE), French (FR), \
Italian (IT) and Portuguese (PT). If the input was not in English, add English (EN) \
first. Use the natural term a native buyer would search, not a literal word-for-word \
translation.

Rules: every term must be short (2-5 words), specific, and different from the others. \
Never repeat the original keyword. No explanations inside terms. Title-case English \
terms like a product name ("Digital signage"), keep other languages natural.

TARGET BUYER. The user may tell you which kind of company they want to find, as a \
"Target company type" line after the keyword. When they do, bias every synonym and \
especially every scenario towards the words THAT kind of company uses when searching \
or describing itself, so the terms become Google queries that surface those companies \
rather than the general market. Keep the terms about the product and its buyers; do \
not simply append the company type to each term. When no target company type is \
given, cover the market broadly as usual.

Reply with a single JSON object and nothing else, in exactly this shape:
{
  "valid": true,
  "normalized_keyword": "LED screen",
  "reason": null,
  "suggestions": [],
  "synonyms": ["..."],
  "scenarios": ["..."],
  "translations": [{"lang": "ES", "term": "Pantalla LED"}]
}
For invalid input set "valid" to false, fill "reason" and "suggestions", and leave \
the other arrays empty."""


class _Translation(BaseModel):
    lang: str = Field(min_length=2, max_length=5)
    term: str


class _ModelReply(BaseModel):
    """The JSON contract we ask the model for. Everything optional so a partially
    broken answer still degrades gracefully instead of failing validation."""

    valid: bool
    normalized_keyword: str | None = None
    reason: str | None = None
    suggestions: list[str] = Field(default_factory=list)
    synonyms: list[str] = Field(default_factory=list)
    scenarios: list[str] = Field(default_factory=list)
    translations: list[_Translation] = Field(default_factory=list)


_WHITESPACE = re.compile(r"\s+")


def _clean(term: str) -> str:
    return _WHITESPACE.sub(" ", term).strip(" \t\r\n\"'.,;:")


class KeywordExpansionService:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        # Injectable so tests can hand in a MockTransport-backed client.
        self._client = client

    async def expand(
        self, keyword: str, company_type: str | None = None
    ) -> ExpandKeywordResponse:
        keyword = _clean(keyword)

        # Cheap local guard: nothing to send if there is not a single letter or
        # CJK character in the input.
        if not re.search(r"[^\W\d_]", keyword):
            return ExpandKeywordResponse(
                valid=False,
                reason="Please enter a product or industry keyword, "
                "for example 'LED screen' or 'hydraulic pump'.",
            )

        if not settings.DEEPSEEK_API_KEY:
            log.warning("keyword_expansion.disabled", reason="DEEPSEEK_API_KEY not set")
            raise ServiceUnavailableError(
                "AI keyword expansion is not configured on this server."
            )

        reply = await self._ask_model(keyword, company_type)
        return self._to_response(keyword, reply)

    # --- DeepSeek call --------------------------------------------------------

    @staticmethod
    def _user_message(keyword: str, company_type: str | None) -> str:
        """The user turn: the keyword, plus who we are trying to reach.

        The company type is expanded into its description rather than sent as a
        bare code, so the model is told what that kind of buyer actually is.
        """
        lines = [f"Keyword: {keyword}"]

        hint = prompt_hint_for(company_type) if company_type else None
        if hint:
            name = name_for(company_type or "")
            lines.append(f"Target company type: {name} — {hint}")

        return "\n".join(lines)

    async def _ask_model(
        self, keyword: str, company_type: str | None = None
    ) -> _ModelReply:
        body = {
            "model": settings.DEEPSEEK_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": self._user_message(keyword, company_type),
                },
            ],
            # DeepSeek's JSON mode: guarantees parseable output as long as the
            # prompt mentions JSON (it does).
            "response_format": {"type": "json_object"},
            "temperature": 0.4,
            "max_tokens": 900,
        }
        headers = {"Authorization": f"Bearer {settings.DEEPSEEK_API_KEY}"}
        url = f"{settings.DEEPSEEK_BASE_URL.rstrip('/')}/chat/completions"

        try:
            if self._client is not None:
                response = await self._client.post(url, json=body, headers=headers)
            else:
                async with httpx.AsyncClient(timeout=settings.DEEPSEEK_TIMEOUT) as client:
                    response = await client.post(url, json=body, headers=headers)
        except httpx.TimeoutException as exc:
            log.warning("keyword_expansion.timeout", keyword=keyword)
            raise UpstreamError(
                "The AI service took too long to answer. Try again."
            ) from exc
        except httpx.HTTPError as exc:
            log.error("keyword_expansion.transport_error", error=str(exc))
            raise UpstreamError("Could not reach the AI service.") from exc

        if response.status_code == 401:
            log.error("keyword_expansion.bad_api_key")
            raise UpstreamError("The AI service rejected the API key.")
        if response.status_code == 402:
            log.error("keyword_expansion.insufficient_balance")
            raise UpstreamError("The AI service account has no remaining balance.")
        if response.status_code == 429:
            raise UpstreamError(
                "The AI service is rate-limiting requests. Try again shortly."
            )
        if response.status_code >= 400:
            log.error(
                "keyword_expansion.http_error",
                status=response.status_code,
                body=response.text[:500],
            )
            raise UpstreamError()

        return self._parse_reply(response.json())

    @staticmethod
    def _parse_reply(payload: Any) -> _ModelReply:
        try:
            content: str = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            log.error("keyword_expansion.unexpected_envelope", payload=str(payload)[:500])
            raise UpstreamError(
                "The AI service returned an unexpected response."
            ) from exc

        # Belt and braces: strip a ```json fence if the model added one anyway.
        content = content.strip()
        if content.startswith("```"):
            content = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", content)

        try:
            return _ModelReply.model_validate(json.loads(content))
        except (json.JSONDecodeError, ValidationError) as exc:
            log.error("keyword_expansion.bad_json", content=content[:500], error=str(exc))
            raise UpstreamError("The AI service returned an unreadable answer.") from exc

    # --- Post-processing ------------------------------------------------------

    @staticmethod
    def _to_response(keyword: str, reply: _ModelReply) -> ExpandKeywordResponse:
        if not reply.valid:
            return ExpandKeywordResponse(
                valid=False,
                normalized_keyword=None,
                reason=_clean(reply.reason or "")
                or "That doesn't look like a product or industry keyword.",
                suggestions=_dedupe(
                    reply.suggestions, MAX_SUGGESTIONS, exclude={keyword}
                ),
            )

        exclude = {keyword}
        normalized = _clean(reply.normalized_keyword or "") or keyword
        exclude.add(normalized)

        synonyms = _dedupe(reply.synonyms, MAX_SYNONYMS, exclude)
        exclude.update(synonyms)
        scenarios = _dedupe(reply.scenarios, MAX_SCENARIOS, exclude)
        exclude.update(scenarios)

        terms: list[ExpandedTerm] = [
            ExpandedTerm(term=t, type="synonym", selected=True) for t in synonyms
        ] + [ExpandedTerm(term=t, type="scenario", selected=True) for t in scenarios]

        # For a non-English keyword the English form IS a translation worth
        # searching, so only the typed keyword and the terms above are excluded
        # here, not the normalized form.
        seen_terms = {e.casefold() for e in exclude if e != normalized}
        seen_langs: set[str] = set()
        for tr in reply.translations:
            term = _clean(tr.term)
            lang = tr.lang.strip().upper()[:2]
            key = term.casefold()
            if (
                not term
                or len(term) > MAX_TERM_LENGTH
                or lang in seen_langs
                or key in seen_terms
            ):
                continue
            seen_langs.add(lang)
            seen_terms.add(key)
            terms.append(ExpandedTerm(term=term, type="lang", lang=lang, selected=False))
            if len(seen_langs) >= MAX_TRANSLATIONS:
                break

        if not terms:
            # The model said "valid" but gave us nothing usable. Treat it as
            # upstream trouble rather than showing an empty step 2.
            log.error("keyword_expansion.empty_result", keyword=keyword)
            raise UpstreamError("The AI service returned no usable terms. Try again.")

        return ExpandKeywordResponse(
            valid=True, normalized_keyword=normalized, terms=terms
        )


def _dedupe(values: list[str], limit: int, exclude: set[str]) -> list[str]:
    """Trim, drop blanks / over-long entries / duplicates (case-insensitive) and
    anything already present in `exclude`, preserving the model's order."""
    seen = {e.casefold() for e in exclude}
    out: list[str] = []
    for raw in values:
        term = _clean(raw)
        key = term.casefold()
        if not term or len(term) > MAX_TERM_LENGTH or key in seen:
            continue
        seen.add(key)
        out.append(term)
        if len(out) >= limit:
            break
    return out
