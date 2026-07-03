"""User profile (bloc 1): own names + profile picture, both faces.

Avatar pipeline: strict content-type allowlist → 2 MiB cap on the RAW
upload → Pillow decode (corrupt file = 422) → EXIF-orientation fix →
center-crop square → 512px → JPEG (RGB, alpha flattened on white). One
storage path per actor (re-upload overwrites), private bucket, ALWAYS
served by the backend (never a direct Supabase URL) — same rule as the
case documents."""

import uuid
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.expat_user import ExpatUser
from src.core import storage
from src.core.exceptions import NotFoundError, PayloadTooLargeError, ValidationError
from src.profile.profile_repository import ProfileRepository
from src.profile.profile_schema import ProfileResponse, ProfileUpdateRequest

_ALLOWED_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
_MAX_BYTES = 2 * 1024 * 1024  # 2 MiB raw upload cap
_SIZE = 512  # stored square edge — plenty for any UI slot


def _profile_response(actor: Agent | ExpatUser) -> ProfileResponse:
    return ProfileResponse(
        id=actor.id,
        first_name=actor.first_name,
        last_name=actor.last_name,
        has_avatar=actor.avatar_path is not None,
    )


def _process_avatar(raw: bytes) -> bytes:
    """Decode + normalize to a 512px JPEG square (never store 4K)."""
    image: Image.Image
    try:
        image = Image.open(BytesIO(raw))
        image = ImageOps.exif_transpose(image) or image
        image = ImageOps.fit(image, (_SIZE, _SIZE))
    except UnidentifiedImageError as exc:
        raise ValidationError(
            "The file is not a readable image.", code="profile.avatar_invalid"
        ) from exc
    if image.mode != "RGB":  # flatten alpha on white for the JPEG encode
        background = Image.new("RGB", image.size, (255, 255, 255))
        background.paste(image, mask=image.getchannel("A") if "A" in image.getbands() else None)
        image = background
    out = BytesIO()
    image.save(out, format="JPEG", quality=85)
    return out.getvalue()


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
        if content_type not in _ALLOWED_TYPES:
            raise ValidationError(
                "Avatar must be a JPEG, PNG or WebP image.",
                code="profile.avatar_bad_type",
                params={"allowed": sorted(_ALLOWED_TYPES)},
            )
        if len(raw) > _MAX_BYTES:
            raise PayloadTooLargeError(
                "Avatar exceeds the 2 MiB limit.",
                code="profile.avatar_too_large",
                params={"max_bytes": _MAX_BYTES},
            )
        processed = _process_avatar(raw)
        face = "agent" if isinstance(actor, Agent) else "expat"
        path = f"avatars/{face}/{actor.id}.jpg"  # stable path: re-upload overwrites
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
