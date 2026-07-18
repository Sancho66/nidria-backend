import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.models.base import Base, PersonNameMixin, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from shared.models.rbac import Role


class Agent(UUIDPrimaryKeyMixin, PersonNameMixin, TimestampMixin, Base):
    """Agency-side user. ONE role per agent (Prism model) — a dynamic
    FK to the editable `role` table, deliberately NO hardcoded enum.
    RESTRICT: a role somebody wears cannot be deleted (the Manager
    rebinds clone wearers before deleting)."""

    __tablename__ = "agent"

    agency_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agency.id", ondelete="CASCADE"), index=True, nullable=False
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("role.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # Profile picture (bloc 1) — private-bucket storage path, served by the
    # backend only (never a direct Supabase URL). NULL = initials fallback.
    avatar_path: Mapped[str | None] = mapped_column(String(500))
    # Last LOGIN (token issuance), posed by auth_manager — NEVER a refresh
    # (same session continuing) nor impersonation (not the agent's own login).
    # The adoption dashboard reads MAX per agency: the heartbeat that tells an
    # agency reflecting apart from an agency that abandoned.
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Denormalized from the role's kind at creation (an external agent =
    # a provider, lawyer/notary/…): the CHEAP filter read by enforce() on
    # every request and by every "agents of the agency" listing to keep
    # externals out. Stable: role reassignment never crosses internal↔
    # external (validated in the managers).
    is_external: Mapped[bool] = mapped_column(
        default=False, server_default=text("false"), nullable=False
    )
    # Offboarding (never a DELETE: the identity lives in activity logs,
    # completions, approvals). Set = login refused, live tokens die at the
    # next request (_resolve_agent re-reads this row), out of every seat/
    # provider count, not an impersonation target. NULL = active.
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Personal notification preferences (2026-07-18): {comments:
    # on|grouped|off, ready_to_validate: on|off} — NULL = the defaults
    # (src/core/notification_prefs.py). The critical is not in the model.
    notification_prefs: Mapped[dict[str, str] | None] = mapped_column(JSONB)

    role: Mapped["Role"] = relationship("Role")
