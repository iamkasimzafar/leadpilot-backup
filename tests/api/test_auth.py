"""End-to-end tests for the authentication flow."""

from httpx import AsyncClient

from app.core.config import settings
from app.services.auth import AuthService

PREFIX = f"{settings.API_V1_PREFIX}/auth"

CREDENTIALS = {"email": "founder@leadpilot.io", "password": "sup3r-secret-pw"}


async def register(client: AsyncClient, **overrides: object):
    payload = {**CREDENTIALS, "full_name": "Zhang Wei", **overrides}
    return await client.post(f"{PREFIX}/register", json=payload)


async def verify(client: AsyncClient, db_session, email: str | None = None) -> None:
    """Confirm an address so the account can sign in."""
    result = await AuthService(db_session).request_verification(
        email or str(CREDENTIALS["email"])
    )
    assert result is not None
    await client.post(f"{PREFIX}/verify-email", json={"token": result[1]})


async def signed_in_tokens(client: AsyncClient, db_session) -> dict:
    """Register, verify, then sign in -- the only way to obtain a session."""
    await register(client)
    await verify(client, db_session)

    response = await client.post(f"{PREFIX}/login", json=CREDENTIALS)
    assert response.status_code == 200

    return response.json()


# --- Registration ------------------------------------------------------------


async def test_register_returns_a_message_not_a_session(client: AsyncClient) -> None:
    """Registration must not sign anyone in: the address is unconfirmed."""
    response = await register(client)

    assert response.status_code == 201
    body = response.json()
    assert "access_token" not in body
    assert body["message"]


async def test_register_rejects_a_duplicate_email(client: AsyncClient) -> None:
    await register(client)
    response = await register(client)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


async def test_register_is_case_insensitive_on_email(client: AsyncClient) -> None:
    await register(client)
    response = await register(client, email="FOUNDER@LeadPilot.io")

    assert response.status_code == 409


async def test_register_rejects_a_short_password(client: AsyncClient) -> None:
    response = await register(client, password="short")

    assert response.status_code == 422


# --- Login -------------------------------------------------------------------


async def test_login_succeeds_once_verified(client: AsyncClient, db_session) -> None:
    await register(client)
    await verify(client, db_session)

    response = await client.post(f"{PREFIX}/login", json=CREDENTIALS)

    assert response.status_code == 200
    assert response.json()["access_token"]


async def test_login_is_refused_while_unverified(client: AsyncClient) -> None:
    await register(client)

    response = await client.post(f"{PREFIX}/login", json=CREDENTIALS)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "email_not_verified"


async def test_login_rejects_a_wrong_password(client: AsyncClient, db_session) -> None:
    await register(client)
    await verify(client, db_session)

    response = await client.post(
        f"{PREFIX}/login", json={**CREDENTIALS, "password": "not-the-password"}
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


async def test_a_wrong_password_hides_the_verification_state(
    client: AsyncClient,
) -> None:
    """Someone without the password learns nothing about the account."""
    await register(client)

    response = await client.post(
        f"{PREFIX}/login", json={**CREDENTIALS, "password": "not-the-password"}
    )

    assert response.status_code == 401


async def test_login_rejects_an_unknown_email(client: AsyncClient) -> None:
    response = await client.post(f"{PREFIX}/login", json=CREDENTIALS)

    assert response.status_code == 401


# --- Me ----------------------------------------------------------------------


async def test_me_returns_the_authenticated_user(
    client: AsyncClient, db_session
) -> None:
    token = (await signed_in_tokens(client, db_session))["access_token"]

    response = await client.get(
        f"{PREFIX}/me", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["email"] == CREDENTIALS["email"]
    assert body["full_name"] == "Zhang Wei"
    assert body["is_verified"] is True
    assert "hashed_password" not in body


async def test_me_rejects_a_garbage_token(client: AsyncClient) -> None:
    response = await client.get(
        f"{PREFIX}/me", headers={"Authorization": "Bearer not-a-jwt"}
    )

    assert response.status_code == 401


async def test_me_rejects_a_refresh_token(client: AsyncClient, db_session) -> None:
    """A refresh token must not be accepted as an access token."""
    refresh = (await signed_in_tokens(client, db_session))["refresh_token"]

    response = await client.get(
        f"{PREFIX}/me", headers={"Authorization": f"Bearer {refresh}"}
    )

    assert response.status_code == 401


# --- Refresh -----------------------------------------------------------------


async def test_refresh_issues_a_new_pair(client: AsyncClient, db_session) -> None:
    refresh = (await signed_in_tokens(client, db_session))["refresh_token"]

    response = await client.post(f"{PREFIX}/refresh", json={"refresh_token": refresh})

    assert response.status_code == 200
    assert response.json()["access_token"]


async def test_refresh_rejects_an_access_token(
    client: AsyncClient, db_session
) -> None:
    access = (await signed_in_tokens(client, db_session))["access_token"]

    response = await client.post(f"{PREFIX}/refresh", json={"refresh_token": access})

    assert response.status_code == 401


# --- Forgot / reset password -------------------------------------------------


async def test_forgot_password_is_silent_about_unknown_emails(
    client: AsyncClient,
) -> None:
    known = await client.post(
        f"{PREFIX}/forgot-password", json={"email": CREDENTIALS["email"]}
    )
    unknown = await client.post(
        f"{PREFIX}/forgot-password", json={"email": "nobody@leadpilot.io"}
    )

    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json()


async def test_password_can_be_reset_with_a_valid_token(
    client: AsyncClient, db_session
) -> None:
    await register(client)
    await verify(client, db_session)

    token = await AuthService(db_session).request_password_reset(
        str(CREDENTIALS["email"])
    )
    assert token is not None

    response = await client.post(
        f"{PREFIX}/reset-password",
        json={"token": token, "password": "a-brand-new-password"},
    )
    assert response.status_code == 200

    # The old password no longer works, the new one does.
    old = await client.post(f"{PREFIX}/login", json=CREDENTIALS)
    assert old.status_code == 401

    new = await client.post(
        f"{PREFIX}/login",
        json={"email": CREDENTIALS["email"], "password": "a-brand-new-password"},
    )
    assert new.status_code == 200


async def test_a_reset_token_cannot_be_replayed(
    client: AsyncClient, db_session
) -> None:
    await register(client)

    token = await AuthService(db_session).request_password_reset(
        str(CREDENTIALS["email"])
    )

    first = await client.post(
        f"{PREFIX}/reset-password", json={"token": token, "password": "first-password"}
    )
    second = await client.post(
        f"{PREFIX}/reset-password", json={"token": token, "password": "second-password"}
    )

    assert first.status_code == 200
    assert second.status_code == 422


async def test_reset_rejects_an_unknown_token(client: AsyncClient) -> None:
    response = await client.post(
        f"{PREFIX}/reset-password",
        json={"token": "never-issued", "password": "some-new-password"},
    )

    assert response.status_code == 422
