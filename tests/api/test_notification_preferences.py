"""Notification preferences: storage, partial updates, and enforcement.

The enforcement tests are the point of the feature -- a stored flag that does
not actually suppress a notification would be decorative.
"""

from typing import Any

from httpx import AsyncClient
from sqlalchemy import select

from app.core.config import settings
from app.models.notification import NotificationKind
from app.models.notification_preference import NotificationPreference
from app.models.user import User
from app.services.notification import NotificationService
from tests.api.test_auth import signed_in_tokens

PREFIX = settings.API_V1_PREFIX
PREFERENCES = f"{PREFIX}/notification-preferences"
NOTIFICATIONS = f"{PREFIX}/notifications"

ALL_FLAGS = ("lead", "reply", "monitor", "credits", "sequence", "weekly_digest")


async def _headers(client: AsyncClient, db_session: Any) -> dict[str, str]:
    tokens = await signed_in_tokens(client, db_session)
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def _user_id(db_session: Any) -> str:
    return (await db_session.execute(select(User))).scalars().first().id


# --- Reading -----------------------------------------------------------------


async def test_preferences_require_auth(client: AsyncClient) -> None:
    response = await client.get(PREFERENCES)

    assert response.status_code == 401


async def test_first_read_returns_defaults(client: AsyncClient, db_session) -> None:
    """Every event kind on, the weekly digest off."""
    headers = await _headers(client, db_session)

    response = await client.get(PREFERENCES, headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == set(ALL_FLAGS)
    assert body["lead"] is True
    assert body["reply"] is True
    assert body["monitor"] is True
    assert body["credits"] is True
    assert body["sequence"] is True
    assert body["weekly_digest"] is False


async def test_first_read_persists_a_row(client: AsyncClient, db_session) -> None:
    headers = await _headers(client, db_session)

    await client.get(PREFERENCES, headers=headers)

    rows = (await db_session.execute(select(NotificationPreference))).scalars().all()
    assert len(rows) == 1
    assert rows[0].user_id == await _user_id(db_session)


# --- Writing -----------------------------------------------------------------


async def test_update_persists_to_the_database(client: AsyncClient, db_session) -> None:
    headers = await _headers(client, db_session)

    response = await client.patch(
        PREFERENCES, json={"credits": False, "weekly_digest": True}, headers=headers
    )

    assert response.status_code == 200
    assert response.json()["credits"] is False
    assert response.json()["weekly_digest"] is True

    # Survives a fresh read, i.e. it is actually stored rather than echoed.
    reread = await client.get(PREFERENCES, headers=headers)
    assert reread.json()["credits"] is False
    assert reread.json()["weekly_digest"] is True

    row = (
        await db_session.execute(
            select(NotificationPreference).where(
                NotificationPreference.user_id == await _user_id(db_session)
            )
        )
    ).scalar_one()
    assert row.credits is False
    assert row.weekly_digest is True


async def test_update_is_partial(client: AsyncClient, db_session) -> None:
    """An omitted flag keeps its stored value rather than resetting."""
    headers = await _headers(client, db_session)

    await client.patch(PREFERENCES, json={"lead": False}, headers=headers)
    second = await client.patch(PREFERENCES, json={"reply": False}, headers=headers)
    body = second.json()

    assert body["lead"] is False, "the earlier change must survive"
    assert body["reply"] is False
    assert body["monitor"] is True


async def test_empty_update_changes_nothing(client: AsyncClient, db_session) -> None:
    headers = await _headers(client, db_session)
    await client.patch(PREFERENCES, json={"monitor": False}, headers=headers)

    body = (await client.patch(PREFERENCES, json={}, headers=headers)).json()

    assert body["monitor"] is False


async def test_update_creates_the_row_when_absent(
    client: AsyncClient, db_session
) -> None:
    """A user who never opened the panel can still save straight away."""
    headers = await _headers(client, db_session)

    response = await client.patch(PREFERENCES, json={"lead": False}, headers=headers)

    assert response.status_code == 200
    assert response.json()["lead"] is False


async def test_rejects_a_non_boolean(client: AsyncClient, db_session) -> None:
    headers = await _headers(client, db_session)

    response = await client.patch(PREFERENCES, json={"lead": "nope"}, headers=headers)

    assert response.status_code == 422


# --- Isolation ---------------------------------------------------------------


async def test_preferences_are_per_user(client: AsyncClient, db_session) -> None:
    """One account's choice must not leak into another's."""
    headers = await _headers(client, db_session)
    await client.patch(PREFERENCES, json={"lead": False}, headers=headers)

    other = await client.post(
        f"{PREFIX}/auth/register",
        json={
            "email": "second@example.com",
            "password": "An0therSecret!",
            "full_name": "Second User",
        },
    )
    assert other.status_code == 201

    second = (
        await db_session.execute(
            select(User).where(User.email == "second@example.com")
        )
    ).scalar_one()
    second.is_verified = True
    await db_session.commit()

    login = await client.post(
        f"{PREFIX}/auth/login",
        json={"email": "second@example.com", "password": "An0therSecret!"},
    )
    second_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    body = (await client.get(PREFERENCES, headers=second_headers)).json()
    assert body["lead"] is True, "the second account keeps its own defaults"


# --- Enforcement: the flags actually suppress delivery ------------------------


async def test_notification_is_suppressed_when_its_kind_is_off(
    client: AsyncClient, db_session
) -> None:
    headers = await _headers(client, db_session)
    user_id = await _user_id(db_session)

    await client.patch(PREFERENCES, json={"lead": False}, headers=headers)

    created = await NotificationService(db_session).create(
        user_id,
        kind=NotificationKind.LEAD,
        title="A new lead",
        subtitle="Should never be stored",
    )

    assert created is None, "create() reports that nothing was written"

    listing = await client.get(NOTIFICATIONS, headers=headers)
    assert listing.json()["items"] == []
    assert listing.json()["unread"] == 0


async def test_other_kinds_still_arrive(client: AsyncClient, db_session) -> None:
    """Switching one kind off must not silence the rest."""
    headers = await _headers(client, db_session)
    user_id = await _user_id(db_session)

    await client.patch(PREFERENCES, json={"lead": False}, headers=headers)

    await NotificationService(db_session).create(
        user_id,
        kind=NotificationKind.CREDITS,
        title="Credits added",
        subtitle="Still wanted",
    )

    body = (await client.get(NOTIFICATIONS, headers=headers)).json()
    assert [n["kind"] for n in body["items"]] == ["credits"]


async def test_notification_arrives_when_its_kind_is_on(
    client: AsyncClient, db_session
) -> None:
    headers = await _headers(client, db_session)
    user_id = await _user_id(db_session)

    notification = await NotificationService(db_session).create(
        user_id,
        kind=NotificationKind.LEAD,
        title="A new lead",
        subtitle="Wanted by default",
    )

    assert notification is not None
    body = (await client.get(NOTIFICATIONS, headers=headers)).json()
    assert body["total"] == 1
    assert body["unread"] == 1


async def test_force_overrides_the_preference(
    client: AsyncClient, db_session
) -> None:
    """Callers that must reach the user can bypass the flag."""
    headers = await _headers(client, db_session)
    user_id = await _user_id(db_session)

    await client.patch(PREFERENCES, json={"credits": False}, headers=headers)

    notification = await NotificationService(db_session).create(
        user_id,
        kind=NotificationKind.CREDITS,
        title="Payment failed",
        subtitle="Must be seen",
        force=True,
    )

    assert notification is not None
    assert (await client.get(NOTIFICATIONS, headers=headers)).json()["total"] == 1


async def test_a_user_without_a_row_receives_everything(
    client: AsyncClient, db_session
) -> None:
    """Accounts predating this feature must not be silenced by the migration."""
    headers = await _headers(client, db_session)
    user_id = await _user_id(db_session)

    rows = (await db_session.execute(select(NotificationPreference))).scalars().all()
    assert rows == [], "no preference row exists yet"

    notification = await NotificationService(db_session).create(
        user_id,
        kind=NotificationKind.MONITOR,
        title="Monitor fired",
        subtitle="Delivered without a preference row",
    )

    assert notification is not None
    assert (await client.get(NOTIFICATIONS, headers=headers)).json()["total"] == 1


async def test_suppressed_billing_notification_does_not_break_the_purchase(
    client: AsyncClient, db_session
) -> None:
    """Turning credit notifications off must not break buying credits."""
    headers = await _headers(client, db_session)
    await client.patch(PREFERENCES, json={"credits": False}, headers=headers)

    response = await client.post(
        f"{PREFIX}/billing/subscriptions",
        json={"plan_code": "starter_monthly"},
        headers=headers,
    )

    assert response.status_code == 201, response.text

    overview = await client.get(f"{PREFIX}/billing/overview", headers=headers)
    assert overview.json()["subscription"] is not None
    assert overview.json()["balance"] > 0

    # The purchase succeeded; only the announcement was withheld.
    assert (await client.get(NOTIFICATIONS, headers=headers)).json()["total"] == 0
