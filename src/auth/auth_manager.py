import asyncio
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.expat_user import ExpatUser
from src.auth.auth_repository import AuthRepository
from src.auth.auth_schema import ActivateResponse, TokenPairResponse
from src.core.config import get_settings
from src.core.email import send_email
from src.core.enums import Audience, InvitationStatus
from src.core.exceptions import BadRequestError, UnauthorizedError
from src.core.security import (
    create_access_token,
    create_refresh_token,
    decode_refresh_token,
    hash_password,
    token_subject,
    verify_password,
)

# One generic message for every login failure (unknown email, wrong
# password, not-activated expat): the response must not reveal which.
_INVALID_CREDENTIALS = "Invalid credentials."
_INVALID_RESET_TOKEN = "Invalid or expired reset token."
_FORGOT_PASSWORD_DETAIL = "If this email exists, a reset link has been sent."


class AuthManager:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = AuthRepository(db)

    # --- token pair issuance ---------------------------------------------------

    def issue_token_pair(self, actor_id: uuid.UUID, audience: Audience) -> TokenPairResponse:
        """Mint access+refresh and register the refresh jti (rotation
        handle). Caller commits. Public: other domain managers issue
        pairs too (e.g. agent-invitation accept)."""
        settings = get_settings()
        jti = uuid.uuid4()
        expires_at = datetime.now(UTC) + timedelta(days=settings.refresh_token_expires_days)
        self.repo.add_refresh_token(jti, audience.value, actor_id, expires_at)
        return TokenPairResponse(
            access_token=create_access_token(str(actor_id), audience),
            refresh_token=create_refresh_token(str(actor_id), audience, jti),
        )

    # --- login -------------------------------------------------------------------

    async def login_agent(self, email: str, password: str) -> TokenPairResponse:
        agent = await self.repo.get_agent_by_email(email)
        if agent is None or not verify_password(password, agent.password_hash):
            raise UnauthorizedError(_INVALID_CREDENTIALS)
        pair = self.issue_token_pair(agent.id, Audience.AGENT)
        await self.db.commit()
        return pair

    async def login_expat(self, email: str, password: str) -> TokenPairResponse:
        expat = await self.repo.get_expat_by_email(email)
        if (
            expat is None
            or expat.activated_at is None
            or expat.password_hash is None
            or not verify_password(password, expat.password_hash)
        ):
            raise UnauthorizedError(_INVALID_CREDENTIALS)
        pair = self.issue_token_pair(expat.id, Audience.EXPAT)
        await self.db.commit()
        return pair

    # --- refresh rotation -----------------------------------------------------------

    async def refresh(self, refresh_token: str, audience: Audience) -> TokenPairResponse:
        """Rotate: revoke the consumed jti, issue a new pair.

        A revoked or UNKNOWN jti under a valid signature means the
        chain of trust is broken (reuse, theft, restored DB) → revoke
        every active refresh token of the actor; the cost is a
        re-login, the alternative is a zombie token. Natural expiry is
        a plain 401 — no reprisals.
        """
        payload = decode_refresh_token(refresh_token, audience)
        # Defensive: no code path issues a refresh token under
        # impersonation (expiry IS the exit) — if one ever carries the
        # claim, reject it BEFORE the jti lookup could honor it.
        if payload.get("impersonator_id") is not None:
            raise UnauthorizedError("Impersonation tokens cannot be refreshed.")
        actor_id = token_subject(payload)
        raw_jti = payload.get("jti")
        if not raw_jti:
            raise UnauthorizedError("Invalid refresh token.")
        jti = uuid.UUID(str(raw_jti))

        now = datetime.now(UTC)
        row = await self.repo.get_refresh_token(jti)
        if row is None or row.revoked_at is not None or row.actor_id != actor_id:
            await self.repo.revoke_all_active_refresh_tokens(audience.value, actor_id, now)
            await self.db.commit()
            raise UnauthorizedError("Invalid refresh token.")
        if row.expires_at <= now:
            raise UnauthorizedError("Refresh token expired.")

        row.revoked_at = now
        pair = self.issue_token_pair(actor_id, audience)
        await self.db.commit()
        return pair

    async def logout(self, refresh_token: str, audience: Audience, actor_id: uuid.UUID) -> None:
        """Revoke the current jti. Idempotent (an already-dead jti still
        logs out cleanly). The ACCESS token stays valid until its expiry
        (30 min max) — stateless by design, revoking it would need a
        per-request blocklist."""
        payload = decode_refresh_token(refresh_token, audience)
        if token_subject(payload) != actor_id:
            raise UnauthorizedError("Refresh token does not belong to this account.")
        raw_jti = payload.get("jti")
        if not raw_jti:
            raise UnauthorizedError("Invalid refresh token.")
        row = await self.repo.get_refresh_token(uuid.UUID(str(raw_jti)))
        if row is not None and row.actor_id == actor_id and row.revoked_at is None:
            row.revoked_at = datetime.now(UTC)
        await self.db.commit()

    # --- expat activation -------------------------------------------------------------

    async def activate_expat(self, token: str, password: str) -> ActivateResponse:
        invitation = await self.repo.get_case_invitation_by_token(token)
        now = datetime.now(UTC)
        if (
            invitation is None
            or invitation.status != InvitationStatus.PENDING
            or invitation.expires_at <= now
        ):
            raise BadRequestError("Invalid or expired invitation token.")

        expat = await self.repo.get_expat_by_email(invitation.email)
        if expat is None:
            # Case creation (step 9) always creates the expat row first;
            # an orphan invitation is a broken state, not a user error path.
            raise BadRequestError("Invalid or expired invitation token.")

        invitation.status = InvitationStatus.ACCEPTED
        invitation.accepted_at = now

        if expat.activated_at is not None:
            # 2nd invitation of an active account: NEVER touch the
            # password (an invitation token travels by email — allowing
            # a password set here would be an account-takeover vector).
            await self.db.commit()
            return ActivateResponse(already_active=True)

        expat.password_hash = hash_password(password)
        expat.activated_at = now
        pair = self.issue_token_pair(expat.id, Audience.EXPAT)
        await self.db.commit()
        return ActivateResponse(access_token=pair.access_token, refresh_token=pair.refresh_token)

    # --- password reset ------------------------------------------------------------------

    async def forgot_password(self, email: str, audience: Audience) -> str:
        """Always the same 200 — the response must not reveal whether
        the email exists. A non-activated expat gets the silent 200 and
        NO mail: their path is activation."""
        actor: Agent | ExpatUser | None
        if audience is Audience.AGENT:
            actor = await self.repo.get_agent_by_email(email)
        else:
            actor = await self.repo.get_expat_by_email(email)
            if actor is not None and actor.activated_at is None:
                actor = None

        if actor is not None:
            settings = get_settings()
            token = secrets.token_urlsafe(32)
            expires_at = datetime.now(UTC) + timedelta(
                minutes=settings.password_reset_token_expires_minutes
            )
            self.repo.add_reset_token(audience.value, actor.id, token, expires_at)
            await self.db.commit()
            reset_link = f"{settings.frontend_url}/{audience.value}/reset-password?token={token}"
            await asyncio.to_thread(
                send_email,
                email,
                "Nidria — Password reset",
                f"Use this link to reset your password (valid "
                f"{settings.password_reset_token_expires_minutes} minutes): {reset_link}",
            )
        return _FORGOT_PASSWORD_DETAIL

    async def reset_password(self, token: str, password: str, audience: Audience) -> None:
        row = await self.repo.get_reset_token(token)
        now = datetime.now(UTC)
        if (
            row is None
            or row.consumed_at is not None
            or row.expires_at <= now
            or row.actor_type != audience.value
        ):
            raise BadRequestError(_INVALID_RESET_TOKEN)

        actor: Agent | ExpatUser | None
        if audience is Audience.AGENT:
            actor = await self.repo.get_agent(row.actor_id)
        else:
            actor = await self.repo.get_expat(row.actor_id)
        if actor is None:
            raise BadRequestError(_INVALID_RESET_TOKEN)

        actor.password_hash = hash_password(password)
        row.consumed_at = now
        # A reset means the old credentials can no longer be trusted:
        # kill every active session.
        await self.repo.revoke_all_active_refresh_tokens(audience.value, actor.id, now)
        await self.db.commit()
