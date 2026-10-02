"""Authorization for signed-in users: is this user allowed to act on this organization
or project, and with which role?

An organization or project the user does not belong to is reported as "not found", never
"forbidden", so its existence is not revealed. "Forbidden" is used only when the user is a
member but their role is too low.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from incident_intel.audit.service import Actor
from incident_intel.core.errors import NotFoundError, PermissionDeniedError
from incident_intel.tenancy.context import TenantScope
from incident_intel.tenancy.models import Membership, Project, User
from incident_intel.tenancy.roles import Role
from incident_intel.tenancy.users import user_actor


def _require(role: Role, minimum: Role) -> None:
    if not role.at_least(minimum):
        raise PermissionDeniedError(f"This action needs the '{minimum.value}' role or higher.")


@dataclass(frozen=True)
class OrganizationAccess:
    organization_id: uuid.UUID
    user: User
    role: Role

    @property
    def actor(self) -> Actor:
        return user_actor(self.user)


@dataclass(frozen=True)
class ProjectAccess:
    project: Project
    user: User
    role: Role

    @property
    def scope(self) -> TenantScope:
        """The tenant scope for queries, taken from the project row the user may access."""
        return TenantScope(organization_id=self.project.organization_id, project_id=self.project.id)

    @property
    def actor(self) -> Actor:
        return user_actor(self.user)


async def authorize_organization(
    session: AsyncSession, user: User, organization_id: uuid.UUID, *, minimum: Role
) -> OrganizationAccess:
    role = await session.scalar(
        select(Membership.role).where(
            Membership.organization_id == organization_id, Membership.user_id == user.id
        )
    )
    if role is None:
        raise NotFoundError("Organization not found.")
    _require(Role(role), minimum)
    return OrganizationAccess(organization_id=organization_id, user=user, role=Role(role))


async def authorize_project(
    session: AsyncSession, user: User, project_id: uuid.UUID, *, minimum: Role
) -> ProjectAccess:
    row = (
        await session.execute(
            select(Project, Membership.role)
            .join(Membership, Membership.organization_id == Project.organization_id)
            .where(Project.id == project_id, Membership.user_id == user.id)
        )
    ).one_or_none()
    if row is None:
        raise NotFoundError("Project not found.")
    project, role = row
    _require(Role(role), minimum)
    return ProjectAccess(project=project, user=user, role=Role(role))
