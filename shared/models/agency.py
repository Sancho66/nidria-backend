from typing import Any

from sqlalchemy import CheckConstraint, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Agency(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Multi-tenant root. Everything agency-side is scoped by `agency_id`."""

    __tablename__ = "agency"
    __table_args__ = (
        # `default_language` = the agency's fallback content language for its
        # i18n blobs (resolved in BLOC 2). Samples (agency_id NULL) have no row
        # here → they fall back to "fr" implicitly at resolution time.
        CheckConstraint(
            "default_language IN ('fr', 'en', 'es', 'ru', 'pt', 'it')",
            name="agency_default_language_check",
        ),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    # Agency branding: private-bucket path of the logo, served by the
    # backend (authenticated, scoped) plus ONE assumed public exception
    # (/public/agencies/{slug}/logo for the client-space login page).
    logo_path: Mapped[str | None] = mapped_column(String(500))
    default_language: Mapped[str] = mapped_column(
        String(2), nullable=False, server_default=text("'fr'")
    )

    @property
    def has_logo(self) -> bool:
        """Derived flag for the responses (model_validate picks it up)."""
        return self.logo_path is not None
