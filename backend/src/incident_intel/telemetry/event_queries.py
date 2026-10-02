"""Tenant-scoped reads of logs and deployments. Every query filters by the caller's project."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from incident_intel.telemetry.models import Deployment, LogRecord, Service
from incident_intel.telemetry.queries import get_service
from incident_intel.tenancy.context import TenantScope


@dataclass(frozen=True)
class LogPage:
    records: list[LogRecord]
    truncated: bool


def _like_literal(text: str) -> str:
    """Escape LIKE wildcards so the search text is matched literally."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def read_logs(
    session: AsyncSession,
    ctx: TenantScope,
    *,
    service_name: str,
    start: datetime,
    end: datetime,
    min_severity: int,
    search: str | None,
    limit: int,
) -> LogPage:
    """A service's log records in ``[start, end)``, newest first, at most ``limit``."""
    service = await get_service(session, ctx, service_name)
    query = select(LogRecord).where(
        LogRecord.project_id == ctx.project_id,
        LogRecord.service_id == service.id,
        LogRecord.ts >= start,
        LogRecord.ts < end,
        LogRecord.severity >= min_severity,
    )
    if search:
        # A scan bounded by the service and time range above; fine at this volume.
        query = query.where(LogRecord.message.ilike(f"%{_like_literal(search)}%", escape="\\"))
    rows = list(
        await session.scalars(
            query.order_by(LogRecord.ts.desc(), LogRecord.id.desc()).limit(limit + 1)
        )
    )
    return LogPage(records=rows[:limit], truncated=len(rows) > limit)


@dataclass(frozen=True)
class DeploymentRow:
    deployment: Deployment
    service_name: str


async def list_deployments(
    session: AsyncSession,
    ctx: TenantScope,
    *,
    service_name: str | None,
    start: datetime,
    end: datetime,
    limit: int,
) -> list[DeploymentRow]:
    """Deployments in ``[start, end)``, newest first, optionally for one service."""
    query = (
        select(Deployment, Service.name)
        .join(
            Service,
            (Service.id == Deployment.service_id) & (Service.project_id == Deployment.project_id),
        )
        .where(
            Deployment.organization_id == ctx.organization_id,
            Deployment.project_id == ctx.project_id,
            Deployment.deployed_at >= start,
            Deployment.deployed_at < end,
        )
    )
    if service_name is not None:
        # Unknown service -> 404, the same as every other per-service read.
        service = await get_service(session, ctx, service_name)
        query = query.where(Deployment.service_id == service.id)
    rows = await session.execute(
        query.order_by(Deployment.deployed_at.desc(), Deployment.id).limit(limit)
    )
    return [DeploymentRow(deployment=deployment, service_name=name) for deployment, name in rows]
