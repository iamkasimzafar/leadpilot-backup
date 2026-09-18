"""Reports: the analytics the Reports page draws.

Every figure here is a real count over the signed-in user's own rows, on the
same principle as the dashboard: a number that cannot be derived from the
database does not appear.

Note what is deliberately absent. The page used to chart "emails sent" and
"reply rate"; LeadPilot does not send email yet, so there is nothing truthful
to put there and those charts are gone rather than filled with a guess.
"""

from datetime import datetime

from pydantic import BaseModel, Field


class TrendPoint(BaseModel):
    """One day on a trend line.

    `date` is the user's local calendar day (YYYY-MM-DD), not a UTC timestamp,
    because that is the day the user thinks in.
    """

    date: str
    leads: int = Field(description="Companies added to My Leads that day.")
    contacts: int = Field(description="Decision makers discovered that day.")
    searches: int = Field(description="Searches started that day.")
    credits_spent: int = Field(
        description="Credits spent that day, as a positive number."
    )


class FunnelStage(BaseModel):
    """One bar of the lead funnel, widest first."""

    key: str
    count: int


class BreakdownSlice(BaseModel):
    """One row of a "top N" breakdown, e.g. a country or a keyword."""

    label: str
    count: int


class ReportTotals(BaseModel):
    """The headline tiles, over the selected window."""

    leads_added: int = Field(description="Companies added to My Leads in the window.")
    contacts_found: int = Field(description="Decision makers discovered in the window.")
    searches_run: int = Field(description="Searches started in the window.")
    credits_spent: int = Field(description="Credits spent in the window, positive.")

    # Quality of what was found, over the same window.
    contacts_with_email: int = Field(description="Contacts with a verified email.")
    contacts_with_phone: int = Field(description="Contacts with a phone number.")
    contacts_on_whatsapp: int = Field(description="Contacts whose number is on WhatsApp.")

    # Derived, but returned rather than computed client-side so the page and
    # any export agree on the arithmetic.
    email_rate: float = Field(
        description="contacts_with_email / contacts_found, 0-100, one decimal."
    )
    contacts_per_search: float = Field(
        description="contacts_found / searches_run, one decimal."
    )
    credits_per_lead: float = Field(
        description="credits_spent / leads_added, one decimal. 0 when no leads."
    )


class SearchOutcomes(BaseModel):
    """How the window's searches ended. Failed searches cost nothing, so this
    is about reliability, not spend."""

    completed: int
    failed: int
    running: int
    success_rate: float = Field(description="completed / finished searches, 0-100.")


class ReportSummary(BaseModel):
    """Everything the Reports page draws, in one round trip."""

    # The window the figures cover, as local calendar days.
    start_date: str
    end_date: str
    days: int

    totals: ReportTotals
    outcomes: SearchOutcomes

    # One point per day across the window, including days with no activity, so
    # the chart's x-axis has no gaps.
    trend: list[TrendPoint] = Field(default_factory=list)

    # Found -> added to My Leads -> contacted -> interested.
    funnel: list[FunnelStage] = Field(default_factory=list)

    # Where the leads are and what found them. Empty lists when there is
    # nothing yet, which the page shows as an empty state.
    top_countries: list[BreakdownSlice] = Field(default_factory=list)
    top_keywords: list[BreakdownSlice] = Field(default_factory=list)

    # How credits were spent in the window, by ledger kind.
    spend_by_kind: list[BreakdownSlice] = Field(default_factory=list)

    # True when the account has no data at all in the window, so the page can
    # explain rather than draw a set of zeroed charts.
    is_empty: bool

    # The UTC instant the window began, for the client to show its range.
    window_start: datetime
