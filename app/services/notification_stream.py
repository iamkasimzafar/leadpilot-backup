"""In-process fan-out for live notification delivery.

Each connected browser holds one SSE request, which parks on an asyncio.Queue
registered here. Creating a notification pushes onto every queue that user has
open (several tabs = several queues).

⚠️ Deliberately in-memory, and therefore per-process. With more than one uvicorn
worker, a notification created in worker A does not reach a client connected to
worker B. That is survivable ONLY because the client also reconciles against the
database (on connect, and on a slow poll), so a missed push shows up moments
later rather than being lost. If you scale past one worker and want instant
delivery everywhere, replace this module with a Redis pub/sub channel -- the
rest of the code does not need to change.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.core.logging import get_logger

log = get_logger(__name__)

# Bounded so a browser that stops reading (laptop asleep, tab throttled) cannot
# grow a queue without limit. On overflow the oldest entry is dropped: the
# client's next reconcile picks up whatever it missed.
_QUEUE_MAX = 32


class NotificationStream:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[str]]] = {}

    @asynccontextmanager
    async def subscribe(self, user_id: str) -> AsyncIterator[asyncio.Queue[str]]:
        """Register a queue for one connection, removing it on disconnect."""
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

    def publish(self, user_id: str, notification_id: str) -> None:
        """Nudge every open connection for a user.

        Only the id travels: the client fetches the row itself, so the payload
        cannot drift from what the database holds. Never raises -- a failed
        push must not roll back the notification that triggered it.
        """
        for queue in self._subscribers.get(user_id, set()):
            try:
                queue.put_nowait(notification_id)
            except asyncio.QueueFull:
                # Drop the oldest and retry once; the client reconciles anyway.
                try:
                    queue.get_nowait()
                    queue.put_nowait(notification_id)
                except (asyncio.QueueEmpty, asyncio.QueueFull):  # pragma: no cover
                    log.warning("notification_stream.dropped", user_id=user_id)

    def connection_count(self, user_id: str) -> int:
        """Open connections for a user. Used by tests."""
        return len(self._subscribers.get(user_id, set()))


# One instance per process, shared by the service and the SSE route.
notification_stream = NotificationStream()
