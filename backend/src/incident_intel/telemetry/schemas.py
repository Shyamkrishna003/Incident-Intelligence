import uuid
from datetime import datetime

from pydantic import BaseModel


class ServiceOut(BaseModel):
    id: uuid.UUID
    name: str
    created_at: datetime


class ServiceListResponse(BaseModel):
    services: list[ServiceOut]


class MetricOut(BaseModel):
    name: str
    unit: str | None
    # One series per distinct attribute set (for example per region).
    series_count: int


class MetricListResponse(BaseModel):
    service: str
    metrics: list[MetricOut]


class PointOut(BaseModel):
    timestamp: datetime
    value: float


class SeriesOut(BaseModel):
    attributes: dict[str, str]
    unit: str | None
    points: list[PointOut]


class MetricRangeResponse(BaseModel):
    service: str
    metric: str
    start: datetime
    end: datetime
    # True when more points exist in the range than `limit` allowed; narrow the range.
    truncated: bool
    series: list[SeriesOut]


class LogRecordOut(BaseModel):
    timestamp: datetime
    severity: str
    # Stored as received from the service: untrusted text.
    message: str
    attributes: dict[str, str]
    trace_id: str | None


class LogListResponse(BaseModel):
    service: str
    start: datetime
    end: datetime
    # True when more records match than `limit` allowed; narrow the range or the filters.
    truncated: bool
    # Newest first.
    records: list[LogRecordOut]


class DeploymentOut(BaseModel):
    id: uuid.UUID
    service: str
    version: str
    deployed_at: datetime
    commit_sha: str | None
    environment: str | None
    deployed_by: str | None
    description: str | None


class DeploymentListResponse(BaseModel):
    start: datetime
    end: datetime
    # Newest first.
    deployments: list[DeploymentOut]
