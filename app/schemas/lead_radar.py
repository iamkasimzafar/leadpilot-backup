"""Lead Radar schemas: AI keyword expansion and search-run progress."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.schemas.common import BaseSchema
from app.services.company_types import is_valid as is_valid_company_type
from app.services.countries import is_valid
from app.services.search_targeting import is_valid_role, is_valid_size


def _normalise_contact_role(value: str | None) -> str | None:
    """Blank means "any role" (no Snov.io filter); an unknown code is refused."""
    if value is None:
        return None

    code = value.strip().lower()
    if not code or code == "any":
        return None

    if not is_valid_role(code):
        raise ValueError("Unknown contact role.")

    return code


def _normalise_company_size(value: str | None) -> str | None:
    """Blank means "any size"; an unknown band is refused."""
    if value is None:
        return None

    code = value.strip().lower()
    if not code or code == "any":
        return None

    if not is_valid_size(code):
        raise ValueError("Unknown company size.")

    return code


def _normalise_company_type(value: str | None) -> str | None:
    """Shared by the expand and search requests: blank means "any type", and an
    unknown code is refused rather than passed on to the model or the workflow."""
    if value is None:
        return None

    code = value.strip().lower()
    if not code:
        return None

    if not is_valid_company_type(code):
        raise ValueError("Unknown company type.")

    return code


TermType = Literal["synonym", "scenario", "lang"]


class ExpandKeywordRequest(BaseModel):
    keyword: str = Field(min_length=1, max_length=120)

    # Which kind of buyer to aim the expansion at. None expands broadly.
    company_type: str | None = Field(default=None, max_length=32)

    @field_validator("company_type")
    @classmethod
    def _known_company_type(cls, value: str | None) -> str | None:
        return _normalise_company_type(value)


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

    # SerpApi `gl` code (https://serpapi.com/google-countries), lower-case.
    # None means "worldwide": the workflow applies no country filter.
    country: str | None = Field(default=None, max_length=2)

    # Which kind of company to target. None means no company-type filter.
    company_type: str | None = Field(default=None, max_length=32)

    # Which decision-makers to extract. None extracts any role. Drives the
    # Snov.io extraction filter, so it also decides what the user is charged for.
    contact_role: str | None = Field(default=None, max_length=32)

    # Snov.io employee-count band. None applies no size filter.
    company_size: str | None = Field(default=None, max_length=16)

    # Opt-in: check each contact's phone number on WhatsApp. Off by default,
    # because it is charged per number checked.
    validate_whatsapp: bool = False

    @field_validator("company_type")
    @classmethod
    def _known_company_type(cls, value: str | None) -> str | None:
        return _normalise_company_type(value)

    @field_validator("contact_role")
    @classmethod
    def _known_contact_role(cls, value: str | None) -> str | None:
        return _normalise_contact_role(value)

    @field_validator("company_size")
    @classmethod
    def _known_company_size(cls, value: str | None) -> str | None:
        return _normalise_company_size(value)

    @field_validator("country")
    @classmethod
    def _known_country(cls, value: str | None) -> str | None:
        """Reject a code the search workflow could not use.

        Validated server-side as well as in the dropdown, so a hand-crafted
        request cannot push an arbitrary string into the n8n payload.
        """
        if value is None:
            return None

        code = value.strip().lower()
        if not code:
            return None

        if not is_valid(code):
            raise ValueError("Unknown country code.")

        return code


class StartSearchResponse(BaseModel):
    # True once n8n has accepted the payload.
    dispatched: bool
    original_keyword: str
    # Exactly what was forwarded, after de-duplication.
    expanded_keywords: list[str]
    # The run to watch for progress. The client subscribes with this id.
    run_id: str
    # Echoed back so the UI can confirm what the search was scoped to.
    # None when the search was worldwide.
    country: str | None = None
    country_name: str | None = None
    # None when the search targeted any company type.
    company_type: str | None = None
    company_type_name: str | None = None
    # None when any role / any size was accepted.
    contact_role: str | None = None
    contact_role_name: str | None = None
    company_size: str | None = None
    company_size_name: str | None = None
    # Whether WhatsApp validation was requested, and what each check costs.
    validate_whatsapp: bool = False
    whatsapp_credits_per_check: int | None = None


# --- Pricing -----------------------------------------------------------------
class QuoteRequest(BaseModel):
    """The inputs of StartSearchRequest that affect the price.

    The same shape, so the client sends what it is about to search and the
    server counts the keywords exactly as the start gate will -- the estimate
    on screen can then never disagree with the decision to allow the search.
    """

    original_keyword: str = Field(min_length=1, max_length=120)
    expanded_keywords: list[str] = Field(default_factory=list, max_length=40)
    validate_whatsapp: bool = False


class QuoteLine(BaseModel):
    """One line of the estimate: run_fee, companies or whatsapp."""

    key: str
    units: int
    unit_credits: int
    credits: int


class SearchQuote(BaseModel):
    """The pre-dispatch estimate the confirm step shows.

    Nothing is charged from this. The real bill is settled from what the
    workflow actually returns; this is what the start gate compares the
    balance against, so it is computed server-side rather than trusted from
    the client.
    """

    keyword_count: int
    validate_whatsapp: bool
    estimated_companies: int
    estimated_whatsapp_checks: int
    lines: list[QuoteLine]
    total: int
    balance: int
    # Whether the balance covers the estimate; `shortfall` is the gap if not.
    affordable: bool
    shortfall: int
    # Whether the estimate came from this account's own completed runs, and
    # how many. False means the catalogue defaults were used.
    based_on_history: bool
    runs_sampled: int
    # The rates, so the UI never hardcodes a price.
    run_fee_credits: int
    company_credits: int
    whatsapp_credits: int


# --- Progress ----------------------------------------------------------------
class SearchEventRead(BaseSchema):
    stage: str
    message: str | None = None
    count: int | None = None
    created_at: datetime


class SearchRunRead(BaseSchema):
    id: str
    original_keyword: str
    keyword_count: int
    auto_add_to_leads: bool
    # The country the run was scoped to; None for a worldwide search.
    country: str | None = None
    # The company type the run targeted; None when it targeted any.
    company_type: str | None = None
    # The extraction filters the run used; None when unfiltered.
    contact_role: str | None = None
    company_size: str | None = None
    # Whether the run asked for WhatsApp validation.
    validate_whatsapp: bool = False
    # Settlement, once the results arrived successfully. Zero and null until
    # then; nothing is charged for a failed run.
    credits_charged: int = 0
    credits_shortfall: int = 0
    whatsapp_checks: int = 0
    credits_charged_at: datetime | None = None
    status: str
    stage: str
    companies_found: int
    contacts_found: int
    error: str | None = None
    # Classified cause when the workflow failed (rate_limited, timeout, ...),
    # so the UI can show advice rather than n8n's raw text. None otherwise.
    error_reason: str | None = None
    # The node n8n was on when it failed, for the detail line.
    error_node: str | None = None
    created_at: datetime
    finished_at: datetime | None = None
    # Null until the workflow's results callback has been ingested. The UI
    # shows "collecting results" between finished_at and this.
    results_received_at: datetime | None = None
    # Every checkpoint reported so far, oldest first.
    events: list[SearchEventRead] = Field(default_factory=list)
    # The checkpoint order the UI renders, so the stage list is server-driven.
    stage_order: list[str] = Field(default_factory=list)


class SearchRunPage(BaseModel):
    """A page of the user's searches, for the Searches list."""

    items: list[SearchRunRead]
    total: int
    page: int
    per_page: int


class WorkflowErrorRequest(BaseModel):
    """What n8n's error trigger POSTs when the workflow fails.

    Every field is optional except the message: n8n's error payload is
    best-effort, and a failure must never be lost to a validation error.

    `run_id` and `progress_token` are NOT part of n8n's error trigger. Pass
    them through from the webhook data if you can -- they are what lets the
    failure be shown to the user whose search it was, instead of only being
    logged.
    """

    workflow_id: str | None = Field(default=None, max_length=64)
    execution_id: str | None = Field(default=None, max_length=64)
    error_message: str = Field(default="", max_length=5000)
    last_node_executed: str | None = Field(default=None, max_length=255)

    run_id: str | None = Field(default=None, max_length=36)
    progress_token: str | None = Field(default=None, max_length=64)

    @field_validator("error_message", mode="before")
    @classmethod
    def _readable(cls, value: Any) -> Any:
        """n8n sends the unresolved expression when a field is missing."""
        if value is None:
            return ""

        text = str(value).strip()

        # "{{ $json.execution.error.message }}" arrives verbatim when the
        # expression could not resolve; that is not an error message.
        if text.startswith("{{") and text.endswith("}}"):
            return ""

        return text


class WorkflowErrorResponse(BaseModel):
    recorded: bool
    # True when the failure was tied to a search run and its owner notified.
    attributed: bool
    reason: str = Field(description="Classified cause, e.g. rate_limited.")


class ProgressCallbackRequest(BaseModel):
    """What n8n POSTs at each checkpoint.

    Only `stage` is required. `status: "completed"` (or `stage: "completed"`)
    closes the run; `error` fails it.
    """

    stage: str = Field(min_length=1, max_length=32)
    message: str | None = Field(default=None, max_length=500)
    count: int | None = Field(default=None, ge=0)
    status: str | None = Field(default=None, max_length=16)
    error: str | None = Field(default=None, max_length=500)


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
