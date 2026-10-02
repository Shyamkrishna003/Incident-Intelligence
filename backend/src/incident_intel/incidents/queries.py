"""Tenant-scoped reads of incidents."""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import case, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from incident_intel.core.errors import NotFoundError
from incident_intel.detection.models import Anomaly
from incident_intel.detection.queries import AnomalyRow
from incident_intel.incidents.dependencies import NamedEdge, list_dependencies
from incident_intel.incidents.models import Incident, IncidentDeployment, IncidentEvent
from incident_intel.telemetry.models import Deployment, MetricSeries, Service
from incident_intel.tenancy.context import TenantScope

IncidentStatusFilter = Literal["open", "resolved", "all"]


@dataclass(frozen=True)
class IncidentSummary:
    incident: Incident
    # In the order the services joined the incident.
    services: list[str]
    anomaly_count: int
    open_anomaly_count: int


async def _summaries(
    session: AsyncSession, scope: TenantScope, incidents: list[Incident]
) -> list[IncidentSummary]:
    if not incidents:
        return []
    rows = await session.execute(
        select(Anomaly.incident_id, Service.name, Anomaly.status, Anomaly.started_at)
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
            Anomaly.project_id == scope.project_id,
            Anomaly.incident_id.in_([incident.id for incident in incidents]),
        )
        .order_by(Anomaly.started_at, Anomaly.id)
    )
    services: dict[uuid.UUID, dict[str, None]] = {incident.id: {} for incident in incidents}
    counts: dict[uuid.UUID, list[int]] = {incident.id: [0, 0] for incident in incidents}
    for incident_id, service_name, status, _started in rows:
        if incident_id is None:
            continue
        services[incident_id][service_name] = None
        counts[incident_id][0] += 1
        counts[incident_id][1] += status == "open"
    return [
        IncidentSummary(
            incident=incident,
            services=list(services[incident.id]),
            anomaly_count=counts[incident.id][0],
            open_anomaly_count=counts[incident.id][1],
        )
        for incident in incidents
    ]


async def list_incidents(
    session: AsyncSession,
    scope: TenantScope,
    *,
    start: datetime,
    end: datetime,
    status: IncidentStatusFilter,
    limit: int,
) -> list[IncidentSummary]:
    """Incidents overlapping ``[start, end)``: open first, then newest first. Merged
    incidents are left out; their content lives in the incident they were merged into."""
    query = select(Incident).where(
        Incident.organization_id == scope.organization_id,
        Incident.project_id == scope.project_id,
        Incident.status != "merged",
        Incident.started_at < end,
        or_(Incident.status == "open", Incident.resolved_at >= start),
    )
    if status != "all":
        query = query.where(Incident.status == status)
    incidents = list(
        await session.scalars(
            query.order_by(
                case((Incident.status == "open", 0), else_=1),
                Incident.started_at.desc(),
                Incident.id,
            ).limit(limit)
        )
    )
    return await _summaries(session, scope, incidents)


@dataclass(frozen=True)
class CandidateDeployment:
    deployment: Deployment
    service_name: str
    timing: str


@dataclass(frozen=True)
class IncidentDetail:
    summary: IncidentSummary
    anomalies: list[AnomalyRow]
    candidate_deployments: list[CandidateDeployment]
    timeline: list[IncidentEvent]
    # Declared dependencies between the incident's own services.
    dependencies: list[NamedEdge]


async def get_incident(
    session: AsyncSession, scope: TenantScope, incident_id: uuid.UUID
) -> IncidentDetail:
    incident = await session.scalar(
        select(Incident).where(
            Incident.organization_id == scope.organization_id,
            Incident.project_id == scope.project_id,
            Incident.id == incident_id,
        )
    )
    if incident is None:
        raise NotFoundError("Incident not found.")
    [summary] = await _summaries(session, scope, [incident])

    anomaly_rows = await session.execute(
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
        .where(Anomaly.project_id == scope.project_id, Anomaly.incident_id == incident.id)
        .order_by(Anomaly.started_at, Anomaly.id)
    )
    deployment_rows = await session.execute(
        select(Deployment, Service.name, IncidentDeployment.timing)
        .join(IncidentDeployment, IncidentDeployment.deployment_id == Deployment.id)
        .join(
            Service,
            (Service.id == Deployment.service_id) & (Service.project_id == Deployment.project_id),
        )
        .where(
            IncidentDeployment.incident_id == incident.id,
            Deployment.project_id == scope.project_id,
        )
        .order_by(Deployment.deployed_at)
    )
    timeline = list(
        await session.scalars(
            select(IncidentEvent)
            .where(
                IncidentEvent.project_id == scope.project_id,
                IncidentEvent.incident_id == incident.id,
            )
            .order_by(IncidentEvent.ts, IncidentEvent.seq)
        )
    )
    involved = set(summary.services)
    return IncidentDetail(
        summary=summary,
        anomalies=[AnomalyRow(anomaly, series, name) for anomaly, series, name in anomaly_rows],
        candidate_deployments=[
            CandidateDeployment(deployment, name, timing)
            for deployment, name, timing in deployment_rows
        ],
        timeline=timeline,
        dependencies=[
            edge
            for edge in await list_dependencies(session, scope)
            if edge[0] in involved and edge[1] in involved
        ],
    )
