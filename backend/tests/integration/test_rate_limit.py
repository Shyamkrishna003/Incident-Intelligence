import pytest
from httpx import AsyncClient

from tests.conftest import TenantFactory
from tests.support import FakeCache

pytestmark = pytest.mark.integration


async def test_requests_over_the_limit_get_429_with_retry_after(
    client: AsyncClient, make_tenant: TenantFactory, cache: FakeCache
) -> None:
    cache.limit = 2
    tenant = await make_tenant()

    statuses = [
        (await client.get("/v1/project", headers=tenant.auth_headers)).status_code for _ in range(3)
    ]
    limited = await client.get("/v1/project", headers=tenant.auth_headers)

    assert statuses == [200, 200, 429]
    assert limited.status_code == 429
    assert 1 <= int(limited.headers["Retry-After"]) <= cache.window_seconds
    error = limited.json()["error"]
    assert error["code"] == "rate_limited"
    assert error["request_id"] == limited.headers["X-Request-ID"]


async def test_each_api_key_has_its_own_budget(
    client: AsyncClient, make_tenant: TenantFactory, cache: FakeCache
) -> None:
    cache.limit = 1
    noisy = await make_tenant()
    quiet = await make_tenant()
    await client.get("/v1/project", headers=noisy.auth_headers)
    assert (await client.get("/v1/project", headers=noisy.auth_headers)).status_code == 429

    response = await client.get("/v1/project", headers=quiet.auth_headers)

    assert response.status_code == 200


async def test_rejected_credentials_do_not_consume_any_budget(
    client: AsyncClient, cache: FakeCache
) -> None:
    await client.get("/v1/project", headers={"Authorization": "Bearer nonsense"})

    assert cache.counters == {}


async def test_limiter_outage_lets_requests_through_by_default(
    client: AsyncClient, make_tenant: TenantFactory, cache: FakeCache
) -> None:
    cache.limit = 0
    tenant = await make_tenant()
    cache.unavailable = True

    response = await client.get("/v1/project", headers=tenant.auth_headers)

    assert response.status_code == 200


async def test_limiter_outage_rejects_with_503_when_configured_to_fail_closed(
    client: AsyncClient, make_tenant: TenantFactory, cache: FakeCache
) -> None:
    tenant = await make_tenant()
    cache.unavailable = True
    cache.fail_open = False

    response = await client.get("/v1/project", headers=tenant.auth_headers)

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
