"""Incidents and service dependencies, for API keys (``router``) and signed-in users
(``project_router``)."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from incident_intel.api.deps import require_project_role, require_scope
from incident_intel.audit.service import Actor
from incident_intel.core.errors import InvalidInputError
from incident_intel.db.session import get_session
from incident_intel.detection.router import AnomalyOut, anomaly_out
from incident_intel.incidents.dependencies import list_dependencies, replace_dependencies
from incident_intel.incidents.queries import (
    IncidentStatusFilter,
    IncidentSummary,
    get_incident,
    list_incidents,
)
from incident_intel.telemetry.messages import ServiceName
from incident_intel.tenancy.access import ProjectAccess
from incident_intel.tenancy.api_keys import ApiKeyScope
from incident_intel.tenancy.context import TenantContext, TenantScope
from incident_intel.tenancy.roles import Role

router = APIRouter(prefix="/v1", tags=["incidents"])
project_router = APIRouter(prefix="/v1/projects/{project_id}", tags=["console"])

MAX_RANGE = timedelta(days=31)
DEFAULT_RANGE = timedelta(days=7)
MAX_DEPENDENCIES = 500


class IncidentOut(BaseModel):
    id: uuid.UUID
    title: str
    status: str
    # The worst severity among its anomalies: a rule on detector scores, not a probability.
    severity: str
    started_at: datetime
    detected_at: datetime
    last_activity_at: datetime
    resolved_at: datetime | None
    merged_into_id: uuid.UUID | None
    # In the order the services joined the incident.
    services: list[str]
    anomaly_count: int
    open_anomaly_count: int


class IncidentListResponse(BaseModel):
    start: datetime
    end: datetime
    # Open incidents first, then newest first.
    incidents: list[IncidentOut]


class CandidateDeploymentOut(BaseModel):
    """A deployment of one of the incident's services, shortly before or during it.
    A candidate for "what changed"; not a finding that it caused the incident."""

    id: uuid.UUID
    service: str
    version: str
    deployed_at: datetime
    commit_sha: str | None
    environment: str | None
    deployed_by: str | None
    description: str | None
    # "before" or "during", relative to the incident's start.
    timing: str


class TimelineEventOut(BaseModel):
    ts: datetime
    kind: str
    # The facts behind the entry, including the rule that decided it.
    details: dict[str, Any]


class DependencyOut(BaseModel):
    service: str
    depends_on: str


class IncidentDetailResponse(IncidentOut):
    anomalies: list[AnomalyOut]
    candidate_deployments: list[CandidateDeploymentOut]
    timeline: list[TimelineEventOut]
    # Declared dependencies between this incident's services.
    dependencies: list[DependencyOut]


class DependencyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: ServiceName
    depends_on: ServiceName


class DependenciesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dependencies: list[DependencyIn] = Field(max_length=MAX_DEPENDENCIES)


class DependenciesResponse(BaseModel):
    dependencies: list[DependencyOut]


ReadContext = Annotated[TenantContext, Depends(require_scope(ApiKeyScope.TELEMETRY_READ))]
WriteContext = Annotated[TenantContext, Depends(require_scope(ApiKeyScope.INGEST_WRITE))]
ViewerAccess = Annotated[ProjectAccess, Depends(require_project_role(Role.VIEWER))]
AdminAccess = Annotated[ProjectAccess, Depends(require_project_role(Role.ADMIN))]
Session = Annotated[AsyncSession, Depends(get_session)]
RangeStart = Annotated[AwareDatetime | None, Query(description="Default: end - 7 days")]
RangeEnd = Annotated[AwareDatetime | None, Query(description="Default: now")]
Status = Annotated[IncidentStatusFilter, Query()]
Limit = Annotated[int, Query(ge=1, le=200)]


def _incident_out(summary: IncidentSummary) -> IncidentOut:
    incident = summary.incident
    return IncidentOut(
        id=incident.id,
        title=incident.title,
        status=incident.status,
        severity=incident.severity,
        started_at=incident.started_at,
        detected_at=incident.detected_at,
        last_activity_at=incident.last_activity_at,
        resolved_at=incident.resolved_at,
        merged_into_id=incident.merged_into_id,
        services=summary.services,
        anomaly_count=summary.anomaly_count,
        open_anomaly_count=summary.open_anomaly_count,
    )


async def _list(
    session: AsyncSession,
    scope: TenantScope,
    start: datetime | None,
    end: datetime | None,
    status: IncidentStatusFilter,
    limit: int,
) -> IncidentListResponse:
    end = end or datetime.now(UTC)
    start = start or end - DEFAULT_RANGE
    if start >= end:
        raise InvalidInputError("start must be before end.")
    if end - start > MAX_RANGE:
        raise InvalidInputError("The time range may be at most 31 days.")
    summaries = await list_incidents(
        session, scope, start=start, end=end, status=status, limit=limit
    )
    return IncidentListResponse(
        start=start, end=end, incidents=[_incident_out(summary) for summary in summaries]
    )


async def _detail(
    session: AsyncSession, scope: TenantScope, incident_id: uuid.UUID
) -> IncidentDetailResponse:
    detail = await get_incident(session, scope, incident_id)
    return IncidentDetailResponse(
        **_incident_out(detail.summary).model_dump(),
        anomalies=[anomaly_out(row) for row in detail.anomalies],
        candidate_deployments=[
            CandidateDeploymentOut(
                id=item.deployment.id,
                service=item.service_name,
                version=item.deployment.version,
                deployed_at=item.deployment.deployed_at,
                commit_sha=item.deployment.commit_sha,
                environment=item.deployment.environment,
                deployed_by=item.deployment.deployed_by,
                description=item.deployment.description,
                timing=item.timing,
            )
            for item in detail.candidate_deployments
        ],
        timeline=[
            TimelineEventOut(ts=event.ts, kind=event.kind, details=event.details)
            for event in detail.timeline
        ],
        dependencies=[
            DependencyOut(service=service, depends_on=depends_on)
            for service, depends_on in detail.dependencies
        ],
    )


def _dependencies_out(edges: list[tuple[str, str]]) -> DependenciesResponse:
    return DependenciesResponse(
        dependencies=[DependencyOut(service=a, depends_on=b) for a, b in edges]
    )


def _edges(body: DependenciesIn) -> list[tuple[str, str]]:
    return [(item.service, item.depends_on) for item in body.dependencies]


# --- API key callers ------------------------------------------------------------------


@router.get("/incidents", response_model=IncidentListResponse)
async def get_incidents(
    ctx: ReadContext,
    session: Session,
    start: RangeStart = None,
    end: RangeEnd = None,
    status: Status = "all",
    limit: Limit = 50,
) -> IncidentListResponse:
    """Incidents overlapping ``[start, end)`` (default: the last 7 days)."""
    return await _list(session, ctx, start, end, status, limit)


@router.get("/incidents/{incident_id}", response_model=IncidentDetailResponse)
async def get_incident_detail(
    incident_id: uuid.UUID, ctx: ReadContext, session: Session
) -> IncidentDetailResponse:
    """One incident with its anomalies, candidate deployments, and timeline."""
    return await _detail(session, ctx, incident_id)


@router.get("/dependencies", response_model=DependenciesResponse)
async def get_dependencies(ctx: ReadContext, session: Session) -> DependenciesResponse:
    return _dependencies_out(await list_dependencies(session, ctx))


@router.put("/dependencies", response_model=DependenciesResponse)
async def put_dependencies(
    body: DependenciesIn, ctx: WriteContext, session: Session
) -> DependenciesResponse:
    """Replace the project's declared service dependencies (for example from CI/CD)."""
    actor = Actor(type="api_key", id=str(ctx.principal.api_key_id))
    return _dependencies_out(await replace_dependencies(session, ctx, _edges(body), actor=actor))


# --- Signed-in users --------------------------------------------------------------------


@project_router.get("/incidents", response_model=IncidentListResponse)
async def get_project_incidents(
    access: ViewerAccess,
    session: Session,
    start: RangeStart = None,
    end: RangeEnd = None,
    status: Status = "all",
    limit: Limit = 50,
) -> IncidentListResponse:
    return await _list(session, access.scope, start, end, status, limit)


@project_router.get("/incidents/{incident_id}", response_model=IncidentDetailResponse)
async def get_project_incident_detail(
    incident_id: uuid.UUID, access: ViewerAccess, session: Session
) -> IncidentDetailResponse:
    return await _detail(session, access.scope, incident_id)


@project_router.get("/dependencies", response_model=DependenciesResponse)
async def get_project_dependencies(access: ViewerAccess, session: Session) -> DependenciesResponse:
    return _dependencies_out(await list_dependencies(session, access.scope))


@project_router.put("/dependencies", response_model=DependenciesResponse)
async def put_project_dependencies(
    body: DependenciesIn, access: AdminAccess, session: Session
) -> DependenciesResponse:
    """Replace the project's declared service dependencies. Needs the admin role."""
    return _dependencies_out(
        await replace_dependencies(session, access.scope, _edges(body), actor=access.actor)
    )
