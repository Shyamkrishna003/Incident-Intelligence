"""Tenant-scoped reads of anomalies."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import case, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from incident_intel.detection.models import Anomaly
from incident_intel.telemetry.models import MetricSeries, Service
from incident_intel.telemetry.queries import get_service
from incident_intel.tenancy.context import TenantScope

StatusFilter = Literal["open", "closed", "all"]


@dataclass(frozen=True)
class AnomalyRow:
    anomaly: Anomaly
    series: MetricSeries
    service_name: str


async def list_anomalies(
    session: AsyncSession,
    ctx: TenantScope,
    *,
    start: datetime,
    end: datetime,
    status: StatusFilter,
    service_name: str | None,
    metric_name: str | None,
    limit: int,
) -> list[AnomalyRow]:
    """Anomalies that overlap ``[start, end)``: open ones first, then newest first."""
    query = (
        select(Anomaly, MetricSeries, Service.name)
        .join(
            MetricSeries,
            (MetricSeries.id == Anomaly.series_id)
            & (MetricSeries.project_id == Anomaly.project_id),
        )
        .join(
            Service,
            (Service.id == MetricSeries.service_id) & (Service.project_id == Anomaly.project_id),
        )
        .where(
            Anomaly.organization_id == ctx.organization_id,
            Anomaly.project_id == ctx.project_id,
            Anomaly.started_at < end,
            or_(Anomaly.status == "open", Anomaly.ended_at >= start),
        )
    )
    if status != "all":
        query = query.where(Anomaly.status == status)
    if service_name is not None:
        service = await get_service(session, ctx, service_name)
        query = query.where(MetricSeries.service_id == service.id)
    if metric_name is not None:
        query = query.where(MetricSeries.name == metric_name)
    rows = await session.execute(
        query.order_by(
            case((Anomaly.status == "open", 0), else_=1), Anomaly.started_at.desc(), Anomaly.id
        ).limit(limit)
    )
    return [AnomalyRow(anomaly, series, name) for anomaly, series, name in rows]
