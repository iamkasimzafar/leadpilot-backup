"""Lead Radar schemas: AI keyword expansion."""

from typing import Literal

from pydantic import BaseModel, Field

TermType = Literal["synonym", "scenario", "lang"]


class ExpandKeywordRequest(BaseModel):
    keyword: str = Field(min_length=1, max_length=120)


class ExpandedTerm(BaseModel):
    term: str
    type: TermType
    # ISO 639-1 code, upper-cased, only for `lang` terms (ES, DE, FR, ...).
    lang: str | None = None
    # Whether the term should start checked in the UI. Synonyms and scenarios
    # are preselected; translations are opt-in.
    selected: bool


class StartSearchRequest(BaseModel):
    original_keyword: str = Field(min_length=1, max_length=120)
    # The terms the user ticked in step 2. The original keyword is prepended
    # server-side, so the client sends only the selected expansions.
    expanded_keywords: list[str] = Field(default_factory=list, max_length=40)
    auto_add_to_leads: bool = True


class StartSearchResponse(BaseModel):
    # True once n8n has accepted the payload.
    dispatched: bool
    original_keyword: str
    # Exactly what was forwarded, after de-duplication.
    expanded_keywords: list[str]


class ExpandKeywordResponse(BaseModel):
    # False when the input is not a usable product / industry keyword
    # (gibberish, a sentence, a person's name, ...).
    valid: bool
    # Credits taken for this call: the AI expansion price on success, 0 when the
    # keyword was rejected (nothing was produced, so nothing is charged).
    credits_charged: int = 0
    # The wallet balance after the charge, so the UI can update without a
    # second round trip. None when nothing was charged.
    balance_after: int | None = None
    # What the model understood the keyword to mean, in English. Lets the UI
    # show the interpretation for non-English or ambiguous input.
    normalized_keyword: str | None = None
    # Human-readable explanation when valid is False.
    reason: str | None = None
    # Suggested real keywords the user may have meant when valid is False.
    suggestions: list[str] = Field(default_factory=list)
    terms: list[ExpandedTerm] = Field(default_factory=list)
