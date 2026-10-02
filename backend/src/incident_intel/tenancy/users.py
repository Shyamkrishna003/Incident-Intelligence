"""Users: our record of a signed-in person, and what they can see."""

import uuid
from dataclasses import dataclass

import structlog
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from incident_intel.audit.service import Actor
from incident_intel.auth.tokens import VerifiedIdentity
from incident_intel.core.errors import ConflictError, PermissionDeniedError
from incident_intel.tenancy.models import Membership, Organization, Project, User
from incident_intel.tenancy.roles import Role
from incident_intel.tenancy.service import create_organization

logger = structlog.get_logger(__name__)

_MAX_EMAIL_LENGTH = 320
_MAX_DISPLAY_NAME_LENGTH = 200


def user_actor(user: User) -> Actor:
    return Actor(type="user", id=str(user.id))


def _clean_email(email: str | None) -> str | None:
    return email if email and len(email) <= _MAX_EMAIL_LENGTH else None


def _clean_name(name: str | None) -> str | None:
    stripped = name.strip() if name else ""
    return stripped[:_MAX_DISPLAY_NAME_LENGTH] or None


async def get_or_create_user(session: AsyncSession, identity: VerifiedIdentity) -> User:
    """Find the user for a verified identity, creating the row on first sign-in.

    Profile fields are refreshed from the token when they change (for example once the
    user verifies their email). Writes happen only then, not on every request.
    """
    email = _clean_email(identity.email)
    display_name = _clean_name(identity.display_name)
    by_uid = select(User).where(User.firebase_uid == identity.uid)

    user = await session.scalar(by_uid)
    if user is None:
        # ON CONFLICT: two first requests arriving together must create exactly one row.
        await session.execute(
            pg_insert(User)
            .values(
                id=uuid.uuid4(),
                firebase_uid=identity.uid,
                email=email,
                email_verified=identity.email_verified,
                display_name=display_name,
            )
            .on_conflict_do_nothing(index_elements=[User.firebase_uid])
        )
        await session.commit()
        user = (await session.scalars(by_uid)).one()
        logger.info("user_created", user_id=str(user.id), provider=identity.sign_in_provider)

    if (user.email, user.email_verified, user.display_name) != (
        email,
        identity.email_verified,
        display_name,
    ):
        user.email = email
        user.email_verified = identity.email_verified
        user.display_name = display_name
        await session.commit()
    return user


@dataclass(frozen=True)
class OrganizationOverview:
    organization: Organization
    role: Role
    projects: list[Project]


async def list_user_organizations(session: AsyncSession, user: User) -> list[OrganizationOverview]:
    """Every organization the user belongs to, with their role and its projects."""
    rows = (
        await session.execute(
            select(Organization, Membership.role)
            .join(Membership, Membership.organization_id == Organization.id)
            .where(Membership.user_id == user.id)
            .order_by(Organization.name, Organization.slug)
        )
    ).all()
    if not rows:
        return []

    projects_by_org: dict[uuid.UUID, list[Project]] = {org.id: [] for org, _ in rows}
    projects = await session.scalars(
        select(Project)
        .where(Project.organization_id.in_(projects_by_org))
        .order_by(Project.name, Project.slug)
    )
    for project in projects:
        projects_by_org[project.organization_id].append(project)
    return [
        OrganizationOverview(organization=org, role=Role(role), projects=projects_by_org[org.id])
        for org, role in rows
    ]


async def create_organization_for_user(
    session: AsyncSession, user: User, *, slug: str, name: str, max_owned: int
) -> Organization:
    """Self-service sign-up: the user becomes the owner of a new organization."""
    if not user.email_verified:
        raise PermissionDeniedError("Verify your email address before creating an organization.")
    owned = await session.scalar(
        select(func.count())
        .select_from(Membership)
        .where(Membership.user_id == user.id, Membership.role == Role.OWNER.value)
    )
    if (owned or 0) >= max_owned:
        raise ConflictError(f"You already own the maximum of {max_owned} organizations.")
    return await create_organization(
        session, slug=slug, name=name, actor=user_actor(user), owner_user_id=user.id
    )
