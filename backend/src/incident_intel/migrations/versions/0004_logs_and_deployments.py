"""logs and deployments: log_records, deployments, and a kind on ingest_batches

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "deployments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.String(length=128), nullable=False),
        sa.Column("deployed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("commit_sha", sa.String(length=40), nullable=True),
        sa.Column("environment", sa.String(length=64), nullable=True),
        sa.Column("deployed_by", sa.String(length=128), nullable=True),
        sa.Column("description", sa.String(length=1000), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "project_id"],
            ["projects.organization_id", "projects.id"],
            name=op.f("fk_deployments_organization_id_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "service_id"],
            ["services.project_id", "services.id"],
            name=op.f("fk_deployments_project_id_service_id_services"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deployments")),
        sa.UniqueConstraint(
            "project_id",
            "service_id",
            "version",
            "deployed_at",
            name=op.f("uq_deployments_project_id_service_id_version_deployed_at"),
        ),
    )
    op.create_index(
        "ix_deployments_project_id_deployed_at",
        "deployments",
        ["project_id", "deployed_at"],
        unique=False,
    )
    op.create_table(
        "log_records",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("severity", sa.SmallInteger(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("attributes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("trace_id", sa.String(length=32), nullable=True),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id", "service_id"],
            ["services.project_id", "services.id"],
            name=op.f("fk_log_records_project_id_service_id_services"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_log_records")),
    )
    op.create_index(
        "ix_log_records_project_id_service_id_ts",
        "log_records",
        ["project_id", "service_id", "ts"],
        unique=False,
    )
    op.add_column(
        "ingest_batches",
        sa.Column("kind", sa.String(length=16), server_default="metrics", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("ingest_batches", "kind")
    op.drop_index("ix_log_records_project_id_service_id_ts", table_name="log_records")
    op.drop_table("log_records")
    op.drop_index("ix_deployments_project_id_deployed_at", table_name="deployments")
    op.drop_table("deployments")
