import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.consents.agency_template import RESPONSIBILITY_DISCLAIMER
from src.core.currencies import is_supported
from src.core.email import NormalizedEmailStr
from src.core.enums import (
    AgencySector,
    BillingCycle,
    ExternalContactType,
    SeatType,
    SubscriptionPlan,
)

# The agency's default content language — the fallback for its i18n blobs.
# Single source of truth: src.core.i18n (SUPPORTED_LANGUAGES / Language).
from src.core.i18n import Language


class DirectoryContactCreateRequest(BaseModel):
    """Create an AGENCY DIRECTORY external contact (case_id NULL): a
    provider named ONCE, reusable across the agency's cases and journey
    templates. NO login, NO invitation, NO seat — a named role only.
    `name` is mandatory (the sole human identifier; email is nullable)."""

    name: str = Field(min_length=1, max_length=200)
    email: NormalizedEmailStr | None = None
    phone: str | None = Field(default=None, max_length=50)
    type: ExternalContactType = ExternalContactType.OTHER


class DirectoryContactResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    email: str | None
    phone: str | None
    type: str


class DirectoryContactListItem(BaseModel):
    """One directory row for the agency table. `access_state` (stable key, the
    front never derives from text): 'none' (named, never invited) | 'invited'
    (agent_id posed, invitation PENDING, mail sent) | 'active' (invitation
    accepted, can log in). `invited_at` = when the pending invitation was
    created (NULL unless 'invited'). `agent_role` names the designated
    account's role; `used_in_steps` = template step participations (what a
    delete would SET NULL — the agency sees what it breaks)."""

    id: uuid.UUID
    name: str
    email: str | None
    phone: str | None
    type: str
    agent_id: uuid.UUID | None
    agent_role: str | None
    access_state: Literal["none", "invited", "active"]
    invited_at: datetime | None
    used_in_steps: int


class ExternalInvitationCreateRequest(BaseModel):
    """Invite a NEW provider: a directory external_contact (name, mandatory —
    the stable label until the account exists) + an invitation (email, role)."""

    name: str = Field(min_length=1, max_length=200)
    email: NormalizedEmailStr
    role_id: uuid.UUID


class ContactInviteRequest(BaseModel):
    """Give an EXISTING directory contact an account. The contact id is
    unchanged; agent_id is set on acceptance."""

    email: NormalizedEmailStr
    role_id: uuid.UUID


class OnboardingStepState(BaseModel):
    """One activation gesture: create_journey | open_case |
    view_as_client."""

    key: str
    done: bool
    done_at: datetime | None


class OnboardingResponse(BaseModel):
    """GET /agencies/me/onboarding - the activation checklist, COMPUTED
    live from the usage milestones/events (no checkbox state table: the
    milestones ARE the truth). Only the dismiss is persisted."""

    steps: list[OnboardingStepState]
    dismissed: bool


class AiUsageResponse(BaseModel):
    """The agency's AI monthly quota state (points). `remaining` is
    served (not front-computed) — the NaN of 2026-07-05 came from the
    front reading a field that did not exist."""

    used: int
    limit: int
    remaining: int
    month: str


class ReaderSeatUsage(BaseModel):
    """Reader-seat pool state (lot lecteur 08/08): `purchased` is the
    pool the agency BOUGHT (the Paddle quantity of the reader SKU),
    `used` the active readers, `free` the seats left to invite onto —
    the front's « 3 sièges lecteur libres »."""

    purchased: int
    used: int
    free: int


class SeatUsage(BaseModel):
    """Seat capacity, DERIVED live (grid nidria.com/#tarifs): `billed`
    starts past included (3 cabinet / 6 agence) + founding offered;
    `max` = None on any ACTIVE subscription — NO ceiling (décision Alex +
    Eric 05/08/2026), extra seats are billed per seat and the front
    displays "illimité", never a blank. Without an active subscription
    (trial, no plan, dead paddle sub): 3 — a TOTAL across seat types.

    Lot lecteur (08/08): `members` stays the TOTAL of active internals
    (the trial gate reads it); `managers`/`reader` ventilate it. The
    included + offered seats are MANAGER seats — `billed` counts past
    them from the managers only; on an active subscription every reader
    comes from the purchased pool (reader SKU price), never from the included
    tier."""

    members: int  # active internal agents, ALL seat types (externals never consume a seat)
    managers: int  # active internal manager seats (the mirror-billed kind)
    included: int
    offered: int  # founding free seats
    billed: int  # manager seats past included + offered
    max: int | None
    reader: ReaderSeatUsage


class ProviderUsage(BaseModel):
    """Provider capacity (grid 2026-07), DERIVED live: `count` = external
    agents (the external flow pre-creates the Agent at invitation, so this
    IS actives + invitees); `included` = free tier (10 cabinet / 15 agence);
    `max` = the cap (15/25, 10 on trial) — None for sur_mesure ("illimité").
    Billing past the included tier is PHASE 2: nothing billed today."""

    count: int
    included: int
    max: int | None


class AgencySubscriptionInfo(BaseModel):
    """Read-only settings block: the agency SEES where it stands, never
    edits it - the conversion is Eric's post-closing gesture."""

    plan: str | None
    billing_cycle: str | None
    is_founding: bool
    seats: SeatUsage
    providers: ProviderUsage
    # Billing lock (read-only mode): the front's banner + greyed states.
    # blocked_reason: "trial_expired" | "past_due" | "canceled" | None —
    # the same value the 403 billing.subscription_required carries.
    is_blocked: bool = False
    blocked_reason: str | None = None
    # Trial deadline — served to EVERY agent (this payload is not
    # manager-gated): the trial countdown must be visible to all, not just
    # to those who can pay (front lot 2026-07-18). None once converted.
    trial_ends_at: datetime | None = None


class ResponsibleStepRef(BaseModel):
    """One step the deactivated agent was responsible for (active steps
    only — DONE steps are history, never reassigned)."""

    case_id: uuid.UUID
    progress_id: uuid.UUID


class MemberDeactivationResponse(BaseModel):
    """POST /agencies/me/members/{agent_id}/deactivate — the INVENTORY of
    what the departed agent leaves behind (nothing silent, nothing rigid:
    the front chains a reassignment screen over the existing PATCHes)."""

    deactivated_at: datetime
    owned_cases: list[uuid.UUID]
    responsible_steps: list[ResponsibleStepRef]


class SubscriptionUpdateRequest(BaseModel):
    """PATCH /agencies/{id}/subscription (superadmin) - the post-closing
    gesture: pose the plan, cycle, founding terms and conversion date.
    Partial: absent fields stay untouched."""

    plan: SubscriptionPlan | None = None
    billing_cycle: BillingCycle | None = None
    is_founding: bool | None = None
    founding_free_seats: int | None = Field(default=None, ge=0, le=3)
    price_locked_until: date | None = None
    converted_at: datetime | None = None


class TrialExtendRequest(BaseModel):
    """PATCH /agencies/{id}/trial (superadmin) — l'essai en JOURS AJOUTÉS,
    la forme la plus sûre constatée : la colonne est un timestamptz, une
    date explicite saisie en UI porte le piège du fuseau, tandis que des
    jours s'ancrent sur max(maintenant, fin actuelle) — le passé est
    STRUCTURELLEMENT impossible."""

    model_config = ConfigDict(extra="forbid")

    extend_days: int = Field(ge=1, le=365)


class TrialResponse(BaseModel):
    trial_ends_at: datetime


class LifetimeAccessRequest(BaseModel):
    """PATCH /agencies/{id}/lifetime-access (superadmin) — offrir l'accès
    à vie, ou le reprendre.

    Reprendre EXIGE une nouvelle durée d'essai : l'ancienne échéance n'est
    pas « restaurée », elle appartenait à un calendrier volontairement
    fermé, et la rendre telle quelle ressusciterait souvent une date déjà
    passée (blocage immédiat, sans que personne l'ait décidé). Des jours,
    pas une date — même raison qu'à la prolongation : un timestamptz saisi
    en UI porte le piège du fuseau."""

    model_config = ConfigDict(extra="forbid")

    lifetime_access: bool
    trial_days: int | None = Field(default=None, ge=1, le=365)


class LifetimeAccessResponse(BaseModel):
    lifetime_access: bool
    # NULL quand l'accès est à vie : c'est l'absence d'échéance qui éteint
    # relances, bannière et blocage — le front la lit telle quelle.
    trial_ends_at: datetime | None


class SignatureCreditGrantRequest(BaseModel):
    """POST /agencies/{id}/signature-credits/grant (superadmin) — crédits
    OFFERTS. Borne constatée : 1..1000 (le plus grand pack vendu est 100 ;
    au-delà du raisonnable, c'est un contrat, pas un geste)."""

    model_config = ConfigDict(extra="forbid")

    credits: int = Field(ge=1, le=1000)
    note: str | None = Field(default=None, max_length=200)


class SignatureCreditGrantResponse(BaseModel):
    granted: int
    available: int
    reserved: int


class AgencyDeleteRequest(BaseModel):
    """DELETE /agencies/{id} (superadmin, HARD delete, Groupe C). The
    front makes the user type the agency name: `confirm_name` must equal
    it EXACTLY (422 otherwise). `force` overrides the active-cases
    guardrail (409 without it when non-demo cases exist)."""

    confirm_name: str = Field(min_length=1)
    force: bool = False


class AgencyDeletedResponse(BaseModel):
    """The outcome of a hard deletion (also the trace's payload)."""

    agency_id: uuid.UUID
    name: str
    deleted_cases_count: int


class AgencyTokenInfo(BaseModel):
    """ONE dynamic token of the client documents, as the editor needs it:
    what to insert, how to say it, and what it renders TODAY for this
    agency. Served (never guessed by the front) — the catalogue lives in
    `consents.agency_tokens`."""

    # Insertable as-is, braces included: a token retyped by hand is a token
    # nobody resolves.
    token: str
    # The catalogue name — doubles as the front's i18n key (7 locales).
    name: str
    # Human FR label, the served fallback for a token this front predates.
    label: str
    # The CURRENT value for this agency; None = « non renseigné » (the
    # client would read a blank there).
    value: str | None = None


class ClientTermsPreviewRequest(BaseModel):
    """The DRAFT being edited (never persisted here) — the agency verifies
    its client screen before publishing, since publishing re-gates every
    client of the agency."""

    content: str = Field(max_length=100_000)


class ClientTermsPreviewResponse(BaseModel):
    """What the CLIENT will read, rendered by the same resolution as the
    client face — plus the two edition-only signals."""

    rendered: str
    # Tokens nobody resolves: rendered verbatim to the client. Named here so
    # the agency fixes them BEFORE publishing.
    unknown_tokens: list[str] = []
    # Known tokens whose profile field is empty: the client reads a blank.
    unfilled_tokens: list[str] = []


class AgencyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    settings: dict[str, Any]
    # Business sector(s) — multi-sector groundwork, INERT (nothing consumes
    # it yet). [] = neutral (every existing agency).
    sectors: list[AgencySector] = []
    # True → a fresh self-signup agency must still pick its sector(s) (the
    # front shows a blocking screen). False for superadmin-created and all
    # existing agencies. Cleared by a PATCH posing >= 1 sector.
    sectors_onboarding_required: bool = False
    default_language: Language
    # Branding: derived from logo_path / cover_path (model properties);
    # the images are served by their endpoints, never a raw storage URL.
    has_logo: bool = False
    has_cover: bool = False
    # ISO 4217 currency for internal cost tracking — readable so the front can
    # show the chosen code and detect "not set yet" (NULL) in Settings. It is
    # writable via PATCH /agencies/me; a written field must be re-readable.
    currency: str | None = None
    # The agency's OWN referral code to share ("Parrainez une agence" in
    # Settings) — generated at creation, stable.
    referral_code: str | None = None
    # Filled on GET /agencies/me only (the settings read); other call
    # sites leave it None.
    subscription: AgencySubscriptionInfo | None = None
    # E-signatures (TEMPS 2, 28/07) : le flag GLOBAL, servi à tout agent —
    # le front ancre ici l'affichage de la feature (précédent
    # trial_ends_at : ce payload n'est pas manager-gated). Rempli sur
    # GET/PATCH /agencies/me.
    signatures_enabled: bool = False
    # The agency's OWN client documents, shown to ITS clients (lot 13/08 —
    # the Nidria fallback is dead, every agency has its own). Filled on
    # GET/PATCH /agencies/me from the active consent_document; the Settings
    # editor shows what is in force (a written field must be re-readable).
    client_terms_md: str | None = None
    client_privacy_md: str | None = None
    # The « J'ai vérifié » gesture — NULL = generated but not yet reviewed
    # (the dashboard reminder + onboarding step nudge the agency). Maps from
    # the column; blocks nothing.
    client_terms_reviewed_at: datetime | None = None
    # Legal identity (lot 13/08) — feeds the generated terms; each is a
    # marker in the text until filled. Read/written via GET/PATCH.
    legal_name: str | None = None
    legal_form: str | None = None
    registration_number: str | None = None
    address: str | None = None
    city: str | None = None
    postal_code: str | None = None
    country: str | None = None
    contact_email: str | None = None
    # The responsibility mention, served at EDITION only — NEVER inside the
    # text shown to a client. Constant, so the front always has it.
    client_terms_disclaimer: str = RESPONSIBILITY_DISCLAIMER
    # Profil & marque fields still empty (omission-by-segment lot): the front
    # warning names them and states the consequence. Served as a list (the
    # brackets left the published text, so the front no longer counts them).
    # Filled on GET/PATCH/validate /me only.
    missing_legal_fields: list[str] = []
    # LES JETONS DYNAMIQUES, SERVIS (lot 13/08) — le front ne devine aucune
    # liste : le catalogue, son libellé humain et la valeur ACTUELLE de
    # chacun pour cette agence (value None = « non renseigné »). Filled on
    # GET/PATCH/validate /me.
    client_terms_tokens: list[AgencyTokenInfo] = []
    # Signalled AT EDITION, never to the client: tokens found in the two
    # published documents that nobody resolves (typo, invention). They reach
    # the client verbatim — that is the deliberate rule, so the warning is
    # here.
    client_terms_unknown_tokens: list[str] = []
    # Known tokens used by the texts whose profile field is empty: the client
    # reads a blank there. Subset of missing_legal_fields that the text
    # actually depends on.
    client_terms_unfilled_tokens: list[str] = []
    # EFFECTIVE client notification prefs (defaults merged) — the front
    # displays THIS and drops its CLIENT_DEFAULTS mirror (lot digest).
    notification_prefs: dict[str, str] | None = None
    # LES RELANCES AUTOMATIQUES (lot 13/08) : false = elles partent seules (la
    # promesse du produit, et le défaut) ; true = elles attendent une
    # approbation (le mode d'avant, pour qui le veut). Ne concerne QUE les
    # relances automatiques — un rappel écrit à la main passe toujours par
    # l'approbation. Rempli sur GET/PATCH/validate /me.
    auto_reminders_require_approval: bool = False


class AgencyCreateRequest(BaseModel):
    """Superadmin-only (gated agency.create). Creates the agency + its first
    admin atomically; the admin is onboarded via a set-password email."""

    name: str = Field(min_length=1, max_length=200)
    # Optional: slugified from `name` when omitted. Immutable afterwards
    # (same rule as AgencyUpdateRequest — public identifier).
    slug: str | None = Field(default=None, min_length=1, max_length=100)
    default_language: Language = "fr"
    # Optional; absent → [] (neutral). Validated in the manager (dedup +
    # each value in AgencySector, else 422 agency.sector_invalid).
    sectors: list[str] | None = None
    admin_email: NormalizedEmailStr
    admin_first_name: str = Field(min_length=1, max_length=100)
    admin_last_name: str = Field(min_length=1, max_length=100)
    # Founding offer (first 20 agencies), posed at creation when known;
    # also editable later via PATCH /agencies/{id}/subscription.
    is_founding: bool = False
    founding_free_seats: int = Field(default=0, ge=0, le=3)
    # Referral attribution ("Code de parrainage", optional): the REFERRER's
    # code. Resolved at creation, IMMUTABLE afterwards — a referral is
    # never re-attributed. Unknown code = explicit refusal (the operator
    # is typing it), never a silent drop.
    referral_code: str | None = Field(default=None, min_length=4, max_length=16)


class CreatedAdminResponse(BaseModel):
    id: uuid.UUID
    email: str
    first_name: str
    last_name: str
    role: str


class AgencyCreateResponse(BaseModel):
    agency: AgencyResponse
    admin: CreatedAdminResponse


class ClientNotificationPrefsPatch(BaseModel):
    """Ce que l'agence regle pour SES clients (audit §5). Strict : une
    valeur hors enum = 422, une cle inconnue = 422 (extra=forbid). Le
    CRITIQUE (invitations, codes, acces) n'apparait pas — non reglable
    par construction. progress_digest est INERTE tant que le job digest
    n'existe pas (lot suivant)."""

    model_config = ConfigDict(extra="forbid")

    requirement_request: Literal["on", "off"] | None = None
    comments: Literal["on", "grouped", "off"] | None = None
    reminders: Literal["on", "off"] | None = None
    progress_digest: Literal["weekly", "daily", "off"] | None = None


class AgencyUpdateRequest(BaseModel):
    """`slug` is deliberately absent: immutable at MVP (public
    identifier — changing it would break links and logs)."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    settings: dict[str, Any] | None = None
    # FULL replacement of the sector list (like preferred_channels /
    # watcher_agent_ids). Manager-validated. INERT — nothing consumes it.
    sectors: list[str] | None = None
    # Prefs notifications clients : PATCH partiel type (merge cle a cle
    # dans settings.notification_prefs.client), jamais un remplacement du
    # settings brut.
    notification_prefs: ClientNotificationPrefsPatch | None = None
    # Le régime des relances AUTOMATIQUES (lot 13/08). None = inchangé.
    auto_reminders_require_approval: bool | None = None
    # i18n fallback language for this agency's content (validated fr/en/es).
    default_language: Language | None = None
    # ISO 4217 code for internal cost tracking. Strict: an EXACT uppercase code
    # of a real currency (the iso4217 library is the source of truth) — "EURO",
    # "eur", "XYZ" → 422. Changing it once costs exist is refused in the manager.
    currency: str | None = None
    # "Vos conditions générales" — the agency's OWN client documents, shown
    # to ITS clients (lot 13/08). Multiline markdown. Absent = leave
    # untouched; "" (or blank) = REGENERATE from the template (never fall
    # back to Nidria — that fallback is dead). Writing PUBLISHES a versioned
    # consent_document (hash, version, automatic re-gating).
    client_terms_md: str | None = Field(default=None, max_length=100_000)
    client_privacy_md: str | None = Field(default=None, max_length=100_000)
    # Legal identity (lot 13/08) — feeds the generated terms. Changing one,
    # while the docs are still the untouched template AND not yet validated,
    # regenerates them so the pre-filled model tracks the profile.
    legal_name: str | None = Field(default=None, max_length=200)
    legal_form: str | None = Field(default=None, max_length=100)
    registration_number: str | None = Field(default=None, max_length=100)
    address: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, max_length=100)
    postal_code: str | None = Field(default=None, max_length=20)
    country: str | None = Field(default=None, max_length=2)
    contact_email: NormalizedEmailStr | None = None

    @field_validator("currency")
    @classmethod
    def _valid_currency(cls, value: str | None) -> str | None:
        if value is not None and not is_supported(value):
            raise ValueError("Unknown ISO 4217 currency code.")
        return value


class AgencyMemberResponse(BaseModel):
    id: uuid.UUID
    first_name: str
    last_name: str
    email: str
    role: str
    role_id: uuid.UUID
    # Lets the front distinguish internal staff from external providers.
    # Internal-members listing → always false; external-members → true.
    is_external: bool
    # Offboarding: NULL = active; set = deactivated (badge + reactivate
    # button on the front — deactivated members STAY listed).
    deactivated_at: datetime | None = None
    # Seat KIND (lot lecteur): manager | reader — the front's pickers
    # exclude readers from actor designation, the settings page shows the
    # badge. Externals carry the default value (no seat).
    seat_type: str = SeatType.MANAGER.value


class SeatTypeUpdateRequest(BaseModel):
    """PUT /agencies/me/members/{agent_id}/seat-type — the traced admin
    gesture that flips a member between manager and reader (billing
    follows: mirror vs purchased pool)."""

    seat_type: SeatType


class RoleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    is_system: bool
    cloned_from_role_id: uuid.UUID | None


class AgentInvitationCreateRequest(BaseModel):
    email: NormalizedEmailStr
    role_id: uuid.UUID
    # Seat KIND the invitee will occupy (lot lecteur): default manager —
    # the historical behaviour. reader requires a read-only role AND a
    # free purchased seat on an active subscription.
    seat_type: SeatType = SeatType.MANAGER


class AgentInvitationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    role_id: uuid.UUID
    status: str
    expires_at: datetime
    invited_by_agent_id: uuid.UUID | None
    created_at: datetime
    seat_type: str


class AcceptInvitationRequest(BaseModel):
    token: str
    password: str = Field(min_length=8)
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
