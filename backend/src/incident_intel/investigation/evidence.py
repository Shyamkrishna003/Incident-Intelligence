"""Collecting the evidence for an investigation. No LLM is involved here.

The set is fixed and bounded: the incident, its anomalies, deployments around it, declared
dependencies, warning-and-error log patterns of the affected services, and which services
were not affected. Each item gets a short reference (E1, E2, ...) that the report must cite.

Text that came from monitored systems (log messages, deployment descriptions) is passed
through the same secret redaction as application logs, and truncated.
"""

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, true
from sqlalchemy.ext.asyncio import AsyncSession

from incident_intel.core.logging import scrub_text
from incident_intel.incidents.dependencies import list_dependencies
from incident_intel.incidents.queries import IncidentDetail, get_incident
from incident_intel.telemetry.messages import SEVERITY_NAMES, SEVERITY_NUMBERS
from incident_intel.telemetry.models import Deployment, LogRecord, Service
from incident_intel.tenancy.context import TenantScope

MAX_ANOMALIES = 30
MAX_OTHER_DEPLOYMENTS = 10
MAX_LOG_ROWS_PER_SERVICE = 2000
MAX_LOG_PATTERNS_PER_SERVICE = 8
MAX_TEXT = 300
LOGS_BEFORE = timedelta(minutes=30)
LOGS_AFTER = timedelta(minutes=5)
DEPLOYMENT_LOOKBACK = timedelta(hours=1)

_NUMBER = re.compile(r"\d+(?:\.\d+)?")


@dataclass(frozen=True)
class EvidenceItem:
    ref: str
    kind: str
    title: str
    data: dict[str, Any]

    def for_model(self) -> dict[str, Any]:
        return {"ref": self.ref, "kind": self.kind, "title": self.title, **self.data}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _clean(text: str | None) -> str | None:
    return scrub_text(text)[:MAX_TEXT] if text else None


def log_pattern(message: str) -> str:
    """Messages that differ only in numbers are the same pattern."""
    return _NUMBER.sub("#", message)[:MAX_TEXT]


async def _log_patterns(
    session: AsyncSession,
    scope: TenantScope,
    service_id: uuid.UUID,
    incident_start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(LogRecord.ts, LogRecord.severity, LogRecord.message)
            .where(
                LogRecord.project_id == scope.project_id,
                LogRecord.service_id == service_id,
                LogRecord.ts >= incident_start - LOGS_BEFORE,
                LogRecord.ts < end,
                LogRecord.severity >= SEVERITY_NUMBERS["warn"],
            )
            .order_by(LogRecord.ts.desc())
            .limit(MAX_LOG_ROWS_PER_SERVICE)
        )
    ).all()
    patterns: dict[str, dict[str, Any]] = {}
    for ts, severity, message in rows:
        cleaned = scrub_text(message)
        entry = patterns.setdefault(
            log_pattern(cleaned),
            {
                "severity": SEVERITY_NAMES.get(severity, "warn"),
                "example": cleaned[:MAX_TEXT],
                "count_before_incident": 0,
                "count_during_incident": 0,
                "first_seen": ts,
                "last_seen": ts,
            },
        )
        entry["count_during_incident" if ts >= incident_start else "count_before_incident"] += 1
        entry["first_seen"] = min(entry["first_seen"], ts)
        entry["last_seen"] = max(entry["last_seen"], ts)
    ranked = sorted(patterns.values(), key=lambda entry: -entry["count_during_incident"])
    return [
        {**entry, "first_seen": _iso(entry["first_seen"]), "last_seen": _iso(entry["last_seen"])}
        for entry in ranked[:MAX_LOG_PATTERNS_PER_SERVICE]
    ]


def _deployment(deployment: Deployment, service: str, incident_start: datetime) -> dict[str, Any]:
    return {
        "service": service,
        "version": deployment.version,
        "deployed_at": _iso(deployment.deployed_at),
        "seconds_before_incident_start": round(
            (incident_start - deployment.deployed_at).total_seconds()
        ),
        "commit": deployment.commit_sha,
        "deployed_by": _clean(deployment.deployed_by),
        "description": _clean(deployment.description),
    }


async def collect_evidence(
    session: AsyncSession, scope: TenantScope, incident_id: uuid.UUID
) -> tuple[IncidentDetail, list[EvidenceItem]]:
    detail = await get_incident(session, scope, incident_id)
    incident = detail.summary.incident
    items: list[EvidenceItem] = []

    def add(kind: str, title: str, data: dict[str, Any]) -> None:
        items.append(EvidenceItem(f"E{len(items) + 1}", kind, title[:300], data))

    add(
        "incident",
        "The incident, as grouped by the system",
        {
            "status": incident.status,
            "started_at": _iso(incident.started_at),
            "detected_at": _iso(incident.detected_at),
            "last_activity_at": _iso(incident.last_activity_at),
            "resolved_at": _iso(incident.resolved_at),
            "affected_services_in_order_of_first_anomaly": detail.summary.services,
            "note": "Anomalies were grouped by time and declared dependencies. Grouping "
            "does not show cause.",
        },
    )

    for row in detail.anomalies[:MAX_ANOMALIES]:
        anomaly = row.anomaly
        add(
            "anomaly",
            f"Anomaly: {row.service_name} {row.series.name}",
            {
                "service": row.service_name,
                "metric": row.series.name,
                "attributes": row.series.attributes,
                "unit": row.series.unit,
                "direction": anomaly.direction,
                "started_at": _iso(anomaly.started_at),
                "ended_at": _iso(anomaly.ended_at),
                "still_ongoing": anomaly.status == "open",
                "most_extreme_value": anomaly.peak_value,
                "usual_value_before": anomaly.baseline_center,
            },
        )

    linked = {item.deployment.id for item in detail.candidate_deployments}
    for candidate in detail.candidate_deployments:
        add(
            "deployment",
            f"Deployment of an affected service: {candidate.service_name} "
            f"{candidate.deployment.version}",
            {
                **_deployment(candidate.deployment, candidate.service_name, incident.started_at),
                "service_is_affected": True,
            },
        )
    others = await session.execute(
        select(Deployment, Service.name)
        .join(
            Service,
            (Service.id == Deployment.service_id) & (Service.project_id == Deployment.project_id),
        )
        .where(
            Deployment.project_id == scope.project_id,
            Deployment.deployed_at >= incident.started_at - DEPLOYMENT_LOOKBACK,
            Deployment.deployed_at <= incident.last_activity_at,
            Deployment.id.not_in(linked) if linked else true(),
        )
        .order_by(Deployment.deployed_at)
        .limit(MAX_OTHER_DEPLOYMENTS)
    )
    for deployment, service_name in others:
        add(
            "deployment",
            f"Deployment of a service with no anomaly: {service_name} {deployment.version}",
            {
                **_deployment(deployment, service_name, incident.started_at),
                "service_is_affected": False,
            },
        )

    dependencies = await list_dependencies(session, scope)
    add(
        "dependencies",
        "Declared service dependencies in this project",
        {
            "depends_on": [{"service": a, "depends_on": b} for a, b in dependencies[:100]],
            "note": "Declared by the project's owners. May be incomplete.",
        },
    )

    services = (
        await session.execute(
            select(Service.id, Service.name).where(Service.project_id == scope.project_id)
        )
    ).all()
    affected = set(detail.summary.services)
    add(
        "unaffected_services",
        "Services in this project with no anomaly in this incident",
        {"services": sorted(name for _, name in services if name not in affected)[:50]},
    )

    log_end = incident.last_activity_at + LOGS_AFTER
    for service_id, name in sorted(services, key=lambda row: row[1]):
        if name not in affected:
            continue
        patterns = await _log_patterns(session, scope, service_id, incident.started_at, log_end)
        add(
            "logs",
            f"Warning and error log patterns: {name}",
            {
                "service": name,
                "window": {
                    "from": _iso(incident.started_at - LOGS_BEFORE),
                    "to": _iso(log_end),
                    "incident_started_at": _iso(incident.started_at),
                },
                "patterns": patterns,
                "note": "Numbers in messages are replaced by # to group similar lines. "
                "An empty list means no warning or error logs were received, not that "
                "none occurred.",
            },
        )
    return detail, items
