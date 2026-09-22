"""Lead Radar schemas: AI keyword expansion and search-run progress."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.common import BaseSchema
from app.services import local_business
from app.services.company_types import is_valid as is_valid_company_type
from app.services.countries import is_valid
from app.services.search_targeting import is_valid_role, is_valid_size

SearchTypeLiteral = Literal["b2b", "local"]

# More categories than this stops being a filter and becomes a second keyword
# list; it also keeps the `subtypes` query string a sane length.
MAX_BUSINESS_CATEGORIES = 10

# B2B search takes several keyword tags at once (see lead-radar.vue's tag
# input), all expanded together into one combined list of up to ~30 AI terms
# (keyword_expansion.py's MAX_SYNONYMS + MAX_SCENARIOS + MAX_TRANSLATIONS) --
# comfortable headroom over that for the terms actually selected and sent on.
MAX_EXPANDED_KEYWORDS = 60

# How many keyword tags a single search (and a single /expand call) can take.
MAX_KEYWORD_TAGS = 5

# `original_keyword` now carries every typed tag joined with ", " (the
# frontend caps at 5 tags), not a single keyword -- 120 was sized for one.
# Capped at the DB column's own limit (SearchRun.original_keyword is
# String(255) in app/models/lead_search.py) rather than past it.
MAX_ORIGINAL_KEYWORD_LENGTH = 255


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
    # One or more root keyword tags from the search box, expanded together
    # into a single combined list of terms (see KeywordExpansionService) --
    # not one AI call per tag, so the 25-30 term budget is shared across all
    # of them rather than each tag getting its own.
    keywords: list[str] = Field(min_length=1, max_length=MAX_KEYWORD_TAGS)

    # Which kind of buyer to aim the expansion at. None expands broadly.
    company_type: str | None = Field(default=None, max_length=32)

    @field_validator("keywords")
    @classmethod
    def _clean_keywords(cls, value: list[str]) -> list[str]:
        cleaned = [" ".join(k.split()) for k in value]
        cleaned = [k for k in cleaned if k]
        if not cleaned:
            raise ValueError("At least one keyword is required.")
        for k in cleaned:
            if len(k) > 120:
                raise ValueError("Each keyword must be 120 characters or fewer.")

        return cleaned

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
    original_keyword: str = Field(
        min_length=1, max_length=MAX_ORIGINAL_KEYWORD_LENGTH
    )
    # The terms the user ticked in step 2. The original keyword is prepended
    # server-side, so the client sends only the selected expansions.
    expanded_keywords: list[str] = Field(
        default_factory=list, max_length=MAX_EXPANDED_KEYWORDS
    )
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

    # --- Local Offline Business search ---------------------------------
    # "b2b" is the original company search and ignores everything below.
    # "local" searches Google Maps listings in one area, through its own n8n
    # workflow, and ignores the B2B-only filters (company type, contact role,
    # company size).
    search_type: SearchTypeLiteral = "b2b"

    # The area to search, as the user typed it: a city, a neighbourhood, a
    # postcode ("Brooklyn, New York"). Required for a local search.
    location: str | None = Field(default=None, max_length=255)

    # Google Business Profile categories to restrict the listings to. Empty is
    # valid and common: the keyword alone is then the whole query.
    business_categories: list[str] = Field(
        default_factory=list, max_length=MAX_BUSINESS_CATEGORIES
    )

    # Google Maps rating window (1.0-5.0) and review-count window. Each bound
    # is independent; None leaves that side open, all four None applies no
    # filter. The API cannot filter on these, so the workflow does.
    min_rating: float | None = Field(default=None, ge=1.0, le=5.0)
    max_rating: float | None = Field(default=None, ge=1.0, le=5.0)
    min_reviews: int | None = Field(default=None, ge=0, le=1_000_000)
    max_reviews: int | None = Field(default=None, ge=0, le=1_000_000)

    @field_validator("location")
    @classmethod
    def _tidy_location(cls, value: str | None) -> str | None:
        if value is None:
            return None

        return " ".join(value.split()) or None

    @field_validator("business_categories")
    @classmethod
    def _known_categories(cls, value: list[str]) -> list[str]:
        """Only Google's own category names: they are what the API's
        `subtypes` filter matches, and an invented one would silently return
        nothing. Returned in Google's spelling, duplicates dropped."""
        known: list[str] = []
        for raw in value:
            name = local_business.canonical(raw)
            if name is None:
                raise ValueError(f"Unknown business category: {raw!r}.")
            if name not in known:
                known.append(name)

        return known

    @model_validator(mode="after")
    def _coherent(self) -> "StartSearchRequest":
        if self.search_type == "local":
            if not self.location:
                raise ValueError("A local search needs a location.")

            # Not applicable to a Maps listing search; dropped rather than
            # refused so the client need not clear fields it is not showing.
            self.company_type = None
            self.contact_role = None
            self.company_size = None
        else:
            self.location = None
            self.business_categories = []
            self.min_rating = self.max_rating = None
            self.min_reviews = self.max_reviews = None

        if (
            self.min_rating is not None
            and self.max_rating is not None
            and self.min_rating > self.max_rating
        ):
            raise ValueError("min_rating cannot be above max_rating.")

        if (
            self.min_reviews is not None
            and self.max_reviews is not None
            and self.min_reviews > self.max_reviews
        ):
            raise ValueError("min_reviews cannot be above max_reviews.")

        return self

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
    # n8n's execution id, when the webhook's response carried one (a "Respond
    # to Webhook" node returning `{{ $execution.id }}`). Lets a later Error
    # Trigger report be tied to this run.
    n8n_execution_id: str | None = None
    # Which workflow took the job, and the local-search scope it was given.
    search_type: SearchTypeLiteral = "b2b"
    location: str | None = None
    business_categories: list[str] = Field(default_factory=list)
    min_rating: float | None = None
    max_rating: float | None = None
    min_reviews: int | None = None
    max_reviews: int | None = None


class CategoryList(BaseModel):
    """Google Business Profile categories, for the local-search dropdown."""

    items: list[str]
    # How many categories exist in all, so the UI can say what it is searching.
    total: int


# --- Pricing -----------------------------------------------------------------
class QuoteRequest(BaseModel):
    """The inputs of StartSearchRequest that affect the price.

    The same shape, so the client sends what it is about to search and the
    server counts the keywords exactly as the start gate will -- the estimate
    on screen can then never disagree with the decision to allow the search.
    """

    original_keyword: str = Field(
        min_length=1, max_length=MAX_ORIGINAL_KEYWORD_LENGTH
    )
    expanded_keywords: list[str] = Field(
        default_factory=list, max_length=MAX_EXPANDED_KEYWORDS
    )
    validate_whatsapp: bool = False


class QuoteLine(BaseModel):
    """One line of the estimate: contacts or whatsapp."""

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
    contact_credits: int
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
    # "b2b" or "local", and for a local run the area, Google categories and
    # rating / review window it was scoped to.
    search_type: str = "b2b"
    location: str | None = None
    business_categories: list[str] = Field(default_factory=list)
    min_rating: float | None = None
    max_rating: float | None = None
    min_reviews: int | None = None
    max_reviews: int | None = None
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
    # The n8n execution handling this run, once known. Shown for debugging: it
    # is the id to look up in n8n's executions list.
    n8n_execution_id: str | None = None
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


def _unresolved(value: Any) -> bool:
    """n8n sends the raw expression ("{{ $json.execution.id }}") when a field
    could not be resolved; that is not a value."""
    text = str(value).strip()

    return text.startswith("{{") and text.endswith("}}")


class WorkflowErrorRequest(BaseModel):
    """What n8n's Error Trigger workflow POSTs when the lead-search workflow
    fails. This is exactly the body its HTTP Request node sends:

        {
          "workflow_id": "{{ $json.workflow.id }}",
          "execution_id": "{{ $json.execution.id }}",
          "error_message": "{{ $json.execution.error.message }}",
          "last_node_executed": "{{ $json.execution.lastNodeExecuted }}"
        }

    Every field is optional: n8n's error payload is best-effort, and a failure
    must never be lost to a validation error.

    The failure is tied to a user's run by `execution_id` -- the run learns its
    execution id from the progress callbacks (send `execution_id` alongside
    `stage`) or from the webhook's response. `run_id` + `progress_token` are
    accepted too for a workflow that can pass them through.
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
        if value is None or _unresolved(value):
            return ""

        return str(value).strip()

    @field_validator(
        "workflow_id",
        "execution_id",
        "last_node_executed",
        "run_id",
        "progress_token",
        mode="before",
    )
    @classmethod
    def _optional(cls, value: Any) -> Any:
        """An unresolved expression is as good as absent. n8n also hands
        numeric ids over as numbers; they are compared as strings here."""
        if value is None or _unresolved(value):
            return None

        text = str(value).strip()

        return text or None


class WorkflowErrorResponse(BaseModel):
    recorded: bool
    # True when the failure was tied to a search run and its owner notified.
    attributed: bool
    # How the run was found: "run_token", "execution_id" or "sole_running_run".
    # None when it could not be attributed.
    attributed_by: str | None = None
    reason: str = Field(description="Classified cause, e.g. rate_limited.")


class ProgressCallbackRequest(BaseModel):
    """What n8n POSTs at each checkpoint.

    Only `stage` is required. `status: "completed"` (or `stage: "completed"`)
    closes the run; `error` fails it.

    `execution_id` (`{{ $execution.id }}` in n8n) is optional but worth
    sending on every checkpoint: it is what lets a later Error Trigger report,
    which only knows the execution id, be attributed to this run.
    """

    stage: str = Field(min_length=1, max_length=32)
    message: str | None = Field(default=None, max_length=500)
    count: int | None = Field(default=None, ge=0)
    status: str | None = Field(default=None, max_length=16)
    error: str | None = Field(default=None, max_length=500)
    execution_id: str | None = Field(default=None, max_length=64)

    @field_validator("execution_id", mode="before")
    @classmethod
    def _execution_id(cls, value: Any) -> Any:
        if value is None or _unresolved(value):
            return None

        text = str(value).strip()

        return text or None


class ExpandKeywordResponse(BaseModel):
    # False when NONE of the submitted keywords are usable product / industry
    # terms (gibberish, a sentence, a person's name, ...). True as long as at
    # least one of several keywords is usable -- the rest are just dropped
    # from the expansion (see `rejected_keywords`).
    valid: bool
    # Credits taken for this call: the AI expansion price on success, 0 when
    # every keyword was rejected (nothing was produced, so nothing is charged).
    # Flat per call, not per keyword -- expanding several tags together costs
    # the same as expanding one.
    credits_charged: int = 0
    # The wallet balance after the charge, so the UI can update without a
    # second round trip. None when nothing was charged.
    balance_after: int | None = None
    # What the model understood the keyword(s) to mean, in English, joined
    # with ", " when there were several. Lets the UI show the interpretation
    # for non-English or ambiguous input.
    normalized_keyword: str | None = None
    # Human-readable explanation when valid is False (every keyword rejected).
    reason: str | None = None
    # Suggested real keywords the user may have meant when valid is False.
    suggestions: list[str] = Field(default_factory=list)
    # Keywords that were dropped as not real product/industry terms while at
    # least one other keyword in the same request was usable -- so the UI can
    # tell the user a tag was silently excluded rather than just losing it.
    rejected_keywords: list[str] = Field(default_factory=list)
    terms: list[ExpandedTerm] = Field(default_factory=list)
