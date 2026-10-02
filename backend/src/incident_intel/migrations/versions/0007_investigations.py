"""investigations: AI investigation runs, their evidence snapshots and recorded steps

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "investigations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("incident_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=True),
        sa.Column("model", sa.String(length=80), nullable=True),
        sa.Column("prompt_version", sa.String(length=16), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.String(length=500), nullable=True),
        sa.Column("report", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("validation_notes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name=op.f("ck_investigations_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "project_id"],
            ["projects.organization_id", "projects.id"],
            name=op.f("fk_investigations_organization_id_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "incident_id"],
            ["incidents.project_id", "incidents.id"],
            name=op.f("fk_investigations_project_id_incident_id_incidents"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["users.id"],
            name=op.f("fk_investigations_requested_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_investigations")),
        sa.UniqueConstraint("project_id", "id", name=op.f("uq_investigations_project_id_id")),
    )
    op.create_index(
        "ix_investigations_incident_id_created_at",
        "investigations",
        ["incident_id", "created_at"],
        unique=False,
    )
    op.create_index("ix_investigations_status", "investigations", ["status"], unique=False)
    op.create_index(
        "uq_investigations_active_per_incident",
        "investigations",
        ["incident_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_table(
        "investigation_evidence",
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("ref", sa.String(length=8), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id", "investigation_id"],
            ["investigations.project_id", "investigations.id"],
            name=op.f("fk_investigation_evidence_project_id_investigation_id_investigations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("investigation_id", "ref", name=op.f("pk_investigation_evidence")),
    )
    op.create_table(
        "investigation_steps",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=8), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("error", sa.String(length=500), nullable=True),
        sa.ForeignKeyConstraint(
            ["project_id", "investigation_id"],
            ["investigations.project_id", "investigations.id"],
            name=op.f("fk_investigation_steps_project_id_investigation_id_investigations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_investigation_steps")),
    )
    op.create_index(
        "ix_investigation_steps_investigation_id",
        "investigation_steps",
        ["investigation_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_investigation_steps_investigation_id", table_name="investigation_steps")
    op.drop_table("investigation_steps")
    op.drop_table("investigation_evidence")
    op.drop_index(
        "uq_investigations_active_per_incident",
        table_name="investigations",
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.drop_index("ix_investigations_status", table_name="investigations")
    op.drop_index("ix_investigations_incident_id_created_at", table_name="investigations")
    op.drop_table("investigations")
