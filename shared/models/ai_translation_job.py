"""One AI-translation job of a journey template (async, per-lot).

The job row IS the progress bar: the worker translates one LOT (one
language) at a time and bumps `progress_done` after each — the front
polls GET /journeys/translate-jobs/{id} while the agency keeps working.
A mid-job failure keeps the completed lots (written, and
`points_charged` debited pro rata); fill-empty-only makes the retry
idempotent and cheap."""

import uuid

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class AiTranslationJob(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "ai_translation_job"

    agency_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agency.id", ondelete="CASCADE"), index=True, nullable=False
    )
    template_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("journey_template.id", ondelete="CASCADE"), index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(  # pending|running|done|done_with_gaps|failed
        String(20), nullable=False
    )
    langs: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    progress_done: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    progress_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    translated_keys: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    points_charged: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(String(120))
    # Residual keys the model could not translate acceptably even after the
    # repair pass (e.g. RU field still not Cyrillic): the good fields are
    # written, these are EXPOSED for manual review. "{lang}:{content_key}".
    # A job with residual keys is done_with_gaps, never failed.
    failed_keys: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )


class AiTranslationSource(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Hash memory of what the AI translated — the staleness detector.

    One row per (template, content_key, lang): `source_hash` fingerprints
    the SOURCE text that was translated, `output_hash` the AI text that
    was written. A variant is STALE only if the source hash drifted AND
    the stored variant still IS the recorded AI output; a variant that
    differs from `output_hash` was corrected by a human and is NEVER
    marked stale (no row at all = human translation, same protection)."""

    __tablename__ = "ai_translation_source"
    __table_args__ = (
        UniqueConstraint("template_id", "content_key", "lang", name="uq_ai_translation_source_key"),
    )

    agency_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agency.id", ondelete="CASCADE"), index=True, nullable=False
    )
    template_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("journey_template.id", ondelete="CASCADE"), index=True, nullable=False
    )
    content_key: Mapped[str] = mapped_column(String(255), nullable=False)
    lang: Mapped[str] = mapped_column(String(5), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    output_hash: Mapped[str] = mapped_column(String(64), nullable=False)
