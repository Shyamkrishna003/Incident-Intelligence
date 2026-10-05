"""GitHub settings for a project. Admin role only: this manages a stored credential."""

import re
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from incident_intel.api.deps import require_project_role
from incident_intel.core.crypto import SecretBox
from incident_intel.core.errors import ServiceUnavailableError
from incident_intel.db.session import get_session
from incident_intel.integrations.github.client import REPOSITORY_PATTERN, GitHubClient
from incident_intel.integrations.github.service import (
    MAX_REPOSITORIES,
    connect,
    disconnect,
    get_status,
    replace_repositories,
)
from incident_intel.telemetry.messages import ServiceName
from incident_intel.tenancy.access import ProjectAccess
from incident_intel.tenancy.context import TenantScope
from incident_intel.tenancy.roles import Role

router = APIRouter(prefix="/v1/projects/{project_id}/github", tags=["console"])

AdminAccess = Annotated[ProjectAccess, Depends(require_project_role(Role.ADMIN))]
Session = Annotated[AsyncSession, Depends(get_session)]

# Fine-grained tokens start with github_pat_; classic ones with ghp_.
_TOKEN = re.compile(r"^(?:github_pat_|ghp_)[A-Za-z0-9_]{20,255}$")

Repository = Annotated[str, StringConstraints(pattern=REPOSITORY_PATTERN)]


class ConnectionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # SecretStr: never shown in logs, reprs, or validation errors.
    token: SecretStr

    @field_validator("token")
    @classmethod
    def _looks_like_a_token(cls, value: SecretStr) -> SecretStr:
        if not _TOKEN.match(value.get_secret_value()):
            raise ValueError("must be a GitHub access token (it starts with github_pat_ or ghp_)")
        return value


class RepositoryMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: ServiceName
    # "owner/name"
    repository: Repository


class RepositoriesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repositories: list[RepositoryMapping] = Field(max_length=MAX_REPOSITORIES)


class GitHubStatusOut(BaseModel):
    # False when the server has no SECRETS_ENCRYPTION_KEY: tokens cannot be stored.
    available: bool
    connected: bool
    # Last four characters of the stored token. The token itself is never returned.
    token_hint: str | None
    connected_at: datetime | None
    repositories: list[RepositoryMapping]


def get_secret_box(request: Request) -> SecretBox | None:
    box: SecretBox | None = request.app.state.secret_box
    return box


def require_secret_box(request: Request) -> SecretBox:
    box = get_secret_box(request)
    if box is None:
        raise ServiceUnavailableError("GitHub integration is not configured on this server.")
    return box


def get_github_client(request: Request) -> GitHubClient:
    client: GitHubClient = request.app.state.github
    return client


Box = Annotated[SecretBox, Depends(require_secret_box)]
OptionalBox = Annotated[SecretBox | None, Depends(get_secret_box)]
Client = Annotated[GitHubClient, Depends(get_github_client)]


async def _status(session: AsyncSession, scope: TenantScope, *, available: bool) -> GitHubStatusOut:
    status = await get_status(session, scope)
    connection = status.connection
    return GitHubStatusOut(
        available=available,
        connected=connection is not None,
        token_hint=connection.token_hint if connection else None,
        connected_at=connection.updated_at if connection else None,
        repositories=[
            RepositoryMapping(service=service, repository=repository)
            for service, repository in status.repositories
        ],
    )


@router.get("", response_model=GitHubStatusOut)
async def get_github(access: AdminAccess, session: Session, box: OptionalBox) -> GitHubStatusOut:
    """Whether the project is connected to GitHub, and its service → repository mappings."""
    return await _status(session, access.scope, available=box is not None)


@router.put("/connection", response_model=GitHubStatusOut)
async def put_connection(
    body: ConnectionIn, access: AdminAccess, session: Session, box: Box
) -> GitHubStatusOut:
    """Store the project's GitHub token (replacing any earlier one). Use a fine-grained
    token with read-only access to the contents of the repositories you will map."""
    await connect(
        session,
        access.scope,
        token=body.token.get_secret_value(),
        box=box,
        user_id=access.user.id,
        actor=access.actor,
    )
    return await _status(session, access.scope, available=True)


@router.delete("/connection", status_code=204)
async def delete_connection(access: AdminAccess, session: Session) -> Response:
    """Delete the stored token. Succeeds if there is none."""
    await disconnect(session, access.scope, actor=access.actor)
    return Response(status_code=204)


@router.put("/repositories", response_model=GitHubStatusOut)
async def put_repositories(
    body: RepositoriesIn, access: AdminAccess, session: Session, box: Box, client: Client
) -> GitHubStatusOut:
    """Replace the service → repository mappings. Each repository is checked with the
    stored token before anything is saved."""
    await replace_repositories(
        session,
        access.scope,
        [(item.service, item.repository) for item in body.repositories],
        box=box,
        client=client,
        actor=access.actor,
    )
    return await _status(session, access.scope, available=True)
