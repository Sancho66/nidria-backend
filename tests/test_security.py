"""Pure unit tests for src/core/security.py — no DB, no HTTP.

The cross-audience cases are THE security property of the double auth:
an agent token must never validate as an expat token, and vice versa.
(The jti rotation/revocation lifecycle is covered in test_auth.py —
here refresh tokens just carry a throwaway jti.)
"""

import uuid

import pytest

from src.core.enums import Audience
from src.core.exceptions import UnauthorizedError
from src.core.security import (
    create_access_token,
    create_refresh_token,
    decode_access_token,
    decode_refresh_token,
    hash_password,
    verify_password,
)

SUBJECT = "1c0a8e6e-6f0e-4a4e-9c7b-2d1f3a5b7c9d"


# --- Passwords ---------------------------------------------------------------


def test_hash_and_verify_password() -> None:
    hashed = hash_password("s3cret-password")
    assert hashed != "s3cret-password"
    assert verify_password("s3cret-password", hashed)


def test_verify_password_wrong_password() -> None:
    assert not verify_password("wrong", hash_password("right"))


def test_verify_password_garbage_hash() -> None:
    assert not verify_password("anything", "not-a-bcrypt-hash")


# --- Access tokens, per audience ---------------------------------------------


@pytest.mark.parametrize("audience", [Audience.AGENT, Audience.EXPAT])
def test_access_token_roundtrip(audience: Audience) -> None:
    token = create_access_token(SUBJECT, audience)
    payload = decode_access_token(token, audience)
    assert payload["sub"] == SUBJECT
    assert payload["type"] == "access"
    assert payload["audience"] == audience.value


def test_agent_token_rejected_for_expat_audience() -> None:
    token = create_access_token(SUBJECT, Audience.AGENT)
    with pytest.raises(UnauthorizedError):
        decode_access_token(token, Audience.EXPAT)


def test_expat_token_rejected_for_agent_audience() -> None:
    token = create_access_token(SUBJECT, Audience.EXPAT)
    with pytest.raises(UnauthorizedError):
        decode_access_token(token, Audience.AGENT)


def test_access_token_rejected_as_refresh() -> None:
    token = create_access_token(SUBJECT, Audience.AGENT)
    with pytest.raises(UnauthorizedError):
        decode_refresh_token(token, Audience.AGENT)


# --- Refresh tokens, audience claim ------------------------------------------


@pytest.mark.parametrize("audience", [Audience.AGENT, Audience.EXPAT])
def test_refresh_token_roundtrip(audience: Audience) -> None:
    token = create_refresh_token(SUBJECT, audience, uuid.uuid4())
    payload = decode_refresh_token(token, audience)
    assert payload["sub"] == SUBJECT
    assert payload["type"] == "refresh"
    assert payload["audience"] == audience.value


def test_refresh_token_audience_mismatch() -> None:
    # Single refresh secret — the `audience` claim check alone must
    # keep the two refresh flows from being interchangeable.
    token = create_refresh_token(SUBJECT, Audience.AGENT, uuid.uuid4())
    with pytest.raises(UnauthorizedError):
        decode_refresh_token(token, Audience.EXPAT)


def test_refresh_token_rejected_as_access() -> None:
    token = create_refresh_token(SUBJECT, Audience.AGENT, uuid.uuid4())
    with pytest.raises(UnauthorizedError):
        decode_access_token(token, Audience.AGENT)


# --- PUBLIC is never a token audience ----------------------------------------


def test_public_audience_raises_value_error() -> None:
    with pytest.raises(ValueError):
        create_access_token(SUBJECT, Audience.PUBLIC)
    with pytest.raises(ValueError):
        create_refresh_token(SUBJECT, Audience.PUBLIC, uuid.uuid4())
    agent_token = create_access_token(SUBJECT, Audience.AGENT)
    with pytest.raises(ValueError):
        decode_access_token(agent_token, Audience.PUBLIC)
    refresh_token = create_refresh_token(SUBJECT, Audience.AGENT, uuid.uuid4())
    with pytest.raises(ValueError):
        decode_refresh_token(refresh_token, Audience.PUBLIC)
