"""One AI-translation job of a journey template (async, per-lot).

The job row IS the progress bar: the worker translates one LOT (one
language) at a time and bumps `progress_done` after each — the front
polls GET /journeys/translate-jobs/{id} while the agency keeps working.
A mid-job failure keeps the completed lots (written, and
`points_charged` debited pro rata); fill-empty-only makes the retry
idempotent and cheap."""

import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class AiTranslationJob(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "ai_translation_job"
    # RAIL ÉLARGI (lot 14/08) : le MÊME job sert deux contenus — un parcours
    # (template_id) OU un modèle de message (message_template_id), jamais les
    # deux, jamais aucun. Une seule table parce que c'est UNE seule infra :
    # même barre de progression, même pool de points, même polling front.
    __table_args__ = (
        CheckConstraint(
            "num_nonnulls(template_id, message_template_id) = 1",
            name="ck_ai_translation_job_one_target",
        ),
    )

    agency_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agency.id", ondelete="CASCADE"), index=True, nullable=False
    )
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("journey_template.id", ondelete="CASCADE"), index=True
    )
    message_template_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("message_template.id", ondelete="CASCADE"), index=True
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
        # Le pendant du unique ci-dessus pour les modèles de message : partiel,
        # parce que les lignes parcours portent message_template_id NULL (et
        # réciproquement — le CHECK garantit l'exclusivité).
        Index(
            "uq_ai_translation_source_message_key",
            "message_template_id",
            "content_key",
            "lang",
            unique=True,
            postgresql_where=text("message_template_id IS NOT NULL"),
        ),
        CheckConstraint(
            "num_nonnulls(template_id, message_template_id) = 1",
            name="ck_ai_translation_source_one_target",
        ),
    )

    agency_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agency.id", ondelete="CASCADE"), index=True, nullable=False
    )
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("journey_template.id", ondelete="CASCADE"), index=True
    )
    message_template_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("message_template.id", ondelete="CASCADE"), index=True
    )
    content_key: Mapped[str] = mapped_column(String(255), nullable=False)
    lang: Mapped[str] = mapped_column(String(5), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    output_hash: Mapped[str] = mapped_column(String(64), nullable=False)
