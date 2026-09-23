"""AI HS Code translation for the Advanced Filters panel, backed by DeepSeek.

Phase 1 of the HS Code search (see services/lead_search.py's start_hs_code_search
for phase 2): the user types a plain product description, and this turns it
into up to 3 candidate 6-digit HS codes with their official descriptions for
the user to review and pick from. Nothing is billed here -- no search has run
yet, there is nothing to charge for.
"""

import json
import re
from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError

from app.core.config import settings
from app.core.exceptions import ServiceUnavailableError, UpstreamError
from app.core.logging import get_logger
from app.schemas.lead_radar import HsCodeCandidate, TranslateHsCodeResponse

log = get_logger(__name__)

MAX_CANDIDATES = 3

SYSTEM_PROMPT = """You are a customs classification expert. A user of a B2B lead-\
generation tool types a plain product description (in any language) and wants to know \
which Harmonized System (HS) code it falls under, so they can search for importers and \
distributors of that exact product worldwide.

The input is usually a product description, but it may instead be a 6-digit HS code the \
user already knows. When it is a valid HS code, treat it as valid and return that exact \
code as the first candidate with its official description, optionally followed by \
closely related codes. Either way the "description" matters as much as the code: it is \
what the lead search actually searches the web for, because almost no company publishes \
an HS code on its own website.

Otherwise, decide whether the input is a genuine product, material or equipment \
description (accept typos, non-English input, and informal wording like "LED screen" or \
"hydraulic pump"). REJECT input that is:
- random or keyboard-mashed characters, a greeting, a question, or a chat message
- a company name, a person's name, or a generic word with no product meaning
- offensive content or unrelated to commerce

If you reject it, set "valid" to false, fill "reason" with one friendly sentence \
explaining why, and leave "candidates" empty.

If it is a real product, set "valid" to true and return the TOP 3 most relevant \
6-digit HS codes for it, most specific/likely match first. Use the official 2022 HS \
nomenclature. Each candidate needs the 6-digit "code" (digits only, no dots or spaces) \
and its \
official "description" (the real HS heading text, in English, not a paraphrase) -- \
return fewer than 3 only if the product genuinely has no other plausible classification.

Reply with a single JSON object and nothing else, in exactly this shape:
{
  "valid": true,
  "reason": null,
  "candidates": [
    {"code": "852859", "description": "Other monitors and projectors"},
    {"code": "854231", "description": "Electronic integrated circuits: processors"}
  ]
}
When the input is not a real product, set "valid" to false, fill "reason", and leave \
"candidates" empty."""


class _Candidate(BaseModel):
    code: str = ""
    description: str = ""


class _ModelReply(BaseModel):
    valid: bool
    reason: str | None = None
    candidates: list[_Candidate] = Field(default_factory=list)


_DIGITS = re.compile(r"\D")


class HsCodeService:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        # Injectable so tests can hand in a MockTransport-backed client.
        self._client = client

    async def translate(self, text: str) -> TranslateHsCodeResponse:
        cleaned = " ".join(text.split())

        # Cheap local guard: nothing with at least one letter or CJK
        # character is worth a model call -- except a bare HS code, which the
        # model resolves to its official description (the search needs that
        # wording, not the digits).
        if not re.fullmatch(r"\d{6}", cleaned) and not re.search(r"[^\W\d_]", cleaned):
            return TranslateHsCodeResponse(
                valid=False,
                reason="Please enter a product description, "
                "for example 'LED screen' or 'hydraulic pump'.",
            )

        if not settings.DEEPSEEK_API_KEY:
            log.warning("hs_code.disabled", reason="DEEPSEEK_API_KEY not set")
            raise ServiceUnavailableError(
                "AI HS code lookup is not configured on this server."
            )

        reply = await self._ask_model(cleaned)

        return self._to_response(reply)

    async def _ask_model(self, text: str) -> _ModelReply:
        body = {
            "model": settings.DEEPSEEK_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Product: {text}"},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
            "max_tokens": 500,
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
            log.warning("hs_code.timeout", text=text)
            raise UpstreamError(
                "The AI service took too long to answer. Try again."
            ) from exc
        except httpx.HTTPError as exc:
            log.error("hs_code.transport_error", error=str(exc))
            raise UpstreamError("Could not reach the AI service.") from exc

        if response.status_code == 401:
            log.error("hs_code.bad_api_key")
            raise UpstreamError("The AI service rejected the API key.")
        if response.status_code == 402:
            log.error("hs_code.insufficient_balance")
            raise UpstreamError("The AI service account has no remaining balance.")
        if response.status_code == 429:
            raise UpstreamError(
                "The AI service is rate-limiting requests. Try again shortly."
            )
        if response.status_code >= 400:
            log.error(
                "hs_code.http_error",
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
            log.error("hs_code.unexpected_envelope", payload=str(payload)[:500])
            raise UpstreamError(
                "The AI service returned an unexpected response."
            ) from exc

        content = content.strip()
        if content.startswith("```"):
            content = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", content)

        try:
            return _ModelReply.model_validate(json.loads(content))
        except (json.JSONDecodeError, ValidationError) as exc:
            log.error("hs_code.bad_json", content=content[:500], error=str(exc))
            raise UpstreamError("The AI service returned an unreadable answer.") from exc

    @staticmethod
    def _to_response(reply: _ModelReply) -> TranslateHsCodeResponse:
        if not reply.valid:
            return TranslateHsCodeResponse(
                valid=False,
                reason=reply.reason
                or "That doesn't look like a product description.",
            )

        candidates: list[HsCodeCandidate] = []
        seen: set[str] = set()
        for c in reply.candidates:
            digits = _DIGITS.sub("", c.code)
            description = " ".join(c.description.split())
            if len(digits) != 6 or digits in seen or not description:
                continue

            seen.add(digits)
            candidates.append(HsCodeCandidate(code=digits, description=description))
            if len(candidates) >= MAX_CANDIDATES:
                break

        if not candidates:
            # The model said "valid" but gave us nothing usable -- upstream
            # trouble, not a real rejection, so the user is not told their
            # product was invalid.
            log.error("hs_code.empty_result")
            raise UpstreamError("The AI service returned no usable HS codes. Try again.")

        return TranslateHsCodeResponse(valid=True, candidates=candidates)


__all__ = ["HsCodeService"]
