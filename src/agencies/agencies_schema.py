import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field

# The agency's default content language — the fallback for its i18n blobs.
# Single source of truth: src.core.i18n (SUPPORTED_LANGUAGES / Language).
from src.core.i18n import Language


class AgencyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    settings: dict[str, Any]
    default_language: Language


class AgencyCreateRequest(BaseModel):
    """Superadmin-only (gated agency.create). Creates the agency + its first
    admin atomically; the admin is onboarded via a set-password email."""

    name: str = Field(min_length=1, max_length=200)
    # Optional: slugified from `name` when omitted. Immutable afterwards
    # (same rule as AgencyUpdateRequest — public identifier).
    slug: str | None = Field(default=None, min_length=1, max_length=100)
    default_language: Language = "fr"
    admin_email: EmailStr
    admin_first_name: str = Field(min_length=1, max_length=100)
    admin_last_name: str = Field(min_length=1, max_length=100)


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
    email: EmailStr
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
