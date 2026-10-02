import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from tests.support import FakePublisher

pytestmark = pytest.mark.integration


async def test_readyz_ok_when_database_is_migrated_and_kafka_is_up(client: AsyncClient) -> None:
    response = await client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"database": "ok", "migrations": "ok", "kafka": "ok"},
    }


async def test_readyz_reports_pending_migrations(app: FastAPI, client: AsyncClient) -> None:
    app.state.expected_migration_head = "9999"

    response = await client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["checks"]["migrations"] == "pending"


async def test_readyz_reports_unreachable_kafka(
    client: AsyncClient, publisher: FakePublisher
) -> None:
    publisher.unreachable = True

    response = await client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["checks"] == {
        "database": "ok",
        "migrations": "ok",
        "kafka": "unavailable",
    }


async def test_readyz_reports_missing_topics(
    app: FastAPI, client: AsyncClient, publisher: FakePublisher
) -> None:
    publisher.missing = {app.state.metrics_topic}

    response = await client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["checks"]["kafka"] == "pending"
