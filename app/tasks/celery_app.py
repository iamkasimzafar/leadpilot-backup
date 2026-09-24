"""The Celery application: broker, beat schedule and worker settings.

Run the worker and beat as two processes (the compose file does), both
pointing here:

    celery -A app.tasks.celery_app worker --pool=solo --concurrency=1
    celery -A app.tasks.celery_app beat

`solo` pool on purpose. Every task here runs async SQLAlchemy under
`asyncio.run`, and a forking pool would hand child processes a copy of the
parent's connection state. One process, one task at a time, is all the
workload needs: the dispatcher's job is to trickle monitors out, not to race.
"""

from celery import Celery

from app.core.config import settings

celery_app = Celery(
    "leadpilot",
    broker=settings.CELERY_BROKER_URL,
    include=["app.tasks.monitors"],
)

celery_app.conf.update(
    # Nothing reads task results, so there is no result backend to keep.
    task_ignore_result=True,
    # Acknowledge after the task finishes, not when it is picked up, so a
    # worker that dies mid-task hands the tick back rather than losing it.
    # Safe because dispatch claims each monitor under a conditional UPDATE:
    # a re-run tick finds nothing left to take.
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    # A tick that could not finish in this long is stuck, not slow.
    task_time_limit=20 * 60,
    task_soft_time_limit=18 * 60,
    timezone="UTC",
    enable_utc=True,
    # Keep retrying the broker at startup: on a VM reboot Redis is often a
    # moment behind the worker.
    broker_connection_retry_on_startup=True,
    beat_schedule={
        "dispatch-due-monitors": {
            "task": "app.tasks.monitors.dispatch_due_monitors",
            "schedule": float(settings.MONITOR_DISPATCH_INTERVAL_SECONDS),
            # A tick the worker was too busy (or down) to take before the next
            # one is due is dropped, not queued up: running yesterday's ticks
            # back-to-back after an outage gains nothing, since each one asks
            # the same question of the same table.
            "options": {"expires": float(settings.MONITOR_DISPATCH_INTERVAL_SECONDS)},
        },
    },
)
