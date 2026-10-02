"""The telemetry message contract shared by the ingestion API (producer) and consumers.

The constrained types below are the single definition of what a valid service name, metric
name, unit, and attribute set look like; both the HTTP API and the consumers validate
against them.
"""

import hashlib
import json
import uuid
from collections.abc import Mapping
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


class MetricPointMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    service: ServiceName
    metric: MetricName
    unit: Unit | None
    timestamp: AwareDatetime
    value: FiniteFloat
    attributes: Attributes


class MetricBatchMessage(BaseModel):
    """One accepted ingestion batch. Tenant ids come from the verified API key."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    batch_id: uuid.UUID
    organization_id: uuid.UUID
    project_id: uuid.UUID
    api_key_id: uuid.UUID
    idempotency_key: str = Field(max_length=128)
    # Hash of the normalized points: detects an idempotency key reused for different data.
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    received_at: AwareDatetime
    points: list[MetricPointMessage] = Field(min_length=1)


def points_content_hash(points: list[MetricPointMessage]) -> str:
    return hashlib.sha256(
        canonical_json([point.model_dump(mode="json") for point in points])
    ).hexdigest()
