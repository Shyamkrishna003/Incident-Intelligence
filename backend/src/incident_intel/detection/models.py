import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Double,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from incident_intel.db.base import Base, CreatedAtMixin


class Anomaly(CreatedAtMixin, Base):
    """A run of abnormal points on one metric series, as judged by one detector.

    ``peak_score`` is the detector's own measure of distance from normal. It is not a
    probability and is only comparable to that detector's threshold.
    """

    __tablename__ = "anomalies"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "project_id"],
            ["projects.organization_id", "projects.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["project_id", "series_id"],
            ["metric_series.project_id", "metric_series.id"],
            ondelete="RESTRICT",
        ),
        # Composite, so an anomaly can only belong to an incident of its own project.
        ForeignKeyConstraint(
            ["project_id", "incident_id"],
            ["incidents.project_id", "incidents.id"],
            ondelete="RESTRICT",
        ),
        Index("ix_anomalies_incident_id", "incident_id"),
        CheckConstraint("status IN ('open', 'closed')", name="status_valid"),
        CheckConstraint("direction IN ('above', 'below')", name="direction_valid"),
        CheckConstraint("severity IN ('low', 'medium', 'high', 'critical')", name="severity_valid"),
        CheckConstraint("(status = 'open') = (ended_at IS NULL)", name="open_has_no_end"),
        # At most one open anomaly per series and detector, enforced by the database.
        Index(
            "uq_anomalies_open_per_series",
            "project_id",
            "series_id",
            "detector",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
        Index("ix_anomalies_project_id_started_at", "project_id", "started_at"),
        Index("ix_anomalies_series_id_detector", "series_id", "detector"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID]
    project_id: Mapped[uuid.UUID]
    series_id: Mapped[uuid.UUID]
    detector: Mapped[str] = mapped_column(String(32))
    detector_version: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(8))
    direction: Mapped[str] = mapped_column(String(8))
    severity: Mapped[str] = mapped_column(String(16))
    # The first abnormal point, and the point at which the anomaly was opened.
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_anomalous_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # "recovered" or "persisted" (open too long; the level became the new normal).
    closed_reason: Mapped[str | None] = mapped_column(String(16))
    peak_score: Mapped[float] = mapped_column(Double)
    peak_value: Mapped[float] = mapped_column(Double)
    peak_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # What "normal" was when the anomaly opened: the evidence behind the judgment.
    baseline_center: Mapped[float] = mapped_column(Double)
    baseline_spread: Mapped[float] = mapped_column(Double)
    point_count: Mapped[int] = mapped_column(Integer)
    # The incident this anomaly was grouped into (null for anomalies from before grouping).
    incident_id: Mapped[uuid.UUID | None]


class DetectionState(Base):
    """How far each series has been evaluated by each detector."""

    __tablename__ = "detection_state"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "series_id"],
            ["metric_series.project_id", "metric_series.id"],
            ondelete="RESTRICT",
        ),
    )

    series_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    detector: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[uuid.UUID]
    # Points up to and including this time have been evaluated. Re-delivered events and
    # late points at or before it are ignored, which makes detection idempotent.
    evaluated_through: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # An unfinished streak of anomalous points that has not opened an anomaly yet.
    pending_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pending_count: Mapped[int] = mapped_column(Integer, default=0)
