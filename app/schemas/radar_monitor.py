"""Radar Monitor schemas: CRUD from the UI, plus the n8n scheduler contract."""

import re
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.common import BaseSchema
from app.schemas.lead import CompanyIn
from app.services.countries import is_valid as is_valid_country
from app.services.search_targeting import is_valid_role, is_valid_size

MonitorSearchTypeLiteral = Literal["keyword", "hs_code"]
MonitorFrequencyLiteral = Literal["daily", "weekly"]
MonitorStatusLiteral = Literal["running", "paused"]

# Capped so one run cannot drain the balance. Mirrors the choices the modal
# offers; anything else is refused rather than silently clamped.
ALLOWED_LIMITS = (20, 50, 100)

# 6-digit HS heading, the form the AI translator confirms.
HS_CODE_RE = re.compile(r"^\d{6}$")

MAX_NAME_LENGTH = 255
MAX_SEARCH_VALUE_LENGTH = 500


def _normalise_country(value: str | None) -> str | None:
    """Blank or "all" means worldwide; an unknown code is refused."""
    if value is None:
        return None

    code = value.strip().lower()
    if not code or code == "all":
        return None

    if not is_valid_country(code):
        raise ValueError("Unknown country.")

    return code


def _normalise_size(value: str | None) -> str | None:
    if value is None:
        return None

    code = value.strip().lower()
    if not code or code == "any":
        return None

    if not is_valid_size(code):
        raise ValueError("Unknown company size.")

    return code


def _normalise_role(value: str | None) -> str | None:
    if value is None:
        return None

    code = value.strip().lower()
    if not code or code == "any":
        return None

    if not is_valid_role(code):
        raise ValueError("Unknown contact role.")

    return code


class MonitorFilters(BaseModel):
    """The Step 2 filters, stored as filters_json and replayed on every run."""

    country: str | None = None
    company_size: str | None = None
    contact_role: str | None = None

    _country = field_validator("country")(_normalise_country)
    _size = field_validator("company_size")(_normalise_size)
    _role = field_validator("contact_role")(_normalise_role)


class MonitorCreateRequest(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=MAX_NAME_LENGTH)]
    search_type: MonitorSearchTypeLiteral
    search_value: Annotated[str, Field(min_length=1, max_length=MAX_SEARCH_VALUE_LENGTH)]

    # For an HS-code monitor, the product wording that came back with the
    # code. The pipeline searches this, not the digits.
    search_label: str | None = Field(default=None, max_length=MAX_SEARCH_VALUE_LENGTH)

    filters: MonitorFilters = Field(default_factory=MonitorFilters)
    frequency: MonitorFrequencyLiteral = "daily"
    limit_per_run: int = 50

    @field_validator("name", "search_value")
    @classmethod
    def _strip(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Must not be blank.")

        return cleaned

    @field_validator("limit_per_run")
    @classmethod
    def _capped(cls, value: int) -> int:
        if value not in ALLOWED_LIMITS:
            allowed = ", ".join(str(item) for item in ALLOWED_LIMITS)
            raise ValueError(f"Limit must be one of: {allowed}.")

        return value

    @field_validator("search_value")
    @classmethod
    def _hs_code_shape(cls, value: str, info: Any) -> str:
        """An HS-code monitor stores the confirmed 6-digit heading."""
        if info.data.get("search_type") == "hs_code" and not HS_CODE_RE.match(value):
            raise ValueError("HS code must be 6 digits.")

        return value


class MonitorUpdateRequest(BaseModel):
    """Every field optional: the UI sends only what changed.

    Changing what the monitor searches is allowed. It resets `serper_offset`
    (see RadarMonitorService.update), because the offset counts pages through
    one particular query -- carried over, it would start the new search on
    page 3 and silently skip its first results.
    """

    name: str | None = Field(default=None, min_length=1, max_length=MAX_NAME_LENGTH)

    search_type: MonitorSearchTypeLiteral | None = None
    search_value: str | None = Field(
        default=None, min_length=1, max_length=MAX_SEARCH_VALUE_LENGTH
    )
    search_label: str | None = Field(default=None, max_length=MAX_SEARCH_VALUE_LENGTH)

    filters: MonitorFilters | None = None
    frequency: MonitorFrequencyLiteral | None = None
    limit_per_run: int | None = None
    status: MonitorStatusLiteral | None = None

    @field_validator("name", "search_value")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        if value is None:
            return None

        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Must not be blank.")

        return cleaned

    @field_validator("limit_per_run")
    @classmethod
    def _capped(cls, value: int | None) -> int | None:
        if value is not None and value not in ALLOWED_LIMITS:
            allowed = ", ".join(str(item) for item in ALLOWED_LIMITS)
            raise ValueError(f"Limit must be one of: {allowed}.")

        return value

    @model_validator(mode="after")
    def _hs_code_shape(self) -> "MonitorUpdateRequest":
        """An HS-code monitor stores the confirmed 6-digit heading.

        Checked here rather than per-field because the type and the value
        arrive together, and either one alone cannot tell whether the pair is
        valid.
        """
        if (
            self.search_type == "hs_code"
            and self.search_value is not None
            and not HS_CODE_RE.match(self.search_value)
        ):
            raise ValueError("HS code must be 6 digits.")

        return self


class MonitorOut(BaseSchema):
    """One monitor, as the Radar Monitors list renders it."""

    id: str
    name: str
    search_type: str
    search_value: str
    search_label: str | None
    frequency: str
    limit_per_run: int
    status: str
    total_leads_generated: int
    running_days: int
    serper_offset: int
    last_run_at: datetime | None
    last_completed_at: datetime | None
    next_run_at: datetime | None
    last_error: str | None
    created_at: datetime

    # Flattened from filters_json so the UI does not have to parse it.
    filters: MonitorFilters


class MonitorListResponse(BaseModel):
    items: list[MonitorOut]
    total: int


# --- The n8n contract -------------------------------------------------------


class DueMonitor(BaseModel):
    """One monitor for the scheduler workflow to run.

    Everything n8n needs for a run, so the workflow never touches the
    database: what to search, how to filter it, where Serper should start,
    and where to post the results back with which token.
    """

    monitor_id: str

    # The run opened for this dispatch. The workflow quotes it back so the
    # results land on the right row in Your Searches.
    run_id: str

    user_id: str
    search_type: str

    # What the pipeline actually searches: the keyword text, or the product
    # wording behind the HS code.
    search_term: str

    # The same value under the name the shared pipeline nodes already read
    # (Backlisting tags each result with it; DeepSeek's prompt quotes it).
    # Keeping their field name means those nodes run unmodified.
    original_keyword: str

    # The confirmed HS code, for labelling. None for keyword monitors.
    hs_code: str | None

    country: str | None
    company_size: str | None
    contact_role: str | None

    # The Snov.io job titles behind `contact_role`, and the size bounds behind
    # `company_size`. The pipeline filters on these directly, so resolving
    # them here keeps the workflow free of the catalogue.
    contact_role_titles: list[str] = Field(default_factory=list)
    company_size_min: int | None = None
    company_size_max: int | None = None

    # Monitors never buy the WhatsApp premium: it is an opt-in a background
    # run cannot ask about, and it would quietly raise the per-lead price.
    validate_whatsapp: bool = False

    limit_per_run: int

    # Where this run's Serper request starts, so it digs past what previous
    # runs already returned.
    serper_offset: int

    # The `page` to send Serper, given the workflow asks for `limit_per_run`
    # results per page (`num`). Serper pages are `num` wide, so page 2 with
    # num=50 is results 51-100. Computed here so the offset arithmetic lives
    # in one place.
    serper_page: int

    results_url: str
    progress_token: str


class DueMonitorsResponse(BaseModel):
    monitors: list[DueMonitor]
    total: int


class MonitorResultsRequest(BaseModel):
    """What the monitor workflow posts back when a run finishes."""

    monitor_id: str

    # The run this dispatch opened. Optional: the backend falls back to the
    # monitor's own open run, so a workflow that drops it still closes the
    # right row.
    run_id: str | None = None

    status: Literal["completed", "failed"] = "completed"

    # Same company shape the manual search returns, so the ingest reuses it.
    companies: list[CompanyIn] = Field(default_factory=list)

    # Set when status is "failed".
    error: str | None = None


class MonitorResultsResponse(BaseModel):
    monitor_id: str

    companies_received: int
    companies_saved: int

    # Contacts that were genuinely new for this user.
    contacts_added: int

    # Contacts discarded because the user already had that email. Never
    # billed.
    duplicates_skipped: int

    credits_charged: int
    credits_shortfall: int

    total_leads_generated: int
    serper_offset: int
