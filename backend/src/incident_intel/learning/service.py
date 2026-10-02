"""Feedback on investigations, the learning records it produces, and evaluation cases."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from incident_intel.audit.service import Actor, record_audit_event
from incident_intel.core.errors import ConflictError, InvalidInputError, NotFoundError
from incident_intel.incidents.models import Incident
from incident_intel.investigation.models import Investigation, InvestigationEvidence
from incident_intel.learning.evaluation import EvalCase
from incident_intel.learning.models import EvaluationCase, InvestigationFeedback, LearningRecord
from incident_intel.tenancy.context import TenantScope
from incident_intel.tenancy.models import Organization, Project, User

Verdict = Literal["correct", "partially_correct", "incorrect"]


async def _reviewable(
    session: AsyncSession, scope: TenantScope, investigation_id: uuid.UUID
) -> Investigation:
    investigation = await session.scalar(
        select(Investigation).where(
            Investigation.organization_id == scope.organization_id,
            Investigation.project_id == scope.project_id,
            Investigation.id == investigation_id,
        )
    )
    if investigation is None:
        raise NotFoundError("Investigation not found.")
    if investigation.status != "succeeded" or investigation.report is None:
        raise ConflictError("Only a finished investigation with a report can be reviewed.")
    return investigation


async def _evidence(session: AsyncSession, investigation: Investigation) -> list[dict[str, Any]]:
    rows = await session.scalars(
        select(InvestigationEvidence)
        .where(
            InvestigationEvidence.project_id == investigation.project_id,
            InvestigationEvidence.investigation_id == investigation.id,
        )
        .order_by(InvestigationEvidence.position)
    )
    return [{"ref": row.ref, "kind": row.kind, "title": row.title, **row.data} for row in rows]


@dataclass(frozen=True)
class FeedbackView:
    feedback: InvestigationFeedback
    author: str


async def list_feedback(
    session: AsyncSession, scope: TenantScope, investigation_id: uuid.UUID
) -> list[FeedbackView]:
    rows = await session.execute(
        select(InvestigationFeedback, User)
        .join(User, User.id == InvestigationFeedback.user_id)
        .where(
            InvestigationFeedback.project_id == scope.project_id,
            InvestigationFeedback.investigation_id == investigation_id,
        )
        .order_by(InvestigationFeedback.updated_at)
    )
    return [
        FeedbackView(feedback, user.display_name or user.email or "A team member")
        for feedback, user in rows
    ]


async def submit_feedback(
    session: AsyncSession,
    scope: TenantScope,
    investigation_id: uuid.UUID,
    *,
    user: User,
    verdict: Verdict,
    actual_cause: str | None,
    notes: str | None,
    actor: Actor,
) -> InvestigationFeedback:
    """Record (or revise) the user's feedback and refresh the learning record. Commits."""
    investigation = await _reviewable(session, scope, investigation_id)
    now = datetime.now(UTC)
    values = {"verdict": verdict, "actual_cause": actual_cause, "notes": notes, "updated_at": now}
    await session.execute(
        pg_insert(InvestigationFeedback)
        .values(
            investigation_id=investigation.id,
            user_id=user.id,
            project_id=scope.project_id,
            **values,
        )
        .on_conflict_do_update(
            index_elements=[InvestigationFeedback.investigation_id, InvestigationFeedback.user_id],
            set_=values,
        )
    )

    incident = await session.get(Incident, investigation.incident_id)
    everyone = await list_feedback(session, scope, investigation.id)
    snapshot = {
        "incident": {
            "id": str(investigation.incident_id),
            "title": incident.title if incident else None,
            "severity": incident.severity if incident else None,
            "started_at": incident.started_at.isoformat() if incident else None,
            "resolved_at": incident.resolved_at.isoformat()
            if incident and incident.resolved_at
            else None,
        },
        "investigation": {
            "id": str(investigation.id),
            "provider": investigation.provider,
            "model": investigation.model,
            "prompt_version": investigation.prompt_version,
            "finished_at": investigation.finished_at.isoformat()
            if investigation.finished_at
            else None,
            "validation_notes": investigation.validation_notes,
        },
        "evidence": await _evidence(session, investigation),
        "report": investigation.report,
        "feedback": [
            {
                "verdict": view.feedback.verdict,
                "actual_cause": view.feedback.actual_cause,
                "notes": view.feedback.notes,
                "at": view.feedback.updated_at.isoformat(),
            }
            for view in everyone
        ],
    }
    # The record's verdict follows the most recent feedback.
    record = {
        "verdict": verdict,
        "confirmed_cause": actual_cause,
        "snapshot": snapshot,
        "updated_at": now,
    }
    await session.execute(
        pg_insert(LearningRecord)
        .values(
            id=uuid.uuid4(),
            organization_id=scope.organization_id,
            project_id=scope.project_id,
            incident_id=investigation.incident_id,
            investigation_id=investigation.id,
            schema_version=1,
            **record,
        )
        .on_conflict_do_update(index_elements=[LearningRecord.investigation_id], set_=record)
    )
    record_audit_event(
        session,
        actor=actor,
        action="investigation.feedback",
        target_type="investigation",
        target_id=investigation.id,
        organization_id=scope.organization_id,
        project_id=scope.project_id,
        details={"verdict": verdict},
    )
    await session.commit()
    saved = await session.get(
        InvestigationFeedback, (investigation.id, user.id), populate_existing=True
    )
    if saved is None:  # cannot happen: it was written in this transaction
        raise LookupError("feedback was not saved")
    return saved


@dataclass(frozen=True)
class LearningOverview:
    counts: dict[str, int]
    records: list[tuple[LearningRecord, str | None]]


async def learning_overview(
    session: AsyncSession, scope: TenantScope, *, limit: int
) -> LearningOverview:
    counted = await session.execute(
        select(LearningRecord.verdict, func.count())
        .where(
            LearningRecord.organization_id == scope.organization_id,
            LearningRecord.project_id == scope.project_id,
        )
        .group_by(LearningRecord.verdict)
    )
    counts = {"correct": 0, "partially_correct": 0, "incorrect": 0}
    counts.update({verdict: count for verdict, count in counted})
    rows = await session.execute(
        select(LearningRecord, Incident.title)
        .join(Incident, Incident.id == LearningRecord.incident_id, isouter=True)
        .where(
            LearningRecord.organization_id == scope.organization_id,
            LearningRecord.project_id == scope.project_id,
        )
        .order_by(LearningRecord.updated_at.desc())
        .limit(limit)
    )
    return LearningOverview(counts, [(record, title) for record, title in rows])


async def create_evaluation_case(
    session: AsyncSession,
    scope: TenantScope,
    investigation_id: uuid.UUID,
    *,
    name: str,
    expect_supported: bool | None,
    must_mention: list[str],
    must_not_mention: list[str],
    user_id: uuid.UUID,
    actor: Actor,
) -> EvaluationCase:
    """Turn a reviewed investigation into a repeatable evaluation case. Commits."""
    investigation = await _reviewable(session, scope, investigation_id)
    if expect_supported is None and not must_mention and not must_not_mention:
        raise InvalidInputError("A case needs at least one rule to judge a report by.")
    case = EvaluationCase(
        organization_id=scope.organization_id,
        project_id=scope.project_id,
        source_investigation_id=investigation.id,
        name=name,
        evidence=await _evidence(session, investigation),
        expect_supported=expect_supported,
        must_mention=must_mention,
        must_not_mention=must_not_mention,
        created_by_user_id=user_id,
    )
    session.add(case)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(f"An evaluation case named '{name}' already exists.") from exc
    record_audit_event(
        session,
        actor=actor,
        action="evaluation_case.created",
        target_type="evaluation_case",
        target_id=case.id,
        organization_id=scope.organization_id,
        project_id=scope.project_id,
        details={"name": name, "investigation_id": str(investigation.id)},
    )
    await session.commit()
    return case


async def list_evaluation_cases(session: AsyncSession, scope: TenantScope) -> list[EvaluationCase]:
    rows = await session.scalars(
        select(EvaluationCase)
        .where(
            EvaluationCase.organization_id == scope.organization_id,
            EvaluationCase.project_id == scope.project_id,
        )
        .order_by(EvaluationCase.name)
    )
    return list(rows)


async def project_eval_cases(
    session: AsyncSession, *, organization_slug: str, project_slug: str
) -> list[EvalCase]:
    """A project's saved cases, for the operator-run evaluation command."""
    project = await session.scalar(
        select(Project)
        .join(Organization, Organization.id == Project.organization_id)
        .where(Organization.slug == organization_slug, Project.slug == project_slug)
    )
    if project is None:
        raise NotFoundError(f"Project '{organization_slug}/{project_slug}' not found.")
    cases = await list_evaluation_cases(session, TenantScope(project.organization_id, project.id))
    return [
        EvalCase(
            name=case.name,
            description="Saved from a reviewed investigation.",
            evidence=case.evidence,
            expect_supported=case.expect_supported,
            must_mention=tuple(case.must_mention),
            must_not_mention=tuple(case.must_not_mention),
        )
        for case in cases
    ]
