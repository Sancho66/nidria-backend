import uuid
from typing import Any

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class CrmImportMapping(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A saved CSV→parcours mapping (BLOC 3), scoped to an agency and reused
    to pre-fill a CRM import. Tied to a `journey_template` because the valid
    targets are exactly that parcours' Informations-tab fields.

    UNIQUE(agency_id, journey_template_id, crm_slug, name): SEVERAL named
    configs per (agency, parcours, CRM) coexist; `name` is part of the key
    (NOT NULL) so two different names create two rows and a same-name create
    is a conflict (409). Editing resolves by row id (a rename collision also
    409s on the unique key).

    `mapping` JSONB = {csv_column: target_token}. agency_id scopes every
    read/write (never cross-agency); the template FK cascades (a mapping is
    meaningless without its parcours)."""

    __tablename__ = "crm_import_mapping"
    __table_args__ = (
        UniqueConstraint(
            "agency_id",
            "journey_template_id",
            "crm_slug",
            "name",
            name="uq_crm_import_mapping",
        ),
    )

    agency_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agency.id", ondelete="CASCADE"), index=True, nullable=False
    )
    journey_template_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("journey_template.id", ondelete="CASCADE"), index=True, nullable=False
    )
    crm_slug: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    mapping: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
