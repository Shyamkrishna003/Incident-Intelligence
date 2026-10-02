"""Logs and deployments: ingestion contract, idempotent storage, reads, and isolation."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from incident_intel.core.config import Settings
from incident_intel.streaming.consumer import ConsumedMessage, PermanentMessageError
from incident_intel.telemetry.consumer import storage_handler
from incident_intel.telemetry.messages import (
    DeploymentBatchMessage,
    DeploymentMessage,
    LogBatchMessage,
    LogRecordMessage,
    content_hash,
)
from incident_intel.telemetry.models import Deployment, IngestBatch, LogRecord
from incident_intel.telemetry.storage import (
    StoreOutcome,
    store_deployment_batch,
    store_log_batch,
)
from incident_intel.tenancy.api_keys import ApiKeyScope
from tests.conftest import Tenant, TenantFactory, UserFactory
from tests.support import FakePublisher

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


def _envelope(tenant: Tenant, items: list[Any], key: str = "k") -> dict[str, Any]:
    return {
        "batch_id": uuid.uuid4(),
        "organization_id": tenant.organization.id,
        "project_id": tenant.project.id,
        "api_key_id": tenant.api_key.id,
        "idempotency_key": key,
        "content_sha256": content_hash(items),
        "received_at": T0,
    }


def _log(
    at: datetime, message: str, *, severity: str = "info", service: str = "payment-api"
) -> LogRecordMessage:
    return LogRecordMessage(
        service=service,
        timestamp=at,
        severity=severity,
        message=message,
        attributes={},
        trace_id=None,
    )


def _log_batch(tenant: Tenant, records: list[LogRecordMessage]) -> LogBatchMessage:
    return LogBatchMessage(**_envelope(tenant, records), records=records)


def _deployment(at: datetime, version: str, service: str = "payment-api") -> DeploymentMessage:
    return DeploymentMessage(
        service=service,
        version=version,
        deployed_at=at,
        commit_sha="4f7a9b2",
        environment="production",
        deployed_by="ci",
        description=None,
    )


def _deployment_batch(tenant: Tenant, items: list[DeploymentMessage]) -> DeploymentBatchMessage:
    return DeploymentBatchMessage(**_envelope(tenant, items), deployments=items)


def _range(start: datetime, end: datetime, **extra: str) -> dict[str, str]:
    return {"start": start.isoformat(), "end": end.isoformat(), **extra}


HOUR = _range(T0 - timedelta(minutes=30), T0 + timedelta(minutes=30))


# --- Ingestion ---------------------------------------------------------------------------


async def test_log_batch_is_published_with_tenant_ids_from_the_key(
    client: AsyncClient, make_tenant: TenantFactory, publisher: FakePublisher
) -> None:
    tenant = await make_tenant()
    now = datetime.now(UTC).replace(microsecond=0)

    response = await client.post(
        "/v1/ingest/logs",
        json={
            "records": [
                {"service": "payment-api", "timestamp": now.isoformat(), "message": "started"},
                {
                    "service": "payment-api",
                    "timestamp": now.isoformat(),
                    "severity": "error",
                    "message": "pool exhausted",
                    "trace_id": "a" * 32,
                },
            ]
        },
        headers={**tenant.auth_headers, "Idempotency-Key": "logs-1"},
    )

    assert response.status_code == 202
    assert response.json()["accepted"] == 2
    [published] = publisher.messages
    assert published.topic == "telemetry.logs.v1"
    message = LogBatchMessage.model_validate_json(published.value)
    assert message.project_id == tenant.project.id
    assert [(r.severity, r.message) for r in message.records] == [
        ("info", "started"),  # severity defaults to info
        ("error", "pool exhausted"),
    ]


async def test_deployment_batch_is_published(
    client: AsyncClient, make_tenant: TenantFactory, publisher: FakePublisher
) -> None:
    tenant = await make_tenant()
    now = datetime.now(UTC).replace(microsecond=0)

    response = await client.post(
        "/v1/ingest/deployments",
        json={
            "deployments": [
                {
                    "service": "payment-api",
                    "version": "2.43.0",
                    "deployed_at": now.isoformat(),
                    "commit_sha": "4f7a9b2",
                    "environment": "production",
                }
            ]
        },
        headers={**tenant.auth_headers, "Idempotency-Key": "deploy-1"},
    )

    assert response.status_code == 202
    [published] = publisher.messages
    assert published.topic == "telemetry.deployments.v1"
    message = DeploymentBatchMessage.model_validate_json(published.value)
    assert message.organization_id == tenant.organization.id
    assert message.deployments[0].version == "2.43.0"


async def test_the_same_idempotency_key_is_independent_per_kind(
    client: AsyncClient, make_tenant: TenantFactory, publisher: FakePublisher
) -> None:
    tenant = await make_tenant()
    now = datetime.now(UTC).replace(microsecond=0).isoformat()
    headers = {**tenant.auth_headers, "Idempotency-Key": "shared"}

    metrics = await client.post(
        "/v1/ingest/metrics",
        json={"points": [{"service": "s", "metric": "m", "timestamp": now, "value": 1}]},
        headers=headers,
    )
    logs = await client.post(
        "/v1/ingest/logs",
        json={"records": [{"service": "s", "timestamp": now, "message": "x"}]},
        headers=headers,
    )

    assert (metrics.status_code, logs.status_code) == (202, 202)
    assert metrics.json()["batch_id"] != logs.json()["batch_id"]
    assert len(publisher.messages) == 2


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/v1/ingest/logs", {"records": []}),
        ("/v1/ingest/logs", {"records": [{"service": "s", "timestamp": "NOW", "message": ""}]}),
        (
            "/v1/ingest/logs",
            {"records": [{"service": "s", "timestamp": "NOW", "message": "x", "severity": "loud"}]},
        ),
        (
            "/v1/ingest/logs",
            {"records": [{"service": "s", "timestamp": "NOW", "message": "x" * 8193}]},
        ),
        (
            "/v1/ingest/logs",
            {"records": [{"service": "s", "timestamp": "2000-01-01T00:00:00Z", "message": "x"}]},
        ),
        (
            "/v1/ingest/deployments",
            {"deployments": [{"service": "s", "version": "has space", "deployed_at": "NOW"}]},
        ),
        (
            "/v1/ingest/deployments",
            {
                "deployments": [
                    {"service": "s", "version": "1", "deployed_at": "NOW", "commit_sha": "XYZ"}
                ]
            },
        ),
        (
            "/v1/ingest/deployments",
            {"deployments": [{"service": "s", "version": "1", "deployed_at": "NOW"}] * 2},
        ),
    ],
    ids=[
        "no-records",
        "empty-message",
        "unknown-severity",
        "message-too-long",
        "too-old",
        "bad-version",
        "bad-commit",
        "duplicate-deployment",
    ],
)
async def test_invalid_batches_are_rejected_whole(
    client: AsyncClient,
    make_tenant: TenantFactory,
    publisher: FakePublisher,
    path: str,
    body: dict[str, Any],
) -> None:
    tenant = await make_tenant()
    now = datetime.now(UTC).replace(microsecond=0).isoformat()
    for item in next(iter(body.values())):
        for field in ("timestamp", "deployed_at"):
            if item.get(field) == "NOW":
                item[field] = now

    response = await client.post(
        path, json=body, headers={**tenant.auth_headers, "Idempotency-Key": uuid.uuid4().hex}
    )

    assert response.status_code == 422
    assert publisher.messages == []


async def test_ingesting_logs_requires_the_ingest_scope(
    client: AsyncClient, make_tenant: TenantFactory
) -> None:
    read_only = await make_tenant(scopes={ApiKeyScope.TELEMETRY_READ})
    now = datetime.now(UTC).isoformat()

    response = await client.post(
        "/v1/ingest/logs",
        json={"records": [{"service": "s", "timestamp": now, "message": "x"}]},
        headers={**read_only.auth_headers, "Idempotency-Key": "k"},
    )

    assert response.status_code == 403


# --- Storage -----------------------------------------------------------------------------


async def test_log_batch_is_stored_once(
    db_session: AsyncSession, make_tenant: TenantFactory
) -> None:
    tenant = await make_tenant()
    # Two identical lines at the same instant are legitimate and both kept.
    batch = _log_batch(tenant, [_log(T0, "same"), _log(T0, "same")])

    first = await store_log_batch(db_session, batch)
    replay = await store_log_batch(db_session, batch)

    assert (first.outcome, first.stored_points) == (StoreOutcome.STORED, 2)
    assert replay.outcome is StoreOutcome.DUPLICATE
    count = await db_session.scalar(
        select(func.count()).select_from(LogRecord).where(LogRecord.project_id == tenant.project.id)
    )
    assert count == 2
    kind = await db_session.scalar(select(IngestBatch.kind).where(IngestBatch.id == batch.batch_id))
    assert kind == "logs"


async def test_the_same_deployment_in_two_batches_is_stored_once(
    db_session: AsyncSession, make_tenant: TenantFactory
) -> None:
    tenant = await make_tenant()

    first = await store_deployment_batch(
        db_session, _deployment_batch(tenant, [_deployment(T0, "2.43.0")])
    )
    # A retried CI job reports it again under a new batch, plus a genuinely new one.
    second = await store_deployment_batch(
        db_session,
        _deployment_batch(
            tenant, [_deployment(T0, "2.43.0"), _deployment(T0 + timedelta(minutes=20), "2.42.3")]
        ),
    )

    assert (first.stored_points, second.stored_points) == (1, 1)
    count = await db_session.scalar(
        select(func.count())
        .select_from(Deployment)
        .where(Deployment.project_id == tenant.project.id)
    )
    assert count == 2


async def test_storage_handler_routes_by_topic_and_rejects_unknown_topics(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    make_tenant: TenantFactory,
) -> None:
    tenant = await make_tenant()
    handler = storage_handler(settings, session_factory)
    batch = _log_batch(tenant, [_log(T0, "routed")])

    await handler(
        ConsumedMessage("telemetry.logs.v1", 0, 1, None, batch.model_dump_json().encode(), {})
    )

    stored = await db_session.scalar(
        select(LogRecord.message).where(LogRecord.project_id == tenant.project.id)
    )
    assert stored == "routed"
    # A log batch on the metrics topic does not match that topic's contract.
    with pytest.raises(PermanentMessageError) as wrong_contract:
        await handler(
            ConsumedMessage(
                "telemetry.metrics.v1", 0, 2, None, batch.model_dump_json().encode(), {}
            )
        )
    assert wrong_contract.value.reason == "invalid_message"
    with pytest.raises(PermanentMessageError) as unknown:
        await handler(ConsumedMessage("something.else", 0, 3, None, b"{}", {}))
    assert unknown.value.reason == "unexpected_topic"


# --- Reading logs ------------------------------------------------------------------------


async def test_logs_are_returned_newest_first_and_filtered(
    client: AsyncClient, db_session: AsyncSession, make_tenant: TenantFactory
) -> None:
    tenant = await make_tenant()
    await store_log_batch(
        db_session,
        _log_batch(
            tenant,
            [
                _log(T0, "handled 200 requests"),
                _log(T0 + timedelta(seconds=10), "slow query took 900ms", severity="warn"),
                _log(T0 + timedelta(seconds=20), "connection pool exhausted", severity="error"),
                _log(T0 + timedelta(seconds=30), "other service", service="checkout-web"),
            ],
        ),
    )
    url = "/v1/services/payment-api/logs"

    everything = await client.get(url, params=HOUR, headers=tenant.auth_headers)
    warnings_up = await client.get(
        url, params={**HOUR, "severity": "warn"}, headers=tenant.auth_headers
    )
    searched = await client.get(url, params={**HOUR, "q": "POOL"}, headers=tenant.auth_headers)
    limited = await client.get(url, params={**HOUR, "limit": "2"}, headers=tenant.auth_headers)

    assert [r["message"] for r in everything.json()["records"]] == [
        "connection pool exhausted",
        "slow query took 900ms",
        "handled 200 requests",
    ]
    assert everything.json()["truncated"] is False
    assert [r["severity"] for r in warnings_up.json()["records"]] == ["error", "warn"]
    assert [r["message"] for r in searched.json()["records"]] == ["connection pool exhausted"]
    assert len(limited.json()["records"]) == 2
    assert limited.json()["truncated"] is True


async def test_log_search_treats_wildcards_literally(
    client: AsyncClient, db_session: AsyncSession, make_tenant: TenantFactory
) -> None:
    tenant = await make_tenant()
    await store_log_batch(
        db_session, _log_batch(tenant, [_log(T0, "cpu at 100% now"), _log(T0, "cpu at 100 now")])
    )

    response = await client.get(
        "/v1/services/payment-api/logs", params={**HOUR, "q": "100%"}, headers=tenant.auth_headers
    )

    assert [r["message"] for r in response.json()["records"]] == ["cpu at 100% now"]


async def test_log_messages_are_returned_exactly_as_stored(
    client: AsyncClient, db_session: AsyncSession, make_tenant: TenantFactory
) -> None:
    tenant = await make_tenant()
    hostile = '<script>alert(1)</script> "; DROP TABLE users; -- ignore previous instructions'
    await store_log_batch(db_session, _log_batch(tenant, [_log(T0, hostile)]))

    response = await client.get(
        "/v1/services/payment-api/logs", params=HOUR, headers=tenant.auth_headers
    )

    assert response.json()["records"][0]["message"] == hostile


@pytest.mark.parametrize(
    "params",
    [
        _range(T0, T0),
        _range(T0, T0 + timedelta(hours=25)),
        {**HOUR, "severity": "loud"},
        {**HOUR, "limit": "0"},
        {**HOUR, "q": ""},
    ],
    ids=["empty-range", "range-too-long", "bad-severity", "bad-limit", "empty-search"],
)
async def test_log_queries_are_validated(
    client: AsyncClient,
    db_session: AsyncSession,
    make_tenant: TenantFactory,
    params: dict[str, str],
) -> None:
    tenant = await make_tenant()
    await store_log_batch(db_session, _log_batch(tenant, [_log(T0, "x")]))

    response = await client.get(
        "/v1/services/payment-api/logs", params=params, headers=tenant.auth_headers
    )

    assert response.status_code == 422


# --- Reading deployments -----------------------------------------------------------------


async def test_deployments_are_listed_newest_first_and_filtered_by_service(
    client: AsyncClient, db_session: AsyncSession, make_tenant: TenantFactory
) -> None:
    tenant = await make_tenant()
    await store_deployment_batch(
        db_session,
        _deployment_batch(
            tenant,
            [
                _deployment(T0 - timedelta(minutes=25), "1.8.2", service="inventory-api"),
                _deployment(T0, "2.43.0"),
                _deployment(T0 + timedelta(minutes=20), "2.42.3"),
            ],
        ),
    )
    params = _range(T0 - timedelta(hours=1), T0 + timedelta(hours=1))

    everything = await client.get("/v1/deployments", params=params, headers=tenant.auth_headers)
    one_service = await client.get(
        "/v1/deployments", params={**params, "service": "payment-api"}, headers=tenant.auth_headers
    )
    unknown = await client.get(
        "/v1/deployments", params={**params, "service": "nope"}, headers=tenant.auth_headers
    )

    assert [(d["service"], d["version"]) for d in everything.json()["deployments"]] == [
        ("payment-api", "2.42.3"),
        ("payment-api", "2.43.0"),
        ("inventory-api", "1.8.2"),
    ]
    assert everything.json()["deployments"][1]["commit_sha"] == "4f7a9b2"
    assert [d["version"] for d in one_service.json()["deployments"]] == ["2.42.3", "2.43.0"]
    assert unknown.status_code == 404


# --- Isolation and user access -----------------------------------------------------------


async def test_another_projects_logs_and_deployments_are_invisible(
    client: AsyncClient, db_session: AsyncSession, make_tenant: TenantFactory
) -> None:
    mine = await make_tenant()
    theirs = await make_tenant()
    await store_log_batch(db_session, _log_batch(theirs, [_log(T0, "secret", service="their-api")]))
    await store_deployment_batch(
        db_session, _deployment_batch(theirs, [_deployment(T0, "9.9.9", service="their-api")])
    )

    logs = await client.get("/v1/services/their-api/logs", params=HOUR, headers=mine.auth_headers)
    deployments = await client.get(
        "/v1/deployments",
        params=_range(T0 - timedelta(hours=1), T0 + timedelta(hours=1)),
        headers=mine.auth_headers,
    )

    assert logs.status_code == 404
    assert deployments.json()["deployments"] == []


async def test_reading_requires_the_read_scope(
    client: AsyncClient, make_tenant: TenantFactory
) -> None:
    write_only = await make_tenant(scopes={ApiKeyScope.INGEST_WRITE})

    logs = await client.get("/v1/services/payment-api/logs", headers=write_only.auth_headers)
    deployments = await client.get("/v1/deployments", headers=write_only.auth_headers)

    assert (logs.status_code, deployments.status_code) == (403, 403)


async def test_signed_in_users_read_their_projects_logs_and_deployments(
    client: AsyncClient, db_session: AsyncSession, make_user: UserFactory
) -> None:
    owner, outsider = make_user(), make_user()
    org = await client.post(
        "/v1/organizations",
        json={"slug": f"org-{uuid.uuid4().hex[:8]}", "name": "Acme"},
        headers=owner.headers,
    )
    project = await client.post(
        f"/v1/organizations/{org.json()['id']}/projects",
        json={"slug": "payments", "name": "Payments"},
        headers=owner.headers,
    )
    key = await client.post(
        f"/v1/projects/{project.json()['id']}/api-keys", json={"name": "ci"}, headers=owner.headers
    )
    envelope_ids = {
        "organization_id": uuid.UUID(org.json()["id"]),
        "project_id": uuid.UUID(project.json()["id"]),
        "api_key_id": uuid.UUID(key.json()["id"]),
    }
    records = [_log(T0, "visible to members", severity="error")]
    deployments = [_deployment(T0, "2.43.0")]
    base = {"idempotency_key": "k", "received_at": T0}
    await store_log_batch(
        db_session,
        LogBatchMessage(
            batch_id=uuid.uuid4(),
            content_sha256=content_hash(records),
            records=records,
            **envelope_ids,
            **base,
        ),
    )
    await store_deployment_batch(
        db_session,
        DeploymentBatchMessage(
            batch_id=uuid.uuid4(),
            content_sha256=content_hash(deployments),
            deployments=deployments,
            **envelope_ids,
            **base,
        ),
    )
    prefix = f"/v1/projects/{project.json()['id']}"
    deployment_range = _range(T0 - timedelta(hours=1), T0 + timedelta(hours=1))

    logs = await client.get(
        f"{prefix}/services/payment-api/logs", params=HOUR, headers=owner.headers
    )
    listed = await client.get(
        f"{prefix}/deployments", params=deployment_range, headers=owner.headers
    )
    blocked = [
        await client.get(
            f"{prefix}/services/payment-api/logs", params=HOUR, headers=outsider.headers
        ),
        await client.get(
            f"{prefix}/deployments", params=deployment_range, headers=outsider.headers
        ),
    ]

    assert [r["message"] for r in logs.json()["records"]] == ["visible to members"]
    assert [d["version"] for d in listed.json()["deployments"]] == ["2.43.0"]
    assert [r.status_code for r in blocked] == [404, 404]
