"""Tests for "Continue with Google".

Google itself is replaced by a fake: what is under test is what we do with a
verified identity -- and that an unverifiable one gets nowhere.
"""

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from app.core import google_identity
from app.core.config import settings
from app.core.exceptions import UnauthorizedError
from app.core.google_identity import GoogleIdentity
from app.models.user import User
from app.repositories.user import UserRepository
from app.services.auth import AuthService

PREFIX = f"{settings.API_V1_PREFIX}/auth"

EMAIL = "founder@leadpilot.io"


@pytest.fixture
def google_says(monkeypatch: pytest.MonkeyPatch):
    """Make Google vouch for the given identity, whatever token is sent."""

    def _configure(email: str = EMAIL, full_name: str | None = "Zhang Wei") -> None:
        async def fake_verify(access_token: str) -> GoogleIdentity:
            return GoogleIdentity(email=email, full_name=full_name)

        monkeypatch.setattr(google_identity, "verify_access_token", fake_verify)

    return _configure


async def google_login(client: AsyncClient):
    return await client.post(f"{PREFIX}/google", json={"access_token": "ya29.token"})


async def test_first_google_sign_in_creates_a_verified_account(
    client: AsyncClient, google_says
) -> None:
    google_says()

    response = await google_login(client)

    assert response.status_code == 200
    tokens = response.json()

    me = await client.get(
        f"{PREFIX}/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}
    )
    assert me.status_code == 200
    assert me.json()["email"] == EMAIL
    assert me.json()["full_name"] == "Zhang Wei"

    # Google confirmed the address, so there is no verification step to do.
    assert me.json()["is_verified"] is True


async def test_second_google_sign_in_reuses_the_account(
    client: AsyncClient, google_says
) -> None:
    google_says()

    first = await google_login(client)
    second = await google_login(client)

    assert first.status_code == second.status_code == 200

    # Registering the same address now conflicts: one account, not two.
    duplicate = await client.post(
        f"{PREFIX}/register", json={"email": EMAIL, "password": "sup3r-secret-pw"}
    )
    assert duplicate.status_code == 409


# --- One account per email -----------------------------------------------------


async def _user_count(db_session) -> int:
    return (await db_session.execute(select(func.count()).select_from(User))).scalar_one()


async def _register_and_verify(client: AsyncClient, db_session, email: str) -> dict:
    """The ordinary email + password path, completed."""
    credentials = {"email": email, "password": "sup3r-secret-pw"}
    await client.post(
        f"{PREFIX}/register", json={**credentials, "full_name": "Zhang Wei"}
    )

    result = await AuthService(db_session).request_verification(email)
    assert result is not None
    await client.post(f"{PREFIX}/verify-email", json={"token": result[1]})

    return credentials


async def _me(client: AsyncClient, tokens: dict) -> dict:
    response = await client.get(
        f"{PREFIX}/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}
    )
    return response.json()


async def test_google_links_to_an_account_registered_with_a_password(
    client: AsyncClient, db_session, google_says
) -> None:
    """Registered by email first, then "Continue with Google" with the same
    address: it must be the SAME account, not a second one."""
    credentials = await _register_and_verify(client, db_session, EMAIL)
    password_session = (await client.post(f"{PREFIX}/login", json=credentials)).json()

    google_says()
    google_response = await google_login(client)

    assert google_response.status_code == 200

    by_password = await _me(client, password_session)
    by_google = await _me(client, google_response.json())

    assert by_google["id"] == by_password["id"]
    assert await _user_count(db_session) == 1


async def test_linking_google_keeps_the_existing_password_working(
    client: AsyncClient, db_session, google_says
) -> None:
    """Google is an extra way in to a verified account, not a replacement."""
    credentials = await _register_and_verify(client, db_session, EMAIL)

    google_says()
    assert (await google_login(client)).status_code == 200

    assert (await client.post(f"{PREFIX}/login", json=credentials)).status_code == 200


async def test_google_matches_the_account_regardless_of_letter_case(
    client: AsyncClient, db_session, google_says
) -> None:
    await _register_and_verify(client, db_session, "Founder@LeadPilot.IO")

    google_says(email="founder@leadpilot.io")
    assert (await google_login(client)).status_code == 200

    assert await _user_count(db_session) == 1


async def test_registering_after_google_does_not_create_a_second_account(
    client: AsyncClient, db_session, google_says
) -> None:
    """The reverse order: Google first, then the register form."""
    google_says()
    await google_login(client)

    response = await client.post(
        f"{PREFIX}/register", json={"email": EMAIL, "password": "sup3r-secret-pw"}
    )

    assert response.status_code == 409
    assert await _user_count(db_session) == 1


async def test_a_lost_creation_race_links_instead_of_failing(
    client: AsyncClient, db_session, google_says, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two first-time sign-ins for one address can both find "no account". The
    loser's insert hits the unique index; it must adopt the winner's account
    rather than answer 500 -- or, worse, create a duplicate."""
    await _register_and_verify(client, db_session, EMAIL)

    real_lookup = UserRepository.get_by_email
    calls = {"n": 0}

    async def blind_once(self: UserRepository, email: str) -> User | None:
        # The first lookup misses, as it would for the slower of two requests.
        calls["n"] += 1
        return None if calls["n"] == 1 else await real_lookup(self, email)

    monkeypatch.setattr(UserRepository, "get_by_email", blind_once)

    google_says()
    response = await google_login(client)

    assert response.status_code == 200
    assert await _user_count(db_session) == 1


async def test_google_sign_in_locks_out_whoever_pre_registered_the_address(
    client: AsyncClient, google_says
) -> None:
    """Someone can register a victim's address and wait. When the real owner
    arrives through Google the account becomes verified -- and the squatter's
    password must stop working at that moment, or they now own a live login."""
    squatter = {"email": EMAIL, "password": "squatters-passw0rd"}
    await client.post(f"{PREFIX}/register", json=squatter)

    google_says()
    assert (await google_login(client)).status_code == 200

    response = await client.post(f"{PREFIX}/login", json=squatter)

    assert response.status_code == 401


async def test_a_rejected_google_token_signs_nobody_in(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def reject(access_token: str) -> GoogleIdentity:
        raise UnauthorizedError("Google sign-in could not be verified.")

    monkeypatch.setattr(google_identity, "verify_access_token", reject)

    response = await google_login(client)

    assert response.status_code == 401


async def test_google_sign_in_is_off_without_a_client_id(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "")
    monkeypatch.setattr(settings, "GOOGLE_DESKTOP_CLIENT_ID", "")

    response = await google_login(client)

    assert response.status_code == 503


# --- The verifier itself -----------------------------------------------------


def _fake_google(monkeypatch: pytest.MonkeyPatch, tokeninfo: dict, status: int = 200):
    """Stand in for Google's two endpoints."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "tokeninfo" in str(request.url):
            return httpx.Response(status, json=tokeninfo)
        return httpx.Response(200, json={"name": "Zhang Wei"})

    real_client = httpx.AsyncClient

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(google_identity.httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "leadpilot-client-id")
    monkeypatch.setattr(settings, "GOOGLE_DESKTOP_CLIENT_ID", "leadpilot-desktop-id")


async def test_verifier_accepts_a_token_minted_for_us(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_google(
        monkeypatch,
        {
            "aud": "leadpilot-client-id",
            "email": "Founder@LeadPilot.io",
            "email_verified": "true",
        },
    )

    identity = await google_identity.verify_access_token("ya29.token")

    assert identity == GoogleIdentity(email=EMAIL, full_name="Zhang Wei")


async def test_verifier_accepts_a_token_minted_for_the_desktop_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The desktop shell signs in through its own OAuth client, so its tokens
    carry that client's ID as the audience."""
    _fake_google(
        monkeypatch,
        {"aud": "leadpilot-desktop-id", "email": EMAIL, "email_verified": "true"},
    )

    identity = await google_identity.verify_access_token("ya29.token")

    assert identity.email == EMAIL


async def test_verifier_rejects_a_token_minted_for_another_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuine Google token for the right person, but issued to someone
    else's OAuth client. Accepting it would let any site that a user signs in
    to with Google sign in to LeadPilot as them."""
    _fake_google(
        monkeypatch,
        {"aud": "some-other-app", "email": EMAIL, "email_verified": "true"},
    )

    with pytest.raises(UnauthorizedError):
        await google_identity.verify_access_token("ya29.token")


async def test_verifier_rejects_an_unverified_google_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_google(
        monkeypatch,
        {"aud": "leadpilot-client-id", "email": EMAIL, "email_verified": "false"},
    )

    with pytest.raises(UnauthorizedError):
        await google_identity.verify_access_token("ya29.token")


async def test_verifier_rejects_a_token_google_does_not_recognise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_google(monkeypatch, {"error": "invalid_token"}, status=400)

    with pytest.raises(UnauthorizedError):
        await google_identity.verify_access_token("ya29.token")
