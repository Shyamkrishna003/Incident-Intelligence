from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query
from pydantic import AwareDatetime
from sqlalchemy.ext.asyncio import AsyncSession

from incident_intel.api.deps import require_scope
from incident_intel.core.errors import InvalidInputError
from incident_intel.db.session import get_session
from incident_intel.telemetry.messages import NAME_PATTERN
from incident_intel.telemetry.queries import list_services, read_metric_range
from incident_intel.telemetry.schemas import (
    MetricRangeResponse,
    PointOut,
    SeriesOut,
    ServiceListResponse,
    ServiceOut,
)
from incident_intel.tenancy.api_keys import ApiKeyScope
from incident_intel.tenancy.context import TenantContext

router = APIRouter(prefix="/v1/services", tags=["telemetry"])

MAX_RANGE = timedelta(hours=24)
DEFAULT_RANGE = timedelta(hours=1)

ReadContext = Annotated[TenantContext, Depends(require_scope(ApiKeyScope.TELEMETRY_READ))]
Session = Annotated[AsyncSession, Depends(get_session)]


@router.get("", response_model=ServiceListResponse)
async def get_services(
    ctx: ReadContext,
    session: Session,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> ServiceListResponse:
    """Services that have sent telemetry to this project, by name."""
    services = await list_services(session, ctx, limit=limit)
    return ServiceListResponse(
        services=[ServiceOut(id=s.id, name=s.name, created_at=s.created_at) for s in services]
    )


@router.get("/{service}/metrics/{metric}", response_model=MetricRangeResponse)
async def get_metric_range(
    ctx: ReadContext,
    session: Session,
    service: Annotated[str, Path(pattern=NAME_PATTERN)],
    metric: Annotated[str, Path(pattern=NAME_PATTERN)],
    start: Annotated[
        AwareDatetime | None, Query(description="Inclusive. Default: end - 1h")
    ] = None,
    end: Annotated[AwareDatetime | None, Query(description="Exclusive. Default: now")] = None,
    limit: Annotated[int, Query(ge=1, le=10_000)] = 1000,
) -> MetricRangeResponse:
    """Points for one metric in ``[start, end)``, grouped by attribute set (series).

    Ingestion is asynchronous: points appear shortly after their batch is accepted.
    """
    end = end or datetime.now(UTC)
    start = start or end - DEFAULT_RANGE
    if start >= end:
        raise InvalidInputError("start must be before end.")
    if end - start > MAX_RANGE:
        raise InvalidInputError("The time range may be at most 24 hours.")

    result = await read_metric_range(
        session, ctx, service_name=service, metric_name=metric, start=start, end=end, limit=limit
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
