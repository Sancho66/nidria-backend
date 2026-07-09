import asyncio
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import pyotp
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.client_case import ClientCase
from shared.models.expat_user import ExpatUser
from shared.models.mfa import MfaTotp
from src.auth.auth_repository import AuthRepository
from src.auth.auth_schema import (
    ActivateResponse,
    MfaEnableResponse,
    MfaRequiredResponse,
    MfaSetupResponse,
    MfaStatusResponse,
    TokenPairResponse,
)
from src.core.config import get_settings
from src.core.email import normalize_email, send_email
from src.core.email_templates import password_reset_email
from src.core.enums import ActorType, Audience, InvitationStatus
from src.core.exceptions import (
    BadRequestError,
    ConflictError,
    ForbiddenError,
    UnauthorizedError,
    ValidationError,
)
from src.core.security import (
    create_access_token,
    create_mfa_token,
    create_refresh_token,
    decode_mfa_token,
    decode_refresh_token,
    hash_password,
    token_subject,
    verify_password,
)
from src.usage.usage_manager import UsageManager

# One generic message for every login failure (unknown email, wrong
# password, not-activated expat): the response must not reveal which.
_INVALID_CREDENTIALS = "Invalid credentials."
_INVALID_RESET_TOKEN = "Invalid or expired reset token."
_FORGOT_PASSWORD_DETAIL = "If this email exists, a reset link has been sent."

logger = logging.getLogger(__name__)


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

    def create_reset_link(
        self, actor_id: uuid.UUID, audience: Audience, expires_minutes: int | None = None
    ) -> str:
        """Stage a password-reset token for an actor and return its link.

        Caller commits (mirrors issue_token_pair — no commit here). Reused
        by forgot-password AND by agency onboarding: the first admin of a
        freshly created agency sets their password through this exact link.
        `expires_minutes` defaults to the forgot-password window (60 min);
        the onboarding caller passes its own 24h invitation window.
        """
        settings = get_settings()
        token = secrets.token_urlsafe(32)
        minutes = (
            expires_minutes
            if expires_minutes is not None
            else settings.password_reset_token_expires_minutes
        )
        expires_at = datetime.now(UTC) + timedelta(minutes=minutes)
        self.repo.add_reset_token(audience.value, actor_id, token, expires_at)
        # Frontend route map: agent flows live at the root, the expat space
        # under /space; tokens are PATH params.
        prefix = "" if audience is Audience.AGENT else "/space"
        return f"{settings.frontend_url}{prefix}/reset-password/{token}"

    # --- login -------------------------------------------------------------------

    async def login_agent(
        self, email: str, password: str
    ) -> TokenPairResponse | MfaRequiredResponse:
        # Belt under the schema-level NormalizedEmailStr: identity lookups
        # are always lowercase, whatever the caller.
        agent = await self.repo.get_agent_by_email(normalize_email(email))
        if agent is None or not verify_password(password, agent.password_hash):
            raise UnauthorizedError(_INVALID_CREDENTIALS)
        # A provider whose invitation is still PENDING has an Agent row (agent_id
        # posed at invite) but NO access: refuse with the SAME error — never
        # reveal that the account exists but is not activated. The throwaway
        # password is unknowable anyway; this is the belt.
        if agent.is_external and await self.repo.has_pending_external_invitation(agent.id):
            raise UnauthorizedError(_INVALID_CREDENTIALS)
        challenge = await self._mfa_challenge_if_enabled(Audience.AGENT, agent.id)
        if challenge is not None:
            return challenge
        pair = self.issue_token_pair(agent.id, Audience.AGENT)
        await self.db.commit()
        return pair

    async def login_expat(
        self, email: str, password: str
    ) -> TokenPairResponse | MfaRequiredResponse:
        expat = await self.repo.get_expat_by_email(normalize_email(email))
        if (
            expat is None
            or expat.activated_at is None
            or expat.password_hash is None
            or not verify_password(password, expat.password_hash)
        ):
            raise UnauthorizedError(_INVALID_CREDENTIALS)
        challenge = await self._mfa_challenge_if_enabled(Audience.EXPAT, expat.id)
        if challenge is not None:
            return challenge
        pair = self.issue_token_pair(expat.id, Audience.EXPAT)
        await self.db.commit()
        return pair

    async def _mfa_challenge_if_enabled(
        self, audience: Audience, actor_id: uuid.UUID
    ) -> MfaRequiredResponse | None:
        """Step 1 of a 2FA login: correct password → NO tokens, an
        ephemeral challenge instead (jti-keyed row = the attempts counter
        a stateless JWT cannot carry)."""
        mfa = await self.repo.get_mfa(audience.value, actor_id)
        if mfa is None or mfa.enabled_at is None:
            return None
        now = datetime.now(UTC)
        await self.repo.sweep_expired_mfa_challenges(now)
        jti = uuid.uuid4()
        settings = get_settings()
        self.repo.add_mfa_challenge(
            jti,
            audience.value,
            actor_id,
            now + timedelta(minutes=settings.mfa_token_expires_minutes),
        )
        await self.db.commit()
        return MfaRequiredResponse(mfa_token=create_mfa_token(str(actor_id), audience, jti))

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
        # Usage tracker: THE key adoption signal (the client now follows
        # their dossier). Agency resolved through the invitation's case.
        case = await self.db.get(ClientCase, invitation.case_id)
        if case is not None:
            await UsageManager(self.db).emit_for_case(
                case,
                "case.client_account_activated",
                actor_type=ActorType.EXPAT,
                actor_id=expat.id,
                details={"via": "activation"},
            )
        pair = self.issue_token_pair(expat.id, Audience.EXPAT)
        await self.db.commit()
        return ActivateResponse(access_token=pair.access_token, refresh_token=pair.refresh_token)

    # --- password reset ------------------------------------------------------------------

    async def forgot_password(self, email: str, audience: Audience) -> str:
        """Always the same 200 — the response must not reveal whether
        the email exists. A non-activated expat gets the silent 200 and
        NO mail: their path is activation. The non-revealing rule is for
        the HTTP response ONLY: both branches log server-side, or a
        'mail never arrived' report is undiagnosable (prod incident)."""
        email = normalize_email(email)
        actor: Agent | ExpatUser | None
        if audience is Audience.AGENT:
            actor = await self.repo.get_agent_by_email(email)
            # A provider with a PENDING invitation is not activated: silent 200,
            # NO mail — identical to an unknown email (mirror of the expat rule).
            if (
                actor is not None
                and actor.is_external
                and await self.repo.has_pending_external_invitation(actor.id)
            ):
                actor = None
        else:
            actor = await self.repo.get_expat_by_email(email)
            if actor is not None and actor.activated_at is None:
                actor = None

        if actor is not None:
            reset_link = self.create_reset_link(actor.id, audience)
            await self.db.commit()
            settings = get_settings()
            content = password_reset_email(
                reset_link, settings.password_reset_token_expires_minutes
            )
            await asyncio.to_thread(send_email, email, content.subject, content.text, content.html)
            logger.info("forgot-password: reset mail sent audience=%s to=%s", audience.value, email)
        else:
            logger.info(
                "forgot-password: no matching %s account for %s (silent 200)",
                audience.value,
                email,
            )
        return _FORGOT_PASSWORD_DETAIL

    async def change_password(
        self, actor: Agent | ExpatUser, audience: Audience, current: str, new: str
    ) -> None:
        """Logged-in change (bloc 1): the CURRENT password is verified
        (403 otherwise), then same semantics as a reset — every active
        refresh token dies (other sessions fall), the current ACCESS
        token stays valid until its natural expiry (stateless)."""
        if actor.password_hash is None or not verify_password(current, actor.password_hash):
            raise ForbiddenError("Current password is incorrect.", code="auth.wrong_password")
        actor.password_hash = hash_password(new)
        await self.repo.revoke_all_active_refresh_tokens(
            audience.value, actor.id, datetime.now(UTC)
        )
        await self.db.commit()

    # --- 2FA TOTP (bloc 2) -------------------------------------------------------------

    async def mfa_status(self, audience: Audience, actor_id: uuid.UUID) -> MfaStatusResponse:
        mfa = await self.repo.get_mfa(audience.value, actor_id)
        if mfa is None or mfa.enabled_at is None:
            return MfaStatusResponse(enabled=False, backup_codes_left=0)
        return MfaStatusResponse(
            enabled=True, backup_codes_left=len(await self.repo.unused_backup_codes(mfa.id))
        )

    async def mfa_setup(
        self, audience: Audience, actor_id: uuid.UUID, account_label: str
    ) -> MfaSetupResponse:
        """Generate (or regenerate) the PENDING secret. The one and only
        response ever carrying the secret (QR provisioning). 409 when 2FA
        is already active: disable first (password + code), no silent
        re-enrollment on a stolen session."""
        mfa = await self.repo.get_mfa(audience.value, actor_id)
        if mfa is not None and mfa.enabled_at is not None:
            raise ConflictError(
                "Two-factor authentication is already enabled.",
                code="auth.mfa_already_enabled",
            )
        secret = pyotp.random_base32()
        if mfa is None:
            self.repo.add_mfa(audience.value, actor_id, secret)
        else:  # pending re-setup: fresh secret replaces the unconfirmed one
            mfa.secret = secret
        await self.db.commit()
        uri = pyotp.totp.TOTP(secret).provisioning_uri(name=account_label, issuer_name="Nidria")
        return MfaSetupResponse(secret=secret, otpauth_uri=uri)

    async def mfa_enable(
        self, audience: Audience, actor_id: uuid.UUID, code: str
    ) -> MfaEnableResponse:
        """A first valid code proves possession → activate + mint the 8
        one-time backup codes (clear ONCE, bcrypt at rest)."""
        mfa = await self.repo.get_mfa(audience.value, actor_id)
        if mfa is None:
            raise ValidationError(
                "No pending 2FA setup. Call setup first.", code="auth.mfa_not_pending"
            )
        if mfa.enabled_at is not None:
            raise ConflictError(
                "Two-factor authentication is already enabled.",
                code="auth.mfa_already_enabled",
            )
        if not pyotp.TOTP(mfa.secret).verify(code, valid_window=1):
            raise ValidationError("Invalid authentication code.", code="auth.mfa_invalid_code")
        mfa.enabled_at = datetime.now(UTC)
        plain_codes = [f"{secrets.token_hex(2)}-{secrets.token_hex(2)}" for _ in range(8)]
        for plain in plain_codes:
            self.repo.add_backup_code(mfa.id, hash_password(plain))
        await self.db.commit()
        return MfaEnableResponse(backup_codes=plain_codes)

    async def mfa_verify(self, audience: Audience, mfa_token: str, code: str) -> TokenPairResponse:
        """Login step 2: TOTP code OR an unused backup code (consumed).
        The challenge row carries the attempts counter — cap reached or
        expired token → back to step 1."""
        payload = decode_mfa_token(mfa_token, audience)  # 401 on bad/expired/foreign
        actor_id = token_subject(payload)
        raw_jti = payload.get("jti")
        if not raw_jti:
            raise UnauthorizedError("Invalid MFA token.", code="auth.mfa_token_expired")
        challenge = await self.repo.get_mfa_challenge(uuid.UUID(str(raw_jti)))
        now = datetime.now(UTC)
        if challenge is None or challenge.expires_at <= now:
            raise UnauthorizedError(
                "The MFA challenge has expired. Log in again.",
                code="auth.mfa_token_expired",
            )
        mfa = await self.repo.get_mfa(audience.value, actor_id)
        if mfa is None or mfa.enabled_at is None:  # disabled between the two steps
            await self.repo.delete_mfa_challenge(challenge)
            await self.db.commit()
            raise UnauthorizedError(
                "The MFA challenge has expired. Log in again.",
                code="auth.mfa_token_expired",
            )
        if not await self._mfa_code_ok(mfa, code):
            challenge.attempts += 1
            if challenge.attempts >= get_settings().mfa_max_attempts:
                await self.repo.delete_mfa_challenge(challenge)
                await self.db.commit()
                raise UnauthorizedError(
                    "Too many invalid codes. Log in again.",
                    code="auth.mfa_too_many_attempts",
                )
            await self.db.commit()
            raise ValidationError("Invalid authentication code.", code="auth.mfa_invalid_code")
        await self.repo.delete_mfa_challenge(challenge)
        pair = self.issue_token_pair(actor_id, audience)
        await self.db.commit()
        return pair

    async def mfa_disable(
        self,
        actor: Agent | ExpatUser,
        audience: Audience,
        current_password: str,
        code: str,
    ) -> None:
        """BOTH factors demanded — a stolen session alone cannot disarm
        2FA. Purges the secret and every backup code (FK CASCADE)."""
        if actor.password_hash is None or not verify_password(
            current_password, actor.password_hash
        ):
            raise ForbiddenError("Current password is incorrect.", code="auth.wrong_password")
        mfa = await self.repo.get_mfa(audience.value, actor.id)
        if mfa is None or mfa.enabled_at is None:
            raise ValidationError(
                "Two-factor authentication is not enabled.", code="auth.mfa_not_enabled"
            )
        if not await self._mfa_code_ok(mfa, code):
            raise ValidationError("Invalid authentication code.", code="auth.mfa_invalid_code")
        await self.repo.delete_mfa(mfa)
        await self.db.commit()

    async def _mfa_code_ok(self, mfa: MfaTotp, code: str) -> bool:
        """TOTP first (standard ±1 step tolerance), then the unused
        backup codes — a match consumes the code permanently."""
        if pyotp.TOTP(mfa.secret).verify(code, valid_window=1):
            return True
        for backup in await self.repo.unused_backup_codes(mfa.id):
            if verify_password(code, backup.code_hash):
                backup.used_at = datetime.now(UTC)
                return True
        return False

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
            # A stray reset token can NEVER activate a pending provider — the
            # invitation is the only entry until accepted.
            if (
                isinstance(actor, Agent)
                and actor.is_external
                and await self.repo.has_pending_external_invitation(actor.id)
            ):
                raise BadRequestError(_INVALID_RESET_TOKEN)
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
