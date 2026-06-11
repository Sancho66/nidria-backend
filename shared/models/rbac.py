import uuid

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Permission(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Relational mirror of the in-code Permission enum (the catalogue).
    Synced at boot: missing keys inserted, NEVER deleted."""

    __tablename__ = "permission"

    key: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    label: Mapped[str | None] = mapped_column(String(200))
    category: Mapped[str | None] = mapped_column(String(50))


class Role(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """`agency_id` NULL = system role (admin/member/viewer/case_manager),
    shared by all agencies. Non-NULL = agency custom role.
    `nulls_not_distinct` so two system roles can't share a name."""

    __tablename__ = "role"
    __table_args__ = (UniqueConstraint("agency_id", "name", postgresql_nulls_not_distinct=True),)

    agency_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agency.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    is_system: Mapped[bool] = mapped_column(default=False, nullable=False)

    permissions: Mapped[list[Permission]] = relationship(Permission, secondary="role_permission")


class RolePermission(Base):
    """THE editable matrix — zero hardcoded role→permission mapping."""

    __tablename__ = "role_permission"

    role_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("role.id", ondelete="CASCADE"), primary_key=True
    )
    permission_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("permission.id", ondelete="CASCADE"), primary_key=True
    )


class AgentRole(Base):
    """M2M: an agent can hold several roles; effective_permissions is
    the union across them."""

    __tablename__ = "agent_role"

    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent.id", ondelete="CASCADE"), primary_key=True
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("role.id", ondelete="CASCADE"), primary_key=True
    )


class ProtectedResource(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Route→permission binding, in data. `audience` drives enforcement:
    PUBLIC passes; AGENT requires a valid agent token, plus the matrix
    permission when the binding carries one (NULL permission = any
    authenticated agent — identity endpoints like /me, /logout); EXPAT
    requires a valid expat token, never a permission (ownership checked
    in Managers). Deny by default: an unbound route is a 403 and the
    boot check refuses to start."""

    __tablename__ = "protected_resource"
    __table_args__ = (
        UniqueConstraint("method", "route"),
        CheckConstraint(
            "(audience IN ('public', 'expat') AND permission_id IS NULL) OR audience = 'agent'",
            name="audience_permission_coherence",
        ),
    )

    method: Mapped[str] = mapped_column(String(10), nullable=False)
    route: Mapped[str] = mapped_column(String(255), nullable=False)
    audience: Mapped[str] = mapped_column(String(20), nullable=False)
    permission_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("permission.id", ondelete="RESTRICT")
    )

    # Many-to-one, eager `joined` (single query, no async lazy-load
    # trap): enforcement reads `binding.permission.key` on every hit.
    permission: Mapped[Permission | None] = relationship(Permission, lazy="joined")
