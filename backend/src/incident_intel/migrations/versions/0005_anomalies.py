"""anomalies: detected anomalies and per-series detection progress

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "anomalies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("series_id", sa.Uuid(), nullable=False),
        sa.Column("detector", sa.String(length=32), nullable=False),
        sa.Column("detector_version", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=8), nullable=False),
        sa.Column("direction", sa.String(length=8), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_anomalous_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_reason", sa.String(length=16), nullable=True),
        sa.Column("peak_score", sa.Double(), nullable=False),
        sa.Column("peak_value", sa.Double(), nullable=False),
        sa.Column("peak_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("baseline_center", sa.Double(), nullable=False),
        sa.Column("baseline_spread", sa.Double(), nullable=False),
        sa.Column("point_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(status = 'open') = (ended_at IS NULL)", name=op.f("ck_anomalies_open_has_no_end")
        ),
        sa.CheckConstraint(
            "direction IN ('above', 'below')", name=op.f("ck_anomalies_direction_valid")
        ),
        sa.CheckConstraint(
            "severity IN ('low', 'medium', 'high', 'critical')",
            name=op.f("ck_anomalies_severity_valid"),
        ),
        sa.CheckConstraint("status IN ('open', 'closed')", name=op.f("ck_anomalies_status_valid")),
        sa.ForeignKeyConstraint(
            ["organization_id", "project_id"],
            ["projects.organization_id", "projects.id"],
            name=op.f("fk_anomalies_organization_id_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "series_id"],
            ["metric_series.project_id", "metric_series.id"],
            name=op.f("fk_anomalies_project_id_series_id_metric_series"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_anomalies")),
    )
    op.create_index(
        "ix_anomalies_project_id_started_at",
        "anomalies",
        ["project_id", "started_at"],
        unique=False,
    )
    op.create_index(
        "ix_anomalies_series_id_detector", "anomalies", ["series_id", "detector"], unique=False
    )
    op.create_index(
        "uq_anomalies_open_per_series",
        "anomalies",
        ["project_id", "series_id", "detector"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )
    op.create_table(
        "detection_state",
        sa.Column("series_id", sa.Uuid(), nullable=False),
        sa.Column("detector", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("evaluated_through", sa.DateTime(timezone=True), nullable=False),
        sa.Column("pending_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pending_count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id", "series_id"],
            ["metric_series.project_id", "metric_series.id"],
            name=op.f("fk_detection_state_project_id_series_id_metric_series"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("series_id", "detector", name=op.f("pk_detection_state")),
    )


def downgrade() -> None:
    op.drop_table("detection_state")
    op.drop_index(
        "uq_anomalies_open_per_series",
        table_name="anomalies",
        postgresql_where=sa.text("status = 'open'"),
    )
    op.drop_index("ix_anomalies_series_id_detector", table_name="anomalies")
    op.drop_index("ix_anomalies_project_id_started_at", table_name="anomalies")
    op.drop_table("anomalies")
