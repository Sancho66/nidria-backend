from datetime import date, datetime
from typing import Any

from sqlalchemy import CheckConstraint, Date, DateTime, String, text
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
        # Founding offer: at most 3 free seats (pricing 2026-07-07).
        CheckConstraint(
            "founding_free_seats >= 0 AND founding_free_seats <= 3",
            name="agency_founding_free_seats_check",
        ),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    # Trial model (usage trackers bloc 1): NULL = no trial running (or
    # converted). Set by the superadmin wizard at creation (now()+30d);
    # extension is a manual script operation, no endpoint by design.
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Agency branding: private-bucket path of the logo, served by the
    # backend (authenticated, scoped) plus ONE assumed public exception
    # (/public/agencies/{slug}/logo for the client-space login page).
    logo_path: Mapped[str | None] = mapped_column(String(500))
    # Client-space cover banner (same family as the logo): private-bucket
    # path, served authenticated-only — no public route for now.
    cover_path: Mapped[str | None] = mapped_column(String(500))
    default_language: Mapped[str] = mapped_column(
        String(2), nullable=False, server_default=text("'fr'")
    )
    # Onboarding checklist (activation): timestamp of the agency-side
    # dismiss. NULL = checklist shown; set once, no un-dismiss. The
    # checklist STATE itself is never stored - computed live from the
    # usage milestones/events, which are the truth.
    onboarding_dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Subscription (structure F, pricing Eric 2026-07-07). Billing is
    # MANUAL at first - these columns store the deal for the future
    # automation and drive the seat capacity. plan NULL = not converted
    # yet (trial state; trial_ends_at above stays THE pre-conversion
    # marker, unchanged). Posed by the superadmin (Eric's post-closing
    # gesture), never by the agency.
    plan: Mapped[str | None] = mapped_column(String(20))  # SubscriptionPlan
    billing_cycle: Mapped[str | None] = mapped_column(String(10))  # BillingCycle
    seats_included: Mapped[int] = mapped_column(default=3, server_default=text("3"))
    # Founding offer (first 20 agencies): up to 3 free seats on top of
    # the included ones.
    founding_free_seats: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    base_price_eur: Mapped[int] = mapped_column(default=99, server_default=text("99"))
    # 35 (cabinet) | 25 (agence), billed from the 4th seat; set with the
    # plan at conversion.
    seat_price_eur: Mapped[int | None] = mapped_column()
    # Annual/founding promise: price locked until this date (or as long
    # as the subscription stays continuous - Eric's call, not enforced).
    price_locked_until: Mapped[date | None] = mapped_column(Date)
    is_founding: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    converted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def has_logo(self) -> bool:
        """Derived flag for the responses (model_validate picks it up)."""
        return self.logo_path is not None

    @property
    def has_cover(self) -> bool:
        return self.cover_path is not None
