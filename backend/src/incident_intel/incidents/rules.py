"""Grouping rules. Pure: no I/O.

An anomaly is related to an incident when both hold:

1. **Time.** The anomaly started no more than ``window`` before the incident started and no
   more than ``window`` after its latest activity (for a resolved incident: after it was
   resolved).
2. **Place.** The anomaly's service is already part of the incident (``same_service``), or
   a declared dependency directly connects it to one of the incident's services
   (``dependency``), in either direction.

Every match names the rule and the services involved, so the decision can be shown to a
person. These are heuristics for grouping; they do not establish what caused what.
"""

import uuid
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

Rule = Literal["same_service", "dependency"]

# (dependent service, service it depends on)
Edge = tuple[uuid.UUID, uuid.UUID]


@dataclass(frozen=True)
class IncidentView:
    """What the rules need to know about a candidate incident."""

    id: uuid.UUID
    started_at: datetime
    last_activity_at: datetime
    # Set for a resolved incident: it can be reopened for a while.
    resolved_at: datetime | None
    service_ids: frozenset[uuid.UUID]


@dataclass(frozen=True)
class Match:
    incident_id: uuid.UUID
    rule: Rule
    # For ``dependency``: the edge that connects the anomaly's service to the incident.
    edge: Edge | None = None


def _in_time(incident: IncidentView, started_at: datetime, window: timedelta) -> bool:
    latest = incident.resolved_at or incident.last_activity_at
    return incident.started_at - window <= started_at <= latest + window


def find_matches(
    *,
    service_id: uuid.UUID,
    started_at: datetime,
    incidents: Iterable[IncidentView],
    edges: Collection[Edge],
    window: timedelta,
) -> list[Match]:
    """Incidents the anomaly is related to, oldest incident first."""
    matches: list[tuple[datetime, Match]] = []
    for incident in incidents:
        if not _in_time(incident, started_at, window):
            continue
        if service_id in incident.service_ids:
            matches.append((incident.started_at, Match(incident.id, "same_service")))
            continue
        # Deterministic choice when several edges connect: the smallest by id.
        connecting = sorted(
            (
                edge
                for edge in edges
                if (edge[0] == service_id and edge[1] in incident.service_ids)
                or (edge[1] == service_id and edge[0] in incident.service_ids)
            ),
            key=lambda edge: (str(edge[0]), str(edge[1])),
        )
        if connecting:
            matches.append((incident.started_at, Match(incident.id, "dependency", connecting[0])))
    return [
        match for _, match in sorted(matches, key=lambda item: (item[0], str(item[1].incident_id)))
    ]


def incident_title(service_names: list[str]) -> str:
    """``service_names`` in the order the services joined the incident."""
    if not service_names:
        return "Anomalies"
    if len(service_names) == 1:
        return f"Anomalies in {service_names[0]}"
    if len(service_names) == 2:
        return f"Anomalies in {service_names[0]} and {service_names[1]}"
    others = len(service_names) - 2
    return (
        f"Anomalies in {service_names[0]}, {service_names[1]} "
        f"and {others} more service{'s' if others > 1 else ''}"
    )


_SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def worst_severity(severities: Iterable[str]) -> str:
    return max(severities, key=lambda severity: _SEVERITY_RANK.get(severity, 0), default="low")
