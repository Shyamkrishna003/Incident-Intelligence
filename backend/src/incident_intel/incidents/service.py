"""Grouping anomalies into incidents against PostgreSQL.

Called from the detection consumer, inside the same transaction that stores the anomalies,
so an anomaly and its place in an incident are committed together.

Every decision is written to the incident's timeline with the rule that made it.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from incident_intel.core.config import RuntimeSettings
from incident_intel.detection.models import Anomaly
from incident_intel.incidents.models import (
    Incident,
    IncidentDeployment,
    IncidentEvent,
    ServiceDependency,
)
from incident_intel.incidents.rules import (
    Edge,
    IncidentView,
    Match,
    find_matches,
    incident_title,
    worst_severity,
)
from incident_intel.telemetry.models import Deployment, MetricSeries, Service

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class CorrelationSettings:
    window: timedelta = timedelta(minutes=15)
    deployment_lookback: timedelta = timedelta(hours=1)

    @classmethod
    def from_settings(cls, settings: RuntimeSettings) -> "CorrelationSettings":
        return cls(
            window=timedelta(seconds=settings.correlation_window_seconds),
            deployment_lookback=timedelta(seconds=settings.deployment_lookback_seconds),
        )


async def _record(
    session: AsyncSession, incident: Incident, ts: datetime, kind: str, details: dict[str, Any]
) -> None:
    """Add a timeline entry. Flushed at once, so entries keep the order they were made in."""
    session.add(
        IncidentEvent(
            project_id=incident.project_id,
            incident_id=incident.id,
            ts=ts,
            kind=kind,
            details=details,
        )
    )
    await session.flush()


async def _describe(session: AsyncSession, anomaly: Anomaly) -> tuple[uuid.UUID, dict[str, Any]]:
    """The anomaly's service id, and the facts about it that timeline entries record."""
    series, service_name = (
        await session.execute(
            select(MetricSeries, Service.name)
            .join(
                Service,
                (Service.id == MetricSeries.service_id)
                & (Service.project_id == MetricSeries.project_id),
            )
            .where(
                MetricSeries.project_id == anomaly.project_id, MetricSeries.id == anomaly.series_id
            )
        )
    ).one()
    return series.service_id, {
        "anomaly_id": str(anomaly.id),
        "service": service_name,
        "metric": series.name,
        "attributes": series.attributes,
        "direction": anomaly.direction,
    }


async def _candidates(
    session: AsyncSession, project_id: uuid.UUID, started_at: datetime, window: timedelta
) -> list[IncidentView]:
    """Open incidents, and resolved ones recent enough to be reopened."""
    incidents = list(
        await session.scalars(
            select(Incident).where(
                Incident.project_id == project_id,
                or_(
                    Incident.status == "open",
                    (Incident.status == "resolved") & (Incident.resolved_at >= started_at - window),
                ),
            )
        )
    )
    if not incidents:
        return []
    services: dict[uuid.UUID, set[uuid.UUID]] = {incident.id: set() for incident in incidents}
    rows = await session.execute(
        select(Anomaly.incident_id, MetricSeries.service_id)
        .join(
            MetricSeries,
            (MetricSeries.id == Anomaly.series_id)
            & (MetricSeries.project_id == Anomaly.project_id),
        )
        .where(Anomaly.project_id == project_id, Anomaly.incident_id.in_(services))
    )
    for incident_id, service_id in rows:
        if incident_id is not None:
            services[incident_id].add(service_id)
    return [
        IncidentView(
            id=incident.id,
            started_at=incident.started_at,
            last_activity_at=incident.last_activity_at,
            resolved_at=incident.resolved_at if incident.status == "resolved" else None,
            service_ids=frozenset(services[incident.id]),
        )
        for incident in incidents
    ]


async def _edges(session: AsyncSession, project_id: uuid.UUID) -> set[Edge]:
    rows = await session.execute(
        select(ServiceDependency.service_id, ServiceDependency.depends_on_id).where(
            ServiceDependency.project_id == project_id
        )
    )
    return {(service_id, depends_on_id) for service_id, depends_on_id in rows}


async def _service_names(
    session: AsyncSession, project_id: uuid.UUID, service_ids: set[uuid.UUID]
) -> dict[uuid.UUID, str]:
    rows = await session.execute(
        select(Service.id, Service.name).where(
            Service.project_id == project_id, Service.id.in_(service_ids)
        )
    )
    return {service_id: name for service_id, name in rows}


async def _refresh(session: AsyncSession, incident: Incident, config: CorrelationSettings) -> bool:
    """Recompute what an incident derives from its anomalies, and link candidate
    deployments. Returns whether any of its anomalies is still open."""
    await session.flush()
    rows = (
        await session.execute(
            select(Anomaly, MetricSeries.service_id)
            .join(
                MetricSeries,
                (MetricSeries.id == Anomaly.series_id)
                & (MetricSeries.project_id == Anomaly.project_id),
            )
            .where(Anomaly.project_id == incident.project_id, Anomaly.incident_id == incident.id)
            .order_by(Anomaly.started_at, Anomaly.id)
        )
    ).all()
    if not rows:
        return False
    anomalies = [anomaly for anomaly, _ in rows]
    incident.started_at = min(anomaly.started_at for anomaly in anomalies)
    incident.detected_at = min(anomaly.detected_at for anomaly in anomalies)
    incident.last_activity_at = max(anomaly.last_anomalous_at for anomaly in anomalies)
    incident.severity = worst_severity(anomaly.severity for anomaly in anomalies)

    ordered_services = list(dict.fromkeys(service_id for _, service_id in rows))
    names = await _service_names(session, incident.project_id, set(ordered_services))
    incident.title = incident_title([names[service_id] for service_id in ordered_services])

    # Candidate deployments: of the incident's services, from shortly before it started
    # until its latest activity. Linked once each; a link is a candidate, not a cause.
    linked = select(IncidentDeployment.deployment_id).where(
        IncidentDeployment.incident_id == incident.id
    )
    deployments = await session.scalars(
        select(Deployment)
        .where(
            Deployment.project_id == incident.project_id,
            Deployment.service_id.in_(ordered_services),
            Deployment.deployed_at >= incident.started_at - config.deployment_lookback,
            Deployment.deployed_at <= incident.last_activity_at,
            Deployment.id.not_in(linked),
        )
        .order_by(Deployment.deployed_at)
    )
    for deployment in deployments:
        timing = "before" if deployment.deployed_at <= incident.started_at else "during"
        session.add(
            IncidentDeployment(
                incident_id=incident.id,
                deployment_id=deployment.id,
                project_id=incident.project_id,
                timing=timing,
            )
        )
        await _record(
            session,
            incident,
            deployment.deployed_at,
            "deployment_linked",
            {
                "deployment_id": str(deployment.id),
                "service": names[deployment.service_id],
                "version": deployment.version,
                "timing": timing,
                "rule": "deployed_service_is_in_incident",
            },
        )
    return any(anomaly.status == "open" for anomaly in anomalies)


async def _merge(
    session: AsyncSession,
    target: Incident,
    other_id: uuid.UUID,
    at: datetime,
    bridge: dict[str, Any],
) -> None:
    """Move everything from another incident into ``target`` and mark the other as merged."""
    other = await session.get(Incident, other_id)
    if other is None or other.id == target.id:
        return
    await session.execute(
        update(Anomaly)
        .where(Anomaly.project_id == target.project_id, Anomaly.incident_id == other.id)
        .values(incident_id=target.id)
    )
    # Deployment links are recreated for the target by _refresh; drop the old ones.
    await session.execute(
        delete(IncidentDeployment).where(IncidentDeployment.incident_id == other.id)
    )
    other.status = "merged"
    other.merged_into_id = target.id
    other.resolved_at = None
    details = {"merged_incident_id": str(other.id), "rule": "one_anomaly_related_to_both", **bridge}
    await _record(session, target, at, "incidents_merged", details)
    await _record(session, other, at, "merged_into", {"incident_id": str(target.id)})


async def correlate_opened(
    session: AsyncSession, anomaly: Anomaly, config: CorrelationSettings
) -> Incident:
    """Place a newly opened anomaly: into a related incident, or a new one."""
    service_id, facts = await _describe(session, anomaly)
    candidates = await _candidates(session, anomaly.project_id, anomaly.started_at, config.window)
    matches: list[Match] = find_matches(
        service_id=service_id,
        started_at=anomaly.started_at,
        incidents=candidates,
        edges=await _edges(session, anomaly.project_id),
        window=config.window,
    )

    if not matches:
        incident = Incident(
            organization_id=anomaly.organization_id,
            project_id=anomaly.project_id,
            title=incident_title([facts["service"]]),
            status="open",
            severity=anomaly.severity,
            started_at=anomaly.started_at,
            detected_at=anomaly.detected_at,
            last_activity_at=anomaly.last_anomalous_at,
        )
        session.add(incident)
        await session.flush()
        await _record(
            session,
            incident,
            anomaly.detected_at,
            "incident_opened",
            {**facts, "rule": "no_related_incident"},
        )
    else:
        first = matches[0]
        found = await session.get(Incident, first.incident_id)
        if found is None:  # cannot happen: the candidate was just loaded in this transaction
            raise LookupError(f"incident {first.incident_id} disappeared")
        incident = found
        if incident.status == "resolved":
            incident.status = "open"
            incident.resolved_at = None
            await _record(session, incident, anomaly.detected_at, "incident_reopened", {**facts})
        details: dict[str, Any] = {**facts, "rule": first.rule}
        if first.edge is not None:
            names = await _service_names(session, anomaly.project_id, set(first.edge))
            details["dependency"] = {
                "service": names[first.edge[0]],
                "depends_on": names[first.edge[1]],
            }
        await _record(session, incident, anomaly.detected_at, "anomaly_attached", details)
        for other in matches[1:]:
            await _merge(session, incident, other.incident_id, anomaly.detected_at, facts)

    anomaly.incident_id = incident.id
    await _refresh(session, incident, config)
    logger.info(
        "anomaly_correlated",
        anomaly_id=str(anomaly.id),
        incident_id=str(incident.id),
        rule=matches[0].rule if matches else "no_related_incident",
        merged=max(0, len(matches) - 1),
    )
    return incident


async def on_anomaly_extended(
    session: AsyncSession, anomaly: Anomaly, config: CorrelationSettings
) -> None:
    """An open anomaly got more abnormal points: keep its incident's summary current."""
    if anomaly.incident_id is None:
        return
    incident = await session.get(Incident, anomaly.incident_id)
    if incident is not None:
        await _refresh(session, incident, config)


async def on_anomaly_closed(
    session: AsyncSession, anomaly: Anomaly, config: CorrelationSettings
) -> None:
    """An anomaly ended. The incident resolves once none of its anomalies is open."""
    if anomaly.incident_id is None or anomaly.ended_at is None:
        return
    incident = await session.get(Incident, anomaly.incident_id)
    if incident is None:
        return
    _, facts = await _describe(session, anomaly)
    await _record(
        session,
        incident,
        anomaly.ended_at,
        "anomaly_ended",
        {**facts, "closed_reason": anomaly.closed_reason},
    )
    still_open = await _refresh(session, incident, config)
    if not still_open and incident.status == "open":
        resolved_at = await session.scalar(
            select(func.max(Anomaly.ended_at)).where(
                Anomaly.project_id == incident.project_id, Anomaly.incident_id == incident.id
            )
        )
        incident.status = "resolved"
        incident.resolved_at = resolved_at or anomaly.ended_at
        await _record(
            session,
            incident,
            incident.resolved_at,
            "incident_resolved",
            {"rule": "all_anomalies_ended"},
        )


async def apply_detection_changes(
    session: AsyncSession,
    *,
    opened: list[Anomaly],
    extended: list[Anomaly],
    closed: list[Anomaly],
    config: CorrelationSettings,
) -> None:
    """Fold one detection run's anomaly changes into incidents, in time order."""
    for anomaly in sorted(opened, key=lambda a: (a.started_at, str(a.id))):
        await correlate_opened(session, anomaly, config)
    for anomaly in extended:
        await on_anomaly_extended(session, anomaly, config)
    for anomaly in sorted(closed, key=lambda a: (a.ended_at or a.last_anomalous_at, str(a.id))):
        await on_anomaly_closed(session, anomaly, config)


async def upsert_services(
    session: AsyncSession, *, organization_id: uuid.UUID, project_id: uuid.UUID, names: set[str]
) -> dict[str, uuid.UUID]:
    """Make sure each named service exists in the project; return their ids."""
    ordered = sorted(names)
    if not ordered:
        return {}
    await session.execute(
        pg_insert(Service)
        .values(
            [
                {
                    "id": uuid.uuid4(),
                    "organization_id": organization_id,
                    "project_id": project_id,
                    "name": name,
                }
                for name in ordered
            ]
        )
        .on_conflict_do_nothing(index_elements=[Service.project_id, Service.name])
    )
    rows = await session.execute(
        select(Service.name, Service.id).where(
            Service.project_id == project_id, Service.name.in_(ordered)
        )
    )
    return {name: service_id for name, service_id in rows}
