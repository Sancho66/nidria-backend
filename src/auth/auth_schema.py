import uuid

from pydantic import BaseModel, EmailStr, Field


class LoginRequest(BaseModel):
    email: EmailStr
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
    roles: list[str]
    effective_permissions: list[str]
    impersonator: ImpersonatorInfo | None = None


class ExpatMeResponse(BaseModel):
    id: uuid.UUID
    first_name: str
    last_name: str
    email: str
    preferred_lang: str
    impersonator: ImpersonatorInfo | None = None


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
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    password: str = Field(min_length=8)
