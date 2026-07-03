import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ConsentDocument(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A versioned legal document subject to blocking consent (point 16).

    `type` is a ConsentDocumentType value; the gate requires the latest
    ACTIVE version of each type of the actor's audience. Publishing a new
    version (new row, `is_active` toggled) automatically re-gates every
    concerned actor: the gate compares against the active version, nothing
    else to do. No publication endpoint at the MVP (seed/script only).

    `content_md` may carry the {agency_name} token, resolved at READ time
    (the responsible agency's name); `content_hash` is the sha256 of the
    RAW content (token unresolved), copied onto every acceptance."""

    __tablename__ = "consent_document"
    __table_args__ = (UniqueConstraint("type", "version", name="uq_consent_document_type_version"),)

    type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    version: Mapped[int] = mapped_column(nullable=False)
    content_md: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)


class ConsentAcceptance(UUIDPrimaryKeyMixin, Base):
    """Immutable clickwrap trace, insert-only like activity_log (no
    TimestampMixin, no update/delete path anywhere). `actor_id` and
    `agency_id` are bare UUIDs (no FK): the legal trace must survive the
    deletion of the account or the agency it binds.

    Scope: an AGENT (admin) acceptance binds their agency once
    (`agency_id` = their agency); an EXPAT accepts PER AGENCY (the data
    controller), one row per (document, agency).

    The unique constraint (NULLS NOT DISTINCT, PG16) is the belt under
    the manager's idempotence check: the same acceptance can never be
    recorded twice."""

    __tablename__ = "consent_acceptance"
    __table_args__ = (
        Index("ix_consent_acceptance_actor", "actor_type", "actor_id"),
        UniqueConstraint(
            "actor_type",
            "actor_id",
            "document_type",
            "document_version",
            "agency_id",
            name="uq_consent_acceptance",
            postgresql_nulls_not_distinct=True,
        ),
    )

    actor_type: Mapped[str] = mapped_column(String(10), nullable=False)  # AGENT | EXPAT
    actor_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    document_type: Mapped[str] = mapped_column(String(20), nullable=False)
    document_version: Mapped[int] = mapped_column(nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # First X-Forwarded-For hop behind Fly, else the direct client host.
    ip: Mapped[str | None] = mapped_column(String(45))
    agency_id: Mapped[uuid.UUID | None] = mapped_column()
