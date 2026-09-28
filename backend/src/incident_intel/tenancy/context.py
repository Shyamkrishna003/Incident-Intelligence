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
class TenantContext:
    """The authenticated tenant scope of a request.

    Derived only from verified credentials, never from request parameters. Every
    tenant-owned query must be filtered by these identifiers.
    """

    organization_id: uuid.UUID
    project_id: uuid.UUID
    principal: ApiKeyPrincipal
