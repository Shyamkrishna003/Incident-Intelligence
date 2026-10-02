from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Annotated

import structlog
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from incident_intel.core.config import Settings
from incident_intel.core.errors import AuthenticationError, PermissionDeniedError
from incident_intel.db.session import get_session
from incident_intel.streaming.producer import MessagePublisher
from incident_intel.tenancy.api_keys import ApiKeyScope
from incident_intel.tenancy.context import TenantContext
from incident_intel.tenancy.service import authenticate_api_key

logger = structlog.get_logger(__name__)

_bearer = HTTPBearer(auto_error=False, description="Project API key: `Bearer ii_...`")


def get_settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_publisher(request: Request) -> MessagePublisher:
    publisher: MessagePublisher = request.app.state.publisher
    return publisher


async def get_tenant_context(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> TenantContext:
    """Authenticate a project API key and bind the tenant scope to the log context."""
    if credentials is None:
        logger.info("api_key_rejected", reason="missing")
        raise AuthenticationError("missing")
    try:
        ctx = await authenticate_api_key(
            session,
            credentials.credentials,
            pepper=settings.api_key_pepper.get_secret_value(),
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
