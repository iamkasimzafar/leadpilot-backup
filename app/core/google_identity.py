"""Verifying a "Continue with Google" sign-in.

The browser runs Google's OAuth popup and hands us the resulting access token.
A token proves nothing by itself -- any site can obtain a perfectly valid Google
token for the same person -- so the check that matters here is the audience:
the token must have been issued to one of *our* OAuth clients (the web UI's, or
the desktop app's). Skipping that lets a token harvested by another app sign in
to LeadPilot as its owner.

Everything that talks to Google lives in this module, so the auth service stays
free of HTTP and tests can replace `verify_access_token` wholesale.
"""

from dataclasses import dataclass

import httpx

from app.core.config import settings
from app.core.exceptions import (
    ServiceUnavailableError,
    UnauthorizedError,
    UpstreamError,
)
from app.core.logging import get_logger

log = get_logger(__name__)

_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
_TIMEOUT = 10.0


@dataclass(frozen=True, slots=True)
class GoogleIdentity:
    email: str
    full_name: str | None


async def verify_access_token(access_token: str) -> GoogleIdentity:
    """Confirm the token with Google and return who it belongs to.

    Raises UnauthorizedError for anything wrong with the token itself, and
    UpstreamError when Google cannot be reached -- the caller's token may be
    fine, so that must not read as "sign-in rejected".
    """
    # Our own OAuth clients: the web UI's, and the desktop app's if there is one.
    accepted_audiences = {
        client_id
        for client_id in (settings.GOOGLE_CLIENT_ID, settings.GOOGLE_DESKTOP_CLIENT_ID)
        if client_id
    }
    if not accepted_audiences:
        raise ServiceUnavailableError("Google sign-in is not configured.")

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            info_response = await client.get(
                _TOKENINFO_URL, params={"access_token": access_token}
            )

            # Google answers 400 for an expired, revoked or malformed token.
            if info_response.status_code != httpx.codes.OK:
                raise UnauthorizedError("Google sign-in could not be verified.")

            info = info_response.json()

            # `aud` is the client the token was minted for. See module docstring.
            if info.get("aud") not in accepted_audiences:
                log.warning("auth.google.audience_mismatch", aud=info.get("aud"))
                raise UnauthorizedError("Google sign-in could not be verified.")

            email = info.get("email")

            # tokeninfo sends booleans as the strings "true" / "false".
            if not email or str(info.get("email_verified")).lower() != "true":
                raise UnauthorizedError(
                    "Your Google account has no verified email address."
                )

            # The display name is a nicety: never fail a sign-in over it.
            full_name: str | None = None
            profile_response = await client.get(
                _USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}
            )
            if profile_response.status_code == httpx.codes.OK:
                full_name = profile_response.json().get("name") or None

    except httpx.HTTPError as exc:
        log.warning("auth.google.unreachable", error=str(exc))
        raise UpstreamError("Google could not be reached. Please try again.") from exc

    return GoogleIdentity(email=email.strip().lower(), full_name=full_name)
