import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class CustomFieldDefinition(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An agency-defined field on case persons (DÉGEL 2). Scoped to the
    agency — definition A never exists for agency B. Values live in
    case_person.custom_fields (JSONB keyed by `key`), independent of
    this row's lifecycle: archiving never corrupts saved values.

    `key` and `field_type` are IMMUTABLE after creation (the key
    identifies values in the JSONB; changing the type would corrupt
    them). Soft archive only (`archived_at`) — no hard delete."""

    __tablename__ = "custom_field_definition"
    __table_args__ = (
        # `key` is the stable JSONB identifier — unique per agency.
        UniqueConstraint("agency_id", "key", name="uq_custom_field_agency_key"),
        Index("ix_custom_field_agency_active", "agency_id", "archived_at"),
    )

    agency_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agency.id", ondelete="CASCADE"), index=True, nullable=False
    )
    key: Mapped[str] = mapped_column(String(50), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    # BLOC 1 i18n — parallel {lang: text} blob for the label (the `key` stays a
    # hard, untranslated identifier). Scalar `label` remains the read source
    # until BLOC 2. Absent language = absent key.
    label_i18n: Mapped[dict[str, str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    field_type: Mapped[str] = mapped_column(String(20), nullable=False)
    # SELECT / MULTI_SELECT only: list of allowed string values.
    options: Mapped[list[str] | None] = mapped_column(JSONB)
    required: Mapped[bool] = mapped_column(default=False, nullable=False)
    position: Mapped[int] = mapped_column(default=0, nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def is_active(self) -> bool:
        return self.archived_at is None

    # Convenience for callers needing the raw options as a typed list.
    @property
    def option_values(self) -> list[Any]:
        return list(self.options or [])
