"""Feedback, learning records and evaluation cases, for signed-in users."""

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy.ext.asyncio import AsyncSession

from incident_intel.api.deps import require_project_role
from incident_intel.db.session import get_session
from incident_intel.learning.models import EvaluationCase
from incident_intel.learning.service import (
    FeedbackView,
    Verdict,
    create_evaluation_case,
    learning_overview,
    list_evaluation_cases,
    submit_feedback,
)
from incident_intel.tenancy.access import ProjectAccess
from incident_intel.tenancy.roles import Role

router = APIRouter(prefix="/v1/projects/{project_id}", tags=["console"])

ViewerAccess = Annotated[ProjectAccess, Depends(require_project_role(Role.VIEWER))]
MemberAccess = Annotated[ProjectAccess, Depends(require_project_role(Role.MEMBER))]
AdminAccess = Annotated[ProjectAccess, Depends(require_project_role(Role.ADMIN))]
Session = Annotated[AsyncSession, Depends(get_session)]

LongText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
Keyword = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=80)]


class FeedbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Verdict
    # What the cause actually was, if known.
    actual_cause: LongText | None = None
    notes: LongText | None = None


class FeedbackOut(BaseModel):
    verdict: str
    actual_cause: str | None
    notes: str | None
    author: str
    updated_at: datetime
    # True for the caller's own feedback.
    mine: bool


def feedback_out(view: FeedbackView, current_user_id: uuid.UUID) -> FeedbackOut:
    return FeedbackOut(
        verdict=view.feedback.verdict,
        actual_cause=view.feedback.actual_cause,
        notes=view.feedback.notes,
        author=view.author,
        updated_at=view.feedback.updated_at,
        mine=view.feedback.user_id == current_user_id,
    )


class LearningRecordOut(BaseModel):
    id: uuid.UUID
    incident_id: uuid.UUID
    incident_title: str | None
    investigation_id: uuid.UUID
    verdict: str
    confirmed_cause: str | None
    model: str | None
    prompt_version: str | None
    updated_at: datetime


class LearningOverviewOut(BaseModel):
    # How many reviewed investigations got each verdict. Counts, not a calibrated accuracy:
    # they cover only investigations someone chose to review.
    counts: dict[str, int]
    # Newest first.
    records: list[LearningRecordOut]


class EvaluationCaseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{1,119}$")]
    expect_supported: bool | None = None
    must_mention: list[Keyword] = Field(default_factory=list, max_length=10)
    must_not_mention: list[Keyword] = Field(default_factory=list, max_length=10)


class EvaluationCaseOut(BaseModel):
    id: uuid.UUID
    name: str
    source_investigation_id: uuid.UUID
    expect_supported: bool | None
    must_mention: list[str]
    must_not_mention: list[str]
    evidence_items: int
    created_at: datetime


def _case_out(case: EvaluationCase) -> EvaluationCaseOut:
    return EvaluationCaseOut(
        id=case.id,
        name=case.name,
        source_investigation_id=case.source_investigation_id,
        expect_supported=case.expect_supported,
        must_mention=list(case.must_mention),
        must_not_mention=list(case.must_not_mention),
        evidence_items=len(case.evidence),
        created_at=case.created_at,
    )


@router.put("/investigations/{investigation_id}/feedback", response_model=FeedbackOut)
async def put_feedback(
    investigation_id: uuid.UUID, body: FeedbackIn, access: MemberAccess, session: Session
) -> FeedbackOut:
    """Say whether a report was right, and what the cause actually was. Each person has one
    feedback per investigation and can revise it."""
    saved = await submit_feedback(
        session,
        access.scope,
        investigation_id,
        user=access.user,
        verdict=body.verdict,
        actual_cause=body.actual_cause,
        notes=body.notes,
        actor=access.actor,
    )
    author = access.user.display_name or access.user.email or "A team member"
    return feedback_out(FeedbackView(saved, author), access.user.id)


@router.get("/learning-records", response_model=LearningOverviewOut)
async def get_learning_records(
    access: ViewerAccess, session: Session, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> LearningOverviewOut:
    """Reviewed investigations: verdict counts, and the records, newest first."""
    overview = await learning_overview(session, access.scope, limit=limit)

    def investigation(snapshot: dict[str, Any], key: str) -> str | None:
        value = (snapshot.get("investigation") or {}).get(key)
        return value if isinstance(value, str) else None

    return LearningOverviewOut(
        counts=overview.counts,
        records=[
            LearningRecordOut(
                id=record.id,
                incident_id=record.incident_id,
                incident_title=title,
                investigation_id=record.investigation_id,
                verdict=record.verdict,
                confirmed_cause=record.confirmed_cause,
                model=investigation(record.snapshot, "model"),
                prompt_version=investigation(record.snapshot, "prompt_version"),
                updated_at=record.updated_at,
            )
            for record, title in overview.records
        ],
    )


@router.post(
    "/investigations/{investigation_id}/evaluation-case",
    response_model=EvaluationCaseOut,
    status_code=201,
)
async def post_evaluation_case(
    investigation_id: uuid.UUID, body: EvaluationCaseIn, access: AdminAccess, session: Session
) -> EvaluationCaseOut:
    """Save a finished investigation's evidence as a repeatable evaluation case, with rules
    a good report must satisfy. Needs the admin role."""
    case = await create_evaluation_case(
        session,
        access.scope,
        investigation_id,
        name=body.name,
        expect_supported=body.expect_supported,
        must_mention=body.must_mention,
        must_not_mention=body.must_not_mention,
        user_id=access.user.id,
        actor=access.actor,
    )
    return _case_out(case)


@router.get("/evaluation-cases", response_model=list[EvaluationCaseOut])
async def get_evaluation_cases(access: AdminAccess, session: Session) -> list[EvaluationCaseOut]:
    return [_case_out(case) for case in await list_evaluation_cases(session, access.scope)]
