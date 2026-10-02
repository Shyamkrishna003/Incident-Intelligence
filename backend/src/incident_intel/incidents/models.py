import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from incident_intel.db.base import Base, CreatedAtMixin


def _project_fk() -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["organization_id", "project_id"],
        ["projects.organization_id", "projects.id"],
        ondelete="RESTRICT",
    )


class Incident(CreatedAtMixin, Base):
    """A group of related anomalies: one problem, as far as the grouping rules can tell."""

    __tablename__ = "incidents"
    __table_args__ = (
        _project_fk(),
        UniqueConstraint("project_id", "id"),  # composite-FK target
        CheckConstraint("status IN ('open', 'resolved', 'merged')", name="status_valid"),
        CheckConstraint("severity IN ('low', 'medium', 'high', 'critical')", name="severity_valid"),
        Index("ix_incidents_project_id_started_at", "project_id", "started_at"),
        Index("ix_incidents_project_id_status", "project_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID]
    project_id: Mapped[uuid.UUID]
    title: Mapped[str] = mapped_column(String(300))
    # open: at least one anomaly is ongoing. resolved: all recovered. merged: its
    # anomalies were moved into ``merged_into_id``.
    status: Mapped[str] = mapped_column(String(16))
    # The worst severity among its anomalies (a rule on detector scores, not a probability).
    severity: Mapped[str] = mapped_column(String(16))
    # When the earliest anomaly started, and when the incident was first detected.
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("incidents.id", ondelete="RESTRICT")
    )


class IncidentEvent(CreatedAtMixin, Base):
    """One entry on an incident's timeline: what happened, and which rule decided it."""

    __tablename__ = "incident_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "incident_id"],
            ["incidents.project_id", "incidents.id"],
            ondelete="RESTRICT",
        ),
        Index("ix_incident_events_incident_id_ts", "incident_id", "ts"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # The order entries were recorded in, which breaks ties between entries with the same
    # time (several anomalies are often detected in the same instant).
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    project_id: Mapped[uuid.UUID]
    incident_id: Mapped[uuid.UUID]
    # When it happened in the monitored system (not when this row was written).
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    kind: Mapped[str] = mapped_column(String(32))
    # Structured facts behind the entry (rule, services, ids). Rendered by the client.
    details: Mapped[dict[str, Any]] = mapped_column(JSONB)


class IncidentDeployment(CreatedAtMixin, Base):
    """A deployment linked to an incident as a *candidate* for "what changed".

    A link means only: this service is part of the incident and was deployed shortly
    before or during it. It is not a finding that the deployment caused anything.
    """

    __tablename__ = "incident_deployments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "incident_id"],
            ["incidents.project_id", "incidents.id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("timing IN ('before', 'during')", name="timing_valid"),
    )

    incident_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    deployment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("deployments.id", ondelete="RESTRICT"), primary_key=True
    )
    project_id: Mapped[uuid.UUID]
    # Relative to the incident's start.
    timing: Mapped[str] = mapped_column(String(8))


class ServiceDependency(CreatedAtMixin, Base):
    """A declared relationship: ``service_id`` depends on (calls) ``depends_on_id``."""

    __tablename__ = "service_dependencies"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "service_id"],
            ["services.project_id", "services.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["project_id", "depends_on_id"],
            ["services.project_id", "services.id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("service_id <> depends_on_id", name="not_self"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    service_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    depends_on_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
