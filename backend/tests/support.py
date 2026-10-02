"""Helpers shared by unit and integration tests."""

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import SecretStr

from incident_intel.core.config import Settings
from incident_intel.streaming.producer import PublishError
from incident_intel.telemetry.messages import (
    MetricBatchMessage,
    MetricPointMessage,
    points_content_hash,
)

TEST_PEPPER = "test-pepper-" + "x" * 40


def make_settings(database_url: str, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": SecretStr(database_url),
        "api_key_pepper": SecretStr(TEST_PEPPER),
        "environment": "test",
        "log_json": True,
        **overrides,
    }
    return Settings(**values)


@dataclass(frozen=True)
class PublishedMessage:
    topic: str
    key: bytes | None
    value: bytes
    headers: dict[str, str]


@dataclass
class FakePublisher:
    """In-memory stand-in for Kafka. Records messages; can simulate outages."""

    fail_publish: bool = False
    unreachable: bool = False
    missing: set[str] = field(default_factory=set)
    messages: list[PublishedMessage] = field(default_factory=list)
    closed: bool = False

    async def publish(
        self,
        topic: str,
        *,
        key: bytes | None,
        value: bytes,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        if self.fail_publish:
            raise PublishError("simulated broker outage")
        self.messages.append(PublishedMessage(topic, key, value, dict(headers or {})))

    async def missing_topics(self, topics: Iterable[str], *, timeout_seconds: float) -> set[str]:
        if self.unreachable:
            raise PublishError("simulated broker outage")
        return set(topics) & self.missing

    async def close(self) -> None:
        self.closed = True


def metric_point(
    timestamp: datetime,
    value: float,
    *,
    service: str = "payment-api",
    metric: str = "http.server.duration.p95",
    unit: str | None = "ms",
    attributes: dict[str, str] | None = None,
) -> MetricPointMessage:
    return MetricPointMessage(
        service=service,
        metric=metric,
        unit=unit,
        timestamp=timestamp,
        value=value,
        attributes=attributes or {},
    )


def metric_batch(
    *,
    organization_id: uuid.UUID,
    project_id: uuid.UUID,
    api_key_id: uuid.UUID,
    points: list[MetricPointMessage],
    received_at: datetime,
    batch_id: uuid.UUID | None = None,
    idempotency_key: str = "test-key",
) -> MetricBatchMessage:
    return MetricBatchMessage(
        batch_id=batch_id or uuid.uuid4(),
        organization_id=organization_id,
        project_id=project_id,
        api_key_id=api_key_id,
        idempotency_key=idempotency_key,
        content_sha256=points_content_hash(points),
        received_at=received_at,
        points=points,
    )
