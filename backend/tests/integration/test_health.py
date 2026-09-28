import pytest
from fastapi import FastAPI
from httpx import AsyncClient

pytestmark = pytest.mark.integration


async def test_readyz_ok_when_database_is_migrated(client: AsyncClient) -> None:
    response = await client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"database": "ok", "migrations": "ok"}}


async def test_readyz_reports_pending_migrations(app: FastAPI, client: AsyncClient) -> None:
    app.state.expected_migration_head = "9999"

    response = await client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["checks"] == {"database": "ok", "migrations": "pending"}
