"""CORS configuration.

The frontend runs on its own origin, so anything the browser must let
JavaScript read has to be declared here rather than merely sent.
"""

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.main import create_app

ORIGIN = "http://localhost:5173"
EXPORT = f"{settings.API_V1_PREFIX}/reports/export"


async def _get(origin: str, monkeypatch: pytest.MonkeyPatch) -> httpx.Response:
    """A cross-origin GET against an app built with a known allowed origin.

    The expose-headers list rides on the actual response, not the preflight,
    so this has to be a real request. Authentication is irrelevant here: CORS
    headers are applied by middleware before the route ever runs, so a 401
    still carries them.
    """
    monkeypatch.setattr(settings, "BACKEND_CORS_ORIGINS", [ORIGIN])
    app = create_app()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(EXPORT, headers={"Origin": origin})


async def test_content_disposition_is_exposed_to_the_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The file exports read the download's name off Content-Disposition.

    A browser hides every response header from cross-origin JavaScript unless
    it is named in Access-Control-Expose-Headers, so without this the CSV and
    Excel downloads silently fall back to a generic filename.
    """
    response = await _get(ORIGIN, monkeypatch)

    assert "Content-Disposition" in response.headers.get(
        "access-control-expose-headers", ""
    )


async def test_an_unknown_origin_is_not_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = await _get("https://evil.example.com", monkeypatch)

    assert (
        response.headers.get("access-control-allow-origin")
        != "https://evil.example.com"
    )
