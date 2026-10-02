"""Tenancy use cases: organizations, projects, and API keys.

Mutating functions commit their own transaction, together with the audit record.
"""

import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from incident_intel.audit.service import Actor, record_audit_event
from incident_intel.cache.api_keys import ApiKeyCache, CachedApiKey
from incident_intel.core.errors import (
    AuthenticationError,
    ConflictError,
    InvalidInputError,
    NotFoundError,
)
from incident_intel.tenancy.api_keys import (
    DEFAULT_SCOPES,
    ApiKeyScope,
    generate_api_key,
    hash_api_key,
    parse_key_prefix,
    verify_api_key,
)
from incident_intel.tenancy.context import ApiKeyPrincipal, TenantContext
from incident_intel.tenancy.models import ApiKey, Organization, Project

logger = structlog.get_logger(__name__)

_SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_MAX_NAME_LENGTH = 100
# Compared against when the presented key is unknown, so both paths do the same work.
_DUMMY_HASH = "0" * 64


def _utcnow() -> datetime:
    return datetime.now(UTC)


def validate_slug(value: str, *, field: str) -> str:
    if not _SLUG.match(value):
        raise InvalidInputError(
            f"{field} must be 1-63 lowercase letters, digits, or hyphens, "
            "starting and ending with a letter or digit."
        )
    return value


def _validate_name(value: str, *, field: str, max_length: int = 200) -> str:
    stripped = value.strip()
    if not stripped or len(stripped) > max_length:
        raise InvalidInputError(f"{field} must be 1-{max_length} characters.")
    return stripped


# --- Organizations and projects -------------------------------------------------------


async def create_organization(
    session: AsyncSession, *, slug: str, name: str, actor: Actor
) -> Organization:
    organization = Organization(
        slug=validate_slug(slug, field="organization slug"),
        name=_validate_name(name, field="organization name"),
    )
    session.add(organization)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(f"Organization '{slug}' already exists.") from exc
    record_audit_event(
        session,
        actor=actor,
        action="organization.created",
        target_type="organization",
        target_id=organization.id,
        organization_id=organization.id,
        details={"slug": organization.slug},
    )
    await session.commit()
    return organization


async def create_project(
    session: AsyncSession, *, organization_id: uuid.UUID, slug: str, name: str, actor: Actor
) -> Project:
    project = Project(
        organization_id=organization_id,
        slug=validate_slug(slug, field="project slug"),
        name=_validate_name(name, field="project name"),
    )
    session.add(project)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(f"Project '{slug}' already exists in this organization.") from exc
    record_audit_event(
        session,
        actor=actor,
        action="project.created",
        target_type="project",
        target_id=project.id,
        organization_id=organization_id,
        project_id=project.id,
        details={"slug": project.slug},
    )
    await session.commit()
    return project


async def get_project_by_slugs(
    session: AsyncSession, *, organization_slug: str, project_slug: str
) -> Project:
    project = await session.scalar(
        select(Project)
        .join(Organization, Project.organization_id == Organization.id)
        .where(Organization.slug == organization_slug, Project.slug == project_slug)
    )
    if project is None:
        raise NotFoundError(f"Project '{organization_slug}/{project_slug}' not found.")
    return project


# --- API keys -------------------------------------------------------------------------


@dataclass(frozen=True)
class IssuedApiKey:
    """Returned once at creation; ``plaintext`` is never stored or retrievable again."""

    api_key: ApiKey
    plaintext: str


async def issue_api_key(
    session: AsyncSession,
    *,
    project: Project,
    name: str,
    pepper: str,
    actor: Actor,
    scopes: Iterable[ApiKeyScope] = DEFAULT_SCOPES,
    expires_at: datetime | None = None,
) -> IssuedApiKey:
    scope_values = sorted({ApiKeyScope(scope).value for scope in scopes})
    if not scope_values:
        raise InvalidInputError("An API key needs at least one scope.")
    if expires_at is not None and expires_at <= _utcnow():
        raise InvalidInputError("expires_at must be in the future.")

    generated = generate_api_key(pepper)
    api_key = ApiKey(
        organization_id=project.organization_id,
        project_id=project.id,
        name=_validate_name(name, field="API key name", max_length=_MAX_NAME_LENGTH),
        key_prefix=generated.prefix,
        key_hash=generated.key_hash,
        scopes=scope_values,
        expires_at=expires_at,
    )
    session.add(api_key)
    await session.flush()
    record_audit_event(
        session,
        actor=actor,
        action="api_key.created",
        target_type="api_key",
        target_id=api_key.id,
        organization_id=project.organization_id,
        project_id=project.id,
        details={"prefix": api_key.key_prefix, "name": api_key.name, "scopes": scope_values},
    )
    await session.commit()
    return IssuedApiKey(api_key=api_key, plaintext=generated.plaintext)


async def revoke_api_key(
    session: AsyncSession, *, key_prefix: str, actor: Actor, cache: ApiKeyCache | None = None
) -> ApiKey:
    """Revoke a key and drop it from the API-key cache.

    If the cache cannot be reached, the key may keep authenticating until its cache entry
    expires (at most ``api_key_cache_ttl_seconds``); that case is logged.
    """
    api_key = await session.scalar(select(ApiKey).where(ApiKey.key_prefix == key_prefix))
    if api_key is None:
        raise NotFoundError(f"API key with prefix '{key_prefix}' not found.")
    if api_key.revoked_at is None:
        api_key.revoked_at = _utcnow()
        record_audit_event(
            session,
            actor=actor,
            action="api_key.revoked",
            target_type="api_key",
            target_id=api_key.id,
            organization_id=api_key.organization_id,
            project_id=api_key.project_id,
            details={"prefix": api_key.key_prefix},
        )
        await session.commit()
    # After the commit, and even when already revoked: a repeat revoke retries the cleanup.
    if cache is not None and not await cache.invalidate(key_prefix):
        logger.warning("api_key_cache_invalidation_failed", key_prefix=key_prefix)
    return api_key


async def list_api_keys(session: AsyncSession, *, project: Project) -> list[ApiKey]:
    result = await session.scalars(
        select(ApiKey).where(ApiKey.project_id == project.id).order_by(ApiKey.created_at)
    )
    return list(result)


def _context_from_cache(cached: CachedApiKey) -> TenantContext:
    return TenantContext(
        organization_id=cached.organization_id,
        project_id=cached.project_id,
        principal=ApiKeyPrincipal(
            api_key_id=cached.api_key_id,
            name=cached.name,
            key_prefix=cached.key_prefix,
            scopes=frozenset(ApiKeyScope(scope) for scope in cached.scopes),
        ),
    )


async def authenticate_api_key(
    session: AsyncSession,
    presented: str,
    *,
    pepper: str,
    cache: ApiKeyCache | None = None,
    last_used_resolution: timedelta = timedelta(seconds=60),
    now: datetime | None = None,
) -> TenantContext:
    """Resolve a presented API key to its tenant scope, or raise AuthenticationError.

    With a cache, a recently verified key is checked against its cached HMAC without
    touching the database. Only active keys are cached, and revocation deletes the entry.
    """
    now = now or _utcnow()
    prefix = parse_key_prefix(presented)
    if prefix is None:
        hash_api_key(presented, pepper)  # keep timing similar to the lookup path
        raise AuthenticationError("malformed")

    cached = await cache.get(prefix) if cache is not None else None
    if cached is not None:
        if not verify_api_key(presented, cached.key_hash, pepper):
            raise AuthenticationError("unknown_key")
        if cached.expires_at is not None and cached.expires_at <= now:
            raise AuthenticationError("expired")
        return _context_from_cache(cached)

    api_key = await session.scalar(select(ApiKey).where(ApiKey.key_prefix == prefix))
    matches = verify_api_key(presented, api_key.key_hash if api_key else _DUMMY_HASH, pepper)
    if api_key is None or not matches:
        raise AuthenticationError("unknown_key")
    if api_key.revoked_at is not None:
        raise AuthenticationError("revoked")
    if api_key.expires_at is not None and api_key.expires_at <= now:
        raise AuthenticationError("expired")

    if api_key.last_used_at is None or now - api_key.last_used_at >= last_used_resolution:
        await session.execute(
            update(ApiKey).where(ApiKey.id == api_key.id).values(last_used_at=now)
        )
        await session.commit()

    entry = CachedApiKey(
        api_key_id=api_key.id,
        organization_id=api_key.organization_id,
        project_id=api_key.project_id,
        name=api_key.name,
        key_prefix=api_key.key_prefix,
        key_hash=api_key.key_hash,
        scopes=tuple(api_key.scopes),
        expires_at=api_key.expires_at,
    )
    if cache is not None:
        await cache.put(entry)
    return _context_from_cache(entry)


async def get_project_overview(
    session: AsyncSession, ctx: TenantContext
) -> tuple[Organization, Project]:
    row = (
        await session.execute(
            select(Organization, Project)
            .join(Project, Project.organization_id == Organization.id)
            .where(
                Project.id == ctx.project_id,
                Project.organization_id == ctx.organization_id,
            )
        )
    ).one_or_none()
    if row is None:
        raise NotFoundError("Project not found.")
    organization, project = row
    return organization, project
