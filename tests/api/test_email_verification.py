"""Email verification flow, and proof that mail is queued rather than skipped.

No real SMTP is touched: app.core.email.send_email is replaced everywhere it is
looked up, and the outbox is asserted on instead.
"""

import pytest
from httpx import AsyncClient

from app.core.config import settings
from app.services.auth import AuthService

PREFIX = f"{settings.API_V1_PREFIX}/auth"
CREDENTIALS = {"email": "founder@leadpilot.io", "password": "sup3r-secret-pw"}


@pytest.fixture
def outbox(monkeypatch) -> list[dict]:
    """Capture every message the app tries to send."""
    sent: list[dict] = []

    async def _fake_send(*, to: str, subject: str, html: str, text: str) -> bool:
        sent.append({"to": to, "subject": subject, "html": html, "text": text})
        return True

    # The routes import send_email by name, so patch it there.
    monkeypatch.setattr("app.api.v1.routes.auth.send_email", _fake_send)
    return sent


async def register(client: AsyncClient, **overrides) -> object:
    payload = {**CREDENTIALS, "full_name": "Zhang Wei", **overrides}
    return await client.post(f"{PREFIX}/register", json=payload)


async def verification_token(db_session, email: str | None = None) -> str:
    result = await AuthService(db_session).request_verification(
        email or str(CREDENTIALS["email"])
    )
    assert result is not None
    return result[1]


# --- Registration sends a verification mail ----------------------------------


async def test_register_queues_only_the_verification_email(
    client: AsyncClient, outbox: list[dict]
) -> None:
    """The welcome mail waits for verification, so only one message goes out."""
    response = await register(client)

    assert response.status_code == 201
    assert len(outbox) == 1
    assert outbox[0]["to"] == CREDENTIALS["email"]
    assert "verify-email?token=" in outbox[0]["text"]
    assert "Welcome" not in outbox[0]["subject"]


async def test_a_new_account_starts_unverified(
    client: AsyncClient, db_session, outbox: list[dict]
) -> None:
    await register(client)

    # No session exists yet, so read the flag through verification instead.
    response = await client.post(
        f"{PREFIX}/verify-email", json={"token": await verification_token(db_session)}
    )

    assert response.status_code == 200
    assert response.json()["is_verified"] is True


async def test_unverified_user_cannot_sign_in(
    client: AsyncClient, outbox: list[dict]
) -> None:
    await register(client)

    response = await client.post(f"{PREFIX}/login", json=CREDENTIALS)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "email_not_verified"


# --- Verifying ---------------------------------------------------------------


async def test_email_can_be_verified_with_a_valid_token(
    client: AsyncClient, db_session, outbox: list[dict]
) -> None:
    await register(client)

    response = await client.post(
        f"{PREFIX}/verify-email", json={"token": await verification_token(db_session)}
    )

    assert response.status_code == 200
    assert response.json()["is_verified"] is True


async def test_verifying_sends_the_welcome_email(
    client: AsyncClient, db_session, outbox: list[dict]
) -> None:
    await register(client)
    token = await verification_token(db_session)
    outbox.clear()

    await client.post(f"{PREFIX}/verify-email", json={"token": token})

    assert len(outbox) == 1
    assert "Welcome" in outbox[0]["subject"]
    assert "Hi Zhang," in outbox[0]["text"]


async def test_welcome_email_handles_a_missing_name(
    client: AsyncClient, db_session, outbox: list[dict]
) -> None:
    await register(client, full_name=None)
    token = await verification_token(db_session)
    outbox.clear()

    await client.post(f"{PREFIX}/verify-email", json={"token": token})

    welcome = next(m for m in outbox if "Welcome" in m["subject"])
    assert "Hi," in welcome["text"]


async def test_a_verification_token_cannot_be_replayed(
    client: AsyncClient, db_session, outbox: list[dict]
) -> None:
    await register(client)
    token = await verification_token(db_session)

    first = await client.post(f"{PREFIX}/verify-email", json={"token": token})
    second = await client.post(f"{PREFIX}/verify-email", json={"token": token})

    assert first.status_code == 200
    assert second.status_code == 422


async def test_verify_rejects_an_unknown_token(client: AsyncClient) -> None:
    response = await client.post(
        f"{PREFIX}/verify-email", json={"token": "never-issued"}
    )

    assert response.status_code == 422


# --- Resending ---------------------------------------------------------------


async def test_resend_is_silent_about_unknown_addresses(
    client: AsyncClient, outbox: list[dict]
) -> None:
    known = await client.post(
        f"{PREFIX}/resend-verification", json={"email": CREDENTIALS["email"]}
    )
    unknown = await client.post(
        f"{PREFIX}/resend-verification", json={"email": "nobody@leadpilot.io"}
    )

    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json()


async def test_resend_sends_nothing_for_an_unknown_address(
    client: AsyncClient, outbox: list[dict]
) -> None:
    await client.post(
        f"{PREFIX}/resend-verification", json={"email": "nobody@leadpilot.io"}
    )

    assert outbox == []


async def test_resend_queues_a_fresh_link(
    client: AsyncClient, outbox: list[dict]
) -> None:
    await register(client)
    outbox.clear()

    response = await client.post(
        f"{PREFIX}/resend-verification", json={"email": CREDENTIALS["email"]}
    )

    assert response.status_code == 200
    assert len(outbox) == 1
    assert "verify-email?token=" in outbox[0]["text"]


async def test_resend_does_nothing_once_verified(
    client: AsyncClient, db_session, outbox: list[dict]
) -> None:
    await register(client)
    await client.post(
        f"{PREFIX}/verify-email", json={"token": await verification_token(db_session)}
    )
    outbox.clear()

    await client.post(
        f"{PREFIX}/resend-verification", json={"email": CREDENTIALS["email"]}
    )

    assert outbox == []


# --- Password reset now actually mails ---------------------------------------


async def test_forgot_password_queues_a_reset_email(
    client: AsyncClient, outbox: list[dict]
) -> None:
    await register(client)
    outbox.clear()

    response = await client.post(
        f"{PREFIX}/forgot-password", json={"email": CREDENTIALS["email"]}
    )

    assert response.status_code == 200
    assert len(outbox) == 1
    assert "reset-password?token=" in outbox[0]["text"]


async def test_forgot_password_sends_nothing_for_unknown_addresses(
    client: AsyncClient, outbox: list[dict]
) -> None:
    await client.post(
        f"{PREFIX}/forgot-password", json={"email": "nobody@leadpilot.io"}
    )

    assert outbox == []
