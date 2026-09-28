import pytest
from httpx import AsyncClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from incident_intel.audit.service import CLI_ACTOR
from incident_intel.core.errors import ConflictError, NotFoundError
from incident_intel.tenancy.api_keys import generate_api_key
from incident_intel.tenancy.context import ApiKeyPrincipal, TenantContext
from incident_intel.tenancy.models import ApiKey
from incident_intel.tenancy.service import create_organization, create_project, get_project_overview
from tests.conftest import TenantFactory
from tests.support import TEST_PEPPER

pytestmark = pytest.mark.integration


async def test_each_key_sees_only_its_own_project(
    client: AsyncClient, make_tenant: TenantFactory
) -> None:
    tenant_a = await make_tenant()
    tenant_b = await make_tenant()

    body_a = (await client.get("/v1/project", headers=tenant_a.auth_headers)).json()
    body_b = (await client.get("/v1/project", headers=tenant_b.auth_headers)).json()

    assert body_a["organization"]["id"] == str(tenant_a.organization.id)
    assert body_a["project"]["id"] == str(tenant_a.project.id)
    assert body_b["organization"]["id"] == str(tenant_b.organization.id)
    assert body_b["project"]["id"] == str(tenant_b.project.id)


async def test_forged_context_mixing_tenants_is_not_found(
    db_session: AsyncSession, make_tenant: TenantFactory
) -> None:
    tenant_a = await make_tenant()
    tenant_b = await make_tenant()
    real = await get_project_overview(
        db_session,
        TenantContext(
            organization_id=tenant_a.organization.id,
            project_id=tenant_a.project.id,
            principal=_principal_stub(tenant_a.api_key),
        ),
    )
    assert real[1].id == tenant_a.project.id

    forged = TenantContext(
        organization_id=tenant_a.organization.id,
        project_id=tenant_b.project.id,
        principal=_principal_stub(tenant_a.api_key),
    )
    with pytest.raises(NotFoundError):
        await get_project_overview(db_session, forged)


async def test_database_rejects_api_key_with_mismatched_org_and_project(
    db_session: AsyncSession, make_tenant: TenantFactory
) -> None:
    tenant_a = await make_tenant()
    tenant_b = await make_tenant()
    generated = generate_api_key(TEST_PEPPER)

    db_session.add(
        ApiKey(
            organization_id=tenant_a.organization.id,
            project_id=tenant_b.project.id,  # belongs to another organization
            name="cross-tenant",
            key_prefix=generated.prefix,
            key_hash=generated.key_hash,
            scopes=["ingest:write"],
        )
    )
    with pytest.raises(IntegrityError, match="fk_api_keys_organization_id_project_id_projects"):
        await db_session.flush()
    await db_session.rollback()


async def test_same_project_slug_allowed_across_organizations_but_not_within(
    db_session: AsyncSession, make_tenant: TenantFactory
) -> None:
    tenant_a = await make_tenant()
    await make_tenant()  # also has a "payments" project, in another organization

    with pytest.raises(ConflictError):
        await create_project(
            db_session,
            organization_id=tenant_a.organization.id,
            slug="payments",
            name="Duplicate",
            actor=CLI_ACTOR,
        )


async def test_duplicate_organization_slug_is_a_conflict(
    db_session: AsyncSession, make_tenant: TenantFactory
) -> None:
    tenant = await make_tenant()

    with pytest.raises(ConflictError):
        await create_organization(
            db_session, slug=tenant.organization.slug, name="Other", actor=CLI_ACTOR
        )


def _principal_stub(api_key: ApiKey) -> ApiKeyPrincipal:
    return ApiKeyPrincipal(
        api_key_id=api_key.id, name=api_key.name, key_prefix=api_key.key_prefix, scopes=frozenset()
    )
