"""Request and response shapes for the signed-in user ("console") endpoints."""

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from incident_intel.tenancy.api_keys import ApiKeyScope
from incident_intel.tenancy.roles import Role
from incident_intel.tenancy.schemas import OrganizationOut, ProjectOut

Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")]
DisplayName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
KeyName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class UserOut(BaseModel):
    id: uuid.UUID
    email: str | None
    email_verified: bool
    display_name: str | None


class OrganizationMembershipOut(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    role: Role
    projects: list[ProjectOut]


class MeResponse(BaseModel):
    user: UserOut
    organizations: list[OrganizationMembershipOut]


class OrganizationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: Slug
    name: DisplayName


class OrganizationCreated(OrganizationOut):
    role: Role


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: Slug
    name: DisplayName


class ApiKeyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: KeyName
    scopes: list[ApiKeyScope] = Field(
        default_factory=lambda: sorted(ApiKeyScope), min_length=1, max_length=len(ApiKeyScope)
    )
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class ApiKeyDetailOut(BaseModel):
    """An API key as shown to users: never the key itself or its hash."""

    id: uuid.UUID
    name: str
    prefix: str
    scopes: list[str]
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None


class ApiKeyListResponse(BaseModel):
    api_keys: list[ApiKeyDetailOut]


class ApiKeyCreated(ApiKeyDetailOut):
    # The only time the full key is ever returned. It cannot be retrieved again.
    key: str
