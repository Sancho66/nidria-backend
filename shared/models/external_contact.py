import uuid

from sqlalchemy import ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from src.core.enums import ExternalContactType


class ExternalContact(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """External professional (notary, lawyer, bank…), NO login.

    Two scopes on ONE table (no duplicate entity):
    - `case_id` set   → a per-case contact (legacy).
    - `case_id` NULL  → an AGENCY DIRECTORY contact: named once, reusable
      across the agency's cases and journey templates.

    `agency_id` is ALWAYS set (the owner). `agent_id` DESIGNATES an
    `is_external` Agent when the contact is later invited: the contact is
    never transformed into an account — the history stays on the contact,
    the ACCESS lives on the Agent. Until `agent_id` is set the contact has
    no auth surface at all (no token, no invitation, no seat) — by
    construction, not by discipline.
    """

    __tablename__ = "external_contact"
    __table_args__ = (
        # One directory entry per (agency, name) — case_id NULL only. Legacy
        # per-case contacts are unconstrained. Case-insensitive on the NAME
        # (email is nullable — that is the whole point of the entity).
        Index(
            "uq_external_contact_directory_name",
            "agency_id",
            text("lower(name)"),
            unique=True,
            postgresql_where=text("case_id IS NULL"),
        ),
    )

    agency_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agency.id", ondelete="CASCADE"), index=True, nullable=False
    )
    case_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("client_case.id", ondelete="CASCADE"), index=True
    )
    # Designated login account (SET NULL: dropping the Agent un-designates,
    # the contact and its history survive).
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent.id", ondelete="SET NULL"), index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(50))
    type: Mapped[str] = mapped_column(String(30), default=ExternalContactType.OTHER, nullable=False)
