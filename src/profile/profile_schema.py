import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


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


class AgentNotificationPrefsPatch(BaseModel):
    """Chaque agent regle SES mails (audit §5). Strict : hors enum ou cle
    inconnue = 422. Le critique n'apparait pas."""

    model_config = ConfigDict(extra="forbid")

    comments: Literal["on", "grouped", "off"] | None = None
    ready_to_validate: Literal["on", "off"] | None = None


class AgentNotificationPrefsResponse(BaseModel):
    """Les prefs EFFECTIVES (defauts fusionnes) — le front affiche l'etat
    reel, jamais un trou."""

    comments: str
    ready_to_validate: str


class AvatarResponse(BaseModel):
    has_avatar: bool
