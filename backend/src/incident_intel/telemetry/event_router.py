"""Read endpoints for logs and deployments, for API keys (``router``) and signed-in users
(``project_router``). Both end up in the same tenant-scoped queries."""

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query
from pydantic import AwareDatetime
from sqlalchemy.ext.asyncio import AsyncSession

from incident_intel.api.deps import require_project_role, require_scope
from incident_intel.core.errors import InvalidInputError
from incident_intel.db.session import get_session
from incident_intel.telemetry.event_queries import list_deployments, read_logs
from incident_intel.telemetry.messages import (
    NAME_PATTERN,
    SEVERITY_NAMES,
    SEVERITY_NUMBERS,
    Severity,
)
from incident_intel.telemetry.schemas import (
    DeploymentListResponse,
    DeploymentOut,
    LogListResponse,
    LogRecordOut,
)
from incident_intel.tenancy.access import ProjectAccess
from incident_intel.tenancy.api_keys import ApiKeyScope
from incident_intel.tenancy.context import TenantContext, TenantScope
from incident_intel.tenancy.roles import Role

router = APIRouter(prefix="/v1", tags=["telemetry"])
project_router = APIRouter(prefix="/v1/projects/{project_id}", tags=["console"])

MAX_LOG_RANGE = timedelta(hours=24)
DEFAULT_LOG_RANGE = timedelta(hours=1)
MAX_DEPLOYMENT_RANGE = timedelta(days=31)
DEFAULT_DEPLOYMENT_RANGE = timedelta(days=7)

ReadContext = Annotated[TenantContext, Depends(require_scope(ApiKeyScope.TELEMETRY_READ))]
ViewerAccess = Annotated[ProjectAccess, Depends(require_project_role(Role.VIEWER))]
Session = Annotated[AsyncSession, Depends(get_session)]

ServiceName = Annotated[str, Path(pattern=NAME_PATTERN)]
RangeStart = Annotated[AwareDatetime | None, Query(description="Inclusive.")]
RangeEnd = Annotated[AwareDatetime | None, Query(description="Exclusive. Default: now")]
MinSeverity = Annotated[
    Severity | None, Query(alias="severity", description="Only this severity and above.")
]
Search = Annotated[
    str | None,
    Query(alias="q", min_length=1, max_length=200, description="Text the message must contain."),
]
LogLimit = Annotated[int, Query(ge=1, le=1000)]
ServiceFilter = Annotated[str | None, Query(alias="service", pattern=NAME_PATTERN)]
DeploymentLimit = Annotated[int, Query(ge=1, le=500)]


def _resolve_range(
    start: datetime | None, end: datetime | None, *, default: timedelta, maximum: timedelta
) -> tuple[datetime, datetime]:
    end = end or datetime.now(UTC)
    start = start or end - default
    if start >= end:
        raise InvalidInputError("start must be before end.")
    if end - start > maximum:
        raise InvalidInputError(f"The time range may be at most {maximum.days or 1} day(s).")
    return start, end


async def _logs(
    session: AsyncSession,
    scope: TenantScope,
    *,
    service: str,
    start: datetime | None,
    end: datetime | None,
    severity: Severity | None,
    search: str | None,
    limit: int,
) -> LogListResponse:
    start, end = _resolve_range(start, end, default=DEFAULT_LOG_RANGE, maximum=MAX_LOG_RANGE)
    page = await read_logs(
        session,
        scope,
        service_name=service,
        start=start,
        end=end,
        min_severity=SEVERITY_NUMBERS[severity] if severity else 0,
        search=search,
        limit=limit,
    )
    return LogListResponse(
        service=service,
        start=start,
        end=end,
        truncated=page.truncated,
        records=[
            LogRecordOut(
                timestamp=record.ts,
                severity=SEVERITY_NAMES.get(record.severity, "info"),
                message=record.message,
                attributes=record.attributes,
                trace_id=record.trace_id,
            )
            for record in page.records
        ],
    )


async def _deployments(
    session: AsyncSession,
    scope: TenantScope,
    *,
    service: str | None,
    start: datetime | None,
    end: datetime | None,
    limit: int,
) -> DeploymentListResponse:
    start, end = _resolve_range(
        start, end, default=DEFAULT_DEPLOYMENT_RANGE, maximum=MAX_DEPLOYMENT_RANGE
    )
    rows = await list_deployments(
        session, scope, service_name=service, start=start, end=end, limit=limit
    )
    return DeploymentListResponse(
        start=start,
        end=end,
        deployments=[
            DeploymentOut(
                id=row.deployment.id,
                service=row.service_name,
                version=row.deployment.version,
                deployed_at=row.deployment.deployed_at,
                commit_sha=row.deployment.commit_sha,
                environment=row.deployment.environment,
                deployed_by=row.deployment.deployed_by,
                description=row.deployment.description,
            )
            for row in rows
        ],
    )


# --- API key callers ------------------------------------------------------------------


@router.get("/services/{service}/logs", response_model=LogListResponse)
async def get_logs(
    ctx: ReadContext,
    session: Session,
    service: ServiceName,
    start: RangeStart = None,
    end: RangeEnd = None,
    severity: MinSeverity = None,
    search: Search = None,
    limit: LogLimit = 100,
) -> LogListResponse:
    """A service's log records in ``[start, end)`` (default: the last hour), newest first."""
    return await _logs(
        session, ctx, service=service, start=start, end=end, severity=severity,
        search=search, limit=limit,
    )  # fmt: skip


@router.get("/deployments", response_model=DeploymentListResponse)
async def get_deployments(
    ctx: ReadContext,
    session: Session,
    service: ServiceFilter = None,
    start: RangeStart = None,
    end: RangeEnd = None,
    limit: DeploymentLimit = 100,
) -> DeploymentListResponse:
    """Deployments in ``[start, end)`` (default: the last 7 days), newest first."""
    return await _deployments(session, ctx, service=service, start=start, end=end, limit=limit)


# --- Signed-in users --------------------------------------------------------------------


@project_router.get("/services/{service}/logs", response_model=LogListResponse)
async def get_project_logs(
    access: ViewerAccess,
    session: Session,
    service: ServiceName,
    start: RangeStart = None,
    end: RangeEnd = None,
    severity: MinSeverity = None,
    search: Search = None,
    limit: LogLimit = 100,
) -> LogListResponse:
    """A service's log records, in a project the user can view."""
    return await _logs(
        session, access.scope, service=service, start=start, end=end, severity=severity,
        search=search, limit=limit,
    )  # fmt: skip


@project_router.get("/deployments", response_model=DeploymentListResponse)
async def get_project_deployments(
    access: ViewerAccess,
    session: Session,
    service: ServiceFilter = None,
    start: RangeStart = None,
    end: RangeEnd = None,
    limit: DeploymentLimit = 100,
) -> DeploymentListResponse:
    """Deployments in a project the user can view."""
    return await _deployments(
        session, access.scope, service=service, start=start, end=end, limit=limit
    )
