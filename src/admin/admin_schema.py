import uuid
from datetime import datetime

from pydantic import BaseModel

from src.agencies.agencies_schema import OnboardingStepState


class AdminAgencyRow(BaseModel):
    """One agency row of the superadmin "Gérer les agences" table.

    `status` is DERIVED from the model (no status/suspended column):
    lifetime (accès à vie offert, tested FIRST — an access state beats any
    calendar derivation; without it the gift would land in `unknown`),
    active (converted_at set — it beats an unexpired trial),
    trial (unconverted + future trial_ends_at, with trial_days_remaining),
    expired (unconverted + past trial_ends_at), unknown (neither set — an
    out-of-wizard/legacy anomaly the table exists to surface, never folded
    into expired). `seats_used` = INTERNAL members (seat consumers);
    `members_count` = ALL agents — the front derives externals by
    subtraction. `cases_count` = live non-demo cases."""

    id: uuid.UUID
    name: str
    slug: str
    logo_url: str | None
    plan: str | None
    seats_used: int
    # None = no ceiling (active subscription — décision 05/08) ; 3 sinon.
    seats_limit: int | None
    is_founding: bool
    # Badge "Interne" (agence maison, hors facturation) — jamais un client.
    is_internal: bool
    # Accès à vie OFFERT à un client (lot accès à vie) : distinct d'interne
    # — l'agence reste un client, elle n'a simplement plus d'échéance. Servi
    # à part du `status` pour que la modale d'appui sache quel geste
    # proposer (offrir / reprendre) sans ré-interpréter le libellé.
    lifetime_access: bool
    # Paddle lot: who writes the subscription (manual | paddle) and the
    # payment health (active | past_due | canceled, NULL pre-checkout) —
    # past_due invisible alerts nobody, hence the ?billing_status= filter.
    billing_mode: str
    billing_status: str | None
    status: str  # active | trial | expired | unknown
    trial_days_remaining: int | None
    # Mini-complément (30/07) : la date brute (le front affiche la
    # deadline exacte, plus seulement le décompte) + le solde de crédits
    # signature — lecture seule, même requête, zéro N+1.
    trial_ends_at: datetime | None
    signature_credits_available: int
    cases_count: int
    members_count: int
    created_at: datetime
    # Adoption signals (Phase 2, Eric): the 3 activation gestures (SAME
    # derivation as GET /agencies/me/onboarding), the S0/S1/S2 funnel state,
    # and the login heartbeat (MAX of the agency's agents, NULL if none yet).
    onboarding: list[OnboardingStepState]
    usage_state: str  # S0 | S1 | S2
    last_login_at: datetime | None
    # Referral lot: the referrer agency's name (Eric sees who referred
    # whom); None when the agency came in without a code.
    referred_by: str | None = None
    # --- Rappeler l'agence (lot acquisition 13/08) -------------------------
    # LE besoin d'origine de l'alerte de signup : « rappeler l'agence
    # rapidement ». Le propriétaire = le PREMIER agent interne de l'agence,
    # exactement la définition qu'utilise le mail d'onboarding. Servi ici
    # plutôt que sur une route de détail : GET /admin/agencies/{id} n'existe
    # pas, et la fiche du front se construit de la ligne.
    # PAS d'owner_phone : `agent` n'a AUCUNE colonne téléphone, le servir
    # serait publier un champ éternellement NULL. Le manque est comblé par
    # `contact_phone` ci-dessous, qui rejoint l'identité légale de l'agence.
    owner_name: str | None = None
    owner_email: str | None = None
    contact_phone: str | None = None
    # --- D'où vient l'inscription ------------------------------------------
    # NULL partout sur le parc d'avant ce lot : la source est PÉRISSABLE, on
    # ne peut pas la reconstituer après coup. Le front distingue « pas de
    # source » de « pas collectée » grâce à acquisition_captured_at.
    utm_source: str | None = None
    utm_medium: str | None = None
    utm_campaign: str | None = None
    referrer: str | None = None
    acquisition_captured_at: datetime | None = None


class AdminAgenciesResponse(BaseModel):
    items: list[AdminAgencyRow]
    total: int
    page: int
    page_size: int
