import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.core.currencies import is_supported
from src.core.email import NormalizedEmailStr
from src.core.enums import BillingCycle, ExternalContactType, SubscriptionPlan

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


class SeatUsage(BaseModel):
    """Seat capacity, DERIVED live (grid nidria.com/#tarifs): `billed`
    starts past included (3 cabinet / 6 agence) + founding offered;
    `max` = 5 (cabinet), 10 (agence), 3 on trial — and None for
    sur_mesure: NO cap, the front displays "illimité", never a blank."""

    members: int  # active internal agents (externals never consume a seat)
    included: int
    offered: int  # founding free seats
    billed: int
    max: int | None


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


class AgencyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    settings: dict[str, Any]
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


class AgencyCreateRequest(BaseModel):
    """Superadmin-only (gated agency.create). Creates the agency + its first
    admin atomically; the admin is onboarded via a set-password email."""

    name: str = Field(min_length=1, max_length=200)
    # Optional: slugified from `name` when omitted. Immutable afterwards
    # (same rule as AgencyUpdateRequest — public identifier).
    slug: str | None = Field(default=None, min_length=1, max_length=100)
    default_language: Language = "fr"
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


class AgencyUpdateRequest(BaseModel):
    """`slug` is deliberately absent: immutable at MVP (public
    identifier — changing it would break links and logs)."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    settings: dict[str, Any] | None = None
    # i18n fallback language for this agency's content (validated fr/en/es).
    default_language: Language | None = None
    # ISO 4217 code for internal cost tracking. Strict: an EXACT uppercase code
    # of a real currency (the iso4217 library is the source of truth) — "EURO",
    # "eur", "XYZ" → 422. Changing it once costs exist is refused in the manager.
    currency: str | None = None

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


class RoleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    is_system: bool
    cloned_from_role_id: uuid.UUID | None


class AgentInvitationCreateRequest(BaseModel):
    email: NormalizedEmailStr
    role_id: uuid.UUID


class AgentInvitationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    role_id: uuid.UUID
    status: str
    expires_at: datetime
    invited_by_agent_id: uuid.UUID | None
    created_at: datetime


class AcceptInvitationRequest(BaseModel):
    token: str
    password: str = Field(min_length=8)
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
