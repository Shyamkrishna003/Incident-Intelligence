"""learning: feedback on investigations, learning records, evaluation cases

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "evaluation_cases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("source_investigation_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("expect_supported", sa.Boolean(), nullable=True),
        sa.Column("must_mention", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("must_not_mention", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_evaluation_cases_created_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "project_id"],
            ["projects.organization_id", "projects.id"],
            name=op.f("fk_evaluation_cases_organization_id_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "source_investigation_id"],
            ["investigations.project_id", "investigations.id"],
            name=op.f("fk_evaluation_cases_project_id_source_investigation_id_investigations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evaluation_cases")),
        sa.UniqueConstraint("project_id", "name", name=op.f("uq_evaluation_cases_project_id_name")),
    )
    op.create_table(
        "investigation_feedback",
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("verdict", sa.String(length=20), nullable=False),
        sa.Column("actual_cause", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "verdict IN ('correct', 'partially_correct', 'incorrect')",
            name=op.f("ck_investigation_feedback_verdict_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "investigation_id"],
            ["investigations.project_id", "investigations.id"],
            name=op.f("fk_investigation_feedback_project_id_investigation_id_investigations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_investigation_feedback_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "investigation_id", "user_id", name=op.f("pk_investigation_feedback")
        ),
    )
    op.create_table(
        "learning_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("incident_id", sa.Uuid(), nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("verdict", sa.String(length=20), nullable=False),
        sa.Column("confirmed_cause", sa.Text(), nullable=True),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "verdict IN ('correct', 'partially_correct', 'incorrect')",
            name=op.f("ck_learning_records_verdict_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "project_id"],
            ["projects.organization_id", "projects.id"],
            name=op.f("fk_learning_records_organization_id_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "investigation_id"],
            ["investigations.project_id", "investigations.id"],
            name=op.f("fk_learning_records_project_id_investigation_id_investigations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_learning_records")),
        sa.UniqueConstraint("investigation_id", name=op.f("uq_learning_records_investigation_id")),
    )
    op.create_index(
        "ix_learning_records_project_id_updated_at",
        "learning_records",
        ["project_id", "updated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_learning_records_project_id_updated_at", table_name="learning_records")
    op.drop_table("learning_records")
    op.drop_table("investigation_feedback")
    op.drop_table("evaluation_cases")
