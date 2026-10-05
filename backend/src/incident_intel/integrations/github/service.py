"""Connecting a project to GitHub and mapping its services to repositories.

Mutating functions commit their own transaction, together with the audit record. The token
is encrypted before it is stored and is never returned or logged; only its last four
characters are kept in clear.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from incident_intel.audit.service import Actor, record_audit_event
from incident_intel.core.crypto import SecretBox, SecretDecryptionError
from incident_intel.core.errors import ConflictError, InvalidInputError, ServiceUnavailableError
from incident_intel.integrations.github.client import GitHubClient, GitHubError
from incident_intel.integrations.github.models import GitHubConnection, ServiceRepository
from incident_intel.tenancy.context import TenantScope

MAX_REPOSITORIES = 50


@dataclass(frozen=True)
class GitHubStatus:
    connection: GitHubConnection | None
    # (service name, "owner/name"), by service name.
    repositories: list[tuple[str, str]]


async def _connection(session: AsyncSession, scope: TenantScope) -> GitHubConnection | None:
    return await session.scalar(
        select(GitHubConnection).where(
            GitHubConnection.organization_id == scope.organization_id,
            GitHubConnection.project_id == scope.project_id,
        )
    )


async def get_status(session: AsyncSession, scope: TenantScope) -> GitHubStatus:
    rows = await session.execute(
        select(ServiceRepository.service_name, ServiceRepository.repository)
        .where(
            ServiceRepository.organization_id == scope.organization_id,
            ServiceRepository.project_id == scope.project_id,
        )
        .order_by(ServiceRepository.service_name)
    )
    return GitHubStatus(
        connection=await _connection(session, scope),
        repositories=[(service, repository) for service, repository in rows],
    )


async def connect(
    session: AsyncSession,
    scope: TenantScope,
    *,
    token: str,
    box: SecretBox,
    user_id: uuid.UUID,
    actor: Actor,
) -> None:
    """Store (or replace) the project's GitHub token. Commits."""
    values = {
        "token_encrypted": box.encrypt(token),
        "token_hint": token[-4:],
        "connected_by_user_id": user_id,
        "updated_at": datetime.now(UTC),
    }
    await session.execute(
        pg_insert(GitHubConnection)
        .values(organization_id=scope.organization_id, project_id=scope.project_id, **values)
        .on_conflict_do_update(index_elements=[GitHubConnection.project_id], set_=values)
    )
    record_audit_event(
        session,
        actor=actor,
        action="github.connected",
        target_type="project",
        target_id=scope.project_id,
        organization_id=scope.organization_id,
        project_id=scope.project_id,
        details={"token_hint": token[-4:]},
    )
    await session.commit()


async def disconnect(session: AsyncSession, scope: TenantScope, *, actor: Actor) -> None:
    """Delete the stored token. Repository mappings are kept. Commits."""
    connection = await _connection(session, scope)
    if connection is None:
        return
    await session.delete(connection)
    record_audit_event(
        session,
        actor=actor,
        action="github.disconnected",
        target_type="project",
        target_id=scope.project_id,
        organization_id=scope.organization_id,
        project_id=scope.project_id,
    )
    await session.commit()


async def _verify_readable(
    client: GitHubClient, token: str, mappings: Sequence[tuple[str, str]]
) -> None:
    """Fail early, naming the entry, if the token cannot read a repository."""
    errors = []
    checked: dict[str, GitHubError | None] = {}
    for index, (_service, repository) in enumerate(mappings):
        if repository not in checked:
            try:
                await client.check_repository(token, repository)
                checked[repository] = None
            except GitHubError as exc:
                if exc.kind == "unauthorized":
                    raise ConflictError(
                        "GitHub rejected the stored token. Connect GitHub again with a valid token."
                    ) from exc
                if exc.transient or exc.kind == "invalid":
                    raise ServiceUnavailableError(
                        f"The repositories could not be checked: {exc}. Try again later."
                    ) from exc
                checked[repository] = exc
        if checked[repository] is not None:
            errors.append(
                {
                    "loc": ["body", "repositories", index, "repository"],
                    "msg": "The repository was not found, or the token cannot read it.",
                    "type": "repository_not_readable",
                }
            )
    if errors:
        raise InvalidInputError("One or more repositories cannot be read.", details=errors)


async def replace_repositories(
    session: AsyncSession,
    scope: TenantScope,
    mappings: Sequence[tuple[str, str]],
    *,
    box: SecretBox,
    client: GitHubClient,
    actor: Actor,
) -> None:
    """Replace the project's service → repository mappings. Commits.

    Every repository is checked with the stored token first, so a typo or a token without
    access is reported now, not during an incident.
    """
    services = [service for service, _ in mappings]
    if len(set(services)) != len(services):
        raise InvalidInputError("Each service can be mapped to one repository only.")
    if mappings:
        connection = await _connection(session, scope)
        if connection is None:
            raise ConflictError("Connect GitHub before setting repositories.")
        try:
            token = box.decrypt(connection.token_encrypted)
        except SecretDecryptionError as exc:
            raise ConflictError(
                "The stored GitHub token can no longer be read. Connect GitHub again."
            ) from exc
        await _verify_readable(client, token, mappings)

    await session.execute(
        delete(ServiceRepository).where(
            ServiceRepository.organization_id == scope.organization_id,
            ServiceRepository.project_id == scope.project_id,
        )
    )
    session.add_all(
        ServiceRepository(
            organization_id=scope.organization_id,
            project_id=scope.project_id,
            service_name=service,
            repository=repository,
        )
        for service, repository in mappings
    )
    record_audit_event(
        session,
        actor=actor,
        action="github.repositories_replaced",
        target_type="project",
        target_id=scope.project_id,
        organization_id=scope.organization_id,
        project_id=scope.project_id,
        details={"count": len(mappings)},
    )
    await session.commit()
