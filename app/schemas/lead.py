"""Lead result schemas: what n8n posts, and what we hand back."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.common import BaseSchema

# n8n frequently emits "N/A", "-" or "" for a field it could not fill. Storing
# those as real values would put "N/A" in the UI, so they become NULL.
_EMPTY_VALUES = {"", "n/a", "na", "none", "null", "-", "--", "unknown"}


def _blank_to_none(value: Any) -> Any:
    if isinstance(value, str):
        cleaned = " ".join(value.split())
        if cleaned.casefold() in _EMPTY_VALUES:
            return None

        return cleaned

    return value


class DecisionMakerIn(BaseModel):
    """One decision maker in the workflow's payload.

    Unknown keys are kept (model_config below) and preserved in extra_json, so
    adding a field in n8n never loses data or breaks this endpoint.
    """

    model_config = ConfigDict(extra="allow")

    full_name: str = Field(max_length=255)
    job_title: str | None = Field(default=None, max_length=255)
    verified_email: str | None = Field(default=None, max_length=320)
    email_status: str | None = Field(default=None, max_length=32)
    linkedin_url: str | None = Field(default=None, max_length=500)
    phone_number: str | None = Field(default=None, max_length=64)
    whatsapp_status: str | None = Field(default=None, max_length=32)

    @field_validator(
        "job_title",
        "verified_email",
        "email_status",
        "linkedin_url",
        "phone_number",
        "whatsapp_status",
        mode="before",
    )
    @classmethod
    def _clean(cls, value: Any) -> Any:
        return _blank_to_none(value)

    @field_validator("full_name", mode="before")
    @classmethod
    def _clean_name(cls, value: Any) -> Any:
        return " ".join(str(value).split()) if value is not None else value


class CompanyIn(BaseModel):
    """One company in the workflow's payload."""

    model_config = ConfigDict(extra="allow")

    company_name: str = Field(max_length=255)
    website: str | None = Field(default=None, max_length=500)
    location: str | None = Field(default=None, max_length=255)
    industry: str | None = Field(default=None, max_length=255)
    company_size: str | None = Field(default=None, max_length=64)
    hq_phone: str | None = Field(default=None, max_length=64)
    decision_makers: list[DecisionMakerIn] = Field(default_factory=list, max_length=500)

    @field_validator(
        "website", "location", "industry", "company_size", "hq_phone", mode="before"
    )
    @classmethod
    def _clean(cls, value: Any) -> Any:
        return _blank_to_none(value)

    @field_validator("company_name", mode="before")
    @classmethod
    def _clean_name(cls, value: Any) -> Any:
        return " ".join(str(value).split()) if value is not None else value

    @field_validator("decision_makers", mode="before")
    @classmethod
    def _tolerate_missing_list(cls, value: Any) -> Any:
        # n8n may send null rather than [] when a company had no contacts.
        return value if value is not None else []


class SearchResultsRequest(BaseModel):
    """The full result payload posted by the workflow when it finishes.

    `run_id` lives in the body (not the URL) to match the shape the n8n
    workflow already builds.
    """

    run_id: str = Field(min_length=1, max_length=36)
    status: str = Field(default="completed", max_length=16)
    total_companies: int | None = Field(default=None, ge=0)
    companies: list[CompanyIn] = Field(default_factory=list, max_length=1000)
    error: str | None = Field(default=None, max_length=500)

    @field_validator("companies", mode="before")
    @classmethod
    def _tolerate_missing_list(cls, value: Any) -> Any:
        return value if value is not None else []


class SearchResultsResponse(BaseModel):
    """What the endpoint answers, so the workflow can log what landed."""

    run_id: str
    status: str
    companies_saved: int = Field(description="Companies created by this call.")
    companies_updated: int = Field(
        description="Companies already present and refreshed instead of duplicated."
    )
    contacts_saved: int
    total_companies: int = Field(description="Companies now attached to this run.")


# --- Reading results back ----------------------------------------------------
LeadStatusValue = Literal["new", "contacted", "interested"]


class DecisionMakerRead(BaseSchema):
    id: str
    full_name: str
    job_title: str | None = None
    verified_email: str | None = None
    email_status: str | None = None
    linkedin_url: str | None = None
    phone_number: str | None = None
    whatsapp_status: str | None = None


class CompanyRead(BaseSchema):
    id: str
    company_name: str
    website: str | None = None
    location: str | None = None
    industry: str | None = None
    company_size: str | None = None
    hq_phone: str | None = None
    created_at: datetime
    # Lead tracking
    in_leads: bool
    added_to_leads_at: datetime | None = None
    status: str
    notes: str | None = None
    decision_makers: list[DecisionMakerRead] = Field(default_factory=list)


# --- My Leads ----------------------------------------------------------------
class LeadCounts(BaseModel):
    all: int
    new: int
    contacted: int
    interested: int


class LeadPage(BaseModel):
    """A page of leads plus the tab counts, so the tabs stay right while
    looking at a filtered list."""

    items: list[CompanyRead]
    total: int
    page: int
    per_page: int
    counts: LeadCounts


class AddToLeadsRequest(BaseModel):
    """Add search results to My Leads: specific companies, or every company a
    run produced. At least one of the two must be given."""

    company_ids: list[str] = Field(default_factory=list, max_length=1000)
    run_id: str | None = Field(default=None, max_length=36)

    @model_validator(mode="after")
    def _one_source(self) -> "AddToLeadsRequest":
        if not self.company_ids and not self.run_id:
            raise ValueError("Provide company_ids or run_id.")

        return self


class AddToLeadsResponse(BaseModel):
    added: int = Field(description="Companies newly added by this call.")
    already_in_leads: int = Field(description="Companies that were already there.")
    total_in_leads: int = Field(description="The user's lead count afterwards.")


class LeadUpdate(BaseModel):
    """Partial update: only the fields sent are changed."""

    company_name: str | None = Field(default=None, min_length=1, max_length=255)
    website: str | None = Field(default=None, max_length=500)
    location: str | None = Field(default=None, max_length=255)
    industry: str | None = Field(default=None, max_length=255)
    company_size: str | None = Field(default=None, max_length=64)
    hq_phone: str | None = Field(default=None, max_length=64)
    status: LeadStatusValue | None = None
    notes: str | None = Field(default=None, max_length=5000)

    @field_validator(
        "website", "location", "industry", "company_size", "hq_phone", mode="before"
    )
    @classmethod
    def _clean(cls, value: Any) -> Any:
        return _blank_to_none(value)


class DecisionMakerUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=255)
    job_title: str | None = Field(default=None, max_length=255)
    verified_email: str | None = Field(default=None, max_length=320)
    email_status: str | None = Field(default=None, max_length=32)
    linkedin_url: str | None = Field(default=None, max_length=500)
    phone_number: str | None = Field(default=None, max_length=64)
    whatsapp_status: str | None = Field(default=None, max_length=32)

    @field_validator(
        "job_title",
        "verified_email",
        "email_status",
        "linkedin_url",
        "phone_number",
        "whatsapp_status",
        mode="before",
    )
    @classmethod
    def _clean(cls, value: Any) -> Any:
        return _blank_to_none(value)


class BulkStatusRequest(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=1000)
    status: LeadStatusValue


class BulkIdsRequest(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=1000)


class BulkResult(BaseModel):
    affected: int
