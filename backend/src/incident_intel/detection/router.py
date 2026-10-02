"""Read endpoints for anomalies, for API keys (``router``) and signed-in users
(``project_router``)."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import AwareDatetime, BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from incident_intel.api.deps import require_project_role, require_scope
from incident_intel.core.errors import InvalidInputError
from incident_intel.db.session import get_session
from incident_intel.detection.queries import AnomalyRow, StatusFilter, list_anomalies
from incident_intel.telemetry.messages import NAME_PATTERN
from incident_intel.tenancy.access import ProjectAccess
from incident_intel.tenancy.api_keys import ApiKeyScope
from incident_intel.tenancy.context import TenantContext, TenantScope
from incident_intel.tenancy.roles import Role

router = APIRouter(prefix="/v1", tags=["detection"])
project_router = APIRouter(prefix="/v1/projects/{project_id}", tags=["console"])

MAX_RANGE = timedelta(days=31)
DEFAULT_RANGE = timedelta(hours=24)


class AnomalyOut(BaseModel):
    id: uuid.UUID
    service: str
    metric: str
    attributes: dict[str, str]
    unit: str | None
    detector: str
    status: str
    # A documented rule on the score (see README), not a measured probability.
    severity: str
    direction: str
    started_at: datetime
    detected_at: datetime
    last_anomalous_at: datetime
    ended_at: datetime | None
    closed_reason: str | None
    peak_value: float
    peak_at: datetime
    # Distance from normal in units of the baseline's spread. Not a probability.
    peak_score: float
    # What "normal" was when the anomaly opened.
    baseline_center: float
    baseline_spread: float
    point_count: int
    # The incident this anomaly was grouped into, if any.
    incident_id: uuid.UUID | None


class AnomalyListResponse(BaseModel):
    start: datetime
    end: datetime
    # Open anomalies first, then newest first.
    anomalies: list[AnomalyOut]


def anomaly_out(row: AnomalyRow) -> AnomalyOut:
    return AnomalyOut(
        id=row.anomaly.id,
        service=row.service_name,
        metric=row.series.name,
        attributes=row.series.attributes,
        unit=row.series.unit,
        detector=row.anomaly.detector,
        status=row.anomaly.status,
        severity=row.anomaly.severity,
        direction=row.anomaly.direction,
        started_at=row.anomaly.started_at,
        detected_at=row.anomaly.detected_at,
        last_anomalous_at=row.anomaly.last_anomalous_at,
        ended_at=row.anomaly.ended_at,
        closed_reason=row.anomaly.closed_reason,
        peak_value=row.anomaly.peak_value,
        peak_at=row.anomaly.peak_at,
        peak_score=row.anomaly.peak_score,
        baseline_center=row.anomaly.baseline_center,
        baseline_spread=row.anomaly.baseline_spread,
        point_count=row.anomaly.point_count,
        incident_id=row.anomaly.incident_id,
    )


ReadContext = Annotated[TenantContext, Depends(require_scope(ApiKeyScope.TELEMETRY_READ))]
ViewerAccess = Annotated[ProjectAccess, Depends(require_project_role(Role.VIEWER))]
Session = Annotated[AsyncSession, Depends(get_session)]
RangeStart = Annotated[AwareDatetime | None, Query(description="Default: end - 24h")]
RangeEnd = Annotated[AwareDatetime | None, Query(description="Default: now")]
Status = Annotated[StatusFilter, Query()]
ServiceFilter = Annotated[str | None, Query(alias="service", pattern=NAME_PATTERN)]
MetricFilter = Annotated[str | None, Query(alias="metric", pattern=NAME_PATTERN)]
Limit = Annotated[int, Query(ge=1, le=500)]


async def _anomalies(
    session: AsyncSession,
    scope: TenantScope,
    *,
    start: datetime | None,
    end: datetime | None,
    status: StatusFilter,
    service: str | None,
    metric: str | None,
    limit: int,
) -> AnomalyListResponse:
    end = end or datetime.now(UTC)
    start = start or end - DEFAULT_RANGE
    if start >= end:
        raise InvalidInputError("start must be before end.")
    if end - start > MAX_RANGE:
        raise InvalidInputError("The time range may be at most 31 days.")
    rows = await list_anomalies(
        session,
        scope,
        start=start,
        end=end,
        status=status,
        service_name=service,
        metric_name=metric,
        limit=limit,
    )
    return AnomalyListResponse(start=start, end=end, anomalies=[anomaly_out(row) for row in rows])


@router.get("/anomalies", response_model=AnomalyListResponse)
async def get_anomalies(
    ctx: ReadContext,
    session: Session,
    start: RangeStart = None,
    end: RangeEnd = None,
    status: Status = "all",
    service: ServiceFilter = None,
    metric: MetricFilter = None,
    limit: Limit = 100,
) -> AnomalyListResponse:
    """Anomalies overlapping ``[start, end)`` (default: the last 24 hours)."""
    return await _anomalies(
        session, ctx, start=start, end=end, status=status, service=service, metric=metric,
        limit=limit,
    )  # fmt: skip


@project_router.get("/anomalies", response_model=AnomalyListResponse)
async def get_project_anomalies(
    access: ViewerAccess,
    session: Session,
    start: RangeStart = None,
    end: RangeEnd = None,
    status: Status = "all",
    service: ServiceFilter = None,
    metric: MetricFilter = None,
    limit: Limit = 100,
) -> AnomalyListResponse:
    """Anomalies in a project the user can view."""
    return await _anomalies(
        session, access.scope, start=start, end=end, status=status, service=service,
        metric=metric, limit=limit,
    )  # fmt: skip
