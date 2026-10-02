import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Annotated

import structlog
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from incident_intel.auth.tokens import TokenVerifier
from incident_intel.cache.services import CacheServices
from incident_intel.core.config import Settings
from incident_intel.core.errors import (
    AuthenticationError,
    PermissionDeniedError,
    RateLimitedError,
    ServiceUnavailableError,
)
from incident_intel.db.session import get_session
from incident_intel.streaming.producer import MessagePublisher
from incident_intel.tenancy.access import (
    OrganizationAccess,
    ProjectAccess,
    authorize_organization,
    authorize_project,
)
from incident_intel.tenancy.api_keys import ApiKeyScope
from incident_intel.tenancy.context import TenantContext
from incident_intel.tenancy.models import User
from incident_intel.tenancy.roles import Role
from incident_intel.tenancy.service import authenticate_api_key
from incident_intel.tenancy.users import get_or_create_user

logger = structlog.get_logger(__name__)

_bearer = HTTPBearer(auto_error=False, description="Project API key: `Bearer ii_...`")
_user_bearer = HTTPBearer(
    auto_error=False, scheme_name="FirebaseIdToken", description="Firebase ID token"
)


def get_settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_publisher(request: Request) -> MessagePublisher:
    publisher: MessagePublisher = request.app.state.publisher
    return publisher


def get_cache(request: Request) -> CacheServices:
    cache: CacheServices = request.app.state.cache
    return cache


async def _enforce_rate_limit(cache: CacheServices, subject: str) -> None:
    decision = await cache.rate_limiter.check(subject)
    if not decision.allowed:
        if decision.limiter_unavailable:  # only when RATE_LIMIT_FAIL_OPEN=false
            raise ServiceUnavailableError("Rate limiting is temporarily unavailable.")
        logger.info("rate_limited", retry_after_seconds=decision.retry_after_seconds)
        raise RateLimitedError(retry_after_seconds=decision.retry_after_seconds)


async def get_tenant_context(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
    cache: Annotated[CacheServices, Depends(get_cache)],
) -> TenantContext:
    """Authenticate a project API key, apply its rate limit, and bind the log context."""
    if credentials is None:
        logger.info("api_key_rejected", reason="missing")
        raise AuthenticationError("missing")
    try:
        ctx = await authenticate_api_key(
            session,
            credentials.credentials,
            pepper=settings.api_key_pepper.get_secret_value(),
            cache=cache.api_keys,
            last_used_resolution=timedelta(seconds=settings.api_key_last_used_resolution_seconds),
        )
    except AuthenticationError as exc:
        logger.info("api_key_rejected", reason=exc.reason)
        raise
    structlog.contextvars.bind_contextvars(
        organization_id=str(ctx.organization_id),
        project_id=str(ctx.project_id),
        api_key_prefix=ctx.principal.key_prefix,
    )

    # Limited per API key, after authentication, so one key cannot exhaust another's budget.
    await _enforce_rate_limit(cache, str(ctx.principal.api_key_id))
    return ctx


def require_scope(scope: ApiKeyScope) -> Callable[..., Awaitable[TenantContext]]:
    """Dependency factory: authenticate, then require ``scope`` on the API key (else 403)."""

    async def dependency(
        ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    ) -> TenantContext:
        if scope not in ctx.principal.scopes:
            logger.info("api_key_scope_denied", required_scope=scope.value)
            raise PermissionDeniedError(f"This API key lacks the '{scope.value}' scope.")
        return ctx

    return dependency


# --- Signed-in users (Firebase ID token) --------------------------------------------------


def get_token_verifier(request: Request) -> TokenVerifier:
    verifier: TokenVerifier = request.app.state.token_verifier
    return verifier


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_user_bearer)],
    session: Annotated[AsyncSession, Depends(get_session)],
    verifier: Annotated[TokenVerifier, Depends(get_token_verifier)],
    cache: Annotated[CacheServices, Depends(get_cache)],
) -> User:
    """Verify the caller's ID token and return our user record (created on first sign-in).

    This establishes identity only. What the user may touch is decided per organization
    or project by ``require_organization_role`` / ``require_project_role``.
    """
    if credentials is None:
        logger.info("id_token_rejected", reason="missing")
        raise AuthenticationError("missing")
    try:
        identity = await verifier.verify(credentials.credentials)
    except AuthenticationError as exc:
        logger.info("id_token_rejected", reason=exc.reason)
        raise
    user = await get_or_create_user(session, identity)
    structlog.contextvars.bind_contextvars(user_id=str(user.id))
    await _enforce_rate_limit(cache, f"user:{user.id}")
    return user


def require_organization_role(minimum: Role) -> Callable[..., Awaitable[OrganizationAccess]]:
    """Dependency factory for routes under ``/v1/organizations/{organization_id}``."""

    async def dependency(
        organization_id: uuid.UUID,
        user: Annotated[User, Depends(get_current_user)],
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> OrganizationAccess:
        access = await authorize_organization(session, user, organization_id, minimum=minimum)
        structlog.contextvars.bind_contextvars(organization_id=str(organization_id))
        return access

    return dependency


def require_project_role(minimum: Role) -> Callable[..., Awaitable[ProjectAccess]]:
    """Dependency factory for routes under ``/v1/projects/{project_id}``."""

    async def dependency(
        project_id: uuid.UUID,
        user: Annotated[User, Depends(get_current_user)],
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> ProjectAccess:
        access = await authorize_project(session, user, project_id, minimum=minimum)
        structlog.contextvars.bind_contextvars(
            organization_id=str(access.project.organization_id), project_id=str(project_id)
        )
        return access

    return dependency
