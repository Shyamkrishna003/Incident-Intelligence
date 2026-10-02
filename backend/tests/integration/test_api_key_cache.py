"""Authentication through the API-key cache, and its interaction with revocation."""

from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from incident_intel.audit.service import CLI_ACTOR
from incident_intel.core.errors import AuthenticationError
from incident_intel.tenancy.service import authenticate_api_key, issue_api_key, revoke_api_key
from tests.conftest import TenantFactory
from tests.support import TEST_PEPPER, FakeCache

pytestmark = pytest.mark.integration


class _NoDatabase:
    """A session that fails the test if authentication touches the database."""

    async def scalar(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("the database was queried despite a cache hit")


NO_DATABASE = cast(AsyncSession, _NoDatabase())


async def test_first_request_caches_the_key_without_its_secret(
    client: AsyncClient, make_tenant: TenantFactory, cache: FakeCache
) -> None:
    tenant = await make_tenant()

    await client.get("/v1/project", headers=tenant.auth_headers)

    entry = cache.api_keys[tenant.api_key.key_prefix]
    assert entry.project_id == tenant.project.id
    assert entry.key_hash == tenant.api_key.key_hash
    secret = tenant.plaintext_key.rsplit("_", 1)[-1]
    assert secret not in entry.model_dump_json()


async def test_cached_key_authenticates_without_the_database(
    db_session: AsyncSession, make_tenant: TenantFactory, cache: FakeCache
) -> None:
    tenant = await make_tenant()
    await authenticate_api_key(db_session, tenant.plaintext_key, pepper=TEST_PEPPER, cache=cache)

    ctx = await authenticate_api_key(
        NO_DATABASE, tenant.plaintext_key, pepper=TEST_PEPPER, cache=cache
    )

    assert ctx.project_id == tenant.project.id
    assert ctx.organization_id == tenant.organization.id
    assert ctx.principal.scopes == frozenset(tenant.api_key.scopes)


async def test_wrong_secret_for_a_cached_prefix_is_rejected(
    db_session: AsyncSession, make_tenant: TenantFactory, cache: FakeCache
) -> None:
    tenant = await make_tenant()
    await authenticate_api_key(db_session, tenant.plaintext_key, pepper=TEST_PEPPER, cache=cache)
    forged = f"ii_{tenant.api_key.key_prefix}_" + "A" * 43

    with pytest.raises(AuthenticationError) as excinfo:
        await authenticate_api_key(NO_DATABASE, forged, pepper=TEST_PEPPER, cache=cache)

    assert excinfo.value.reason == "unknown_key"


async def test_cached_key_still_expires_on_time(
    db_session: AsyncSession, make_tenant: TenantFactory, cache: FakeCache
) -> None:
    tenant = await make_tenant()
    expires_at = datetime.now(UTC) + timedelta(hours=1)
    issued = await issue_api_key(
        db_session,
        project=tenant.project,
        name="expiring",
        pepper=TEST_PEPPER,
        actor=CLI_ACTOR,
        expires_at=expires_at,
    )
    await authenticate_api_key(db_session, issued.plaintext, pepper=TEST_PEPPER, cache=cache)

    with pytest.raises(AuthenticationError) as excinfo:
        await authenticate_api_key(
            NO_DATABASE, issued.plaintext, pepper=TEST_PEPPER, cache=cache, now=expires_at
        )

    assert excinfo.value.reason == "expired"


async def test_revoked_keys_are_never_cached(
    client: AsyncClient, db_session: AsyncSession, make_tenant: TenantFactory, cache: FakeCache
) -> None:
    tenant = await make_tenant()
    await revoke_api_key(db_session, key_prefix=tenant.api_key.key_prefix, actor=CLI_ACTOR)

    response = await client.get("/v1/project", headers=tenant.auth_headers)

    assert response.status_code == 401
    assert cache.api_keys == {}


async def test_revocation_takes_effect_immediately_despite_the_cache(
    client: AsyncClient, db_session: AsyncSession, make_tenant: TenantFactory, cache: FakeCache
) -> None:
    tenant = await make_tenant()
    assert (await client.get("/v1/project", headers=tenant.auth_headers)).status_code == 200
    assert tenant.api_key.key_prefix in cache.api_keys

    await revoke_api_key(
        db_session, key_prefix=tenant.api_key.key_prefix, actor=CLI_ACTOR, cache=cache
    )

    assert tenant.api_key.key_prefix not in cache.api_keys
    assert (await client.get("/v1/project", headers=tenant.auth_headers)).status_code == 401


async def test_cache_outage_falls_back_to_the_database(
    client: AsyncClient, make_tenant: TenantFactory, cache: FakeCache
) -> None:
    tenant = await make_tenant()
    cache.unavailable = True

    response = await client.get("/v1/project", headers=tenant.auth_headers)

    assert response.status_code == 200
