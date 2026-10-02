import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient

from incident_intel.telemetry.messages import MetricBatchMessage
from incident_intel.tenancy.api_keys import ApiKeyScope
from tests.conftest import TenantFactory
from tests.support import FakeCache, FakePublisher

pytestmark = pytest.mark.integration

TOPIC = "telemetry.metrics.v1"


def _body(*values: float, service: str = "payment-api") -> dict[str, Any]:
    now = datetime.now(UTC).replace(microsecond=0)
    return {
        "points": [
            {
                "service": service,
                "metric": "http.server.duration.p95",
                "unit": "ms",
                "timestamp": (now - timedelta(seconds=len(values) - i)).isoformat(),
                "value": value,
                "attributes": {"region": "eu-west-1"},
            }
            for i, value in enumerate(values)
        ]
    }


def _headers(auth: dict[str, str], key: str | None = None) -> dict[str, str]:
    return {**auth, "Idempotency-Key": key or uuid.uuid4().hex}


async def test_accepts_batch_and_publishes_it_with_tenant_ids_from_the_key(
    client: AsyncClient, make_tenant: TenantFactory, publisher: FakePublisher
) -> None:
    tenant = await make_tenant()

    response = await client.post(
        "/v1/ingest/metrics",
        json=_body(120.0, 350.0),
        headers={**_headers(tenant.auth_headers, "batch-1"), "X-Request-ID": "req-42"},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "accepted"
    assert body["accepted_points"] == 2

    [published] = publisher.messages
    assert published.topic == TOPIC
    assert published.key == str(tenant.project.id).encode()
    assert published.headers == {"schema_version": "1", "request_id": "req-42"}
    message = MetricBatchMessage.model_validate_json(published.value)
    assert str(message.batch_id) == body["batch_id"]
    assert message.organization_id == tenant.organization.id
    assert message.project_id == tenant.project.id
    assert message.api_key_id == tenant.api_key.id
    assert message.idempotency_key == "batch-1"
    assert [p.value for p in message.points] == [120.0, 350.0]
    assert all(p.timestamp.utcoffset() == timedelta(0) for p in message.points)


async def test_retry_with_same_idempotency_key_returns_same_batch_id(
    client: AsyncClient, make_tenant: TenantFactory
) -> None:
    tenant = await make_tenant()
    body = _body(1.0)

    first = await client.post(
        "/v1/ingest/metrics", json=body, headers=_headers(tenant.auth_headers, "same")
    )
    retry = await client.post(
        "/v1/ingest/metrics", json=body, headers=_headers(tenant.auth_headers, "same")
    )
    other = await client.post(
        "/v1/ingest/metrics", json=body, headers=_headers(tenant.auth_headers, "different")
    )

    assert first.json()["batch_id"] == retry.json()["batch_id"]
    assert other.json()["batch_id"] != first.json()["batch_id"]


async def test_same_idempotency_key_in_two_projects_does_not_collide(
    client: AsyncClient, make_tenant: TenantFactory
) -> None:
    tenant_a = await make_tenant()
    tenant_b = await make_tenant()

    a = await client.post(
        "/v1/ingest/metrics", json=_body(1.0), headers=_headers(tenant_a.auth_headers, "k")
    )
    b = await client.post(
        "/v1/ingest/metrics", json=_body(1.0), headers=_headers(tenant_b.auth_headers, "k")
    )

    assert a.json()["batch_id"] != b.json()["batch_id"]


@pytest.mark.parametrize("key", [None, "", "has space", "x" * 129])
async def test_requires_a_valid_idempotency_key(
    client: AsyncClient, make_tenant: TenantFactory, publisher: FakePublisher, key: str | None
) -> None:
    tenant = await make_tenant()
    headers = dict(tenant.auth_headers)
    if key is not None:
        headers["Idempotency-Key"] = key

    response = await client.post("/v1/ingest/metrics", json=_body(1.0), headers=headers)

    assert response.status_code == 422
    assert publisher.messages == []


async def test_requires_authentication(client: AsyncClient, publisher: FakePublisher) -> None:
    response = await client.post(
        "/v1/ingest/metrics", json=_body(1.0), headers={"Idempotency-Key": "k"}
    )

    assert response.status_code == 401
    assert publisher.messages == []


async def test_requires_ingest_scope(
    client: AsyncClient, make_tenant: TenantFactory, publisher: FakePublisher
) -> None:
    read_only = await make_tenant(scopes={ApiKeyScope.TELEMETRY_READ})

    response = await client.post(
        "/v1/ingest/metrics", json=_body(1.0), headers=_headers(read_only.auth_headers)
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"
    assert publisher.messages == []


async def test_invalid_point_rejects_the_whole_batch(
    client: AsyncClient, make_tenant: TenantFactory, publisher: FakePublisher
) -> None:
    tenant = await make_tenant()
    body = _body(1.0, 2.0)
    body["points"][1]["timestamp"] = "2000-01-01T00:00:00Z"

    response = await client.post(
        "/v1/ingest/metrics", json=body, headers=_headers(tenant.auth_headers)
    )

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"] == [
        {
            "loc": ["body", "points", 1, "timestamp"],
            "msg": "Timestamp is older than allowed.",
            "type": "timestamp_too_old",
        }
    ]
    assert publisher.messages == []


async def test_schema_errors_do_not_echo_submitted_values(
    client: AsyncClient, make_tenant: TenantFactory
) -> None:
    tenant = await make_tenant()
    body = _body(1.0)
    body["points"][0]["value"] = "secret-looking-string"

    response = await client.post(
        "/v1/ingest/metrics", json=body, headers=_headers(tenant.auth_headers)
    )

    assert response.status_code == 422
    assert "secret-looking-string" not in response.text


async def test_kafka_outage_returns_503_with_retry_after(
    client: AsyncClient, make_tenant: TenantFactory, publisher: FakePublisher
) -> None:
    tenant = await make_tenant()
    publisher.fail_publish = True

    response = await client.post(
        "/v1/ingest/metrics", json=_body(1.0), headers=_headers(tenant.auth_headers)
    )

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert response.json()["error"]["code"] == "service_unavailable"


# --- Idempotency at the API edge (Redis-backed; an in-memory fake here) --------------------


async def test_retry_of_a_published_batch_is_not_published_again(
    client: AsyncClient, make_tenant: TenantFactory, publisher: FakePublisher
) -> None:
    tenant = await make_tenant()
    body = _body(1.0, 2.0)
    headers = _headers(tenant.auth_headers, "retry-me")

    first = await client.post("/v1/ingest/metrics", json=body, headers=headers)
    retry = await client.post("/v1/ingest/metrics", json=body, headers=headers)

    assert (first.status_code, retry.status_code) == (202, 202)
    assert retry.json() == first.json()
    assert len(publisher.messages) == 1


async def test_reusing_a_key_with_different_data_is_a_409(
    client: AsyncClient, make_tenant: TenantFactory, publisher: FakePublisher
) -> None:
    tenant = await make_tenant()
    headers = _headers(tenant.auth_headers, "reused")
    await client.post("/v1/ingest/metrics", json=_body(1.0), headers=headers)

    response = await client.post("/v1/ingest/metrics", json=_body(999.0), headers=headers)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "idempotency_conflict"
    assert len(publisher.messages) == 1  # the conflicting batch never reached Kafka


async def test_retry_after_a_failed_publish_is_published(
    client: AsyncClient, make_tenant: TenantFactory, publisher: FakePublisher
) -> None:
    tenant = await make_tenant()
    body = _body(1.0)
    headers = _headers(tenant.auth_headers, "kafka-was-down")
    publisher.fail_publish = True
    failed = await client.post("/v1/ingest/metrics", json=body, headers=headers)
    assert failed.status_code == 503

    publisher.fail_publish = False
    retry = await client.post("/v1/ingest/metrics", json=body, headers=headers)

    # The claim was left "pending", so the retry must reach Kafka (not be mistaken for done).
    assert retry.status_code == 202
    assert len(publisher.messages) == 1


async def test_rejected_batch_does_not_use_up_its_key(
    client: AsyncClient, make_tenant: TenantFactory, publisher: FakePublisher
) -> None:
    tenant = await make_tenant()
    headers = _headers(tenant.auth_headers, "fix-and-resend")
    invalid = _body(1.0)
    invalid["points"][0]["timestamp"] = "2000-01-01T00:00:00Z"
    assert (
        await client.post("/v1/ingest/metrics", json=invalid, headers=headers)
    ).status_code == 422

    corrected = await client.post("/v1/ingest/metrics", json=_body(1.0), headers=headers)

    assert corrected.status_code == 202
    assert len(publisher.messages) == 1


async def test_redis_outage_does_not_block_ingestion(
    client: AsyncClient, make_tenant: TenantFactory, publisher: FakePublisher, cache: FakeCache
) -> None:
    tenant = await make_tenant()
    cache.unavailable = True
    body = _body(1.0)
    headers = _headers(tenant.auth_headers, "redis-down")

    first = await client.post("/v1/ingest/metrics", json=body, headers=headers)
    retry = await client.post("/v1/ingest/metrics", json=body, headers=headers)

    assert (first.status_code, retry.status_code) == (202, 202)
    assert first.json()["batch_id"] == retry.json()["batch_id"]
    # Without the edge check both reach Kafka; the storage consumer stores the batch once.
    assert len(publisher.messages) == 2
