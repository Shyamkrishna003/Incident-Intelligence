from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from incident_intel.audit.models import AuditLog
from incident_intel.audit.service import CLI_ACTOR
from incident_intel.core.errors import AuthenticationError, InvalidInputError
from incident_intel.tenancy.api_keys import ApiKeyScope, generate_api_key
from incident_intel.tenancy.models import ApiKey
from incident_intel.tenancy.service import authenticate_api_key, issue_api_key, revoke_api_key
from tests.conftest import TenantFactory
from tests.support import TEST_PEPPER

pytestmark = pytest.mark.integration

GENERIC_401 = {"code": "unauthenticated", "message": "Missing or invalid credentials."}


def _assert_generic_401(response_json: dict[str, dict[str, str]]) -> None:
    error = response_json["error"]
    assert {"code": error["code"], "message": error["message"]} == GENERIC_401


async def test_valid_key_returns_its_project(
    client: AsyncClient, make_tenant: TenantFactory
) -> None:
    tenant = await make_tenant()

    response = await client.get("/v1/project", headers=tenant.auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["organization"]["id"] == str(tenant.organization.id)
    assert body["project"] == {
        "id": str(tenant.project.id),
        "slug": "payments",
        "name": "Payments",
    }
    assert body["api_key"]["prefix"] == tenant.api_key.key_prefix
    assert body["api_key"]["scopes"] == sorted(scope.value for scope in ApiKeyScope)


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Basic dXNlcjpwYXNz"},
        {"Authorization": "Bearer not-a-key"},
        {"Authorization": "Bearer " + generate_api_key(TEST_PEPPER).plaintext},  # unknown prefix
    ],
    ids=["missing", "wrong-scheme", "malformed", "unknown"],
)
async def test_rejects_invalid_credentials(client: AsyncClient, headers: dict[str, str]) -> None:
    response = await client.get("/v1/project", headers=headers)

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    _assert_generic_401(response.json())


async def test_rejects_known_prefix_with_wrong_secret(
    client: AsyncClient, make_tenant: TenantFactory
) -> None:
    tenant = await make_tenant()
    forged = f"ii_{tenant.api_key.key_prefix}_" + "A" * 43

    response = await client.get("/v1/project", headers={"Authorization": f"Bearer {forged}"})

    assert response.status_code == 401


async def test_rejects_revoked_key(
    client: AsyncClient, db_session: AsyncSession, make_tenant: TenantFactory
) -> None:
    tenant = await make_tenant()
    await revoke_api_key(db_session, key_prefix=tenant.api_key.key_prefix, actor=CLI_ACTOR)

    response = await client.get("/v1/project", headers=tenant.auth_headers)

    assert response.status_code == 401
    _assert_generic_401(response.json())


async def test_rejects_expired_key(
    client: AsyncClient, db_session: AsyncSession, make_tenant: TenantFactory
) -> None:
    tenant = await make_tenant()
    tenant.api_key.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()

    response = await client.get("/v1/project", headers=tenant.auth_headers)

    assert response.status_code == 401


async def test_expiry_is_enforced_at_the_boundary(
    db_session: AsyncSession, make_tenant: TenantFactory
) -> None:
    tenant = await make_tenant()
    expires_at = datetime.now(UTC) + timedelta(days=1)
    issued = await issue_api_key(
        db_session,
        project=tenant.project,
        name="expiring",
        pepper=TEST_PEPPER,
        actor=CLI_ACTOR,
        expires_at=expires_at,
    )

    before = await authenticate_api_key(
        db_session, issued.plaintext, pepper=TEST_PEPPER, now=expires_at - timedelta(seconds=1)
    )
    assert before.project_id == tenant.project.id
    with pytest.raises(AuthenticationError) as excinfo:
        await authenticate_api_key(db_session, issued.plaintext, pepper=TEST_PEPPER, now=expires_at)
    assert excinfo.value.reason == "expired"


async def test_cannot_issue_key_that_is_already_expired(
    db_session: AsyncSession, make_tenant: TenantFactory
) -> None:
    tenant = await make_tenant()

    with pytest.raises(InvalidInputError):
        await issue_api_key(
            db_session,
            project=tenant.project,
            name="bad",
            pepper=TEST_PEPPER,
            actor=CLI_ACTOR,
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
        )


async def test_last_used_at_is_updated_with_bounded_writes(
    db_session: AsyncSession, make_tenant: TenantFactory
) -> None:
    tenant = await make_tenant()
    first = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
    resolution = timedelta(seconds=60)

    async def use_key(at: datetime) -> datetime | None:
        await authenticate_api_key(
            db_session,
            tenant.plaintext_key,
            pepper=TEST_PEPPER,
            last_used_resolution=resolution,
            now=at,
        )
        await db_session.refresh(tenant.api_key)
        return tenant.api_key.last_used_at

    assert await use_key(first) == first
    assert await use_key(first + timedelta(seconds=30)) == first  # within resolution: no write
    assert await use_key(first + timedelta(seconds=61)) == first + timedelta(seconds=61)


async def test_only_a_hash_of_the_key_is_stored(
    db_session: AsyncSession, make_tenant: TenantFactory
) -> None:
    tenant = await make_tenant()

    stored = await db_session.scalar(select(ApiKey).where(ApiKey.id == tenant.api_key.id))

    assert stored is not None
    assert stored.key_hash != tenant.plaintext_key
    secret_part = tenant.plaintext_key.rsplit("_", 1)[-1]
    for value in (stored.key_hash, stored.key_prefix, stored.name):
        assert secret_part not in value


async def test_lifecycle_is_audited_without_secrets(
    db_session: AsyncSession, make_tenant: TenantFactory
) -> None:
    tenant = await make_tenant()
    await revoke_api_key(db_session, key_prefix=tenant.api_key.key_prefix, actor=CLI_ACTOR)

    rows = (
        await db_session.scalars(
            select(AuditLog)
            .where(AuditLog.organization_id == tenant.organization.id)
            .order_by(AuditLog.created_at, AuditLog.action)
        )
    ).all()

    actions = sorted(row.action for row in rows)
    assert actions == [
        "api_key.created",
        "api_key.revoked",
        "organization.created",
        "project.created",
    ]
    assert all(row.actor_type == "cli" for row in rows)
    serialized = " ".join(str(row.details) for row in rows)
    assert tenant.plaintext_key not in serialized
    assert tenant.api_key.key_hash not in serialized


async def test_revoking_twice_is_idempotent_and_audited_once(
    db_session: AsyncSession, make_tenant: TenantFactory
) -> None:
    tenant = await make_tenant()
    first = await revoke_api_key(db_session, key_prefix=tenant.api_key.key_prefix, actor=CLI_ACTOR)
    revoked_at = first.revoked_at
    second = await revoke_api_key(db_session, key_prefix=tenant.api_key.key_prefix, actor=CLI_ACTOR)

    assert second.revoked_at == revoked_at
    count = len(
        (
            await db_session.scalars(
                select(AuditLog).where(
                    AuditLog.action == "api_key.revoked",
                    AuditLog.target_id == str(tenant.api_key.id),
                )
            )
        ).all()
    )
    assert count == 1
