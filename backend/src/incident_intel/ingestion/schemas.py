import uuid
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from incident_intel.telemetry.messages import (
    Attributes,
    FiniteFloat,
    MetricName,
    ServiceName,
    Unit,
)

MAX_POINTS_PER_BATCH = 1000


class MetricPointIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: ServiceName
    metric: MetricName
    unit: Unit | None = None
    timestamp: AwareDatetime
    value: FiniteFloat
    attributes: Attributes = Field(default_factory=dict)


class MetricBatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    points: list[MetricPointIn] = Field(min_length=1, max_length=MAX_POINTS_PER_BATCH)


class IngestAccepted(BaseModel):
    status: Literal["accepted"] = "accepted"
    # Stable for a given Idempotency-Key: retries return the same id.
    batch_id: uuid.UUID
    accepted_points: int
