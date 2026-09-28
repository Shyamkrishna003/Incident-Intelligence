import uuid
from dataclasses import dataclass
from typing import Any, Literal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from incident_intel.audit.models import AuditLog

ActorType = Literal["cli", "api_key", "user", "system"]


@dataclass(frozen=True)
class Actor:
    type: ActorType
    id: str


CLI_ACTOR = Actor(type="cli", id="cli")


def record_audit_event(
    session: AsyncSession,
    *,
    actor: Actor,
    action: str,
    target_type: str,
    target_id: uuid.UUID | str,
    organization_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Add an audit row to the session; it commits atomically with the audited change."""
    request_id = structlog.contextvars.get_contextvars().get("request_id")
    session.add(
        AuditLog(
            organization_id=organization_id,
            project_id=project_id,
            actor_type=actor.type,
            actor_id=actor.id,
            action=action,
            target_type=target_type,
            target_id=str(target_id),
            details=details or {},
            request_id=request_id if isinstance(request_id, str) else None,
        )
    )
