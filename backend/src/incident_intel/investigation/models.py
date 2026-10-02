import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from incident_intel.db.base import Base, CreatedAtMixin


class Investigation(CreatedAtMixin, Base):
    """One AI investigation of an incident: also the work queue (``status``) and the audit
    record of the run."""

    __tablename__ = "investigations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "project_id"],
            ["projects.organization_id", "projects.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["project_id", "incident_id"],
            ["incidents.project_id", "incidents.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("project_id", "id"),  # composite-FK target
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')", name="status_valid"
        ),
        # At most one queued or running investigation per incident.
        Index(
            "uq_investigations_active_per_incident",
            "incident_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
        Index("ix_investigations_incident_id_created_at", "incident_id", "created_at"),
        Index("ix_investigations_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID]
    project_id: Mapped[uuid.UUID]
    incident_id: Mapped[uuid.UUID]
    status: Mapped[str] = mapped_column(String(16))
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    # Filled in by the worker.
    provider: Mapped[str | None] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(80))
    prompt_version: Mapped[str | None] = mapped_column(String(16))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String(500))
    # The checked report (investigation.schemas.Report), and what the checks changed.
    report: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    validation_notes: Mapped[list[str]] = mapped_column(JSONB, default=list)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)


class InvestigationEvidence(Base):
    """One piece of evidence, exactly as the model saw it. Kept even if the underlying
    telemetry is later deleted, so the report stays checkable."""

    __tablename__ = "investigation_evidence"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "investigation_id"],
            ["investigations.project_id", "investigations.id"],
            ondelete="RESTRICT",
        ),
    )

    investigation_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    # The reference the report cites, for example "E3".
    ref: Mapped[str] = mapped_column(String(8), primary_key=True)
    project_id: Mapped[uuid.UUID]
    position: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(300))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


class InvestigationStep(Base):
    """One step of a run: what was done, how long it took, and how it ended."""

    __tablename__ = "investigation_steps"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "investigation_id"],
            ["investigations.project_id", "investigations.id"],
            ondelete="RESTRICT",
        ),
        Index("ix_investigation_steps_investigation_id", "investigation_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    investigation_id: Mapped[uuid.UUID]
    project_id: Mapped[uuid.UUID]
    attempt: Mapped[int] = mapped_column(Integer)
    seq: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(8))  # "ok" or "failed"
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int] = mapped_column(Integer)
    # Counts and outcomes only. Never prompts, replies, or secrets.
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(String(500))
