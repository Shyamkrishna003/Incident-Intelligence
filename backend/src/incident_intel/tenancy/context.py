import uuid
from dataclasses import dataclass

from incident_intel.tenancy.api_keys import ApiKeyScope


@dataclass(frozen=True)
class ApiKeyPrincipal:
    api_key_id: uuid.UUID
    name: str
    key_prefix: str
    scopes: frozenset[ApiKeyScope]


@dataclass(frozen=True)
class TenantScope:
    """The organization and project a request may touch.

    Derived only from verified credentials (an API key, or a signed-in user's membership),
    never from unchecked request parameters. Every tenant-owned query must be filtered by
    these identifiers.
    """

    organization_id: uuid.UUID
    project_id: uuid.UUID


@dataclass(frozen=True)
class TenantContext(TenantScope):
    """The tenant scope of a request authenticated with a project API key."""

    principal: ApiKeyPrincipal
