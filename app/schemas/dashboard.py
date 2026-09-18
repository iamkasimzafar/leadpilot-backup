"""Dashboard summary."""

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import BaseSchema


class DashboardRun(BaseSchema):
    """A recent search, as listed on the dashboard."""

    id: str
    original_keyword: str
    status: str
    stage: str
    companies_found: int
    contacts_found: int
    auto_add_to_leads: bool
    created_at: datetime
    finished_at: datetime | None = None
    results_received_at: datetime | None = None


class DashboardSummary(BaseModel):
    """Every number on the dashboard, in one round trip. All counts are real
    rows for the signed-in user; "today" is the user's local day, derived from
    the timezone offset the client sends."""

    new_leads_today: int = Field(description="Companies added to My Leads today.")
    awaiting_contact: int = Field(description='Leads still in status "new".')
    searches_today: int = Field(description="Lead searches started today.")
    contacts_found_today: int = Field(description="Decision makers discovered today.")
    credits_left: int
    leads_total: int
    # The UTC instant the user's "today" began -- lets the client show the
    # window the counts cover.
    day_start: datetime
    recent_runs: list[DashboardRun] = Field(default_factory=list)
