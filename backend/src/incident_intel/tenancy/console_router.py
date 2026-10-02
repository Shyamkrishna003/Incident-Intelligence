"""Endpoints for signed-in users (the web console): account, organizations, projects, and
API-key management. Every route checks the user's role in the target organization."""

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Response
from sqlalchemy.ext.asyncio import AsyncSession

from incident_intel.api.deps import (
    get_cache,
    get_current_user,
    get_settings_dep,
    require_organization_role,
    require_project_role,
)
from incident_intel.cache.services import CacheServices
from incident_intel.core.config import Settings
from incident_intel.db.session import get_session
from incident_intel.tenancy.access import OrganizationAccess, ProjectAccess
from incident_intel.tenancy.console_schemas import (
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyDetailOut,
    ApiKeyListResponse,
    MeResponse,
    OrganizationCreate,
    OrganizationCreated,
    OrganizationMembershipOut,
    ProjectCreate,
    UserOut,
)
from incident_intel.tenancy.models import ApiKey, User
from incident_intel.tenancy.roles import Role
from incident_intel.tenancy.schemas import ProjectOut
from incident_intel.tenancy.service import (
    create_project,
    issue_api_key,
    list_api_keys,
    revoke_api_key,
)
from incident_intel.tenancy.users import create_organization_for_user, list_user_organizations

router = APIRouter(prefix="/v1", tags=["console"])

CurrentUser = Annotated[User, Depends(get_current_user)]
Session = Annotated[AsyncSession, Depends(get_session)]
AppSettings = Annotated[Settings, Depends(get_settings_dep)]
OrgAdmin = Annotated[OrganizationAccess, Depends(require_organization_role(Role.ADMIN))]
ProjectAdmin = Annotated[ProjectAccess, Depends(require_project_role(Role.ADMIN))]


def _api_key_out(api_key: ApiKey) -> ApiKeyDetailOut:
    return ApiKeyDetailOut(
        id=api_key.id,
        name=api_key.name,
        prefix=api_key.key_prefix,
        scopes=list(api_key.scopes),
        created_at=api_key.created_at,
        expires_at=api_key.expires_at,
        last_used_at=api_key.last_used_at,
        revoked_at=api_key.revoked_at,
    )


@router.get("/me", response_model=MeResponse)
async def read_me(user: CurrentUser, session: Session) -> MeResponse:
    """The signed-in user, their organizations and roles, and each organization's projects."""
    organizations = await list_user_organizations(session, user)
    return MeResponse(
        user=UserOut(
            id=user.id,
            email=user.email,
            email_verified=user.email_verified,
            display_name=user.display_name,
        ),
        organizations=[
            OrganizationMembershipOut(
                id=item.organization.id,
                slug=item.organization.slug,
                name=item.organization.name,
                role=item.role,
                projects=[ProjectOut.model_validate(project) for project in item.projects],
            )
            for item in organizations
        ],
    )


@router.post("/organizations", response_model=OrganizationCreated, status_code=201)
async def create_organization_route(
    body: OrganizationCreate, user: CurrentUser, session: Session, settings: AppSettings
) -> OrganizationCreated:
    """Create an organization; the caller becomes its owner. Needs a verified email."""
    organization = await create_organization_for_user(
        session,
        user,
        slug=body.slug,
        name=body.name,
        max_owned=settings.max_owned_organizations_per_user,
    )
    return OrganizationCreated(
        id=organization.id, slug=organization.slug, name=organization.name, role=Role.OWNER
    )


@router.post(
    "/organizations/{organization_id}/projects", response_model=ProjectOut, status_code=201
)
async def create_project_route(
    body: ProjectCreate, access: OrgAdmin, session: Session
) -> ProjectOut:
    """Create a project in an organization. Needs the admin role."""
    project = await create_project(
        session,
        organization_id=access.organization_id,
        slug=body.slug,
        name=body.name,
        actor=access.actor,
    )
    return ProjectOut.model_validate(project)


@router.get("/projects/{project_id}/api-keys", response_model=ApiKeyListResponse)
async def list_api_keys_route(access: ProjectAdmin, session: Session) -> ApiKeyListResponse:
    """A project's API keys, including revoked ones. Never returns key material."""
    api_keys = await list_api_keys(session, project=access.project)
    return ApiKeyListResponse(api_keys=[_api_key_out(api_key) for api_key in api_keys])


@router.post("/projects/{project_id}/api-keys", response_model=ApiKeyCreated, status_code=201)
async def create_api_key_route(
    body: ApiKeyCreate, access: ProjectAdmin, session: Session, settings: AppSettings
) -> ApiKeyCreated:
    """Create an API key. The full key is in this response only; it is not stored."""
    expires_at = (
        datetime.now(UTC) + timedelta(days=body.expires_in_days)
        if body.expires_in_days is not None
        else None
    )
    issued = await issue_api_key(
        session,
        project=access.project,
        name=body.name,
        pepper=settings.api_key_pepper.get_secret_value(),
        actor=access.actor,
        scopes=body.scopes,
        expires_at=expires_at,
    )
    return ApiKeyCreated(**_api_key_out(issued.api_key).model_dump(), key=issued.plaintext)


@router.delete("/projects/{project_id}/api-keys/{prefix}", status_code=204)
async def revoke_api_key_route(
    prefix: Annotated[str, Path(pattern=r"^[a-z0-9]{12}$")],
    access: ProjectAdmin,
    session: Session,
    cache: Annotated[CacheServices, Depends(get_cache)],
) -> Response:
    """Revoke one of this project's API keys. Revoking an already revoked key succeeds."""
    await revoke_api_key(
        session,
        key_prefix=prefix,
        actor=access.actor,
        cache=cache.api_keys,
        project_id=access.project.id,
    )
    return Response(status_code=204)
