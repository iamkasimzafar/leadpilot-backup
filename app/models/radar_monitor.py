"""Radar Monitor ORM model.

A monitor is a saved search the user wants re-run on a schedule. n8n's monitor
workflow wakes on its own cron, asks the backend which monitors are due, runs
each one through the same Serper -> DeepSeek -> Snov.io pipeline a manual
search uses, and posts the results back.

Two fields carry the state that makes a repeat run worth anything:

`serper_offset`
    Where the next Google page starts. Re-running the same query tomorrow
    returns the same top results, so every completed run advances this and the
    next one asks Serper to skip that far in. Without it a monitor finds zero
    new leads on day two.

`total_leads_generated`
    Running count of contacts this monitor actually added, after the
    email-level dedupe. What the UI shows as "Total Leads Caught".
"""

import json
import secrets
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


def _uuid_str() -> str:
    return str(uuid.uuid4())


def _callback_token() -> str:
    """Unguessable per-monitor secret.

    n8n quotes this back when posting a monitor's results. Knowing the endpoint
    is not enough to write into someone else's monitor: you need its token.
    """
    return secrets.token_urlsafe(32)


class MonitorSearchType(StrEnum):
    """How the monitor finds companies."""

    KEYWORD = "keyword"
    HS_CODE = "hs_code"


class MonitorFrequency(StrEnum):
    """How often the monitor runs."""

    DAILY = "daily"
    WEEKLY = "weekly"

    @property
    def interval_days(self) -> int:
        return 1 if self is MonitorFrequency.DAILY else 7


class MonitorStatus(StrEnum):
    """Whether the scheduler should pick this monitor up."""

    RUNNING = "running"
    PAUSED = "paused"


class RadarMonitor(Base, TimestampMixin):
    """One saved, scheduled search.

    Table name is set explicitly: Base derives snake_case from the class name,
    which would give `radar_monitor`, and the agreed name is plural.
    """

    __tablename__ = "radar_monitors"

    __table_args__ = (
        # The scheduler's query: due monitors for a status, oldest first.
        Index("ix_radar_monitors_status_next_run_at", "status", "next_run_at"),
        Index("ix_radar_monitors_user_id_created_at", "user_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid_str)

    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # What the user called it, e.g. "Hardware Wholesalers - Germany".
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # MonitorSearchType. Plain string, so adding a type needs no migration.
    search_type: Mapped[str] = mapped_column(String(32), nullable=False)

    # The keyword text, or the confirmed 6-digit HS code.
    search_value: Mapped[str] = mapped_column(String(500), nullable=False)

    # For an HS-code monitor, the product wording the AI returned with the
    # code. The pipeline searches this rather than the digits: almost nobody
    # publishes an HS code on their site, so searching the code finds tariff
    # pages instead of buyers.
    search_label: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Country / company size / contact role, as sent to the workflow. JSON in
    # a Text column so a new filter needs no migration.
    filters_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    # MonitorFrequency.
    frequency: Mapped[str] = mapped_column(String(16), nullable=False)

    # Capped at dispatch, so one run cannot drain the whole balance.
    limit_per_run: Mapped[int] = mapped_column(Integer, nullable=False, default=50)

    # MonitorStatus.
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=MonitorStatus.RUNNING.value
    )

    # Contacts this monitor has added, after dedupe. Never counts a lead the
    # user already had.
    total_leads_generated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Where the next Serper request starts. Advanced by limit_per_run after
    # each completed run so tomorrow digs past today's results.
    serper_offset: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Quoted back by n8n when it posts this monitor's results.
    callback_token: Mapped[str] = mapped_column(
        String(64), nullable=False, default=_callback_token
    )

    # When the scheduler may next pick this up. NULL means "as soon as due",
    # which is how a freshly created or just-resumed monitor starts.
    next_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Claimed at dispatch. Doubles as the guard that stops a second scheduler
    # pass dispatching the same monitor twice.
    last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Set when a run's results land, so the UI can distinguish "dispatched"
    # from "actually produced something".
    last_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Why the last run failed, if it did. Cleared on the next success.
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # First dispatch, for "Running for X days".
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    @property
    def filters(self) -> dict[str, Any]:
        """The stored filters, decoded. Never raises on bad JSON."""
        try:
            value = json.loads(self.filters_json or "{}")
        except ValueError:
            return {}

        return value if isinstance(value, dict) else {}

    @property
    def running_days(self) -> int:
        """Whole days since the first dispatch, for the UI's "Running X days"."""
        if self.started_at is None:
            return 0

        started = self.started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)

        return max((datetime.now(UTC) - started).days, 0)
