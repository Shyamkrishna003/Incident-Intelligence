from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from incident_intel.telemetry.messages import MetricPointMessage
from incident_intel.telemetry.storage import store_metric_batch
from incident_intel.tenancy.api_keys import ApiKeyScope
from tests.conftest import Tenant, TenantFactory
from tests.support import metric_batch, metric_point

pytestmark = pytest.mark.integration

T0 = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
URL = "/v1/services/payment-api/metrics/http.server.duration.p95"


async def _store(session: AsyncSession, tenant: Tenant, points: list[MetricPointMessage]) -> None:
    await store_metric_batch(
        session,
        metric_batch(
            organization_id=tenant.organization.id,
            project_id=tenant.project.id,
            api_key_id=tenant.api_key.id,
            points=points,
            received_at=T0,
        ),
    )


def _range(start: datetime, end: datetime, **extra: str) -> dict[str, str]:
    return {"start": start.isoformat(), "end": end.isoformat(), **extra}


async def test_lists_services_for_the_callers_project_only(
    client: AsyncClient, db_session: AsyncSession, make_tenant: TenantFactory
) -> None:
    tenant_a = await make_tenant()
    tenant_b = await make_tenant()
    await _store(
        db_session, tenant_a, [metric_point(T0, 1.0, service="checkout"), metric_point(T0, 1.0)]
    )
    await _store(db_session, tenant_b, [metric_point(T0, 1.0, service="b-only")])

    response = await client.get("/v1/services", headers=tenant_a.auth_headers)

    assert response.status_code == 200
    assert [s["name"] for s in response.json()["services"]] == ["checkout", "payment-api"]


async def test_returns_points_grouped_by_series(
    client: AsyncClient, db_session: AsyncSession, make_tenant: TenantFactory
) -> None:
    tenant = await make_tenant()
    await _store(
        db_session,
        tenant,
        [
            metric_point(T0, 100.0, attributes={"region": "eu"}),
            metric_point(T0 + timedelta(seconds=10), 120.0, attributes={"region": "eu"}),
            metric_point(T0, 90.0, attributes={"region": "us"}),
        ],
    )

    response = await client.get(
        URL, params=_range(T0, T0 + timedelta(minutes=1)), headers=tenant.auth_headers
    )

    assert response.status_code == 200
    body = response.json()
    assert body["truncated"] is False
    by_region = {s["attributes"]["region"]: s for s in body["series"]}
    assert [p["value"] for p in by_region["eu"]["points"]] == [100.0, 120.0]
    assert [p["value"] for p in by_region["us"]["points"]] == [90.0]
    assert by_region["eu"]["unit"] == "ms"


async def test_range_is_start_inclusive_end_exclusive(
    client: AsyncClient, db_session: AsyncSession, make_tenant: TenantFactory
) -> None:
    tenant = await make_tenant()
    await _store(
        db_session,
        tenant,
        [metric_point(T0 + timedelta(seconds=s), float(s)) for s in (0, 30, 60)],
    )

    response = await client.get(
        URL, params=_range(T0, T0 + timedelta(seconds=60)), headers=tenant.auth_headers
    )

    [series] = response.json()["series"]
    assert [p["value"] for p in series["points"]] == [0.0, 30.0]


async def test_limit_truncates_and_says_so(
    client: AsyncClient, db_session: AsyncSession, make_tenant: TenantFactory
) -> None:
    tenant = await make_tenant()
    await _store(
        db_session, tenant, [metric_point(T0 + timedelta(seconds=s), 1.0) for s in range(5)]
    )

    response = await client.get(
        URL,
        params=_range(T0, T0 + timedelta(minutes=1), limit="3"),
        headers=tenant.auth_headers,
    )

    body = response.json()
    assert body["truncated"] is True
    assert len(body["series"][0]["points"]) == 3


async def test_defaults_to_the_last_hour(
    client: AsyncClient, db_session: AsyncSession, make_tenant: TenantFactory
) -> None:
    tenant = await make_tenant()
    now = datetime.now(UTC).replace(microsecond=0)
    await _store(
        db_session,
        tenant,
        [
            metric_point(now - timedelta(hours=2), 1.0),
            metric_point(now - timedelta(minutes=5), 2.0),
        ],
    )

    response = await client.get(URL, headers=tenant.auth_headers)

    [series] = response.json()["series"]
    assert [p["value"] for p in series["points"]] == [2.0]


@pytest.mark.parametrize(
    "params",
    [
        _range(T0, T0),  # empty range
        _range(T0 + timedelta(hours=1), T0),  # reversed
        _range(T0, T0 + timedelta(hours=25)),  # longer than 24h
        {"start": "2026-09-28T12:00:00"},  # naive timestamp
        {"limit": "0"},
        {"limit": "10001"},
    ],
    ids=["empty", "reversed", "too-long", "naive", "limit-zero", "limit-too-high"],
)
async def test_rejects_invalid_ranges(
    client: AsyncClient, make_tenant: TenantFactory, params: dict[str, str]
) -> None:
    tenant = await make_tenant()

    response = await client.get(URL, params=params, headers=tenant.auth_headers)

    assert response.status_code == 422


async def test_unknown_service_or_metric_is_not_found(
    client: AsyncClient, db_session: AsyncSession, make_tenant: TenantFactory
) -> None:
    tenant = await make_tenant()
    await _store(db_session, tenant, [metric_point(T0, 1.0)])

    missing_service = await client.get(
        "/v1/services/nope/metrics/http.server.duration.p95", headers=tenant.auth_headers
    )
    missing_metric = await client.get(
        "/v1/services/payment-api/metrics/nope", headers=tenant.auth_headers
    )

    assert missing_service.status_code == 404
    assert missing_metric.status_code == 404


async def test_another_projects_data_is_not_found(
    client: AsyncClient, db_session: AsyncSession, make_tenant: TenantFactory
) -> None:
    owner = await make_tenant()
    other = await make_tenant()
    await _store(db_session, owner, [metric_point(T0, 1.0)])

    response = await client.get(
        URL, params=_range(T0, T0 + timedelta(minutes=1)), headers=other.auth_headers
    )

    assert response.status_code == 404


async def test_reads_require_the_read_scope(
    client: AsyncClient, make_tenant: TenantFactory
) -> None:
    write_only = await make_tenant(scopes={ApiKeyScope.INGEST_WRITE})

    services = await client.get("/v1/services", headers=write_only.auth_headers)
    metric = await client.get(URL, headers=write_only.auth_headers)

    assert services.status_code == 403
    assert metric.status_code == 403


async def test_lists_a_services_metrics_with_series_counts(
    client: AsyncClient, db_session: AsyncSession, make_tenant: TenantFactory
) -> None:
    tenant = await make_tenant()
    other = await make_tenant()
    await _store(
        db_session,
        tenant,
        [
            metric_point(T0, 1.0, metric="latency", attributes={"region": "eu"}),
            metric_point(T0, 2.0, metric="latency", attributes={"region": "us"}),
            metric_point(T0, 3.0, metric="errors", unit=None),
            metric_point(T0, 4.0, metric="other-service-metric", service="checkout"),
        ],
    )
    await _store(db_session, other, [metric_point(T0, 9.0, metric="not-mine")])

    response = await client.get("/v1/services/payment-api/metrics", headers=tenant.auth_headers)

    assert response.status_code == 200
    assert response.json() == {
        "service": "payment-api",
        "metrics": [
            {"name": "errors", "unit": None, "series_count": 1},
            {"name": "latency", "unit": "ms", "series_count": 2},
        ],
    }


async def test_metrics_of_an_unknown_or_foreign_service_are_not_found(
    client: AsyncClient, db_session: AsyncSession, make_tenant: TenantFactory
) -> None:
    tenant = await make_tenant()
    other = await make_tenant()
    await _store(db_session, other, [metric_point(T0, 1.0, service="b-only")])

    for service in ("nope", "b-only"):
        response = await client.get(f"/v1/services/{service}/metrics", headers=tenant.auth_headers)
        assert response.status_code == 404
