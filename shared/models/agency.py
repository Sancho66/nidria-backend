import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, String, text
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
            "default_language IN ('fr', 'en', 'es', 'ru', 'pt', 'it', 'hu')",
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
    # Business sector(s) — multi-sector groundwork. INERT: only the agency
    # CRUD reads/writes it; nothing branches on it yet. Never null ([] =
    # neutral, the behaviour of every agency today).
    sectors: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    # True ONLY for a fresh SELF-SIGNUP agency that must still pick its
    # sector(s) (a blocking onboarding screen). Superadmin-created agencies
    # (sector mandatory at creation) and ALL existing agencies (migration
    # default false) are NEVER flagged — the guarantee. Cleared by the
    # first PATCH that poses >= 1 sector.
    sectors_onboarding_required: Mapped[bool] = mapped_column(
        default=False, server_default=text("false"), nullable=False
    )
    # ISO 4217 currency (3 letters) for the agency's internal cost tracking.
    # Posed at creation (NID-16a) from the UI language where unambiguous, else
    # EUR — always editable in Settings. NULL only on LEGACY agencies created
    # before NID-16a (the add-currency migration ran no backfill): those still
    # pick it before entering costs. Column stays nullable for them.
    # It drives the DISPLAYED decimals; amounts are stored DECIMAL(18,4).
    currency: Mapped[str | None] = mapped_column(String(3))
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
    # Founding offer (first 20 agencies): up to 3 free seats on top of
    # the included ones.
    founding_free_seats: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    # DEPRECATED (2026-07-12) — informationnel, la vérité tarifaire vit chez
    # Paddle (PRICE_IDS) ; ne jamais servir sans re-valider. Colonnes dormantes
    # (lues nulle part) : le défaut 99 est FAUX pour Agence (129) depuis la
    # grille 2026-07, et n'est volontairement pas corrigé — corriger une
    # grille en dur re-créerait ce qu'on vient de sortir.
    base_price_eur: Mapped[int] = mapped_column(default=99, server_default=text("99"))
    # DEPRECATED (2026-07-12) — même statut : posé à la conversion (35/25),
    # informationnel seulement ; la vérité vit chez Paddle (PRICE_IDS).
    seat_price_eur: Mapped[int | None] = mapped_column()
    # Annual/founding promise: price locked until this date (or as long
    # as the subscription stays continuous - Eric's call, not enforced).
    price_locked_until: Mapped[date | None] = mapped_column(Date)
    is_founding: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    # Internal agency (Nidria Demo, future in-house workspaces): lifetime,
    # outside billing entirely (409 billing.internal_agency, never blocked,
    # never nurtured) and badged "Interne" in Eric's admin table.
    is_internal: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    converted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Paddle (Merchant of Record, self-serve). billing_mode drives WHO writes
    # the subscription state: "manual" (default — the superadmin's PATCH, the
    # only writer, Nicolas & large accounts forever) or "paddle" (the signed
    # webhooks write; the manual PATCH refuses plan/cycle/converted_at).
    # NEVER hand-editable towards "paddle": only the subscription.activated
    # webhook poses it (the event is the proof of the self-serve checkout).
    billing_mode: Mapped[str] = mapped_column(
        String(10), default="manual", server_default=text("'manual'"), nullable=False
    )
    # active | past_due | canceled — informational (admin table + filter),
    # AND the input of the billing lock (billing_lock.blocking_reason).
    billing_status: Mapped[str | None] = mapped_column(String(20))
    # First instant the subscription entered past_due (webhook clock) — the
    # grace anchor of the billing lock (7 days by default). Posed at the
    # FIRST past_due status write, kept across re-deliveries, cleared by
    # any other status (active, canceled).
    past_due_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paddle_customer_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    paddle_subscription_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    # Referral program (parrainage). `referral_code` = the agency's OWN code
    # to share (dedicated, not the guessable public slug); generated at
    # creation, backfilled for existing rows. `referred_by_agency_id` = who
    # referred THIS agency — typed at signup/wizard, IMMUTABLE afterwards
    # (a referral is never re-attributed). The credits ledger lives in
    # referral_credit; these two columns are the attribution only.
    referral_code: Mapped[str | None] = mapped_column(String(16), unique=True, index=True)
    referred_by_agency_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agency.id", ondelete="SET NULL")
    )

    @property
    def has_logo(self) -> bool:
        """Derived flag for the responses (model_validate picks it up)."""
        return self.logo_path is not None

    @property
    def has_cover(self) -> bool:
        return self.cover_path is not None
