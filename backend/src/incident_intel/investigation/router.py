"""Investigations, for signed-in users. Requesting one needs the member role, because each
run uses the LLM budget."""

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from incident_intel.api.deps import get_settings_dep, require_project_role
from incident_intel.core.config import Settings
from incident_intel.db.session import get_session
from incident_intel.investigation.models import Investigation
from incident_intel.investigation.schemas import Report
from incident_intel.investigation.service import (
    get_investigation,
    list_investigations,
    request_investigation,
)
from incident_intel.tenancy.access import ProjectAccess
from incident_intel.tenancy.roles import Role

router = APIRouter(prefix="/v1/projects/{project_id}", tags=["console"])

ViewerAccess = Annotated[ProjectAccess, Depends(require_project_role(Role.VIEWER))]
MemberAccess = Annotated[ProjectAccess, Depends(require_project_role(Role.MEMBER))]
Session = Annotated[AsyncSession, Depends(get_session)]


class InvestigationOut(BaseModel):
    id: uuid.UUID
    incident_id: uuid.UUID
    status: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    # Which model wrote the report, and with which prompt version.
    provider: str | None
    model: str | None
    prompt_version: str | None
    error: str | None


class EvidenceOut(BaseModel):
    ref: str
    kind: str
    title: str
    # Exactly what the model was shown.
    data: dict[str, Any]


class StepOut(BaseModel):
    attempt: int
    seq: int
    kind: str
    status: str
    duration_ms: int
    summary: dict[str, Any]
    error: str | None


class InvestigationDetailOut(InvestigationOut):
    # Written by an AI model, then checked by code: every reference in it exists in
    # `evidence`. Hypotheses are not confirmed findings.
    report: Report | None
    # What the checks removed or changed in the model's draft.
    validation_notes: list[str]
    evidence: list[EvidenceOut]
    steps: list[StepOut]
    input_tokens: int
    output_tokens: int


class InvestigationListResponse(BaseModel):
    # Newest first.
    investigations: list[InvestigationOut]


def _out(investigation: Investigation) -> InvestigationOut:
    return InvestigationOut(
        id=investigation.id,
        incident_id=investigation.incident_id,
        status=investigation.status,
        created_at=investigation.created_at,
        started_at=investigation.started_at,
        finished_at=investigation.finished_at,
        provider=investigation.provider,
        model=investigation.model,
        prompt_version=investigation.prompt_version,
        error=investigation.error,
    )


@router.post(
    "/incidents/{incident_id}/investigations", response_model=InvestigationOut, status_code=202
)
async def request_investigation_route(
    incident_id: uuid.UUID,
    access: MemberAccess,
    session: Session,
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> InvestigationOut:
    """Queue an AI investigation of the incident. It runs in the background."""
    investigation = await request_investigation(
        session,
        access.scope,
        incident_id,
        user_id=access.user.id,
        actor=access.actor,
        max_per_hour=settings.investigations_per_incident_per_hour,
    )
    return _out(investigation)


@router.get("/incidents/{incident_id}/investigations", response_model=InvestigationListResponse)
async def list_investigations_route(
    incident_id: uuid.UUID, access: ViewerAccess, session: Session
) -> InvestigationListResponse:
    rows = await list_investigations(session, access.scope, incident_id)
    return InvestigationListResponse(investigations=[_out(row) for row in rows])


@router.get("/investigations/{investigation_id}", response_model=InvestigationDetailOut)
async def get_investigation_route(
    investigation_id: uuid.UUID, access: ViewerAccess, session: Session
) -> InvestigationDetailOut:
    detail = await get_investigation(session, access.scope, investigation_id)
    investigation = detail.investigation
    return InvestigationDetailOut(
        **_out(investigation).model_dump(),
        report=Report.model_validate(investigation.report) if investigation.report else None,
        validation_notes=list(investigation.validation_notes or []),
        evidence=[
            EvidenceOut(ref=item.ref, kind=item.kind, title=item.title, data=item.data)
            for item in detail.evidence
        ],
        steps=[
            StepOut(
                attempt=step.attempt,
                seq=step.seq,
                kind=step.kind,
                status=step.status,
                duration_ms=step.duration_ms,
                summary=step.summary,
                error=step.error,
            )
            for step in detail.steps
        ],
        input_tokens=investigation.input_tokens,
        output_tokens=investigation.output_tokens,
    )
