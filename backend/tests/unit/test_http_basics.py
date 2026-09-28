"""HTTP behavior that needs no database: liveness, readiness when the DB is down, request IDs."""

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from incident_intel.main import create_app
from tests.support import make_settings

# Nothing listens on port 1, so connections are refused immediately.
UNREACHABLE_DB = "postgresql+asyncpg://user:pw@127.0.0.1:1/unreachable_test"


@pytest.fixture
async def offline_app() -> AsyncIterator[FastAPI]:
    app = create_app(make_settings(UNREACHABLE_DB, readiness_timeout_seconds=2.0))
    yield app
    await app.state.engine.dispose()


@pytest.fixture
async def offline_client(offline_app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=offline_app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def test_healthz_does_not_depend_on_database(offline_client: AsyncClient) -> None:
    response = await offline_client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readyz_reports_unavailable_database(offline_client: AsyncClient) -> None:
    response = await offline_client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "checks": {"database": "unavailable", "migrations": "unknown"},
    }


async def test_generates_request_id_when_absent(offline_client: AsyncClient) -> None:
    response = await offline_client.get("/healthz")

    assert len(response.headers["X-Request-ID"]) == 32


async def test_propagates_valid_incoming_request_id(offline_client: AsyncClient) -> None:
    response = await offline_client.get("/healthz", headers={"X-Request-ID": "abc-123.x_y"})

    assert response.headers["X-Request-ID"] == "abc-123.x_y"


async def test_replaces_invalid_incoming_request_id(offline_client: AsyncClient) -> None:
    response = await offline_client.get("/healthz", headers={"X-Request-ID": "bad id\twith spaces"})

    assert response.headers["X-Request-ID"] != "bad id\twith spaces"
    assert len(response.headers["X-Request-ID"]) == 32


async def test_unknown_route_uses_error_envelope(offline_client: AsyncClient) -> None:
    response = await offline_client.get("/nope")

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "not_found"
    assert body["error"]["request_id"] == response.headers["X-Request-ID"]


async def test_protected_route_without_credentials_needs_no_database(
    offline_client: AsyncClient,
) -> None:
    response = await offline_client.get("/v1/project")

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
