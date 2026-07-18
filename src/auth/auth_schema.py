import uuid

from pydantic import BaseModel, Field

from src.core.email import NormalizedEmailStr
from src.core.rbac.permissions import Permission


class LoginRequest(BaseModel):
    email: NormalizedEmailStr
    password: str


class TokenPairResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


class MessageResponse(BaseModel):
    detail: str


class ImpersonatorInfo(BaseModel):
    """Present on /me when the token was issued by impersonation —
    the frontend banner reads it."""

    agent_id: uuid.UUID
    first_name: str
    last_name: str


class AgentMeResponse(BaseModel):
    id: uuid.UUID
    first_name: str
    last_name: str
    email: str
    agency_id: uuid.UUID
    role: str
    # Authoritative internal↔external discriminant (denormalized on the
    # agent at creation, never crosses on role reassignment). The front
    # routes the audience on THIS fact, not on a permission proxy.
    is_external: bool
    # Typed on the catalogue ENUM (not bare strings): the openapi enumerates
    # every key, so the front's generated types — and its PermissionKey —
    # derive mechanically instead of being a hand-kept mirror that drifts.
    effective_permissions: list[Permission]
    has_avatar: bool = False
    # Prefs notifications EFFECTIVES (defauts fusionnes) — le front affiche
    # l'etat reel du reglage personnel.
    notification_prefs: dict[str, str] = {}
    impersonator: ImpersonatorInfo | None = None


class ExpatMeResponse(BaseModel):
    id: uuid.UUID
    first_name: str
    last_name: str
    email: str
    preferred_lang: str
    has_avatar: bool = False
    impersonator: ImpersonatorInfo | None = None


class ChangePasswordRequest(BaseModel):
    """Logged-in password change (bloc 1). The CURRENT password check is
    what distinguishes this flow from the email reset; the new password
    follows the same strength policy as the reset."""

    current_password: str
    new_password: str = Field(min_length=8)


class MfaRequiredResponse(BaseModel):
    """Login step 1 outcome when 2FA is active: NO tokens — an ephemeral
    challenge token only, consumable by /2fa/verify and rejected
    everywhere else (its `type` claim is not "access")."""

    mfa_required: bool = True
    mfa_token: str


class MfaSetupResponse(BaseModel):
    """The ONLY time the secret ever leaves the server (QR provisioning);
    every later response carries state booleans, never the secret."""

    secret: str
    otpauth_uri: str


class MfaCodeRequest(BaseModel):
    code: str = Field(min_length=6, max_length=32)


class MfaEnableResponse(BaseModel):
    """The 8 one-time backup codes, IN CLEAR exactly once (bcrypt-hashed
    at rest). They ARE the phone-lost recovery path."""

    enabled: bool = True
    backup_codes: list[str]


class MfaVerifyRequest(BaseModel):
    mfa_token: str
    code: str = Field(min_length=6, max_length=32)


class MfaDisableRequest(BaseModel):
    """BOTH factors required: a stolen session alone cannot disarm 2FA."""

    current_password: str
    code: str = Field(min_length=6, max_length=32)


class MfaStatusResponse(BaseModel):
    enabled: bool
    backup_codes_left: int


class ActivateRequest(BaseModel):
    token: str
    password: str = Field(min_length=8)


class ActivateResponse(BaseModel):
    """Either a fresh token pair (account just activated, logged in) or
    `already_active=True` with no tokens (2nd invitation of an existing
    account — password untouched, go through login)."""

    already_active: bool = False
    access_token: str | None = None
    refresh_token: str | None = None
    token_type: str = "bearer"


class ForgotPasswordRequest(BaseModel):
    email: NormalizedEmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    password: str = Field(min_length=8)
