import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.core.email import NormalizedEmailStr
from src.core.enums import BillingCycle, SubscriptionPlan

# The agency's default content language — the fallback for its i18n blobs.
# Single source of truth: src.core.i18n (SUPPORTED_LANGUAGES / Language).
from src.core.i18n import Language


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
    """Seat capacity, DERIVED live (structure F): `billed` starts at the
    4th seat (past included + founding offered); `max` = 5 (cabinet),
    10 (agence), 3 on trial (the included seats of the future base)."""

    members: int  # active internal agents (externals never consume a seat)
    included: int
    offered: int  # founding free seats
    billed: int
    max: int


class AgencySubscriptionInfo(BaseModel):
    """Read-only settings block: the agency SEES where it stands, never
    edits it - the conversion is Eric's post-closing gesture."""

    plan: str | None
    billing_cycle: str | None
    is_founding: bool
    seats: SeatUsage


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
