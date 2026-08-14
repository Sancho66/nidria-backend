import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    text,
)
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
        # Reader pool can never go negative (the release gesture re-checks
        # against ACTIVE readers in the manager; this is the last guard).
        CheckConstraint(
            "reader_seats_purchased >= 0",
            name="agency_reader_seats_purchased_check",
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
    # READER seat pool (lot lecteur 08/08): the number of reader seats
    # the agency PURCHASED — this pool IS the Paddle quantity of the
    # reader SKU (13.99 EUR/month · 131.88 EUR/year, plan-transverse),
    # never the live reader count. Bought/released in ONE gesture (one
    # proration, one invoice line: POST /billing/seats/add|remove), then
    # the agency invites onto the free seats (active readers <= pool,
    # enforced at invite/accept/reactivate/type-change). 0 and untouched
    # without an active subscription (trial readers live inside the
    # 3-seat TOTAL); the conversion adopts max(pool, active readers) —
    # a trial reader is billed from day one, never offered by accident.
    reader_seats_purchased: Mapped[int] = mapped_column(
        default=0, server_default=text("0"), nullable=False
    )
    # Annual/founding promise: price locked until this date (or as long
    # as the subscription stays continuous - Eric's call, not enforced).
    price_locked_until: Mapped[date | None] = mapped_column(Date)
    is_founding: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    # Internal agency (Nidria Demo, future in-house workspaces): lifetime,
    # outside billing entirely (409 billing.internal_agency, never blocked,
    # never nurtured) and badged "Interne" in Eric's admin table.
    is_internal: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    # ACCÈS À VIE (geste superadmin) : un CLIENT à qui la plateforme offre
    # l'app sans échéance. À ne pas confondre avec `is_internal` juste
    # au-dessus — celle-là est une agence MAISON (hors facturation, badgée
    # « Interne », jamais un client) ; celle-ci reste un client à part
    # entière, avec son plan, ses crédits et ses statistiques : elle n'a
    # simplement plus de calendrier d'essai.
    #
    # Le drapeau et `trial_ends_at = NULL` sont posés ENSEMBLE par le même
    # geste. Le NULL fait déjà tout le travail (aucune relance, aucune
    # bannière, aucun blocage — tous les lecteurs traitent déjà l'absence
    # d'échéance) ; le drapeau, lui, dit POURQUOI il n'y a plus de date —
    # sans lui, un cadeau serait indistinguable de l'anomalie « unknown »
    # que la table superadmin existe justement pour signaler.
    lifetime_access: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
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
    # LEGAL IDENTITY (lot conditions au nom de l'agence, 13/08). All
    # OPTIONAL (migration without default): the generated terms template
    # fills what exists and leaves a VISIBLE marker («[Votre numéro
    # d'immatriculation]») for what is missing — never a silent gap, never
    # an invention. `name` stays the commercial/brand name; `legal_name` is
    # the registered denomination. Fed by the front « Profil & marque ».
    legal_name: Mapped[str | None] = mapped_column(String(200))
    legal_form: Mapped[str | None] = mapped_column(String(100))
    registration_number: Mapped[str | None] = mapped_column(String(100))
    address: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str | None] = mapped_column(String(100))
    postal_code: Mapped[str | None] = mapped_column(String(20))
    country: Mapped[str | None] = mapped_column(String(2))  # ISO 3166-1 alpha-2
    contact_email: Mapped[str | None] = mapped_column(String(255))
    # Le téléphone rejoint l'identité légale (décision Alexandre 13/08) : à
    # côté de contact_email, même cycle de vie — écrit sur PATCH /agencies/me,
    # jeton {contact_phone}, segment « joignable au … » du modèle généré.
    contact_phone: Mapped[str | None] = mapped_column(String(50))
    # The agency VALIDATED (relu) its generated terms — the « J'ai vérifié »
    # gesture. NULL = generated but not yet reviewed → the dashboard task and
    # the onboarding step nudge the agency. Distinct from « generated »: the
    # template is published immediately (the Nidria fallback dies), this
    # flags the human review. Blocks NOTHING.
    client_terms_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # ONBOARDING MAIL (spec Eric 13/08): stamped AFTER a successful send, so
    # a send that raises is retried by the next sweep — never posed upfront.
    # NULL = not sent (yet, or never: the sweep only looks at agencies inside
    # its catch-up window, so the pre-feature park stays untouched).
    onboarding_email_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Internal signup alert (demande Eric 13/08): posed AFTER a successful
    # send. Deliberately NOT the same column as onboarding_email_sent_at —
    # that mail goes TO the agency at J+10 min, this one goes to the team
    # immediately. One flag each, so replaying one never suppresses the other.
    signup_alert_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- Acquisition (lot 13/08) -------------------------------------------
    # D'OÙ vient l'inscription. Colonnes plates et non un JSONB : ce n'est pas
    # un champ « spécifique à l'agence » (règle 6) mais un jeu FIXE et connu,
    # servi tel quel dans AdminAgencyRow et destiné à être filtré/compté par
    # campagne — même statut que referral_code, l'autre signal d'acquisition.
    # Bornés à 200 comme à l'entrée : c'est du texte public non authentifié.
    utm_source: Mapped[str | None] = mapped_column(String(200))
    utm_medium: Mapped[str | None] = mapped_column(String(200))
    utm_campaign: Mapped[str | None] = mapped_column(String(200))
    referrer: Mapped[str | None] = mapped_column(String(200))
    # La PREMIÈRE TOUCHE, distincte de created_at : le délai entre l'arrivée
    # sur /signup et la création du compte est un signal en soi.
    acquisition_captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def has_logo(self) -> bool:
        """Derived flag for the responses (model_validate picks it up)."""
        return self.logo_path is not None

    @property
    def has_cover(self) -> bool:
        return self.cover_path is not None


class AgencyProfileSection(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """LA SECTION DE FICHE, devenue une donnée d'agence (lot du 07/08).

    Les 4 sections (identity / contact / situation / misc) vivaient dans
    `PROFILE_SECTIONS`, en dur. Elles vivent désormais en base, par agence
    ET PAR SURFACE (`person` | `company`) : les deux faces servent la même
    taxonomie par défaut, mais rien n'oblige une agence à les faire
    évoluer ensemble.

    `key` NE CHANGE JAMAIS après création : c'est elle que portent
    `custom_field_definition.profile_section` et
    `company_field_definition.profile_section`. Renommer une section
    touche son LIBELLÉ, jamais sa clé — sans quoi tous ses champs
    tomberaient en « Divers » d'un coup.

    `label_i18n` VIDE = « je n'ai pas renommé » : le libellé se résout
    alors depuis le catalogue produit (`PROFILE_SECTIONS`), qui porte les
    7 langues et suit les corrections de traduction. Graver les libellés à
    la migration aurait figé 8 agences × 4 sections × 7 langues sur l'état
    du jour — elles ne suivraient plus jamais une correction.
    """

    __tablename__ = "agency_profile_section"
    __table_args__ = (
        UniqueConstraint("agency_id", "surface", "key", name="uq_agency_profile_section"),
    )

    agency_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agency.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # 'person' | 'company' — la face dont cette section est une section.
    surface: Mapped[str] = mapped_column(String(10), nullable=False)
    key: Mapped[str] = mapped_column(String(50), nullable=False)
    label_i18n: Mapped[dict[str, str]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    position: Mapped[int] = mapped_column(default=0, nullable=False, server_default=text("0"))
