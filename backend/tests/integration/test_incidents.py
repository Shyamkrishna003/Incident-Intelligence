"""Grouping anomalies into incidents, through the real detection handler."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from incident_intel.audit.service import CLI_ACTOR
from incident_intel.detection.consumer import detection_handler
from incident_intel.detection.detectors import RobustZScoreDetector
from incident_intel.detection.engine import EngineConfig
from incident_intel.detection.models import Anomaly
from incident_intel.detection.service import DetectionSettings
from incident_intel.incidents.dependencies import replace_dependencies
from incident_intel.incidents.models import Incident, IncidentEvent
from incident_intel.incidents.service import CorrelationSettings
from incident_intel.streaming.consumer import ConsumedMessage
from incident_intel.telemetry.messages import (
    DeploymentBatchMessage,
    DeploymentMessage,
    MetricsStoredEvent,
    content_hash,
)
from incident_intel.telemetry.storage import store_deployment_batch, store_metric_batch
from incident_intel.tenancy.api_keys import ApiKeyScope
from incident_intel.tenancy.context import TenantScope
from incident_intel.tenancy.models import Membership
from incident_intel.tenancy.roles import Role
from tests.conftest import Tenant, TenantFactory, UserFactory
from tests.support import metric_batch, metric_point

pytestmark = pytest.mark.integration

STEP = timedelta(seconds=15)
T0 = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
DETECTION = DetectionSettings(
    detector=RobustZScoreDetector(threshold=6.0, min_history=30), engine=EngineConfig()
)
WOBBLE = [100.0, 101.0, 99.0, 100.5, 99.5]


class World:
    """Stores metric points for a tenant and runs the real detection handler on them."""

    def __init__(
        self,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        tenant: Tenant,
    ) -> None:
        self.session = session
        self.tenant = tenant
        # Plain values, so nothing here depends on ORM objects staying loaded.
        self.organization_id = tenant.organization.id
        self.project_id = tenant.project.id
        self.api_key_id = tenant.api_key.id
        self.auth_headers = tenant.auth_headers
        self.scope = TenantScope(self.organization_id, self.project_id)
        self._handle = detection_handler(session_factory, DETECTION, CorrelationSettings())

    async def send(
        self, service: str, values: list[float], *, start: int = 0, metric: str = "latency"
    ) -> None:
        """Store `values` (one per 15 s from step `start`) and run detection on the series."""
        result = await store_metric_batch(
            self.session,
            metric_batch(
                organization_id=self.organization_id,
                project_id=self.project_id,
                api_key_id=self.api_key_id,
                points=[
                    metric_point(T0 + (start + i) * STEP, value, service=service, metric=metric)
                    for i, value in enumerate(values)
                ],
                received_at=T0,
            ),
        )
        event = MetricsStoredEvent(
            organization_id=self.organization_id,
            project_id=self.project_id,
            batch_id=uuid.uuid4(),
            series_ids=list(result.series_ids),
        )
        await self._handle(
            ConsumedMessage("stored", 0, 1, None, event.model_dump_json().encode(), {})
        )

    async def break_(self, service: str, *, at: int = 40, metric: str = "latency") -> None:
        """A healthy series that jumps at step `at` and stays high (an open anomaly)."""
        await self.send(service, [WOBBLE[i % 5] for i in range(at)] + [300.0] * 4, metric=metric)

    async def recover(self, service: str, *, start: int, metric: str = "latency") -> None:
        await self.send(service, [WOBBLE[i % 5] for i in range(12)], start=start, metric=metric)

    async def depends(self, *edges: tuple[str, str]) -> None:
        await replace_dependencies(self.session, self.scope, list(edges), actor=CLI_ACTOR)

    async def deploy(self, service: str, version: str, *, minute: float) -> None:
        items = [
            DeploymentMessage(
                service=service,
                version=version,
                deployed_at=T0 + timedelta(minutes=minute),
                commit_sha=None,
                environment=None,
                deployed_by="ci",
                description=None,
            )
        ]
        await store_deployment_batch(
            self.session,
            DeploymentBatchMessage(
                batch_id=uuid.uuid4(),
                organization_id=self.organization_id,
                project_id=self.project_id,
                api_key_id=self.api_key_id,
                idempotency_key=uuid.uuid4().hex,
                content_sha256=content_hash(items),
                received_at=T0,
                deployments=items,
            ),
        )

    async def incidents(self) -> list[Incident]:
        # The handler changed these rows through other sessions: reload what is cached here.
        rows = await self.session.scalars(
            select(Incident)
            .where(Incident.project_id == self.project_id)
            .order_by(Incident.started_at, Incident.created_at)
            .execution_options(populate_existing=True)
        )
        return list(rows)

    async def timeline(self, incident: Incident) -> list[tuple[str, dict[str, Any]]]:
        rows = await self.session.scalars(
            select(IncidentEvent)
            .where(IncidentEvent.incident_id == incident.id)
            .order_by(IncidentEvent.ts, IncidentEvent.seq)
        )
        return [(event.kind, event.details) for event in rows]


@pytest.fixture
async def world(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    make_tenant: TenantFactory,
) -> World:
    return World(db_session, session_factory, await make_tenant())


def kinds(timeline: list[tuple[str, dict[str, Any]]]) -> list[str]:
    return [kind for kind, _ in timeline]


# --- Grouping ----------------------------------------------------------------------------


async def test_a_first_anomaly_opens_an_incident_and_says_why(world: World) -> None:
    await world.break_("payment-api")

    [incident] = await world.incidents()
    assert (incident.status, incident.title) == ("open", "Anomalies in payment-api")
    assert incident.started_at == T0 + 40 * STEP
    assert incident.detected_at == T0 + 41 * STEP
    [(kind, details)] = await world.timeline(incident)
    assert kind == "incident_opened"
    assert details["rule"] == "no_related_incident"
    assert (details["service"], details["metric"]) == ("payment-api", "latency")


async def test_anomalies_on_the_same_service_share_an_incident(world: World) -> None:
    await world.break_("payment-api", metric="latency")
    await world.break_("payment-api", metric="errors", at=44)

    [incident] = await world.incidents()
    timeline = await world.timeline(incident)
    assert kinds(timeline) == ["incident_opened", "anomaly_attached"]
    assert timeline[1][1]["rule"] == "same_service"
    assert timeline[1][1]["metric"] == "errors"


async def test_a_declared_dependency_groups_two_services_and_is_recorded(world: World) -> None:
    await world.depends(("payment-api", "payments-db"))
    await world.break_("payments-db")
    await world.break_("payment-api", at=44)

    [incident] = await world.incidents()
    assert incident.title == "Anomalies in payments-db and payment-api"
    attached = (await world.timeline(incident))[1][1]
    assert attached["rule"] == "dependency"
    assert attached["dependency"] == {"service": "payment-api", "depends_on": "payments-db"}


async def test_timeline_keeps_the_order_decisions_were_made_in(world: World) -> None:
    """Several anomalies detected in the same instant share a timestamp. The timeline must
    still read in the order they were grouped, or the reasons read backwards."""
    await world.depends(("payment-api", "payments-db"))
    healthy = [WOBBLE[i % 5] for i in range(40)]
    for metric in ("m1", "m2", "m3"):
        await world.send("payments-db", healthy, metric=metric)
    await world.break_("payment-api")
    # Three payments-db series break together: all detected at the same moment.
    for metric in ("m1", "m2", "m3"):
        await world.send("payments-db", [300.0] * 4, start=40, metric=metric)

    [incident] = await world.incidents()
    attached = [d for kind, d in await world.timeline(incident) if kind == "anomaly_attached"]
    # The first one joins through the dependency; after that the service is already in.
    assert [(d["service"], d["rule"]) for d in attached] == [
        ("payments-db", "dependency"),
        ("payments-db", "same_service"),
        ("payments-db", "same_service"),
    ]


async def test_without_a_declared_dependency_services_get_separate_incidents(world: World) -> None:
    await world.break_("payments-db")
    await world.break_("payment-api", at=44)

    assert [i.title for i in await world.incidents()] == [
        "Anomalies in payments-db",
        "Anomalies in payment-api",
    ]


async def test_an_anomaly_outside_the_time_window_starts_a_new_incident(world: World) -> None:
    await world.break_("payment-api", metric="latency")
    # Same service, but this one starts 25 minutes after the first one's last activity.
    await world.break_("payment-api", metric="errors", at=140)

    assert len(await world.incidents()) == 2


async def test_an_anomaly_bridging_two_incidents_merges_them(
    world: World, client: AsyncClient
) -> None:
    # web -> api -> db. web and db are two hops apart, so they start as two incidents.
    await world.depends(("checkout-web", "payment-api"), ("payment-api", "payments-db"))
    await world.break_("payments-db")
    await world.break_("checkout-web", at=42)
    assert len(await world.incidents()) == 2

    await world.break_("payment-api", at=44)  # related to both

    older, newer = await world.incidents()
    assert (older.status, newer.status) == ("open", "merged")
    assert newer.merged_into_id == older.id
    assert older.title == "Anomalies in payments-db, checkout-web and 1 more service"
    anomalies = await world.session.scalars(
        select(Anomaly.incident_id).where(Anomaly.project_id == world.project_id)
    )
    assert set(anomalies) == {older.id}
    timeline = await world.timeline(older)
    merged = next(details for kind, details in timeline if kind == "incidents_merged")
    assert merged["merged_incident_id"] == str(newer.id)
    assert merged["rule"] == "one_anomaly_related_to_both"

    # The merged incident disappears from lists but can still be opened, pointing onward.
    listed = await client.get(
        "/v1/incidents",
        params={
            "start": (T0 - timedelta(hours=1)).isoformat(),
            "end": (T0 + timedelta(hours=2)).isoformat(),
        },
        headers=world.auth_headers,
    )
    assert [i["id"] for i in listed.json()["incidents"]] == [str(older.id)]
    old_link = await client.get(f"/v1/incidents/{newer.id}", headers=world.auth_headers)
    assert (old_link.json()["status"], old_link.json()["merged_into_id"]) == (
        "merged",
        str(older.id),
    )


# --- Lifecycle ---------------------------------------------------------------------------


async def test_incident_resolves_when_every_anomaly_has_ended(world: World) -> None:
    await world.break_("payment-api", metric="latency")
    await world.break_("payment-api", metric="errors", at=44)

    await world.recover("payment-api", start=44, metric="latency")
    [still_open] = await world.incidents()
    assert still_open.status == "open"

    await world.recover("payment-api", start=48, metric="errors")
    [resolved] = await world.incidents()
    assert resolved.status == "resolved"
    assert resolved.resolved_at is not None
    assert kinds(await world.timeline(resolved))[-3:] == [
        "anomaly_ended",
        "anomaly_ended",
        "incident_resolved",
    ]


async def test_a_related_anomaly_soon_after_reopens_the_incident(world: World) -> None:
    await world.break_("payment-api", metric="latency")
    await world.recover("payment-api", start=44, metric="latency")
    assert (await world.incidents())[0].status == "resolved"

    await world.break_("payment-api", metric="errors", at=70)  # about 5 minutes later

    [incident] = await world.incidents()
    assert (incident.status, incident.resolved_at) == ("open", None)
    assert "incident_reopened" in kinds(await world.timeline(incident))


async def test_incident_severity_is_the_worst_of_its_anomalies(world: World) -> None:
    await world.send("payment-api", [WOBBLE[i % 5] for i in range(40)] + [150.0] * 4)
    assert (await world.incidents())[0].severity == "low"

    await world.send("payment-api", [5000.0] * 4, start=44)

    assert (await world.incidents())[0].severity == "critical"


# --- Candidate deployments ---------------------------------------------------------------


async def test_deployments_of_incident_services_are_linked_as_candidates(world: World) -> None:
    await world.deploy("payment-api", "2.43.0", minute=9.5)  # 30 s before the anomaly
    await world.deploy("inventory-api", "1.8.2", minute=5)  # unrelated service
    await world.deploy("payment-api", "2.40.0", minute=-120)  # too long ago
    await world.break_("payment-api")

    [incident] = await world.incidents()
    linked = [
        details for kind, details in await world.timeline(incident) if kind == "deployment_linked"
    ]
    assert [(d["service"], d["version"], d["timing"]) for d in linked] == [
        ("payment-api", "2.43.0", "before")
    ]
    assert linked[0]["rule"] == "deployed_service_is_in_incident"


async def test_a_deployment_during_the_incident_is_linked_on_its_next_activity(
    world: World,
) -> None:
    await world.break_("payment-api")
    await world.deploy("payment-api", "2.42.3", minute=10.5)  # a rollback, mid-incident
    await world.send("payment-api", [300.0] * 4, start=44)  # the anomaly continues

    [incident] = await world.incidents()
    linked = [
        details for kind, details in await world.timeline(incident) if kind == "deployment_linked"
    ]
    assert [(d["version"], d["timing"]) for d in linked] == [("2.42.3", "during")]

    await world.send("payment-api", [300.0] * 4, start=48)  # more activity: not linked twice
    again = [d for kind, d in await world.timeline(incident) if kind == "deployment_linked"]
    assert len(again) == 1


# --- API ---------------------------------------------------------------------------------


async def test_incident_api_returns_the_evidence(world: World, client: AsyncClient) -> None:
    await world.depends(("payment-api", "payments-db"), ("checkout-web", "payment-api"))
    await world.deploy("payment-api", "2.43.0", minute=9.5)
    await world.break_("payments-db")
    await world.break_("payment-api", at=42)
    headers = world.auth_headers
    window = {
        "start": (T0 - timedelta(hours=1)).isoformat(),
        "end": (T0 + timedelta(hours=1)).isoformat(),
    }

    listed = await client.get("/v1/incidents", params=window, headers=headers)
    [summary] = listed.json()["incidents"]
    detail = (await client.get(f"/v1/incidents/{summary['id']}", headers=headers)).json()

    assert summary["services"] == ["payments-db", "payment-api"]
    assert (summary["anomaly_count"], summary["open_anomaly_count"]) == (2, 2)
    assert [a["service"] for a in detail["anomalies"]] == ["payments-db", "payment-api"]
    assert all(a["incident_id"] == summary["id"] for a in detail["anomalies"])
    assert [(d["service"], d["version"], d["timing"]) for d in detail["candidate_deployments"]] == [
        ("payment-api", "2.43.0", "before")
    ]
    # Only dependencies between the incident's own services.
    assert detail["dependencies"] == [{"service": "payment-api", "depends_on": "payments-db"}]
    assert [e["kind"] for e in detail["timeline"]] == [
        "deployment_linked",
        "incident_opened",
        "anomaly_attached",
    ]

    only_resolved = await client.get(
        "/v1/incidents", params={**window, "status": "resolved"}, headers=headers
    )
    assert only_resolved.json()["incidents"] == []


async def test_incidents_are_isolated_between_projects(
    world: World, client: AsyncClient, make_tenant: TenantFactory
) -> None:
    await world.break_("payment-api")
    [incident] = await world.incidents()
    other = await make_tenant()

    listed = await client.get("/v1/incidents", headers=other.auth_headers)
    detail = await client.get(f"/v1/incidents/{incident.id}", headers=other.auth_headers)
    write_only = await make_tenant(scopes={ApiKeyScope.INGEST_WRITE})
    forbidden = await client.get("/v1/incidents", headers=write_only.auth_headers)

    assert listed.json()["incidents"] == []
    assert detail.status_code == 404
    assert forbidden.status_code == 403


# --- Dependencies API --------------------------------------------------------------------


async def test_dependencies_are_replaced_as_a_whole(
    client: AsyncClient, make_tenant: TenantFactory
) -> None:
    tenant = await make_tenant()
    other = await make_tenant()

    first = await client.put(
        "/v1/dependencies",
        json={
            "dependencies": [
                {"service": "checkout-web", "depends_on": "payment-api"},
                {"service": "payment-api", "depends_on": "payments-db"},
                {"service": "payment-api", "depends_on": "payments-db"},  # duplicate
            ]
        },
        headers=tenant.auth_headers,
    )
    replaced = await client.put(
        "/v1/dependencies",
        json={"dependencies": [{"service": "a", "depends_on": "b"}]},
        headers=tenant.auth_headers,
    )
    mine = await client.get("/v1/dependencies", headers=tenant.auth_headers)
    theirs = await client.get("/v1/dependencies", headers=other.auth_headers)
    # Declaring a dependency registers services that have not sent telemetry yet.
    services = await client.get("/v1/services", headers=tenant.auth_headers)

    assert first.status_code == 200
    assert len(first.json()["dependencies"]) == 2
    assert replaced.json()["dependencies"] == [{"service": "a", "depends_on": "b"}]
    assert mine.json() == replaced.json()
    assert theirs.json()["dependencies"] == []
    assert {"a", "b", "payment-api"} <= {s["name"] for s in services.json()["services"]}


@pytest.mark.parametrize(
    "body",
    [
        {"dependencies": [{"service": "a", "depends_on": "a"}]},
        {"dependencies": [{"service": "Bad Name", "depends_on": "b"}]},
        {"dependencies": [{"service": "a"}]},
        {},
    ],
    ids=["self", "bad-name", "missing-field", "missing-list"],
)
async def test_invalid_dependencies_are_rejected(
    client: AsyncClient, make_tenant: TenantFactory, body: dict[str, Any]
) -> None:
    tenant = await make_tenant()
    await client.put(
        "/v1/dependencies",
        json={"dependencies": [{"service": "x", "depends_on": "y"}]},
        headers=tenant.auth_headers,
    )

    response = await client.put("/v1/dependencies", json=body, headers=tenant.auth_headers)

    assert response.status_code == 422
    kept = await client.get("/v1/dependencies", headers=tenant.auth_headers)
    assert kept.json()["dependencies"] == [{"service": "x", "depends_on": "y"}]


async def test_dependency_permissions(
    client: AsyncClient,
    db_session: AsyncSession,
    make_tenant: TenantFactory,
    make_user: UserFactory,
) -> None:
    read_only = await make_tenant(scopes={ApiKeyScope.TELEMETRY_READ})
    body = {"dependencies": [{"service": "a", "depends_on": "b"}]}
    assert (
        await client.put("/v1/dependencies", json=body, headers=read_only.auth_headers)
    ).status_code == 403

    owner, viewer, outsider = make_user(), make_user(), make_user()
    org = await client.post(
        "/v1/organizations",
        json={"slug": f"org-{uuid.uuid4().hex[:8]}", "name": "Acme"},
        headers=owner.headers,
    )
    project = await client.post(
        f"/v1/organizations/{org.json()['id']}/projects",
        json={"slug": "p", "name": "P"},
        headers=owner.headers,
    )
    me = await client.get("/v1/me", headers=viewer.headers)
    db_session.add(
        Membership(
            organization_id=uuid.UUID(org.json()["id"]),
            user_id=uuid.UUID(me.json()["user"]["id"]),
            role=Role.VIEWER.value,
        )
    )
    await db_session.commit()
    base = f"/v1/projects/{project.json()['id']}"

    as_owner = await client.put(f"{base}/dependencies", json=body, headers=owner.headers)
    as_viewer_write = await client.put(f"{base}/dependencies", json=body, headers=viewer.headers)
    as_viewer_read = await client.get(f"{base}/dependencies", headers=viewer.headers)
    as_outsider = [
        (await client.get(f"{base}/dependencies", headers=outsider.headers)).status_code,
        (await client.get(f"{base}/incidents", headers=outsider.headers)).status_code,
        (
            await client.get(f"{base}/incidents/{uuid.uuid4()}", headers=outsider.headers)
        ).status_code,
    ]

    assert as_owner.status_code == 200
    assert as_viewer_write.status_code == 403
    assert as_viewer_read.json() == as_owner.json()
    assert (await client.get(f"{base}/incidents", headers=viewer.headers)).status_code == 200
    assert as_outsider == [404, 404, 404]
