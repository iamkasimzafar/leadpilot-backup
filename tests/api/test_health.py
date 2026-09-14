"""Smoke tests proving the app wiring works end to end."""

from httpx import AsyncClient

from app.core.config import settings


async def test_health_returns_ok(client: AsyncClient) -> None:
    response = await client.get(f"{settings.API_V1_PREFIX}/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_readiness_queries_the_database(client: AsyncClient) -> None:
    response = await client.get(f"{settings.API_V1_PREFIX}/health/ready")

    assert response.status_code == 200
    assert response.json()["database"] == "ok"


async def test_protected_route_rejects_anonymous_requests(client: AsyncClient) -> None:
    response = await client.get(f"{settings.API_V1_PREFIX}/auth/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
