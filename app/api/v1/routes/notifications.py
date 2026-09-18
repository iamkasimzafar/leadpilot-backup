"""Notification endpoints, including the live SSE stream."""

import asyncio
import json
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from jwt.exceptions import InvalidTokenError

from app.api.deps import CurrentUser, DbSession, Pagination
from app.core.exceptions import UnauthorizedError
from app.core.logging import get_logger
from app.core.security import decode_token
from app.repositories.user import UserRepository
from app.schemas.common import Message
from app.schemas.notification import (
    MarkReadRequest,
    NotificationPage,
    NotificationRead,
    UnreadCount,
)
from app.services.notification import NotificationService
from app.services.notification_stream import notification_stream

log = get_logger(__name__)

router = APIRouter()

# How often the stream emits a comment line when nothing is happening. Keeps
# proxies from closing an apparently idle connection.
_HEARTBEAT_SECONDS = 25.0


@router.get("", response_model=NotificationPage, summary="List notifications")
async def list_notifications(
    db: DbSession,
    pagination: Pagination,
    current_user: CurrentUser,
    unread_only: bool = Query(False, description="Only notifications not yet read."),
) -> NotificationPage:
    """Newest first, with the account-wide unread count for the bell badge."""
    items, total, unread = await NotificationService(db).list_for_user(
        current_user.id,
        offset=pagination.offset,
        limit=pagination.limit,
        unread_only=unread_only,
    )

    return NotificationPage(
        items=[NotificationRead.model_validate(n) for n in items],
        total=total,
        unread=unread,
        page=pagination.page,
        per_page=pagination.per_page,
    )


@router.get(
    "/unread-count", response_model=UnreadCount, summary="Unread notification count"
)
async def unread_count(db: DbSession, current_user: CurrentUser) -> UnreadCount:
    """Cheap endpoint for the reconcile poll."""
    return UnreadCount(unread=await NotificationService(db).unread_count(current_user.id))


@router.post("/read", response_model=Message, summary="Mark notifications read")
async def mark_read(
    payload: MarkReadRequest, db: DbSession, current_user: CurrentUser
) -> Message:
    """Mark the given notifications read (or unread with `read: false`).

    Ids belonging to another account are silently ignored rather than erroring,
    so a stale client cannot probe for which ids exist.
    """
    changed = await NotificationService(db).set_read(
        current_user.id, payload.ids, read=payload.read
    )

    return Message(message=f"{changed} notification(s) updated.")


@router.post("/read-all", response_model=Message, summary="Mark all read")
async def mark_all_read(db: DbSession, current_user: CurrentUser) -> Message:
    changed = await NotificationService(db).mark_all_read(current_user.id)

    return Message(message=f"{changed} notification(s) marked read.")


@router.delete(
    "/{notification_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a notification",
)
async def delete_notification(
    notification_id: str, db: DbSession, current_user: CurrentUser
) -> Response:
    await NotificationService(db).delete(current_user.id, notification_id)

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/stream", summary="Live notification stream (SSE)")
async def stream(request: Request, db: DbSession, token: str = Query(...)) -> Response:
    """Server-sent events: one `notification` event per new notification.

    Authenticated by a `token` query parameter rather than a header, because
    the browser's EventSource API cannot set headers. The token is the ordinary
    access token and is validated exactly as the header would be.

    Each event carries only an id; the client fetches the row. Clients must
    still reconcile periodically -- see app/services/notification_stream.py for
    why a push can be missed.
    """
    try:
        payload = decode_token(token, expected_type="access")
    except InvalidTokenError as exc:
        # Any decode failure -- bad signature, wrong type, expired -- is a 401.
        raise UnauthorizedError("Invalid or expired token.") from exc

    user_id = payload.get("sub")
    if not user_id:
        raise UnauthorizedError("Malformed token.")

    user = await UserRepository(db).get(user_id)
    if user is None or not user.is_active:
        raise UnauthorizedError("User no longer active.")

    # The stream needs no database from here on, but FastAPI keeps the session
    # dependency open until the response ends -- for SSE that is hours, and
    # every open tab would pin one pooled connection. Hand it back now.
    await db.close()

    async def events() -> AsyncGenerator[str, None]:
        async with notification_stream.subscribe(user.id) as queue:
            # Tell the client it is live, so it can do its initial reconcile.
            yield f"event: ready\ndata: {json.dumps({'ok': True})}\n\n"

            while True:
                if await request.is_disconnected():
                    break

                try:
                    notification_id = await asyncio.wait_for(
                        queue.get(), timeout=_HEARTBEAT_SECONDS
                    )
                except TimeoutError:
                    # Comment frame: keeps intermediaries from timing us out.
                    yield ": keep-alive\n\n"
                    continue

                data = json.dumps({"id": notification_id})
                yield f"event: notification\ndata: {data}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            # Stops nginx buffering the stream into uselessness.
            "X-Accel-Buffering": "no",
        },
    )
