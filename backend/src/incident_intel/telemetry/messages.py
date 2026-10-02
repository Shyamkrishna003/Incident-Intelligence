"""The telemetry message contract shared by the ingestion API (producer) and consumers.

The constrained types below are the single definition of what a valid service name, metric
name, unit, and attribute set look like; both the HTTP API and the consumers validate
against them.
"""

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

NAME_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,127}$"

ServiceName = Annotated[str, StringConstraints(pattern=NAME_PATTERN)]
MetricName = Annotated[str, StringConstraints(pattern=NAME_PATTERN)]
Unit = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9%/._-]{1,32}$")]
AttributeKey = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{1,64}$")]
AttributeValue = Annotated[str, StringConstraints(max_length=256)]
Attributes = Annotated[dict[AttributeKey, AttributeValue], Field(max_length=16)]


# Strict: a JSON number (integers allowed). Rejects "350", true, NaN, and infinities, which
# lax parsing would otherwise coerce into floats.
FiniteFloat = Annotated[float, Field(strict=True, allow_inf_nan=False)]


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def attributes_hash(attributes: Mapping[str, str]) -> str:
    """Stable identity of an attribute set, independent of key order."""
    return hashlib.sha256(canonical_json(dict(attributes))).hexdigest()


# Log severities, ordered. The numbers follow OpenTelemetry's severity ranges, so
# "at least this severe" is a simple comparison and OTLP ingestion can map onto them later.
Severity = Literal["trace", "debug", "info", "warn", "error", "fatal"]
SEVERITY_NUMBERS: dict[str, int] = {
    "trace": 1,
    "debug": 5,
    "info": 9,
    "warn": 13,
    "error": 17,
    "fatal": 21,
}
SEVERITY_NAMES: dict[int, str] = {number: name for name, number in SEVERITY_NUMBERS.items()}

LogText = Annotated[str, StringConstraints(min_length=1, max_length=8192)]
TraceId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
Version = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")]
CommitSha = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{7,40}$")]
Environment = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=128)]
Description = Annotated[str, StringConstraints(min_length=1, max_length=1000)]


class BatchEnvelope(BaseModel):
    """Fields every ingestion batch carries. Tenant ids come from the verified API key."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    batch_id: uuid.UUID
    organization_id: uuid.UUID
    project_id: uuid.UUID
    api_key_id: uuid.UUID
    idempotency_key: str = Field(max_length=128)
    # Hash of the normalized items: detects an idempotency key reused for different data.
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    received_at: AwareDatetime


class MetricPointMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    service: ServiceName
    metric: MetricName
    unit: Unit | None
    timestamp: AwareDatetime
    value: FiniteFloat
    attributes: Attributes


class MetricBatchMessage(BatchEnvelope):
    """One accepted batch of metric points."""

    points: list[MetricPointMessage] = Field(min_length=1)


class LogRecordMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    service: ServiceName
    timestamp: AwareDatetime
    severity: Severity
    message: LogText
    attributes: Attributes
    trace_id: TraceId | None


class LogBatchMessage(BatchEnvelope):
    """One accepted batch of log records."""

    records: list[LogRecordMessage] = Field(min_length=1)


class DeploymentMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    service: ServiceName
    version: Version
    deployed_at: AwareDatetime
    commit_sha: CommitSha | None
    environment: Environment | None
    deployed_by: ShortText | None
    description: Description | None


class DeploymentBatchMessage(BatchEnvelope):
    """One accepted batch of deployment events."""

    deployments: list[DeploymentMessage] = Field(min_length=1)


class MetricsStoredEvent(BaseModel):
    """Published after a metric batch is stored: "these series have new points".

    Carries ids only. Consumers read the points themselves from PostgreSQL, so the event
    can be delivered more than once, or late, without harm.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    organization_id: uuid.UUID
    project_id: uuid.UUID
    batch_id: uuid.UUID
    series_ids: list[uuid.UUID]


def content_hash(items: Sequence[BaseModel]) -> str:
    """Fingerprint of a batch's normalized items."""
    return hashlib.sha256(
        canonical_json([item.model_dump(mode="json") for item in items])
    ).hexdigest()


def points_content_hash(points: list[MetricPointMessage]) -> str:
    return content_hash(points)
