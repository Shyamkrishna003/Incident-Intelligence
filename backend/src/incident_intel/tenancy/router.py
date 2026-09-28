from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from incident_intel.api.deps import get_tenant_context
from incident_intel.db.session import get_session
from incident_intel.tenancy.context import TenantContext
from incident_intel.tenancy.schemas import (
    ApiKeyOut,
    OrganizationOut,
    ProjectOut,
    ProjectOverviewResponse,
)
from incident_intel.tenancy.service import get_project_overview

router = APIRouter(prefix="/v1", tags=["tenancy"])


@router.get("/project", response_model=ProjectOverviewResponse)
async def read_current_project(
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ProjectOverviewResponse:
    """Return the organization and project that the presented API key belongs to."""
    organization, project = await get_project_overview(session, ctx)
    return ProjectOverviewResponse(
        organization=OrganizationOut.model_validate(organization),
        project=ProjectOut.model_validate(project),
        api_key=ApiKeyOut(
            id=ctx.principal.api_key_id,
            name=ctx.principal.name,
            prefix=ctx.principal.key_prefix,
            scopes=sorted(scope.value for scope in ctx.principal.scopes),
        ),
    )
