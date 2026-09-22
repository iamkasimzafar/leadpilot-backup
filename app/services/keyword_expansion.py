"""AI keyword expansion for Lead Radar, backed by DeepSeek.

One chat completion does two jobs: it decides which of the submitted keywords
are real product / industry keywords at all (random characters, chat messages
and the like are turned away with an explanation instead of producing
nonsense), and for the usable ones it returns ONE combined set of synonyms,
buyer scenarios and translations covering all of them together -- the search
box takes several keyword tags at once, but the AI expansion budget (25-30
terms) is shared across the whole batch, not multiplied per tag. The model is
forced into JSON mode and its answer is validated with pydantic before
anything reaches the client, so a malformed reply becomes a clean 502 rather
than a crash.
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

# Hard caps so a chatty model can't flood the UI -- 25-30 terms TOTAL across
# every keyword in the request, not per keyword: 10 + 10 + 10 = 30 at most,
# covering however many keywords were submitted together.
MAX_SYNONYMS = 10
MAX_SCENARIOS = 10
MAX_TRANSLATIONS = 10
MAX_SUGGESTIONS = 4
MAX_TERM_LENGTH = 60

SYSTEM_PROMPT = """You are a B2B export lead-generation keyword expert. A user of a \
lead-finding tool types one or more product, service or industry keywords at once (as \
separate tags). Your job has two steps.

STEP 1 - VALIDATE EACH KEYWORD. For every keyword in the "Keywords" list, decide \
whether it is a genuine product, service, material, equipment or industry keyword that \
a sales team could use to find buyer companies. Accept keywords in any language \
(English and Chinese are most common), including technical terms, HS-code style \
descriptions, and common misspellings of real products (treat "LED screena" as "LED \
screen"). REJECT a keyword when it is:
- random, repeated or keyboard-mashed characters that form no real word in any language
- a greeting, question, sentence, command or chat message
- a person's name, a single pronoun, a number, or a generic word with no product meaning
- offensive content or something unrelated to commerce
List every rejected keyword in "rejected_keywords" with a one-sentence friendly reason \
each. A keyword not listed there is accepted. If ALL keywords are rejected, also set \
"valid" to false and ALWAYS fill "suggestions" with exactly 4 real product keywords the \
user could search instead (the closest matches to what was typed if it resembles a real \
product, otherwise 4 varied, commonly traded products from different industries). If at \
least one keyword is accepted, set "valid" to true and leave "suggestions" empty --  \
proceed to Step 2 using only the accepted keywords.

STEP 2 - EXPAND (using every accepted keyword together). Produce ONE combined set of 25 \
to 30 high-quality search terms a lead-generation engine would use to find companies \
that BUY or USE the accepted products -- this budget is shared across ALL accepted \
keywords in this request, not given separately to each one (2 accepted keywords might \
split it roughly 15/15, or unevenly if one product has far more real variants than the \
other -- use judgement, but the combined total across every category must land in the \
25-30 range and cover every accepted keyword with at least a few terms each). This is a \
HARD REQUIREMENT: a short, safe list is a bad answer even if every term is correct. All \
three of the following categories are REQUIRED in every valid response -- never skip or \
shortchange one to pad another:
- "synonyms": 8 to 10 alternative names for the accepted products, as used in \
international trade, customs/HS-code descriptions, and product catalogues. Include \
general trade terms, more technical/industry-jargon terms, and looser everyday terms \
buyers actually type -- real, commonly used variants only, not invented ones. Draw from \
every accepted keyword, not just the first one.
- "scenarios": 8 to 10 application, use-case or buyer-segment phrases describing WHERE \
the accepted products are used, WHO buys them, or for WHAT project (e.g. for "LED \
display": "Stadium LED display", "Mall video wall", "Outdoor advertising screen", \
"Airport digital signage", "Retail storefront display", "Concert stage screen", \
"Control room video wall", "Church LED screen"). Vary the venue/industry across the \
full list -- do not produce near-duplicates of the same scenario, and cover every \
accepted keyword's own use cases, not only one product's.
- "translations": the accepted keyword(s) in AT LEAST 8 different languages combined, \
and it MUST include German (DE), Spanish (ES) and French (FR) every time -- never omit \
these three. Fill the rest from: Italian (IT), Portuguese (PT), Dutch (NL), Polish \
(PL), Turkish (TR), Arabic (AR), Russian (RU), Japanese (JA), Korean (KO), Vietnamese \
(VI), Indonesian (ID), Thai (TH). If an accepted keyword was not in English, add its \
English form (EN) first, before the rest. Use the natural term a native buyer in that \
market would actually search, not a literal word-for-word translation. With several \
accepted keywords, translate the main/first one unless the others need their own \
distinct translation to stay recognisable.
- "normalized_keyword": every accepted keyword in clean English (fix typos, translate \
if needed), joined with ", " if there is more than one.

Rules: every term must be short (2-5 words), specific, and different from the others. \
Never repeat an accepted keyword verbatim, and never repeat a synonym as a scenario or \
vice versa. No explanations inside terms. Title-case English terms like a product name \
("Digital signage"), keep other languages natural. Before answering, count each array: \
if "synonyms" or "scenarios" has fewer than 8 entries, or "translations" has fewer than \
8 entries or is missing DE, ES or FR, add more before replying. Never exceed 10 in any \
one category, and never let the combined total across all three pass 30.

TARGET BUYER. The user may tell you which kind of company they want to find, as a \
"Target company type" line after the keywords. When they do, bias every synonym and \
especially every scenario towards the words THAT kind of company uses when searching \
or describing itself, so the terms become Google queries that surface those companies \
rather than the general market. Keep the terms about the products and their buyers; do \
not simply append the company type to each term. When no target company type is \
given, cover the market broadly as usual.

Reply with a single JSON object and nothing else, in exactly this shape:
{
  "valid": true,
  "normalized_keyword": "LED screen, Digital signage",
  "reason": null,
  "suggestions": [],
  "rejected_keywords": [{"keyword": "asdf123", "reason": "..."}],
  "synonyms": ["...", "...", "... (8-10 total, combined across all accepted keywords)"],
  "scenarios": ["...", "...", "... (8-10 total, combined across all accepted keywords)"],
  "translations": [
    {"lang": "DE", "term": "LED-Anzeige"},
    {"lang": "ES", "term": "Pantalla LED"},
    {"lang": "FR", "term": "Ecran LED"},
    {"lang": "...", "term": "... (8+ total, DE/ES/FR always included)"}
  ]
}
When EVERY keyword is rejected, set "valid" to false, fill "reason" (the main reason, \
covering the rejected keywords) and "suggestions", and leave the other arrays empty."""


class _RejectedKeyword(BaseModel):
    keyword: str
    reason: str = ""


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
    rejected_keywords: list[_RejectedKeyword] = Field(default_factory=list)
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
        self, keywords: list[str], company_type: str | None = None
    ) -> ExpandKeywordResponse:
        """Expand one or more keyword tags together into a single combined
        list of up to 30 terms. Costs one flat charge for the whole batch,
        not one per keyword -- see the /expand route."""
        cleaned = [_clean(k) for k in keywords]
        cleaned = [k for k in cleaned if k]

        # Cheap local guard, applied per keyword: drop anything with not a
        # single letter or CJK character before it ever reaches the model.
        local_rejects = [k for k in cleaned if not re.search(r"[^\W\d_]", k)]
        usable = [k for k in cleaned if k not in local_rejects]

        if not usable:
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

        reply = await self._ask_model(usable, company_type)
        return self._to_response(usable, reply, pre_rejected=local_rejects)

    # --- DeepSeek call --------------------------------------------------------

    @staticmethod
    def _user_message(keywords: list[str], company_type: str | None) -> str:
        """The user turn: the keyword tags, plus who we are trying to reach.

        The company type is expanded into its description rather than sent as a
        bare code, so the model is told what that kind of buyer actually is.
        """
        lines = ["Keywords:"] + [f"- {k}" for k in keywords]

        hint = prompt_hint_for(company_type) if company_type else None
        if hint:
            name = name_for(company_type or "")
            lines.append(f"Target company type: {name} — {hint}")

        return "\n".join(lines)

    async def _ask_model(
        self, keywords: list[str], company_type: str | None = None
    ) -> _ModelReply:
        body = {
            "model": settings.DEEPSEEK_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": self._user_message(keywords, company_type),
                },
            ],
            # DeepSeek's JSON mode: guarantees parseable output as long as the
            # prompt mentions JSON (it does).
            "response_format": {"type": "json_object"},
            "temperature": 0.4,
            # The combined reply (up to 30 terms plus per-keyword rejection
            # reasons) needs real headroom -- a truncated JSON reply fails
            # parsing and surfaces as a 502 rather than a partial result.
            "max_tokens": 1600,
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
            log.warning("keyword_expansion.timeout", keywords=keywords)
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
    def _to_response(
        keywords: list[str],
        reply: _ModelReply,
        *,
        pre_rejected: list[str] | None = None,
    ) -> ExpandKeywordResponse:
        """`keywords` is what was actually sent to the model (the local guard
        in `expand()` already dropped anything with no letters at all --
        those come back in `pre_rejected` so they still show up to the user
        as rejected, same as one the model itself turned down)."""
        pre_rejected = pre_rejected or []

        # Keyed case-insensitively: the model may not echo a keyword's exact
        # casing back, and a mismatch here would silently un-reject it.
        model_rejected = {
            _clean(r.keyword).casefold(): _clean(r.reason)
            for r in reply.rejected_keywords
        }

        # The model can only judge what it was actually sent, so a keyword it
        # never mentions was implicitly accepted -- same rule the prompt uses.
        accepted = [k for k in keywords if k.casefold() not in model_rejected]

        # Display casing follows what the user actually typed (`keywords`),
        # not however the model happened to echo it back.
        all_rejected = dict.fromkeys(pre_rejected, "")
        all_rejected.update(
            {k: model_rejected[k.casefold()] for k in keywords if k not in accepted}
        )

        # Every keyword rejected, whether by the local guard or the model.
        if not reply.valid or not accepted:
            reason = _clean(reply.reason or "") or next(
                (r for r in all_rejected.values() if r), ""
            )
            return ExpandKeywordResponse(
                valid=False,
                normalized_keyword=None,
                reason=reason
                or "That doesn't look like a product or industry keyword.",
                suggestions=_dedupe(
                    reply.suggestions, MAX_SUGGESTIONS, exclude=set(keywords)
                ),
                rejected_keywords=sorted(all_rejected),
            )

        exclude = set(accepted)
        normalized = _clean(reply.normalized_keyword or "") or ", ".join(accepted)
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

        # DE, ES and FR are a hard requirement (see SYSTEM_PROMPT), so they go
        # first regardless of where the model placed them -- otherwise a
        # chatty reply could push one past MAX_TRANSLATIONS before it is seen.
        priority_langs = ("DE", "ES", "FR")
        ordered_translations = sorted(
            reply.translations,
            key=lambda tr: priority_langs.index(tr.lang.strip().upper()[:2])
            if tr.lang.strip().upper()[:2] in priority_langs
            else len(priority_langs),
        )

        for tr in ordered_translations:
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
            log.error("keyword_expansion.empty_result", keywords=accepted)
            raise UpstreamError("The AI service returned no usable terms. Try again.")

        # The prompt requires DE/ES/FR every time; not worth failing the
        # request over (English is still a fully usable expansion), but worth
        # knowing about if the model is drifting from the instruction.
        missing_required_langs = set(priority_langs) - seen_langs
        if missing_required_langs:
            log.warning(
                "keyword_expansion.missing_required_langs",
                keywords=accepted,
                missing=sorted(missing_required_langs),
            )

        return ExpandKeywordResponse(
            valid=True,
            normalized_keyword=normalized,
            terms=terms,
            # Keywords dropped even though at least one other was usable --
            # both the local guard's and the model's own rejections.
            rejected_keywords=sorted(all_rejected),
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
