"""Requesting, claiming and reading investigations."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from incident_intel.audit.service import Actor, record_audit_event
from incident_intel.core.errors import ConflictError, NotFoundError, RateLimitedError
from incident_intel.incidents.models import Incident
from incident_intel.investigation.models import (
    Investigation,
    InvestigationEvidence,
    InvestigationStep,
)
from incident_intel.tenancy.context import TenantScope


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def request_investigation(
    session: AsyncSession,
    scope: TenantScope,
    incident_id: uuid.UUID,
    *,
    user_id: uuid.UUID,
    actor: Actor,
    max_per_hour: int,
) -> Investigation:
    """Queue an investigation of an incident. Commits."""
    incident = await session.scalar(
        select(Incident).where(
            Incident.organization_id == scope.organization_id,
            Incident.project_id == scope.project_id,
            Incident.id == incident_id,
        )
    )
    if incident is None:
        raise NotFoundError("Incident not found.")
    if incident.status == "merged":
        raise ConflictError("This incident was merged into another one. Investigate that one.")

    # Each investigation calls a paid or rate-limited model: cap how many one incident gets.
    recent = await session.scalar(
        select(func.count())
        .select_from(Investigation)
        .where(
            Investigation.project_id == scope.project_id,
            Investigation.incident_id == incident_id,
            Investigation.created_at >= _utcnow() - timedelta(hours=1),
        )
    )
    if (recent or 0) >= max_per_hour:
        raise RateLimitedError(retry_after_seconds=600)

    investigation = Investigation(
        organization_id=scope.organization_id,
        project_id=scope.project_id,
        incident_id=incident_id,
        status="queued",
        requested_by_user_id=user_id,
        validation_notes=[],
    )
    session.add(investigation)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError("An investigation of this incident is already in progress.") from exc
    record_audit_event(
        session,
        actor=actor,
        action="investigation.requested",
        target_type="incident",
        target_id=incident_id,
        organization_id=scope.organization_id,
        project_id=scope.project_id,
        details={"investigation_id": str(investigation.id)},
    )
    await session.commit()
    return investigation


async def claim_next(
    session: AsyncSession, *, lease: timedelta, max_attempts: int, now: datetime | None = None
) -> Investigation | None:
    """Take the oldest investigation that needs running, and mark it running. Commits.

    ``FOR UPDATE SKIP LOCKED`` lets several workers poll without taking the same row. A
    "running" row whose lease has expired belonged to a worker that died: it is retried,
    or failed once it has used up its attempts.
    """
    now = now or _utcnow()
    while True:
        investigation = await session.scalar(
            select(Investigation)
            .where(
                or_(
                    Investigation.status == "queued",
                    (Investigation.status == "running") & (Investigation.locked_until < now),
                )
            )
            .order_by(Investigation.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if investigation is None:
            await session.commit()
            return None
        if investigation.attempts >= max_attempts:
            investigation.status = "failed"
            investigation.finished_at = now
            investigation.locked_until = None
            investigation.error = "The investigation was interrupted and could not be completed."
            await session.commit()
            continue
        investigation.status = "running"
        investigation.attempts += 1
        investigation.locked_until = now + lease
        investigation.started_at = investigation.started_at or now
        await session.commit()
        return investigation


async def list_investigations(
    session: AsyncSession, scope: TenantScope, incident_id: uuid.UUID, *, limit: int = 20
) -> list[Investigation]:
    rows = await session.scalars(
        select(Investigation)
        .where(
            Investigation.organization_id == scope.organization_id,
            Investigation.project_id == scope.project_id,
            Investigation.incident_id == incident_id,
        )
        .order_by(Investigation.created_at.desc())
        .limit(limit)
    )
    return list(rows)


@dataclass(frozen=True)
class InvestigationDetail:
    investigation: Investigation
    evidence: list[InvestigationEvidence]
    steps: list[InvestigationStep]


async def get_investigation(
    session: AsyncSession, scope: TenantScope, investigation_id: uuid.UUID
) -> InvestigationDetail:
    investigation = await session.scalar(
        select(Investigation).where(
            Investigation.organization_id == scope.organization_id,
            Investigation.project_id == scope.project_id,
            Investigation.id == investigation_id,
        )
    )
    if investigation is None:
        raise NotFoundError("Investigation not found.")
    evidence = await session.scalars(
        select(InvestigationEvidence)
        .where(
            InvestigationEvidence.project_id == scope.project_id,
            InvestigationEvidence.investigation_id == investigation_id,
        )
        .order_by(InvestigationEvidence.position)
    )
    steps = await session.scalars(
        select(InvestigationStep)
        .where(
            InvestigationStep.project_id == scope.project_id,
            InvestigationStep.investigation_id == investigation_id,
        )
        .order_by(InvestigationStep.attempt, InvestigationStep.seq)
    )
    return InvestigationDetail(investigation, list(evidence), list(steps))
