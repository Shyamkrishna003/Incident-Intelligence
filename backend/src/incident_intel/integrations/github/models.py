import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from incident_intel.db.base import Base, CreatedAtMixin


def _project_fk() -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["organization_id", "project_id"],
        ["projects.organization_id", "projects.id"],
        ondelete="RESTRICT",
    )


class GitHubConnection(CreatedAtMixin, Base):
    """A project's GitHub access token. At most one per project."""

    __tablename__ = "github_connections"
    __table_args__ = (_project_fk(),)

    project_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[uuid.UUID]
    # Encrypted with SECRETS_ENCRYPTION_KEY (core.crypto). Never returned by the API.
    token_encrypted: Mapped[str] = mapped_column(Text)
    # The token's last four characters, so an admin can tell which token is stored.
    token_hint: Mapped[str] = mapped_column(String(4))
    connected_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ServiceRepository(CreatedAtMixin, Base):
    """Which repository a service's code lives in ("owner/name")."""

    __tablename__ = "service_repositories"
    __table_args__ = (_project_fk(),)

    project_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    # By name, not service id: a mapping can be set up before the service first reports.
    service_name: Mapped[str] = mapped_column(String(128), primary_key=True)
    organization_id: Mapped[uuid.UUID]
    repository: Mapped[str] = mapped_column(String(141))
