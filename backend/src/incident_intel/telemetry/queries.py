"""Tenant-scoped read queries. Every query filters by the caller's project."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from incident_intel.core.errors import NotFoundError
from incident_intel.telemetry.models import MetricPoint, MetricSeries, Service
from incident_intel.tenancy.context import TenantScope


async def list_services(session: AsyncSession, ctx: TenantScope, *, limit: int) -> list[Service]:
    result = await session.scalars(
        select(Service)
        .where(
            Service.organization_id == ctx.organization_id,
            Service.project_id == ctx.project_id,
        )
        .order_by(Service.name)
        .limit(limit)
    )
    return list(result)


async def get_service(session: AsyncSession, ctx: TenantScope, name: str) -> Service:
    service = await session.scalar(
        select(Service).where(
            Service.organization_id == ctx.organization_id,
            Service.project_id == ctx.project_id,
            Service.name == name,
        )
    )
    if service is None:
        raise NotFoundError(f"Service '{name}' not found.")
    return service


@dataclass(frozen=True)
class MetricSummary:
    name: str
    unit: str | None
    series_count: int


async def list_metrics(
    session: AsyncSession, ctx: TenantScope, *, service_name: str, limit: int
) -> list[MetricSummary]:
    """The metrics a service has reported, by name, with how many series each has."""
    service = await get_service(session, ctx, service_name)
    rows = await session.execute(
        select(MetricSeries.name, func.min(MetricSeries.unit), func.count())
        .where(MetricSeries.project_id == ctx.project_id, MetricSeries.service_id == service.id)
        .group_by(MetricSeries.name)
        .order_by(MetricSeries.name)
        .limit(limit)
    )
    return [MetricSummary(name=name, unit=unit, series_count=count) for name, unit, count in rows]


@dataclass(frozen=True)
class SeriesPoints:
    series: MetricSeries
    points: list[tuple[datetime, float]]


@dataclass(frozen=True)
class MetricRange:
    series: list[SeriesPoints]
    truncated: bool


async def read_metric_range(
    session: AsyncSession,
    ctx: TenantScope,
    *,
    service_name: str,
    metric_name: str,
    start: datetime,
    end: datetime,
    limit: int,
) -> MetricRange:
    """Points in ``[start, end)`` for every series of one metric, at most ``limit`` in total."""
    service = await get_service(session, ctx, service_name)
    series = list(
        await session.scalars(
            select(MetricSeries)
            .where(
                MetricSeries.project_id == ctx.project_id,
                MetricSeries.service_id == service.id,
                MetricSeries.name == metric_name,
            )
            .order_by(MetricSeries.attributes_hash)
        )
    )
    if not series:
        raise NotFoundError(f"Metric '{metric_name}' not found for service '{service_name}'.")

    rows = (
        await session.execute(
            select(MetricPoint.series_id, MetricPoint.ts, MetricPoint.value)
            .where(
                MetricPoint.project_id == ctx.project_id,
                MetricPoint.series_id.in_([s.id for s in series]),
                MetricPoint.ts >= start,
                MetricPoint.ts < end,
            )
            .order_by(MetricPoint.series_id, MetricPoint.ts)
            .limit(limit + 1)
        )
    ).all()

    truncated = len(rows) > limit
    by_series: dict[object, list[tuple[datetime, float]]] = {s.id: [] for s in series}
    for series_id, ts, value in rows[:limit]:
        by_series[series_id].append((ts, value))
    return MetricRange(
        series=[SeriesPoints(series=s, points=by_series[s.id]) for s in series],
        truncated=truncated,
    )
