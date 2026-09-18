"""Lead-search run ORM models.

A run is created before the workflow is dispatched, so every progress callback
from n8n has a row to attach to. Each checkpoint n8n reports becomes one
LeadSearchEvent; the run's own `stage`/`status` hold the latest position so the
UI can render without replaying the event list.
"""

import secrets
import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


def _uuid_str() -> str:
    return str(uuid.uuid4())


def _callback_token() -> str:
    """Unguessable per-run secret.

    n8n quotes this back on every progress POST. Knowing the endpoint is not
    enough to write to someone else's run: you need the token for that run.
    """
    return secrets.token_urlsafe(32)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class SearchStage(StrEnum):
    """The checkpoints n8n reports, in the order they happen.

    `value` is what n8n sends in the `stage` field. Adding a stage here (and to
    STAGE_ORDER below) is all the backend needs -- no migration, because the
    column is a plain string.
    """

    QUEUED = "queued"
    SEARCHING_COMPANIES = "searching_companies"
    AI_ANALYSING = "ai_analysing"
    DOMAIN_SEARCH = "domain_search"
    FINDING_DECISION_MAKERS = "finding_decision_makers"
    FINDING_EMAILS = "finding_emails"
    VERIFYING_CONTACTS = "verifying_contacts"
    COMPLETED = "completed"


# Display order for the progress tracker. `queued` is implicit (the run starts
# there) and `completed` is the terminal tick, so neither is a checkpoint the
# user sees as a separate row.
STAGE_ORDER: tuple[SearchStage, ...] = (
    SearchStage.SEARCHING_COMPANIES,
    SearchStage.AI_ANALYSING,
    SearchStage.DOMAIN_SEARCH,
    SearchStage.FINDING_DECISION_MAKERS,
    SearchStage.FINDING_EMAILS,
    SearchStage.VERIFYING_CONTACTS,
)


class RunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class LeadSearchRun(Base, TimestampMixin):
    """One keyword search, from dispatch to completion."""

    __table_args__ = (
        Index("ix_lead_search_run_user_id_created_at", "user_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid_str)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    )

    # Shared secret for this run's progress callbacks. Indexed because the
    # webhook looks a run up by (id, token).
    callback_token: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=_callback_token
    )

    original_keyword: Mapped[str] = mapped_column(String(255), nullable=False)
    # JSON-encoded list. A plain text column rather than JSON: portable across
    # MySQL and the SQLite used by the tests, and never queried by content.
    keywords_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    keyword_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # The "Auto-add to My Leads" checkbox at dispatch. When set, every company
    # the workflow returns lands in My Leads without the user doing anything.
    auto_add_to_leads: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=func.true(), nullable=False
    )

    # SerpApi `gl` code the search was scoped to; NULL for a worldwide search.
    # Recorded so the run history shows what a search actually covered.
    country: Mapped[str | None] = mapped_column(String(2), nullable=True)

    # Which kind of company the search targeted (see services/company_types.py);
    # NULL when it targeted any type.
    company_type: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Snov.io extraction filters (see services/search_targeting.py). NULL means
    # the filter was not applied. Recorded because they decide which contacts
    # were extracted, and therefore what the run was charged for.
    contact_role: Mapped[str | None] = mapped_column(String(32), nullable=True)
    company_size: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # Whether the run asked the workflow to check each number on WhatsApp.
    # Recorded because it is billed per check, so it explains part of the cost.
    validate_whatsapp: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=func.false(), nullable=False
    )

    # Settlement. Written once, by whichever results ingest claims it, and only
    # for a successful run -- a null credits_charged_at means the run has not
    # been billed: still running, failed, or its results never came.
    # `credits_shortfall` is the part of the bill the balance could not cover
    # (the wallet cannot go below zero), kept so the run can show what it cost
    # and what is still owed. `whatsapp_checks` is the count that was billed.
    credits_charged: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    credits_shortfall: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    whatsapp_checks: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    credits_charged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    status: Mapped[str] = mapped_column(
        String(16), default=RunStatus.RUNNING.value, nullable=False
    )
    stage: Mapped[str] = mapped_column(
        String(32), default=SearchStage.QUEUED.value, nullable=False
    )

    # Running totals reported by the workflow, for the summary line.
    companies_found: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    contacts_found: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Set when the workflow reports a failure, or the run is abandoned.
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # When the workflow's results callback was first ingested. Distinct from
    # finished_at: the last progress checkpoint usually closes the run seconds
    # BEFORE the results POST lands, and the UI needs to know which has
    # happened -- "finished, waiting for results" vs "results are in".
    results_received_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<LeadSearchRun {self.id} {self.status}/{self.stage}>"


class LeadSearchEvent(Base):
    """One checkpoint reported by the workflow.

    Append-only: the history of a run is the ordered list of its events, which
    is what lets the UI show which steps have already ticked over after a page
    reload.
    """

    __table_args__ = (
        Index("ix_lead_search_event_run_id_created_at", "run_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid_str)
    run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("lead_search_run.id", ondelete="CASCADE"),
        nullable=False,
    )

    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    # Optional human-readable detail from the workflow ("142 companies matched").
    message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Optional count carried by this checkpoint.
    count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        server_default=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<LeadSearchEvent {self.run_id} {self.stage}>"
