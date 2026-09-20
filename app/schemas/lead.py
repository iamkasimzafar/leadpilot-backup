"""Lead result schemas: what n8n posts, and what we hand back."""

import re
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


def _coerce_to_text(value: Any) -> Any:
    """Accept the non-string values the workflow's JavaScript can produce.

    n8n builds these payloads in JS, where a field that looks textual may arrive
    as a boolean (`whatsapp_status: true`) or a number (a phone number without
    quotes). Rejecting those would fail the whole results POST -- every company
    in the batch -- over one contact, so they are converted instead:

      True / False  ->  "Active" / "Not Active", matching what the workflow
                        sends when it reports the status as a label.
      int / float   ->  the digits as written, so a phone number survives.

    Anything else is handed on unchanged for the normal validators to judge.
    """
    if isinstance(value, bool):
        return "Active" if value else "Not Active"

    if isinstance(value, int | float):
        # int() first so 4930123456789.0 does not become "4930123456789.0".
        return str(int(value)) if float(value).is_integer() else str(value)

    return value


WHATSAPP_ACTIVE = "Active"
WHATSAPP_NOT_ACTIVE = "Not Active"

# What the workflows and their validator APIs actually send, lower-cased.
_WHATSAPP_POSITIVE = {
    "active", "valid", "exists", "exist", "true", "yes", "registered", "on whatsapp",
}  # fmt: skip
_WHATSAPP_NEGATIVE = {
    "not active", "inactive", "invalid", "not found", "false", "no", "not registered",
    "unregistered", "unavailable", "not available", "not on whatsapp",
}  # fmt: skip

# Phrases unambiguous enough to decide a longer label by its opening words.
# Deliberately without "no" / "yes" / "true": "no response from the API" is
# not a negative result.
_WHATSAPP_NEGATIVE_LEADS = (
    "not active", "inactive", "invalid", "not found", "not registered", "not on whatsapp",
)  # fmt: skip
_WHATSAPP_POSITIVE_LEADS = ("active", "valid", "exists", "registered", "on whatsapp")


def normalise_whatsapp_status(value: Any) -> str | None:
    """One vocabulary for a WhatsApp check, whatever the workflow called it.

    Three consumers read this column and each used to have its own idea of the
    words: billing charges a check for every non-null value, the reports count
    the positives, and the UI shows a green, a dimmed or no icon. So the value
    is pinned here, where results enter:

      "Active"      the number is on WhatsApp   ("valid", true, "exists", ...)
      "Not Active"  checked, and it is not      ("invalid", false, ...)
      None          no check was made

    None is also the answer for a label that is neither -- "Not Checked", an
    API error text, anything unrecognised. Claiming a number is reachable, or
    charging for a check, on the strength of a word we do not know would both
    be wrong; saying nothing is not.
    """
    value = _coerce_to_text(value)
    if not isinstance(value, str):
        return None

    label = " ".join(value.split()).casefold()
    if label in _WHATSAPP_POSITIVE:
        return WHATSAPP_ACTIVE
    if label in _WHATSAPP_NEGATIVE:
        return WHATSAPP_NOT_ACTIVE

    # A descriptive label ("Active on WhatsApp Business, verified name") is
    # read by its leading phrase. Negatives first, so "not active ..." is never
    # taken for "active ..."; and only whole words, so "validation failed" is
    # not "valid".
    for phrases, verdict in (
        (_WHATSAPP_NEGATIVE_LEADS, WHATSAPP_NOT_ACTIVE),
        (_WHATSAPP_POSITIVE_LEADS, WHATSAPP_ACTIVE),
    ):
        for phrase in phrases:
            if (
                len(label) > len(phrase)
                and label.startswith(phrase)
                and not label[len(phrase)].isalnum()
            ):
                return verdict

    return None


# Column widths, so a value too long for the database is trimmed here rather
# than failing the whole batch. The untrimmed original is kept in extra_json.
_CONTACT_LIMITS = {
    "full_name": 255,
    "job_title": 255,
    "verified_email": 320,
    "email_status": 32,
    "linkedin_url": 500,
    "phone_number": 64,
    "whatsapp_status": 32,
}


class DecisionMakerIn(BaseModel):
    """One decision maker in the workflow's payload.

    Mirrors what the n8n workflow pushes per contact: full_name, job_title,
    verified_email, email_status, linkedin_url, phone_number, whatsapp_status.

    Nothing from the payload is discarded. Unknown keys are kept (extra="allow")
    and stored in extra_json; over-long values are trimmed to fit their column
    with the full original preserved in extra_json under "<field>_full"; and
    values the workflow's JavaScript emits as booleans or numbers are coerced
    rather than rejected. This matters because the results POST is validated as
    one document: without it, a single odd contact would lose every company in
    the batch.
    """

    model_config = ConfigDict(extra="allow")

    full_name: str = Field(max_length=255)
    job_title: str | None = Field(default=None, max_length=255)
    verified_email: str | None = Field(default=None, max_length=320)
    email_status: str | None = Field(default=None, max_length=32)
    linkedin_url: str | None = Field(default=None, max_length=500)
    phone_number: str | None = Field(default=None, max_length=64)
    whatsapp_status: str | None = Field(default=None, max_length=32)

    @model_validator(mode="before")
    @classmethod
    def _fit_to_columns(cls, data: Any) -> Any:
        """Coerce JS types and trim over-long values, keeping the originals."""
        if not isinstance(data, dict):
            return data

        fitted = dict(data)
        for field, limit in _CONTACT_LIMITS.items():
            value = _coerce_to_text(fitted.get(field))
            if not isinstance(value, str):
                fitted[field] = value
                continue

            cleaned = " ".join(value.split())
            if len(cleaned) > limit:
                # Truncating silently would lose the tail of a long LinkedIn URL
                # or job title, so the whole value is kept alongside it.
                fitted[f"{field}_full"] = cleaned
                cleaned = cleaned[:limit]

            fitted[field] = cleaned

        return fitted

    @field_validator(
        "job_title",
        "verified_email",
        "email_status",
        "linkedin_url",
        "phone_number",
        mode="before",
    )
    @classmethod
    def _clean(cls, value: Any) -> Any:
        return _blank_to_none(value)

    @field_validator("whatsapp_status", mode="before")
    @classmethod
    def _whatsapp(cls, value: Any) -> Any:
        return normalise_whatsapp_status(value)

    @field_validator("full_name", mode="before")
    @classmethod
    def _clean_name(cls, value: Any) -> Any:
        return " ".join(str(value).split()) if value is not None else value


_COMPANY_LIMITS = {
    "company_name": 255,
    "website": 500,
    "location": 255,
    "industry": 255,
    "company_size": 64,
    "hq_phone": 64,
}


class CompanyIn(BaseModel):
    """One company in the workflow's payload.

    Same contract as DecisionMakerIn: unknown keys are preserved, over-long
    values are trimmed with the original kept, and JS booleans / numbers are
    coerced rather than failing the batch.
    """

    model_config = ConfigDict(extra="allow")

    company_name: str = Field(max_length=255)
    website: str | None = Field(default=None, max_length=500)
    location: str | None = Field(default=None, max_length=255)
    industry: str | None = Field(default=None, max_length=255)
    company_size: str | None = Field(default=None, max_length=64)
    hq_phone: str | None = Field(default=None, max_length=64)
    decision_makers: list[DecisionMakerIn] = Field(default_factory=list, max_length=500)

    @model_validator(mode="before")
    @classmethod
    def _fit_to_columns(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        fitted = dict(data)
        for field, limit in _COMPANY_LIMITS.items():
            value = _coerce_to_text(fitted.get(field))
            if not isinstance(value, str):
                fitted[field] = value
                continue

            cleaned = " ".join(value.split())
            if len(cleaned) > limit:
                fitted[f"{field}_full"] = cleaned
                cleaned = cleaned[:limit]

            fitted[field] = cleaned

        return fitted

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
    """The final payload posted by the workflow when it finishes.

    Two shapes arrive on this endpoint and both must work:

      success   {"run_id": ..., "status": "completed", "companies": [...]}
      failure   {"status": "failed", "reason": "no_valid_emails_found",
                 "message": "Zero valid emails were found ..."}

    `run_id` is read from the body when present, otherwise from the
    `X-LeadPilot-Run-Id` header (see the route), so the failure branch of the
    workflow does not have to rebuild the success body. `reason` is a short
    machine code the workflow chooses; it is stored as-is so failures can be
    counted by cause. `message` and `error` are the same thing under two
    names -- the human-readable text.
    """

    run_id: str | None = Field(default=None, min_length=1, max_length=36)
    status: str = Field(default="completed", max_length=16)
    total_companies: int | None = Field(default=None, ge=0)
    companies: list[CompanyIn] = Field(default_factory=list, max_length=1000)
    error: str | None = Field(default=None, max_length=500)
    message: str | None = Field(default=None, max_length=500)
    reason: str | None = Field(default=None, max_length=64)

    @field_validator("companies", mode="before")
    @classmethod
    def _tolerate_missing_list(cls, value: Any) -> Any:
        return value if value is not None else []

    @field_validator("status", mode="before")
    @classmethod
    def _lower_status(cls, value: Any) -> Any:
        """ "FAILED", "Failed" and "failed" all mean the same thing."""
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("reason", mode="before")
    @classmethod
    def _code_reason(cls, value: Any) -> Any:
        """Normalise to a snake_case code: "No Valid Emails Found" becomes
        no_valid_emails_found, so one cause is always spelled one way."""
        if not isinstance(value, str):
            return value

        code = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")

        return code or None

    @field_validator("error", "message", mode="before")
    @classmethod
    def _clean_text(cls, value: Any) -> Any:
        return _blank_to_none(value)

    @property
    def is_failed(self) -> bool:
        """A failure is anything that says so: the status, an error text, or
        a reason code -- a reason is only ever sent for a failure."""
        return (
            self.status == "failed" or self.error is not None or self.reason is not None
        )

    @property
    def error_text(self) -> str | None:
        """The human-readable failure text, whichever key it came under."""
        return self.error or self.message


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
        mode="before",
    )
    @classmethod
    def _clean(cls, value: Any) -> Any:
        return _blank_to_none(value)

    @field_validator("whatsapp_status", mode="before")
    @classmethod
    def _whatsapp(cls, value: Any) -> Any:
        return normalise_whatsapp_status(value)


class BulkStatusRequest(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=1000)
    status: LeadStatusValue


class BulkIdsRequest(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=1000)


class BulkResult(BaseModel):
    affected: int
