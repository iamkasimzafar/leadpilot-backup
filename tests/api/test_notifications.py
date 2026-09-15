"""Notification endpoints, and the notifications raised by billing events."""

from typing import Any

from httpx import AsyncClient

from app.core.config import settings
from app.models.notification import NotificationKind
from app.services.notification import NotificationService
from app.services.notification_stream import notification_stream
from tests.api.test_auth import signed_in_tokens

PREFIX = settings.API_V1_PREFIX
NOTIFICATIONS = f"{PREFIX}/notifications"
BILLING = f"{PREFIX}/billing"


async def _headers(client: AsyncClient, db_session: Any) -> dict[str, str]:
    tokens = await signed_in_tokens(client, db_session)
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def _user_id(db_session: Any) -> str:
    from sqlalchemy import select

    from app.models.user import User

    return (await db_session.execute(select(User))).scalars().first().id


async def _seed(db_session: Any, user_id: str, count: int = 3) -> list[str]:
    """Create notifications directly, bypassing HTTP."""
    service = NotificationService(db_session)
    ids = []
    for index in range(count):
        notification = await service.create(
            user_id,
            kind=NotificationKind.LEAD,
            title=f"Notification {index}",
            subtitle="Something happened",
        )
        ids.append(notification.id)

    return ids


async def _list(client: AsyncClient, headers: dict[str, str], **params: Any) -> dict:
    response = await client.get(NOTIFICATIONS, headers=headers, params=params)
    assert response.status_code == 200
    return response.json()


# --- Listing -----------------------------------------------------------------


async def test_listing_starts_empty(client: AsyncClient, db_session) -> None:
    headers = await _headers(client, db_session)

    body = await _list(client, headers)

    assert body["items"] == []
    assert body["total"] == 0
    assert body["unread"] == 0


async def test_notifications_are_listed_newest_first(
    client: AsyncClient, db_session
) -> None:
    headers = await _headers(client, db_session)
    await _seed(db_session, await _user_id(db_session), count=3)

    body = await _list(client, headers)

    assert [item["title"] for item in body["items"]] == [
        "Notification 2",
        "Notification 1",
        "Notification 0",
    ]
    assert body["total"] == 3
    assert body["unread"] == 3
    assert all(item["is_read"] is False for item in body["items"])


async def test_unread_only_filter(client: AsyncClient, db_session) -> None:
    headers = await _headers(client, db_session)
    ids = await _seed(db_session, await _user_id(db_session), count=3)
    await client.post(f"{NOTIFICATIONS}/read", json={"ids": [ids[0]]}, headers=headers)

    body = await _list(client, headers, unread_only=True)

    assert body["total"] == 2
    # The badge count is account-wide, not page-scoped.
    assert body["unread"] == 2
    assert all(item["is_read"] is False for item in body["items"])


async def test_listing_requires_auth(client: AsyncClient) -> None:
    assert (await client.get(NOTIFICATIONS)).status_code == 401


# --- Marking read ------------------------------------------------------------


async def test_marking_read_and_unread(client: AsyncClient, db_session) -> None:
    headers = await _headers(client, db_session)
    ids = await _seed(db_session, await _user_id(db_session), count=3)

    read = await client.post(
        f"{NOTIFICATIONS}/read", json={"ids": ids[:2]}, headers=headers
    )
    assert read.status_code == 200
    assert (await _list(client, headers))["unread"] == 1

    unread = await client.post(
        f"{NOTIFICATIONS}/read",
        json={"ids": ids[:2], "read": False},
        headers=headers,
    )
    assert unread.status_code == 200
    assert (await _list(client, headers))["unread"] == 3


async def test_mark_all_read(client: AsyncClient, db_session) -> None:
    headers = await _headers(client, db_session)
    await _seed(db_session, await _user_id(db_session), count=3)

    response = await client.post(f"{NOTIFICATIONS}/read-all", headers=headers)

    assert response.status_code == 200
    body = await _list(client, headers)
    assert body["unread"] == 0
    assert all(item["is_read"] for item in body["items"])
    assert all(item["read_at"] is not None for item in body["items"])


async def test_unread_count_endpoint(client: AsyncClient, db_session) -> None:
    headers = await _headers(client, db_session)
    await _seed(db_session, await _user_id(db_session), count=2)

    response = await client.get(f"{NOTIFICATIONS}/unread-count", headers=headers)

    assert response.status_code == 200
    assert response.json() == {"unread": 2}


async def test_marking_an_unknown_id_changes_nothing(
    client: AsyncClient, db_session
) -> None:
    """A forged id is ignored rather than erroring, so it cannot be used to
    probe which ids exist."""
    headers = await _headers(client, db_session)
    await _seed(db_session, await _user_id(db_session), count=1)

    response = await client.post(
        f"{NOTIFICATIONS}/read",
        json={"ids": ["11111111-2222-3333-4444-555555555555"]},
        headers=headers,
    )

    assert response.status_code == 200
    assert (await _list(client, headers))["unread"] == 1


# --- Deleting ----------------------------------------------------------------


async def test_deleting_a_notification(client: AsyncClient, db_session) -> None:
    headers = await _headers(client, db_session)
    ids = await _seed(db_session, await _user_id(db_session), count=2)

    response = await client.delete(f"{NOTIFICATIONS}/{ids[0]}", headers=headers)

    assert response.status_code == 204
    assert (await _list(client, headers))["total"] == 1


async def test_deleting_an_unknown_notification_is_404(
    client: AsyncClient, db_session
) -> None:
    headers = await _headers(client, db_session)

    response = await client.delete(f"{NOTIFICATIONS}/does-not-exist", headers=headers)

    assert response.status_code == 404


# --- Raised by billing -------------------------------------------------------


async def test_subscribing_raises_a_notification(client: AsyncClient, db_session) -> None:
    headers = await _headers(client, db_session)

    await client.post(
        f"{BILLING}/subscriptions", json={"plan_code": "starter_monthly"}, headers=headers
    )

    body = await _list(client, headers)
    assert body["total"] == 1
    newest = body["items"][0]
    assert newest["kind"] == "credits"
    assert "Starter plan activated" in newest["title"]
    assert "490 credits" in newest["subtitle"]
    assert newest["link"] == "/wallet"
    assert newest["is_read"] is False


async def test_topping_up_raises_a_notification_with_the_new_balance(
    client: AsyncClient, db_session
) -> None:
    headers = await _headers(client, db_session)
    await client.post(
        f"{BILLING}/subscriptions", json={"plan_code": "starter_monthly"}, headers=headers
    )

    await client.post(
        f"{BILLING}/top-ups", json={"pack_code": "popular"}, headers=headers
    )

    body = await _list(client, headers)
    assert body["total"] == 2
    newest = body["items"][0]
    assert newest["title"] == "990 credits added"
    assert "1,480 credits" in newest["subtitle"]
    assert body["unread"] == 2


async def test_a_refused_top_up_raises_no_notification(
    client: AsyncClient, db_session
) -> None:
    """The notification shares the purchase's transaction, so a 403 leaves
    nothing behind."""
    headers = await _headers(client, db_session)

    response = await client.post(
        f"{BILLING}/top-ups", json={"pack_code": "basic"}, headers=headers
    )

    assert response.status_code == 403
    assert (await _list(client, headers))["total"] == 0


# --- Isolation between accounts ----------------------------------------------


async def test_notifications_are_scoped_to_their_owner(
    client: AsyncClient, db_session
) -> None:
    from app.services.auth import AuthService

    owner_headers = await _headers(client, db_session)
    owner_id = await _user_id(db_session)
    ids = await _seed(db_session, owner_id, count=2)

    # A second account, verified and signed in.
    service = AuthService(db_session)
    other, _ = await service.register("other@leadpilot.io", "other-password-1", "Other")
    other.is_verified = True
    await service.commit()
    login = await client.post(
        f"{PREFIX}/auth/login",
        json={"email": "other@leadpilot.io", "password": "other-password-1"},
    )
    other_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    # They see none of the owner's notifications...
    assert (await _list(client, other_headers))["total"] == 0

    # ...cannot mark them read...
    await client.post(f"{NOTIFICATIONS}/read", json={"ids": ids}, headers=other_headers)
    assert (await _list(client, owner_headers))["unread"] == 2

    # ...and cannot delete them.
    assert (
        await client.delete(f"{NOTIFICATIONS}/{ids[0]}", headers=other_headers)
    ).status_code == 404
    assert (await _list(client, owner_headers))["total"] == 2


# --- Live stream fan-out -----------------------------------------------------


async def test_publishing_reaches_a_subscribed_connection(db_session) -> None:
    """The SSE fan-out delivers the id to an open connection."""
    user_id = "user-under-test"

    async with notification_stream.subscribe(user_id) as queue:
        assert notification_stream.connection_count(user_id) == 1

        notification_stream.publish(user_id, "notification-1")

        assert queue.get_nowait() == "notification-1"

    # Unsubscribed on exit.
    assert notification_stream.connection_count(user_id) == 0


async def test_publishing_to_nobody_is_harmless() -> None:
    notification_stream.publish("user-with-no-connections", "notification-1")


async def test_stream_rejects_a_bad_token(client: AsyncClient) -> None:
    response = await client.get(f"{NOTIFICATIONS}/stream", params={"token": "nonsense"})

    assert response.status_code == 401
