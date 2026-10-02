"""Declared service dependencies: "service A depends on (calls) service B".

Configuration rather than telemetry, so it is written straight to PostgreSQL. A project's
dependencies are replaced as a whole, which makes declaring them idempotent.
"""

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from incident_intel.audit.service import Actor, record_audit_event
from incident_intel.core.errors import InvalidInputError
from incident_intel.incidents.models import ServiceDependency
from incident_intel.incidents.service import upsert_services
from incident_intel.telemetry.models import Service
from incident_intel.tenancy.context import TenantScope

# (service, the service it depends on)
NamedEdge = tuple[str, str]


async def list_dependencies(session: AsyncSession, scope: TenantScope) -> list[NamedEdge]:
    dependent, dependency = aliased(Service), aliased(Service)
    rows = await session.execute(
        select(dependent.name, dependency.name)
        .select_from(ServiceDependency)
        .join(
            dependent,
            (dependent.id == ServiceDependency.service_id)
            & (dependent.project_id == ServiceDependency.project_id),
        )
        .join(
            dependency,
            (dependency.id == ServiceDependency.depends_on_id)
            & (dependency.project_id == ServiceDependency.project_id),
        )
        .where(ServiceDependency.project_id == scope.project_id)
        .order_by(dependent.name, dependency.name)
    )
    return [(service, depends_on) for service, depends_on in rows]


async def replace_dependencies(
    session: AsyncSession, scope: TenantScope, edges: list[NamedEdge], *, actor: Actor
) -> list[NamedEdge]:
    """Replace the project's dependencies with ``edges``. Commits."""
    unique = sorted(set(edges))
    for service, depends_on in unique:
        if service == depends_on:
            raise InvalidInputError(f"A service cannot depend on itself: '{service}'.")
    # A service may be declared before it has sent any telemetry.
    ids = await upsert_services(
        session,
        organization_id=scope.organization_id,
        project_id=scope.project_id,
        names={name for edge in unique for name in edge},
    )
    await session.execute(
        delete(ServiceDependency).where(ServiceDependency.project_id == scope.project_id)
    )
    session.add_all(
        ServiceDependency(
            project_id=scope.project_id, service_id=ids[service], depends_on_id=ids[depends_on]
        )
        for service, depends_on in unique
    )
    record_audit_event(
        session,
        actor=actor,
        action="service_dependencies.replaced",
        target_type="project",
        target_id=scope.project_id,
        organization_id=scope.organization_id,
        project_id=scope.project_id,
        details={"count": len(unique)},
    )
    await session.commit()
    return unique
