"""Request-body size limit. Rejection happens before authentication or any database access."""

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from incident_intel.main import create_app
from tests.support import FakePublisher, make_settings

UNREACHABLE_DB = "postgresql+asyncpg://user:pw@127.0.0.1:1/unreachable_test"
LIMIT = 2048


@pytest.fixture
async def small_limit_client() -> AsyncIterator[AsyncClient]:
    app: FastAPI = create_app(
        make_settings(UNREACHABLE_DB, max_request_body_bytes=LIMIT), publisher=FakePublisher()
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http
    await app.state.engine.dispose()


async def test_rejects_declared_oversized_body(small_limit_client: AsyncClient) -> None:
    response = await small_limit_client.post(
        "/v1/ingest/metrics",
        content=b"x" * (LIMIT + 1),
        headers={"Content-Type": "application/json", "Idempotency-Key": "k"},
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"
    assert response.headers["X-Request-ID"]


async def test_rejects_oversized_streamed_body_without_content_length(
    small_limit_client: AsyncClient,
) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(4):
            yield b"x" * 1024

    response = await small_limit_client.post(
        "/v1/ingest/metrics",
        content=chunks(),
        headers={"Content-Type": "application/json", "Idempotency-Key": "k"},
    )

    assert "content-length" not in {k.lower() for k in response.request.headers}
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


async def test_body_within_limit_reaches_authentication(small_limit_client: AsyncClient) -> None:
    response = await small_limit_client.post(
        "/v1/ingest/metrics",
        json={"points": []},
        headers={"Idempotency-Key": "k"},
    )

    assert response.status_code == 401
