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
