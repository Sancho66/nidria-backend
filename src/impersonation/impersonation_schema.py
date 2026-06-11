from pydantic import BaseModel


class ImpersonationTokenResponse(BaseModel):
    """Access token ONLY — no refresh token by design: the 30-minute
    expiry IS the exit from impersonation."""

    access_token: str
    token_type: str = "bearer"
    audience: str
    expires_in_minutes: int
