"""The pure grouping rules."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from incident_intel.incidents.rules import (
    IncidentView,
    find_matches,
    incident_title,
    worst_severity,
)

T0 = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
WINDOW = timedelta(minutes=15)
WEB, API, DB, OTHER = (uuid.uuid4() for _ in range(4))
# web depends on api, api depends on db.
EDGES = {(WEB, API), (API, DB)}


def incident(
    *services: uuid.UUID,
    started: int = 0,
    last_activity: int = 5,
    resolved: int | None = None,
) -> IncidentView:
    return IncidentView(
        id=uuid.uuid4(),
        started_at=T0 + timedelta(minutes=started),
        last_activity_at=T0 + timedelta(minutes=last_activity),
        resolved_at=T0 + timedelta(minutes=resolved) if resolved is not None else None,
        service_ids=frozenset(services),
    )


def matches(service: uuid.UUID, minute: int, *incidents: IncidentView) -> list[tuple[str, object]]:
    found = find_matches(
        service_id=service,
        started_at=T0 + timedelta(minutes=minute),
        incidents=incidents,
        edges=EDGES,
        window=WINDOW,
    )
    return [(match.rule, match.edge) for match in found]


def test_same_service_matches() -> None:
    assert matches(API, 3, incident(API)) == [("same_service", None)]


def test_a_direct_dependency_matches_in_either_direction() -> None:
    # The anomaly is on the caller of an incident's service...
    assert matches(WEB, 3, incident(API)) == [("dependency", (WEB, API))]
    # ...or on something an incident's service calls.
    assert matches(DB, 3, incident(API)) == [("dependency", (API, DB))]


def test_two_hops_do_not_match() -> None:
    assert matches(DB, 3, incident(WEB)) == []


def test_an_unrelated_service_does_not_match_even_at_the_same_time() -> None:
    assert matches(OTHER, 3, incident(WEB, API, DB)) == []


@pytest.mark.parametrize(
    ("minute", "expected"),
    [(-15, True), (-16, False), (20, True), (21, False)],
    ids=["window-before-start", "too-early", "window-after-activity", "too-late"],
)
def test_time_window_is_relative_to_the_incidents_start_and_latest_activity(
    minute: int, expected: bool
) -> None:
    found = matches(API, minute, incident(API, started=0, last_activity=5))

    assert bool(found) is expected


def test_a_resolved_incident_can_be_rejoined_for_one_window_after_it_resolved() -> None:
    resolved = incident(API, started=0, last_activity=5, resolved=8)

    assert matches(API, 23, resolved) == [("same_service", None)]
    assert matches(API, 24, resolved) == []


def test_several_related_incidents_are_returned_oldest_first() -> None:
    newer = incident(WEB, started=4)
    older = incident(DB, started=1)

    found = find_matches(
        service_id=API,
        started_at=T0 + timedelta(minutes=6),
        incidents=[newer, older],
        edges=EDGES,
        window=WINDOW,
    )

    assert [match.incident_id for match in found] == [older.id, newer.id]


@pytest.mark.parametrize(
    ("names", "title"),
    [
        (["payment-api"], "Anomalies in payment-api"),
        (["payments-db", "payment-api"], "Anomalies in payments-db and payment-api"),
        (["a", "b", "c"], "Anomalies in a, b and 1 more service"),
        (["a", "b", "c", "d"], "Anomalies in a, b and 2 more services"),
    ],
)
def test_title_names_the_services_in_the_order_they_joined(names: list[str], title: str) -> None:
    assert incident_title(names) == title


def test_worst_severity() -> None:
    assert worst_severity(["low", "critical", "medium"]) == "critical"
    assert worst_severity([]) == "low"
