import uuid
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from incident_intel.telemetry.messages import (
    Attributes,
    CommitSha,
    Description,
    Environment,
    FiniteFloat,
    LogText,
    MetricName,
    ServiceName,
    Severity,
    ShortText,
    TraceId,
    Unit,
    Version,
)

MAX_POINTS_PER_BATCH = 1000
MAX_LOGS_PER_BATCH = 1000
MAX_DEPLOYMENTS_PER_BATCH = 100


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


class LogRecordIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: ServiceName
    timestamp: AwareDatetime
    severity: Severity = "info"
    message: LogText
    attributes: Attributes = Field(default_factory=dict)
    trace_id: TraceId | None = None


class LogBatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    records: list[LogRecordIn] = Field(min_length=1, max_length=MAX_LOGS_PER_BATCH)


class DeploymentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: ServiceName
    version: Version
    deployed_at: AwareDatetime
    commit_sha: CommitSha | None = None
    environment: Environment | None = None
    deployed_by: ShortText | None = None
    description: Description | None = None


class DeploymentBatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deployments: list[DeploymentIn] = Field(min_length=1, max_length=MAX_DEPLOYMENTS_PER_BATCH)


class IngestAccepted(BaseModel):
    status: Literal["accepted"] = "accepted"
    # Stable for a given Idempotency-Key: retries return the same id.
    batch_id: uuid.UUID
    # Number of items (points, log records, or deployments) in the accepted batch.
    accepted: int


class MetricIngestAccepted(IngestAccepted):
    # Same value as `accepted`; kept so existing metric clients keep working.
    accepted_points: int
