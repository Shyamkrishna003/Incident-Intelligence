import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from incident_intel.db.base import Base, CreatedAtMixin


class Organization(CreatedAtMixin, Base):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(63), unique=True)
    name: Mapped[str] = mapped_column(String(200))


class Project(CreatedAtMixin, Base):
    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("organization_id", "slug"),
        # Target for composite foreign keys: lets child rows prove that their
        # (organization_id, project_id) pair is consistent, enforced by the database.
        UniqueConstraint("organization_id", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT")
    )
    slug: Mapped[str] = mapped_column(String(63))
    name: Mapped[str] = mapped_column(String(200))


class ApiKey(CreatedAtMixin, Base):
    """A project-scoped machine credential. Only an HMAC of the key is stored."""

    __tablename__ = "api_keys"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "project_id"],
            ["projects.organization_id", "projects.id"],
            ondelete="RESTRICT",
        ),
        Index("ix_api_keys_project_id", "project_id"),
        CheckConstraint("cardinality(scopes) > 0", name="scopes_not_empty"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID]
    project_id: Mapped[uuid.UUID]
    name: Mapped[str] = mapped_column(String(100))
    # Public, non-secret lookup identifier embedded in the key.
    key_prefix: Mapped[str] = mapped_column(String(12), unique=True)
    # Hex HMAC-SHA256(pepper, full key).
    key_hash: Mapped[str] = mapped_column(String(64))
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String(32)))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class User(CreatedAtMixin, Base):
    """A person. Identity is proven by Firebase; this row is our own record of them."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # The stable Firebase account id (the ID token's subject). Emails can change; this cannot.
    firebase_uid: Mapped[str] = mapped_column(String(128), unique=True)
    email: Mapped[str | None] = mapped_column(String(320))
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    display_name: Mapped[str | None] = mapped_column(String(200))


class Membership(CreatedAtMixin, Base):
    """A user's role in an organization. This table, not Firebase, decides authorization."""

    __tablename__ = "memberships"
    __table_args__ = (
        CheckConstraint("role IN ('viewer', 'member', 'admin', 'owner')", name="role_valid"),
        Index("ix_memberships_user_id", "user_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(16))
