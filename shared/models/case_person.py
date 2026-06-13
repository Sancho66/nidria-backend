import uuid
from datetime import date
from typing import TYPE_CHECKING, Any

from sqlalchemy import Date, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.orm import relationship as orm_relationship

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from shared.models.expat_user import ExpatUser


class CasePerson(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A person attached to a case — the unified carrier of CIVIL STATUS
    (RGPD: scoped to case_id, NEVER on the shared expat_user).

    `kind=PRINCIPAL`: exactly one per case (partial unique index below),
    not deletable, `expat_user_id` links to the shared login identity —
    its name/email/lang are read from expat_user, so `full_name` is NULL.
    `kind=FAMILY`: any number, no login, carries its own `full_name` +
    `relationship`. Both kinds carry the same nullable civil-status
    fields, so the frontend edits everyone with one component."""

    __tablename__ = "case_person"
    __table_args__ = (
        # One PRINCIPAL per case — the invariant. Partial: FAMILY rows
        # are unconstrained.
        Index(
            "uq_case_person_principal",
            "case_id",
            unique=True,
            postgresql_where=text("kind = 'principal'"),
        ),
    )

    case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("client_case.id", ondelete="CASCADE"), index=True, nullable=False
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    # PRINCIPAL only: the shared login identity (name/email/lang live there).
    expat_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("expat_user.id", ondelete="RESTRICT"), index=True
    )
    # FAMILY: required local name. PRINCIPAL: NULL (read from expat_user).
    full_name: Mapped[str | None] = mapped_column(String(200))
    relationship: Mapped[str | None] = mapped_column(String(50))

    # --- civil status (all nullable, case-scoped) ---------------------------------
    passport_number: Mapped[str | None] = mapped_column(String(50))
    date_of_birth: Mapped[date | None] = mapped_column(Date)
    nationality: Mapped[str | None] = mapped_column(String(100))
    place_of_birth: Mapped[str | None] = mapped_column(String(200))
    sex: Mapped[str | None] = mapped_column(String(1))
    marital_status: Mapped[str | None] = mapped_column(String(20))
    residence_permit_number: Mapped[str | None] = mapped_column(String(50))
    phone: Mapped[str | None] = mapped_column(String(50))

    # Agency-defined custom fields (DÉGEL 2): {definition.key: value}.
    # Independent of the definitions' lifecycle — archiving a definition
    # never touches this sack; orphan keys are kept but not exposed.
    custom_fields: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )

    expat_user: Mapped["ExpatUser | None"] = orm_relationship("ExpatUser")
