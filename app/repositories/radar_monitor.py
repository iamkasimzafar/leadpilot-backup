"""Queries for radar monitors and the email dedupe they depend on."""

from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, func, select, update

from app.models.lead import DecisionMaker
from app.models.radar_monitor import MonitorStatus, RadarMonitor
from app.repositories.base import BaseRepository


class RadarMonitorRepository(BaseRepository[RadarMonitor]):
    model = RadarMonitor

    async def list_for_user(
        self, user_id: str, *, offset: int = 0, limit: int = 50
    ) -> list[RadarMonitor]:
        stmt = (
            select(RadarMonitor)
            .where(RadarMonitor.user_id == user_id)
            .order_by(RadarMonitor.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        result = await self.db.execute(stmt)

        return list(result.scalars().all())

    async def count_for_user(self, user_id: str) -> int:
        stmt = (
            select(func.count())
            .select_from(RadarMonitor)
            .where(RadarMonitor.user_id == user_id)
        )
        result = await self.db.execute(stmt)

        return result.scalar_one()

    async def get_for_user(self, monitor_id: str, user_id: str) -> RadarMonitor | None:
        """Scoped fetch: one user can never address another's monitor."""
        return await self.find_one_by(id=monitor_id, user_id=user_id)

    async def get_for_callback(
        self, monitor_id: str, token: str | None
    ) -> RadarMonitor | None:
        """The monitor a results POST is allowed to write to.

        Authenticated by the per-monitor token rather than a session: n8n has
        no user. Knowing the endpoint is not enough.
        """
        if not token:
            return None

        return await self.find_one_by(id=monitor_id, callback_token=token)

    async def due(self, now: datetime, *, limit: int = 100) -> list[RadarMonitor]:
        """Running monitors whose next run has come around.

        A NULL next_run_at means "never run", which is due immediately -- how
        a newly created or just-resumed monitor starts.
        """
        stmt = (
            select(RadarMonitor)
            .where(
                RadarMonitor.status == MonitorStatus.RUNNING.value,
                (RadarMonitor.next_run_at.is_(None)) | (RadarMonitor.next_run_at <= now),
            )
            .order_by(RadarMonitor.next_run_at.is_(None).desc(), RadarMonitor.next_run_at)
            .limit(limit)
        )
        result = await self.db.execute(stmt)

        return list(result.scalars().all())

    async def claim(
        self, monitor: RadarMonitor, now: datetime, next_run: datetime
    ) -> bool:
        """Mark the monitor dispatched, but only if nobody else just did.

        A conditional UPDATE on the same predicate the `due` query used. If
        two scheduler passes overlap -- n8n retrying, or a second worker --
        exactly one UPDATE matches a row and the other sees rowcount 0 and
        skips. Without this a monitor could run twice in a night and bill the
        user twice over.
        """
        stmt = (
            update(RadarMonitor)
            .where(
                RadarMonitor.id == monitor.id,
                RadarMonitor.status == MonitorStatus.RUNNING.value,
                (RadarMonitor.next_run_at.is_(None)) | (RadarMonitor.next_run_at <= now),
            )
            .values(last_run_at=now, next_run_at=next_run)
        )
        # execute() is typed as returning Result, but an UPDATE always yields a
        # CursorResult -- the only kind that carries rowcount, which is the
        # whole point here.
        result = cast(CursorResult[Any], await self.db.execute(stmt))

        return bool(result.rowcount)


class ContactEmailRepository(BaseRepository[DecisionMaker]):
    """The dedupe lookup that keeps a monitor from billing a contact twice."""

    model = DecisionMaker

    async def existing_emails(self, user_id: str, emails: list[str]) -> set[str]:
        """Which of `emails` this user already has, lowercased.

        One query for the whole batch rather than one per candidate: a run can
        carry hundreds of contacts, and this sits on the billing path.
        """
        wanted = {email.strip().lower() for email in emails if email and email.strip()}
        if not wanted:
            return set()

        stmt = select(func.lower(DecisionMaker.verified_email)).where(
            DecisionMaker.user_id == user_id,
            func.lower(DecisionMaker.verified_email).in_(wanted),
        )
        result = await self.db.execute(stmt)

        return {row for row in result.scalars().all() if row}
