"""telemetry metrics: services, metric_series, metric_points, ingest_batches

Composite foreign keys make the database reject any row whose project differs from its
parent's. The unique constraints are what make the storage consumer idempotent.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "services",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "project_id"],
            ["projects.organization_id", "projects.id"],
            name=op.f("fk_services_organization_id_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_services")),
        sa.UniqueConstraint("project_id", "id", name=op.f("uq_services_project_id_id")),
        sa.UniqueConstraint("project_id", "name", name=op.f("uq_services_project_id_name")),
    )
    op.create_table(
        "ingest_batches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("api_key_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("point_count", sa.Integer(), nullable=False),
        sa.Column("stored_point_count", sa.Integer(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "stored_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["api_key_id"],
            ["api_keys.id"],
            name=op.f("fk_ingest_batches_api_key_id_api_keys"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "project_id"],
            ["projects.organization_id", "projects.id"],
            name=op.f("fk_ingest_batches_organization_id_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ingest_batches")),
    )
    op.create_index(
        "ix_ingest_batches_project_id_received_at",
        "ingest_batches",
        ["project_id", "received_at"],
        unique=False,
    )
    op.create_table(
        "metric_series",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("unit", sa.String(length=32), nullable=True),
        sa.Column("attributes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("attributes_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "project_id"],
            ["projects.organization_id", "projects.id"],
            name=op.f("fk_metric_series_organization_id_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "service_id"],
            ["services.project_id", "services.id"],
            name=op.f("fk_metric_series_project_id_service_id_services"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_metric_series")),
        sa.UniqueConstraint("project_id", "id", name=op.f("uq_metric_series_project_id_id")),
        sa.UniqueConstraint(
            "project_id",
            "service_id",
            "name",
            "attributes_hash",
            name=op.f("uq_metric_series_project_id_service_id_name_attributes_hash"),
        ),
    )
    op.create_table(
        "metric_points",
        sa.Column("series_id", sa.Uuid(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("value", sa.Double(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id", "series_id"],
            ["metric_series.project_id", "metric_series.id"],
            name=op.f("fk_metric_points_project_id_series_id_metric_series"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("series_id", "ts", name=op.f("pk_metric_points")),
    )


def downgrade() -> None:
    op.drop_table("metric_points")
    op.drop_table("metric_series")
    op.drop_index("ix_ingest_batches_project_id_received_at", table_name="ingest_batches")
    op.drop_table("ingest_batches")
    op.drop_table("services")
