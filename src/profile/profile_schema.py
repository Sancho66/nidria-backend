import uuid

from pydantic import BaseModel, Field


class ProfileUpdateRequest(BaseModel):
    """Partial PATCH of the actor's own names — both faces share the
    shape. For an EXPAT, first/last_name belong to the GLOBAL identity,
    shared across every agency holding a dossier: deliberate, the client
    manages THEIR OWN identity (same rule as their email pivot)."""

    first_name: str | None = Field(default=None, min_length=1, max_length=100)
    last_name: str | None = Field(default=None, min_length=1, max_length=100)


class ProfileResponse(BaseModel):
    id: uuid.UUID
    first_name: str
    last_name: str
    has_avatar: bool


class AvatarResponse(BaseModel):
    has_avatar: bool
