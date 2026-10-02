"""incidents: incidents, their timeline and candidate deployments, service dependencies

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "incidents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("merged_into_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "severity IN ('low', 'medium', 'high', 'critical')",
            name=op.f("ck_incidents_severity_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('open', 'resolved', 'merged')", name=op.f("ck_incidents_status_valid")
        ),
        sa.ForeignKeyConstraint(
            ["merged_into_id"],
            ["incidents.id"],
            name=op.f("fk_incidents_merged_into_id_incidents"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "project_id"],
            ["projects.organization_id", "projects.id"],
            name=op.f("fk_incidents_organization_id_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_incidents")),
        sa.UniqueConstraint("project_id", "id", name=op.f("uq_incidents_project_id_id")),
    )
    op.create_index(
        "ix_incidents_project_id_started_at",
        "incidents",
        ["project_id", "started_at"],
        unique=False,
    )
    op.create_index(
        "ix_incidents_project_id_status", "incidents", ["project_id", "status"], unique=False
    )
    op.create_table(
        "incident_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("incident_id", sa.Uuid(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "incident_id"],
            ["incidents.project_id", "incidents.id"],
            name=op.f("fk_incident_events_project_id_incident_id_incidents"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_incident_events")),
        sa.UniqueConstraint("seq", name=op.f("uq_incident_events_seq")),
    )
    op.create_index(
        "ix_incident_events_incident_id_ts", "incident_events", ["incident_id", "ts"], unique=False
    )
    op.create_table(
        "service_dependencies",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.Column("depends_on_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "service_id <> depends_on_id", name=op.f("ck_service_dependencies_not_self")
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "depends_on_id"],
            ["services.project_id", "services.id"],
            name=op.f("fk_service_dependencies_project_id_depends_on_id_services"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "service_id"],
            ["services.project_id", "services.id"],
            name=op.f("fk_service_dependencies_project_id_service_id_services"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "project_id", "service_id", "depends_on_id", name=op.f("pk_service_dependencies")
        ),
    )
    op.create_table(
        "incident_deployments",
        sa.Column("incident_id", sa.Uuid(), nullable=False),
        sa.Column("deployment_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("timing", sa.String(length=8), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "timing IN ('before', 'during')", name=op.f("ck_incident_deployments_timing_valid")
        ),
        sa.ForeignKeyConstraint(
            ["deployment_id"],
            ["deployments.id"],
            name=op.f("fk_incident_deployments_deployment_id_deployments"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "incident_id"],
            ["incidents.project_id", "incidents.id"],
            name=op.f("fk_incident_deployments_project_id_incident_id_incidents"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "incident_id", "deployment_id", name=op.f("pk_incident_deployments")
        ),
    )
    op.add_column("anomalies", sa.Column("incident_id", sa.Uuid(), nullable=True))
    op.create_index("ix_anomalies_incident_id", "anomalies", ["incident_id"], unique=False)
    op.create_foreign_key(
        op.f("fk_anomalies_project_id_incident_id_incidents"),
        "anomalies",
        "incidents",
        ["project_id", "incident_id"],
        ["project_id", "id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_anomalies_project_id_incident_id_incidents"), "anomalies", type_="foreignkey"
    )
    op.drop_index("ix_anomalies_incident_id", table_name="anomalies")
    op.drop_column("anomalies", "incident_id")
    op.drop_table("incident_deployments")
    op.drop_table("service_dependencies")
    op.drop_index("ix_incident_events_incident_id_ts", table_name="incident_events")
    op.drop_table("incident_events")
    op.drop_index("ix_incidents_project_id_status", table_name="incidents")
    op.drop_index("ix_incidents_project_id_started_at", table_name="incidents")
    op.drop_table("incidents")
