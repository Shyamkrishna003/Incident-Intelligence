import uuid
from datetime import datetime

from pydantic import BaseModel


class ServiceOut(BaseModel):
    id: uuid.UUID
    name: str
    created_at: datetime


class ServiceListResponse(BaseModel):
    services: list[ServiceOut]


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
