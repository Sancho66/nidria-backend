import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from shared.models.agent import Agent


class SavedView(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Saved list view, ported from Prism (saved_views): filters +
    visible columns + sort, persisted per AGENT. `is_shared` rows are
    visible to the whole agency but mutable by their owner only.

    Duplicate view names are allowed (Prism dropped the name-unique
    constraint on purpose). At most one customizable "All" row per
    (agent, agency, entity) — partial unique index below, the upsert
    guarantee of the /views/default-all endpoints."""

    __tablename__ = "saved_view"
    __table_args__ = (
        Index(
            "uq_saved_view_default_all",
            "agent_id",
            "agency_id",
            "entity",
            unique=True,
            postgresql_where=text("is_default_all"),
        ),
    )

    agency_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agency.id", ondelete="CASCADE"), index=True, nullable=False
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    entity: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True, default="cases", server_default=text("'cases'")
    )
    # Free-form JSONB — the frontend owns the schema (legacy per-field
    # bag or AdvancedFilters tree). PATCH is a full replace, unknown
    # keys are preserved verbatim.
    filters: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    # Ordered list of visible column keys; NULL → frontend defaults.
    columns: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True, default=None)
    # Per-column pixel widths keyed by column slug; NULL → frontend defaults.
    column_sizing: Mapped[dict[str, int] | None] = mapped_column(JSONB, nullable=True, default=None)
    sort_by: Mapped[str | None] = mapped_column(String(50), nullable=True)
    sort_order: Mapped[str | None] = mapped_column(String(10), nullable=True)
    is_default: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default=text("false")
    )
    # True only for the per-agent customizable "All" rows, managed
    # exclusively through /views/default-all — generic CRUD refuses them.
    is_default_all: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default=text("false")
    )
    is_shared: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default=text("false")
    )

    agent: Mapped["Agent"] = relationship()
