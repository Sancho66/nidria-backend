"""User profile (bloc 1): own names + profile picture, both faces.

The image pipeline lives in core.images (shared with the agency logo):
allowlist, 2 MiB cap, Pillow decode, EXIF fix, then the avatar-specific
square 512px JPEG. One storage path per actor (re-upload overwrites),
private bucket, ALWAYS served by the backend (never a direct Supabase
URL) — same rule as the case documents."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.expat_user import ExpatUser
from src.core import storage
from src.core.exceptions import NotFoundError
from src.core.images import process_avatar
from src.profile.profile_repository import ProfileRepository
from src.profile.profile_schema import ProfileResponse, ProfileUpdateRequest


def _profile_response(actor: Agent | ExpatUser) -> ProfileResponse:
    return ProfileResponse(
        id=actor.id,
        first_name=actor.first_name,
        last_name=actor.last_name,
        has_avatar=actor.avatar_path is not None,
    )


class ProfileManager:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = ProfileRepository(db)

    # --- names -----------------------------------------------------------------------

    async def update_names(
        self, actor: Agent | ExpatUser, payload: ProfileUpdateRequest
    ) -> ProfileResponse:
        if payload.first_name is not None:
            actor.first_name = payload.first_name
        if payload.last_name is not None:
            actor.last_name = payload.last_name
        await self.db.commit()
        return _profile_response(actor)

    # --- avatar ----------------------------------------------------------------------

    async def upload_avatar(
        self, actor: Agent | ExpatUser, content_type: str | None, raw: bytes
    ) -> ProfileResponse:
        processed = process_avatar(content_type, raw)
        face = "agent" if isinstance(actor, Agent) else "expat"
        path = f"avatars/{face}/{actor.id}.jpg"
        # Stable path — but Supabase refuses a same-path overwrite (409
        # Duplicate), so the previous blob is deleted first (same fix as
        # the agency logo/cover).
        if actor.avatar_path is not None:
            storage.delete(actor.avatar_path)
        storage.upload(path, processed, "image/jpeg")
        actor.avatar_path = path
        await self.db.commit()
        return _profile_response(actor)

    async def delete_avatar(self, actor: Agent | ExpatUser) -> ProfileResponse:
        """Back to the initials fallback. Idempotent."""
        if actor.avatar_path is not None:
            storage.delete(actor.avatar_path)
            actor.avatar_path = None
            await self.db.commit()
        return _profile_response(actor)

    # --- avatar reads (backend-served, scoped like the actor's name) ------------------

    async def agent_avatar(self, viewer: Agent, agent_id: uuid.UUID) -> bytes:
        """An agent's avatar is visible INSIDE their agency (own included)."""
        target = await self.repo.get_agent_in_agency(viewer.agency_id, agent_id)
        if target is None or target.avatar_path is None:
            raise NotFoundError("Avatar not found.")
        return storage.download(target.avatar_path)

    async def client_avatar(self, viewer: Agent, expat_user_id: uuid.UUID) -> bytes:
        """A client's avatar follows their NAME's visibility: agencies
        holding at least one live case. No new cross-agency exposure."""
        expat = await self.repo.get_expat(expat_user_id)
        if (
            expat is None
            or expat.avatar_path is None
            or not await self.repo.expat_is_client_of_agency(expat_user_id, viewer.agency_id)
        ):
            raise NotFoundError("Avatar not found.")
        return storage.download(expat.avatar_path)

    async def own_expat_avatar(self, expat: ExpatUser) -> bytes:
        if expat.avatar_path is None:
            raise NotFoundError("Avatar not found.")
        return storage.download(expat.avatar_path)
