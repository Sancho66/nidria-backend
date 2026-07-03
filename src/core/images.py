"""Shared image-upload pipeline (bloc 1 avatars + agency logos).

Common trunk: strict content-type allowlist, 2 MiB cap on the RAW
upload, Pillow decode (corrupt = 422), EXIF-orientation fix. The error
codes carry the CALLER's prefix ("profile.avatar" / "agency.logo") so
the frontend translates each context on its own.

Three normalizations on top:
- avatars: center-crop square, 512px, always JPEG (alpha flattened);
- logos: NO forced square (logos are often rectangular) — bounded to
  1024px wide, ratio kept, PNG preserved when the image carries alpha
  (transparent backgrounds), JPEG otherwise;
- covers (client-space banner): center-crop to the 4:1 banner ratio,
  bounded to 2560px wide, always JPEG (a banner is a photo, no alpha) —
  with its own 5 MiB raw cap (photos are heavier than logos).
"""

from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError

from src.core.exceptions import PayloadTooLargeError, ValidationError

ALLOWED_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
MAX_IMAGE_BYTES = 2 * 1024 * 1024  # 2 MiB raw upload cap (avatars, logos)
AVATAR_SIZE = 512
LOGO_MAX_WIDTH = 1024
COVER_MAX_BYTES = 5 * 1024 * 1024  # covers are photos: their own 5 MiB cap
COVER_MAX_WIDTH = 2560
COVER_RATIO = 4  # banner width:height


def _decode(
    content_type: str | None,
    raw: bytes,
    error_prefix: str,
    max_bytes: int = MAX_IMAGE_BYTES,
) -> Image.Image:
    if content_type not in ALLOWED_IMAGE_TYPES:
        raise ValidationError(
            "The file must be a JPEG, PNG or WebP image.",
            code=f"{error_prefix}_bad_type",
            params={"allowed": sorted(ALLOWED_IMAGE_TYPES)},
        )
    if len(raw) > max_bytes:
        raise PayloadTooLargeError(
            f"The image exceeds the {max_bytes // (1024 * 1024)} MiB limit.",
            code=f"{error_prefix}_too_large",
            params={"max_bytes": max_bytes},
        )
    try:
        image: Image.Image = Image.open(BytesIO(raw))
        return ImageOps.exif_transpose(image) or image
    except UnidentifiedImageError as exc:
        raise ValidationError(
            "The file is not a readable image.", code=f"{error_prefix}_invalid"
        ) from exc


def _flatten_to_rgb(image: Image.Image) -> Image.Image:
    """Alpha composited on white — for the JPEG encodes."""
    if image.mode == "RGB":
        return image
    background = Image.new("RGB", image.size, (255, 255, 255))
    background.paste(image, mask=image.getchannel("A") if "A" in image.getbands() else None)
    return background


def _has_alpha(image: Image.Image) -> bool:
    if image.mode in ("RGBA", "LA"):
        return True
    return image.mode == "P" and "transparency" in image.info


def process_avatar(content_type: str | None, raw: bytes) -> bytes:
    """512px JPEG square (never store 4K portraits)."""
    image = _decode(content_type, raw, "profile.avatar")
    image = ImageOps.fit(image, (AVATAR_SIZE, AVATAR_SIZE))
    out = BytesIO()
    _flatten_to_rgb(image).save(out, format="JPEG", quality=85)
    return out.getvalue()


def process_cover(content_type: str | None, raw: bytes) -> bytes:
    """Client-space banner: center-crop to 4:1, width bounded to 2560px
    (never upscaled), always JPEG."""
    image = _decode(content_type, raw, "agency.cover", max_bytes=COVER_MAX_BYTES)
    # Largest 4:1 target the source covers WITHOUT upscaling, capped at
    # 2560 wide — fit() then only downscales/crops, never inflates.
    width = max(COVER_RATIO, min(COVER_MAX_WIDTH, image.width, image.height * COVER_RATIO))
    height = max(1, round(width / COVER_RATIO))
    image = ImageOps.fit(image, (width, height))
    out = BytesIO()
    _flatten_to_rgb(image).save(out, format="JPEG", quality=85)
    return out.getvalue()


def process_logo(content_type: str | None, raw: bytes) -> tuple[bytes, str]:
    """Bounded logo, ratio kept: → (bytes, media_type). PNG survives when
    the source carries transparency; everything else lands as JPEG."""
    image = _decode(content_type, raw, "agency.logo")
    if image.width > LOGO_MAX_WIDTH:
        height = max(1, round(image.height * LOGO_MAX_WIDTH / image.width))
        image = image.resize((LOGO_MAX_WIDTH, height))
    out = BytesIO()
    if _has_alpha(image):
        image.convert("RGBA").save(out, format="PNG", optimize=True)
        return out.getvalue(), "image/png"
    _flatten_to_rgb(image).save(out, format="JPEG", quality=85)
    return out.getvalue(), "image/jpeg"
