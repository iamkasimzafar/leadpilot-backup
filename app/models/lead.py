"""Company and decision-maker ORM models.

These are the results a lead search produces: one company row per business the
workflow found, and one contact row per decision maker inside it.

Stored as real columns rather than a JSON blob so My Leads can filter, sort and
paginate them later without unpacking JSON on every query. Anything the
workflow sends that has no column of its own is preserved in `extra_json`, so a
new field from n8n is never silently lost.
"""

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin


def _uuid_str() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(UTC)


class LeadStatus(StrEnum):
    """Where a lead sits in the user's follow-up. The My Leads tabs."""

    NEW = "new"
    CONTACTED = "contacted"
    INTERESTED = "interested"


class Company(Base, TimestampMixin):
    """A company found by a lead search.

    Every result is a company row; it becomes a *lead* -- something the user
    is tracking in My Leads -- once `added_to_leads_at` is set. That happens
    automatically when the search was started with auto-add on, or by hand
    from the results view.
    """

    __table_args__ = (
        Index("ix_company_user_id_created_at", "user_id", "created_at"),
        Index("ix_company_run_id", "run_id"),
        # The My Leads tabs: filter by user, in-leads, status.
        Index("ix_company_user_id_status", "user_id", "added_to_leads_at", "status"),
        # One row per website per user. This is what makes the results
        # callback safe to receive many times at once: concurrent inserts of
        # the same company collide here, and the loser adopts the winner's
        # row. NULL websites are exempt (NULLs are distinct in a unique index),
        # so companies without a site fall back to name matching.
        UniqueConstraint("user_id", "website", name="uq_company_user_id_website"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid_str)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    )
    # The run that produced this company. Kept if the run is deleted so the
    # lead itself survives; only the link is cleared.
    run_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("lead_search_run.id", ondelete="SET NULL"),
        nullable=True,
    )

    company_name: Mapped[str] = mapped_column(String(255), nullable=False)
    website: Mapped[str | None] = mapped_column(String(500), nullable=True)
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    industry: Mapped[str | None] = mapped_column(String(255), nullable=True)
    company_size: Mapped[str | None] = mapped_column(String(64), nullable=True)
    hq_phone: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Any additional keys the workflow sent, as JSON. Empty object when none.
    extra_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    # --- Lead tracking (My Leads) -------------------------------------------
    # Null until the company is added to My Leads. Doubles as "when".
    added_to_leads_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(16), default=LeadStatus.NEW.value, server_default="new", nullable=False
    )
    # Free-form, written by the user.
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    @property
    def in_leads(self) -> bool:
        return self.added_to_leads_at is not None

    decision_makers: Mapped[list["DecisionMaker"]] = relationship(
        back_populates="company",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Company {self.company_name}>"


class DecisionMaker(Base):
    """A contact at a company, as returned by the workflow."""

    __table_args__ = (
        Index("ix_decision_maker_company_id", "company_id"),
        # Radar monitors dedupe on the contact's email before inserting, so
        # the user is never billed twice for the same person. That check runs
        # per candidate email on every monitor run, so it needs an index.
        #
        # Deliberately NOT unique. The same person legitimately appears under
        # two company domains -- production already holds such pairs -- and a
        # manual search must stay free to record them. Uniqueness here would
        # reject those rows and break the existing ingest path; the guarantee
        # the monitor needs is enforced in MonitorResultsService instead.
        Index("ix_decision_maker_user_id_email", "user_id", "verified_email"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid_str)
    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("company.id", ondelete="CASCADE"), nullable=False
    )

    # Denormalised from the owning company. The dedupe lookup is "has this
    # user already got this email?", and MySQL cannot index across the join.
    # Nullable so the backfill can run online; written on every insert.
    user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("user.id", ondelete="CASCADE"), nullable=True
    )

    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    job_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    verified_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    # "valid", "invalid", "catch-all", ... whatever the verifier reports.
    email_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    linkedin_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    phone_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    whatsapp_status: Mapped[str | None] = mapped_column(String(32), nullable=True)

    extra_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        server_default=func.now(),
        nullable=False,
    )

    company: Mapped[Company] = relationship(back_populates="decision_makers")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<DecisionMaker {self.full_name}>"
