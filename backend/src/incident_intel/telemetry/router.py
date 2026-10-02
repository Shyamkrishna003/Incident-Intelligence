"""Read endpoints for stored telemetry.

The same data is served to two kinds of caller, through two routers:
- ``router``: a program holding a project API key (the project comes from the key).
- ``project_router``: a signed-in user (the project is in the path and is checked against
  the user's organization membership).
Both end up in the same tenant-scoped queries.
"""

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query
from pydantic import AwareDatetime
from sqlalchemy.ext.asyncio import AsyncSession

from incident_intel.api.deps import require_project_role, require_scope
from incident_intel.core.errors import InvalidInputError
from incident_intel.db.session import get_session
from incident_intel.telemetry.messages import NAME_PATTERN
from incident_intel.telemetry.queries import list_metrics, list_services, read_metric_range
from incident_intel.telemetry.schemas import (
    MetricListResponse,
    MetricOut,
    MetricRangeResponse,
    PointOut,
    SeriesOut,
    ServiceListResponse,
    ServiceOut,
)
from incident_intel.tenancy.access import ProjectAccess
from incident_intel.tenancy.api_keys import ApiKeyScope
from incident_intel.tenancy.context import TenantContext, TenantScope
from incident_intel.tenancy.roles import Role

router = APIRouter(prefix="/v1/services", tags=["telemetry"])
project_router = APIRouter(prefix="/v1/projects/{project_id}/services", tags=["console"])

MAX_RANGE = timedelta(hours=24)
DEFAULT_RANGE = timedelta(hours=1)

ReadContext = Annotated[TenantContext, Depends(require_scope(ApiKeyScope.TELEMETRY_READ))]
ViewerAccess = Annotated[ProjectAccess, Depends(require_project_role(Role.VIEWER))]
Session = Annotated[AsyncSession, Depends(get_session)]

ServiceName = Annotated[str, Path(pattern=NAME_PATTERN)]
MetricName = Annotated[str, Path(pattern=NAME_PATTERN)]
ServiceLimit = Annotated[int, Query(ge=1, le=1000)]
RangeStart = Annotated[AwareDatetime | None, Query(description="Inclusive. Default: end - 1h")]
RangeEnd = Annotated[AwareDatetime | None, Query(description="Exclusive. Default: now")]
PointLimit = Annotated[int, Query(ge=1, le=10_000)]


async def _services(session: AsyncSession, scope: TenantScope, limit: int) -> ServiceListResponse:
    services = await list_services(session, scope, limit=limit)
    return ServiceListResponse(
        services=[ServiceOut(id=s.id, name=s.name, created_at=s.created_at) for s in services]
    )


async def _metrics(
    session: AsyncSession, scope: TenantScope, service: str, limit: int
) -> MetricListResponse:
    metrics = await list_metrics(session, scope, service_name=service, limit=limit)
    return MetricListResponse(
        service=service,
        metrics=[MetricOut(name=m.name, unit=m.unit, series_count=m.series_count) for m in metrics],
    )


async def _metric_range(
    session: AsyncSession,
    scope: TenantScope,
    *,
    service: str,
    metric: str,
    start: datetime | None,
    end: datetime | None,
    limit: int,
) -> MetricRangeResponse:
    end = end or datetime.now(UTC)
    start = start or end - DEFAULT_RANGE
    if start >= end:
        raise InvalidInputError("start must be before end.")
    if end - start > MAX_RANGE:
        raise InvalidInputError("The time range may be at most 24 hours.")

    result = await read_metric_range(
        session,
        scope,
        service_name=service,
        metric_name=metric,
        start=start,
        end=end,
        limit=limit,
    )
    return MetricRangeResponse(
        service=service,
        metric=metric,
        start=start,
        end=end,
        truncated=result.truncated,
        series=[
            SeriesOut(
                attributes=item.series.attributes,
                unit=item.series.unit,
                points=[PointOut(timestamp=ts, value=value) for ts, value in item.points],
            )
            for item in result.series
        ],
    )


# --- API key callers ------------------------------------------------------------------


@router.get("", response_model=ServiceListResponse)
async def get_services(
    ctx: ReadContext, session: Session, limit: ServiceLimit = 100
) -> ServiceListResponse:
    """Services that have sent telemetry to this project, by name."""
    return await _services(session, ctx, limit)


@router.get("/{service}/metrics", response_model=MetricListResponse)
async def get_metrics(
    ctx: ReadContext, session: Session, service: ServiceName, limit: ServiceLimit = 500
) -> MetricListResponse:
    """The metrics a service has reported, by name."""
    return await _metrics(session, ctx, service, limit)


@router.get("/{service}/metrics/{metric}", response_model=MetricRangeResponse)
async def get_metric_range(
    ctx: ReadContext,
    session: Session,
    service: ServiceName,
    metric: MetricName,
    start: RangeStart = None,
    end: RangeEnd = None,
    limit: PointLimit = 1000,
) -> MetricRangeResponse:
    """Points for one metric in ``[start, end)``, grouped by attribute set (series).

    Ingestion is asynchronous: points appear shortly after their batch is accepted.
    """
    return await _metric_range(
        session, ctx, service=service, metric=metric, start=start, end=end, limit=limit
    )


# --- Signed-in users --------------------------------------------------------------------


@project_router.get("", response_model=ServiceListResponse)
async def get_project_services(
    access: ViewerAccess, session: Session, limit: ServiceLimit = 100
) -> ServiceListResponse:
    """Services that have sent telemetry to a project the user can view."""
    return await _services(session, access.scope, limit)


@project_router.get("/{service}/metrics", response_model=MetricListResponse)
async def get_project_metrics(
    access: ViewerAccess, session: Session, service: ServiceName, limit: ServiceLimit = 500
) -> MetricListResponse:
    """The metrics a service has reported, in a project the user can view."""
    return await _metrics(session, access.scope, service, limit)


@project_router.get("/{service}/metrics/{metric}", response_model=MetricRangeResponse)
async def get_project_metric_range(
    access: ViewerAccess,
    session: Session,
    service: ServiceName,
    metric: MetricName,
    start: RangeStart = None,
    end: RangeEnd = None,
    limit: PointLimit = 1000,
) -> MetricRangeResponse:
    """Points for one metric in ``[start, end)`` in a project the user can view."""
    return await _metric_range(
        session, access.scope, service=service, metric=metric, start=start, end=end, limit=limit
    )
