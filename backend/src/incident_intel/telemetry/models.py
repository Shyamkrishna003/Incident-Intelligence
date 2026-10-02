"""Telemetry storage. Every row is tenant-scoped, and composite foreign keys make the database
reject a row whose project does not match its parent's project."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    Double,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
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


class Service(CreatedAtMixin, Base):
    """A monitored service, registered implicitly the first time it sends telemetry."""

    __tablename__ = "services"
    __table_args__ = (
        _project_fk(),
        UniqueConstraint("project_id", "name"),
        UniqueConstraint("project_id", "id"),  # composite-FK target
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID]
    project_id: Mapped[uuid.UUID]
    name: Mapped[str] = mapped_column(String(128))


class MetricSeries(CreatedAtMixin, Base):
    """One time series: a metric name on a service with one specific attribute set."""

    __tablename__ = "metric_series"
    __table_args__ = (
        _project_fk(),
        ForeignKeyConstraint(
            ["project_id", "service_id"],
            ["services.project_id", "services.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("project_id", "service_id", "name", "attributes_hash"),
        UniqueConstraint("project_id", "id"),  # composite-FK target
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID]
    project_id: Mapped[uuid.UUID]
    service_id: Mapped[uuid.UUID]
    name: Mapped[str] = mapped_column(String(128))
    # Recorded from the first point written; not part of the series identity.
    unit: Mapped[str | None] = mapped_column(String(32))
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB)
    attributes_hash: Mapped[str] = mapped_column(String(64))


class MetricPoint(Base):
    """A single observation. The highest-volume table: it carries only ``project_id`` (not
    ``organization_id``) and relies on its series for the rest of the tenant chain."""

    __tablename__ = "metric_points"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "series_id"],
            ["metric_series.project_id", "metric_series.id"],
            ondelete="RESTRICT",
        ),
    )

    # Primary key (series_id, ts) makes storage idempotent and serves time-range reads.
    series_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    project_id: Mapped[uuid.UUID]
    value: Mapped[float] = mapped_column(Double)


class IngestBatch(Base):
    """Bookkeeping for each stored batch; its primary key is what makes replays no-ops."""

    __tablename__ = "ingest_batches"
    __table_args__ = (
        _project_fk(),
        Index("ix_ingest_batches_project_id_received_at", "project_id", "received_at"),
    )

    # Derived from (project_id, idempotency key), so a retried request maps to the same row.
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[uuid.UUID]
    project_id: Mapped[uuid.UUID]
    api_key_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("api_keys.id", ondelete="RESTRICT"))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    content_sha256: Mapped[str] = mapped_column(String(64))
    point_count: Mapped[int] = mapped_column(Integer)
    # Can be lower than point_count: points already stored for the same series and
    # timestamp are skipped (first write wins).
    stored_point_count: Mapped[int] = mapped_column(Integer)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    stored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
