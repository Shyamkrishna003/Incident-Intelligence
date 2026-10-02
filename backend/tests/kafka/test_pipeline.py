"""End-to-end: HTTP API → real Kafka → storage consumer → PostgreSQL → read API.

Each test gets its own topics (random prefix), created before and deleted after, so tests
never see each other's messages. Needs TEST_KAFKA_BOOTSTRAP_SERVERS and the test database.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from incident_intel.core.config import Settings
from incident_intel.db.session import get_session
from incident_intel.main import create_app
from incident_intel.streaming.admin import delete_topics, ensure_topics, read_topic_from_start
from incident_intel.streaming.consumer import ConsumedMessage, KafkaMessageSource, run_consumer
from incident_intel.streaming.producer import KafkaPublisher
from incident_intel.streaming.topics import ALL_TOPICS, METRICS, METRICS_DLQ, topic_name
from incident_intel.telemetry.consumer import MetricBatchHandler
from incident_intel.telemetry.models import IngestBatch, MetricPoint
from tests.conftest import TenantFactory, _TestEnvironment

pytestmark = [pytest.mark.integration, pytest.mark.kafka]

URL = "/v1/services/payment-api/metrics/latency"


@pytest.fixture
def kafka_settings(settings: Settings, test_environment: _TestEnvironment) -> Iterator[Settings]:
    servers = test_environment.test_kafka_bootstrap_servers
    if not servers:
        pytest.skip("TEST_KAFKA_BOOTSTRAP_SERVERS is not set")
    isolated = settings.model_copy(
        update={
            "kafka_bootstrap_servers": servers,
            "kafka_topic_prefix": f"test-{uuid.uuid4().hex[:8]}.",
        }
    )
    ensure_topics(isolated)
    yield isolated
    delete_topics(isolated, [topic_name(isolated, spec) for spec in ALL_TOPICS])


@pytest.fixture
async def kafka_publisher(kafka_settings: Settings) -> AsyncIterator[KafkaPublisher]:
    publisher = KafkaPublisher(kafka_settings, delivery_timeout_seconds=10)
    yield publisher
    await publisher.close()


@pytest.fixture
async def kafka_client(
    kafka_settings: Settings, kafka_publisher: KafkaPublisher, db_session: AsyncSession
) -> AsyncIterator[AsyncClient]:
    app: FastAPI = create_app(kafka_settings, publisher=kafka_publisher)

    async def _test_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_session] = _test_session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http
    await app.state.engine.dispose()


async def _consume(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    publisher: KafkaPublisher,
    *,
    expected: int,
) -> None:
    """Run the real storage consumer until ``expected`` messages were handled."""
    stop = asyncio.Event()
    handled = 0
    store = MetricBatchHandler(session_factory)

    async def handler(message: ConsumedMessage) -> None:
        nonlocal handled
        try:
            await store(message)
        finally:
            handled += 1
            if handled >= expected:
                stop.set()

    source = KafkaMessageSource(
        settings, group_id=f"test-{uuid.uuid4().hex}", topics=[topic_name(settings, METRICS)]
    )
    try:
        await asyncio.wait_for(
            run_consumer(
                source=source,
                handler=handler,
                publisher=publisher,
                dlq_topic=topic_name(settings, METRICS_DLQ),
                stop=stop,
                poll_timeout_seconds=0.5,
            ),
            timeout=60,
        )
    finally:
        await source.close()


def _body(*values: float) -> dict[str, Any]:
    now = datetime.now(UTC).replace(microsecond=0)
    return {
        "points": [
            {
                "service": "payment-api",
                "metric": "latency",
                "unit": "ms",
                "timestamp": (now - timedelta(seconds=len(values) - i)).isoformat(),
                "value": value,
            }
            for i, value in enumerate(values)
        ]
    }


async def test_batch_flows_from_api_through_kafka_into_postgres(
    kafka_settings: Settings,
    kafka_client: AsyncClient,
    kafka_publisher: KafkaPublisher,
    session_factory: async_sessionmaker[AsyncSession],
    make_tenant: TenantFactory,
) -> None:
    tenant = await make_tenant()

    accepted = await kafka_client.post(
        "/v1/ingest/metrics",
        json=_body(120.0, 350.0, 800.0),
        headers={**tenant.auth_headers, "Idempotency-Key": "e2e-1"},
    )
    assert accepted.status_code == 202

    await _consume(kafka_settings, session_factory, kafka_publisher, expected=1)

    read = await kafka_client.get(URL, headers=tenant.auth_headers)
    assert read.status_code == 200
    [series] = read.json()["series"]
    assert [p["value"] for p in series["points"]] == [120.0, 350.0, 800.0]


async def test_redelivered_batch_is_stored_once(
    kafka_settings: Settings,
    kafka_client: AsyncClient,
    kafka_publisher: KafkaPublisher,
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    make_tenant: TenantFactory,
) -> None:
    tenant = await make_tenant()
    body = _body(1.0, 2.0)
    headers = {**tenant.auth_headers, "Idempotency-Key": "client-retry"}

    # A client retry after a timeout: both requests reach Kafka.
    first = await kafka_client.post("/v1/ingest/metrics", json=body, headers=headers)
    retry = await kafka_client.post("/v1/ingest/metrics", json=body, headers=headers)
    assert first.json()["batch_id"] == retry.json()["batch_id"]

    await _consume(kafka_settings, session_factory, kafka_publisher, expected=2)

    project = tenant.project.id
    batches = await db_session.scalar(select(func.count()).where(IngestBatch.project_id == project))
    points = await db_session.scalar(select(func.count()).where(MetricPoint.project_id == project))
    assert (batches, points) == (1, 2)


async def test_poison_message_goes_to_the_dead_letter_topic_and_does_not_block(
    kafka_settings: Settings,
    kafka_client: AsyncClient,
    kafka_publisher: KafkaPublisher,
    session_factory: async_sessionmaker[AsyncSession],
    make_tenant: TenantFactory,
) -> None:
    tenant = await make_tenant()
    metrics_topic = topic_name(kafka_settings, METRICS)
    await kafka_publisher.publish(metrics_topic, key=b"bad", value=b"not json at all")
    await kafka_client.post(
        "/v1/ingest/metrics",
        json=_body(5.0),
        headers={**tenant.auth_headers, "Idempotency-Key": "after-poison"},
    )

    await _consume(kafka_settings, session_factory, kafka_publisher, expected=2)

    [dead] = await asyncio.to_thread(
        read_topic_from_start, kafka_settings, topic_name(kafka_settings, METRICS_DLQ), limit=10
    )
    assert dead.value == b"not json at all"
    assert dead.headers["dlq.reason"] == "invalid_message"
    assert dead.headers["dlq.source"].startswith(f"{metrics_topic}/")
    # The valid batch behind the poison message was still stored.
    read = await kafka_client.get(URL, headers=tenant.auth_headers)
    assert [p["value"] for p in read.json()["series"][0]["points"]] == [5.0]


async def test_missing_topics_reports_only_absent_topics(
    kafka_settings: Settings, kafka_publisher: KafkaPublisher
) -> None:
    existing = topic_name(kafka_settings, METRICS)

    missing = await kafka_publisher.missing_topics(
        [existing, "does-not-exist-anywhere"], timeout_seconds=5
    )

    assert missing == {"does-not-exist-anywhere"}
