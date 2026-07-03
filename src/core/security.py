import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
from jose import JWTError, jwt

from src.core.config import get_settings
from src.core.enums import Audience
from src.core.exceptions import UnauthorizedError

# bcrypt rejects inputs longer than 72 bytes. Truncate so callers don't have
# to know about the limit; passwords beyond 72 bytes are vanishingly rare.
_BCRYPT_MAX_BYTES = 72


def hash_password(plain: str) -> str:
    digest = bcrypt.hashpw(plain.encode("utf-8")[:_BCRYPT_MAX_BYTES], bcrypt.gensalt(12))
    return digest.decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(
            plain.encode("utf-8")[:_BCRYPT_MAX_BYTES],
            hashed.encode("utf-8"),
        )
    except (ValueError, TypeError):
        return False


# --- JWT, two strict audiences ----------------------------------------------
# Access tokens: one secret PER audience (agent / expat) — a token from one
# flow can never validate in the other, even before any claim check.
# Refresh tokens: a single refresh secret, the `audience` claim does the
# separation and every decode validates it against the expected audience.


def _require_token_audience(audience: Audience) -> None:
    if audience is Audience.PUBLIC:
        raise ValueError("PUBLIC is a binding audience, not a token audience.")


def _access_secret(audience: Audience) -> str:
    _require_token_audience(audience)
    settings = get_settings()
    if audience is Audience.AGENT:
        return settings.jwt_agent_secret
    return settings.jwt_expat_secret


def create_access_token(
    subject: str,
    audience: Audience,
    extra_claims: dict[str, Any] | None = None,
    expires_minutes: int | None = None,
) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    minutes = (
        expires_minutes if expires_minutes is not None else settings.access_token_expires_minutes
    )
    payload: dict[str, Any] = {
        "sub": subject,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=minutes)).timestamp()),
        "type": "access",
        "audience": audience.value,
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, _access_secret(audience), algorithm=settings.jwt_algorithm)


def create_refresh_token(subject: str, audience: Audience, jti: uuid.UUID) -> str:
    """`jti` identifies this token in the `refresh_token` table — the
    rotation/revocation handle. Issuers insert the row; /refresh
    validates it is still active before honoring the token."""
    _require_token_audience(audience)
    settings = get_settings()
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": subject,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=settings.refresh_token_expires_days)).timestamp()),
        "type": "refresh",
        "audience": audience.value,
        "jti": str(jti),
    }
    return jwt.encode(payload, settings.jwt_refresh_secret, algorithm=settings.jwt_algorithm)


def _decode(token: str, secret: str) -> dict[str, Any]:
    settings = get_settings()
    try:
        payload: dict[str, Any] = jwt.decode(token, secret, algorithms=[settings.jwt_algorithm])
    except JWTError as e:
        raise UnauthorizedError("Invalid or expired token.") from e
    return payload


def _check_claims(payload: dict[str, Any], expected_type: str, expected: Audience) -> None:
    if payload.get("type") != expected_type:
        raise UnauthorizedError("Wrong token type.")
    if payload.get("audience") != expected.value:
        raise UnauthorizedError("Wrong token audience.")


def create_mfa_token(subject: str, audience: Audience, jti: uuid.UUID) -> str:
    """Ephemeral login step-2 token (2FA): type "mfa_pending" makes it
    unusable on every access-authenticated route (the decoders check the
    type claim), `jti` keys the server-side attempts counter."""
    settings = get_settings()
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": subject,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.mfa_token_expires_minutes)).timestamp()),
        "type": "mfa_pending",
        "audience": audience.value,
        "jti": str(jti),
    }
    return jwt.encode(payload, _access_secret(audience), algorithm=settings.jwt_algorithm)


def decode_mfa_token(token: str, expected_audience: Audience) -> dict[str, Any]:
    payload = _decode(token, _access_secret(expected_audience))
    _check_claims(payload, "mfa_pending", expected_audience)
    return payload


def decode_access_token(token: str, expected_audience: Audience) -> dict[str, Any]:
    """Decode with the expected audience's OWN secret, then double-check
    the `audience` claim (defense in depth — a cross-audience token
    already fails on the signature)."""
    payload = _decode(token, _access_secret(expected_audience))
    _check_claims(payload, "access", expected_audience)
    return payload


def token_subject(payload: dict[str, Any]) -> uuid.UUID:
    """Extract and validate the `sub` claim as a UUID."""
    sub = payload.get("sub")
    if not sub:
        raise UnauthorizedError("Invalid token payload.")
    try:
        return uuid.UUID(str(sub))
    except ValueError as e:
        raise UnauthorizedError("Invalid token subject.") from e


def decode_refresh_token(token: str, expected_audience: Audience) -> dict[str, Any]:
    """Decode with the single refresh secret; here the `audience` claim
    is what keeps the two refresh flows from being interchangeable."""
    _require_token_audience(expected_audience)
    settings = get_settings()
    payload = _decode(token, settings.jwt_refresh_secret)
    _check_claims(payload, "refresh", expected_audience)
    return payload
