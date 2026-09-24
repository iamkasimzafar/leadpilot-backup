"""The Radar Monitors dispatcher, as a Celery task.

There is no per-monitor Celery task with a long ETA. The schedule lives in the
database: every running monitor carries the exact random second it next wakes
(`radar_monitors.next_run_at`), chosen when it was created, resumed, or last
dispatched. Beat only has to ask, every few minutes, "whose time is it?" and
send those to n8n.

That shape is deliberate. Long-ETA Celery tasks over Redis are fragile -- a
worker restart or the broker's visibility timeout can replay them, which here
would mean billing a user twice for one night's run. A database row cannot be
replayed, and the dispatcher claims each monitor under a conditional UPDATE
before anything is sent.
"""

import asyncio

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.core.logging import get_logger
from app.db.session import create_engine
from app.services.radar_monitor import RadarMonitorService
from app.tasks.celery_app import celery_app

log = get_logger(__name__)


# Celery ships no type information, so its decorator is opaque to mypy.
@celery_app.task(name="app.tasks.monitors.dispatch_due_monitors")  # type: ignore[untyped-decorator]
def dispatch_due_monitors() -> int:
    """Send every monitor whose slot has come round to n8n. Returns how many."""
    return asyncio.run(_dispatch())


async def _dispatch() -> int:
    # A fresh engine per tick: `asyncio.run` opens a new event loop each time,
    # and connections pooled on the previous loop cannot be reused on it. The
    # tick runs every few minutes, so the cost of a new pool is nothing.
    engine = create_engine()
    factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )

    try:
        async with factory() as db:
            sent = await RadarMonitorService(db).dispatch_due(
                limit=settings.MONITOR_DISPATCH_BATCH,
                spacing=settings.MONITOR_DISPATCH_SPACING_SECONDS,
            )
    except Exception:
        # Logged and swallowed: a broken tick must not poison beat's schedule,
        # and the next tick will look at the same table again.
        log.exception("radar_monitor.dispatch_tick_failed")

        return 0
    finally:
        await engine.dispose()

    log.info("radar_monitor.dispatch_tick", dispatched=len(sent))

    return len(sent)
