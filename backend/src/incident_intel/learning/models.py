import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from incident_intel.db.base import Base, CreatedAtMixin

_VERDICTS = "verdict IN ('correct', 'partially_correct', 'incorrect')"


def _project_fk() -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["organization_id", "project_id"],
        ["projects.organization_id", "projects.id"],
        ondelete="RESTRICT",
    )


def _investigation_fk(column: str = "investigation_id") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["project_id", column],
        ["investigations.project_id", "investigations.id"],
        ondelete="RESTRICT",
    )


class InvestigationFeedback(CreatedAtMixin, Base):
    """One person's judgment of one investigation report. A person can revise theirs."""

    __tablename__ = "investigation_feedback"
    __table_args__ = (_investigation_fk(), CheckConstraint(_VERDICTS, name="verdict_valid"))

    investigation_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True
    )
    project_id: Mapped[uuid.UUID]
    verdict: Mapped[str] = mapped_column(String(20))
    # What the cause actually was, in the reviewer's words.
    actual_cause: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LearningRecord(CreatedAtMixin, Base):
    """A reviewed investigation, kept whole: what was seen, what the model concluded, and
    what a person said about it. Self-contained, so it stays meaningful after the
    underlying telemetry is deleted."""

    __tablename__ = "learning_records"
    __table_args__ = (
        _project_fk(),
        _investigation_fk(),
        UniqueConstraint("investigation_id"),
        CheckConstraint(_VERDICTS, name="verdict_valid"),
        Index("ix_learning_records_project_id_updated_at", "project_id", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID]
    project_id: Mapped[uuid.UUID]
    incident_id: Mapped[uuid.UUID]
    investigation_id: Mapped[uuid.UUID]
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    # From the most recent feedback.
    verdict: Mapped[str] = mapped_column(String(20))
    confirmed_cause: Mapped[str | None] = mapped_column(Text)
    # Incident summary, evidence, report, model and prompt version, and all feedback.
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EvaluationCase(CreatedAtMixin, Base):
    """A real investigation turned into a repeatable test: its evidence, and rules a good
    report must satisfy. Run with ``ii eval investigation --project``."""

    __tablename__ = "evaluation_cases"
    __table_args__ = (
        _project_fk(),
        _investigation_fk("source_investigation_id"),
        UniqueConstraint("project_id", "name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID]
    project_id: Mapped[uuid.UUID]
    source_investigation_id: Mapped[uuid.UUID]
    name: Mapped[str] = mapped_column(String(120))
    # The evidence exactly as the model was shown it.
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    # Should the report contain a hypothesis assessed "supported"?
    expect_supported: Mapped[bool | None] = mapped_column(Boolean)
    # The leading hypothesis must mention at least one of these.
    must_mention: Mapped[list[str]] = mapped_column(JSONB, default=list)
    # No supported or weakly supported hypothesis may name any of these.
    must_not_mention: Mapped[list[str]] = mapped_column(JSONB, default=list)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
