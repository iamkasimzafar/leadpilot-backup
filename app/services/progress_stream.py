"""In-process fan-out for live lead-search progress.

Same design as app/services/notification_stream.py, and the same caveat: it is
per-process, so with several uvicorn workers a push raised in one worker does
not reach a browser connected to another. The client reconciles by fetching the
run, so a missed push delays an update rather than losing it. Swap for Redis
pub/sub if you scale past one worker.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.core.logging import get_logger

log = get_logger(__name__)

_QUEUE_MAX = 64


class ProgressStream:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[str]]] = {}

    @asynccontextmanager
    async def subscribe(self, user_id: str) -> AsyncIterator[asyncio.Queue[str]]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=_QUEUE_MAX)
        self._subscribers.setdefault(user_id, set()).add(queue)

        try:
            yield queue
        finally:
            listeners = self._subscribers.get(user_id)
            if listeners is not None:
                listeners.discard(queue)
                if not listeners:
                    del self._subscribers[user_id]

    def publish(self, user_id: str, run_id: str) -> None:
        """Nudge every open connection for a user. Only the run id travels; the
        client fetches the run so the payload cannot drift from the database."""
        for queue in self._subscribers.get(user_id, set()):
            try:
                queue.put_nowait(run_id)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait(run_id)
                except (asyncio.QueueEmpty, asyncio.QueueFull):  # pragma: no cover
                    log.warning("progress_stream.dropped", user_id=user_id)

    def connection_count(self, user_id: str) -> int:
        return len(self._subscribers.get(user_id, set()))


progress_stream = ProgressStream()
