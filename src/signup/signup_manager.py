"""Self-serve signup — the first public WRITE surface of the product.

Code-by-email design (Alexandre, 2026-07-17): a crypto 6-digit code
(hashed at rest, 15-minute expiry, DEAD after 5 wrong attempts — the
anti-brute-force a magic link never had), then a long completion token
(30 min) that authorizes the single-transaction creation through THE
shared writer (_create_agency_core): trial, referral, demo case,
milestones — everything wired to creation fires identically to the
wizard. No oracle anywhere: dead/expired/wrong codes share one answer,
and the initial POST answers 200 whether the email is known or not
(the forgot-password pattern — the EMAIL differs, never the response)."""

import hashlib
import logging
import secrets
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.signup import SignupVerification
from src.agencies.agencies_manager import AgenciesManager, _slugify
from src.agencies.agencies_repository import AgenciesRepository
from src.auth.auth_manager import AuthManager
from src.auth.auth_schema import TokenPairResponse
from src.core.config import get_settings
from src.core.email import send_email
from src.core.email_templates import signup_code_email, signup_existing_account_email
from src.core.enums import Audience
from src.core.exceptions import BadRequestError, ValidationError
from src.core.security import hash_password
from src.signup.signup_schema import (
    SignupCompleteRequest,
    SignupRequest,
    SignupVerifyRequest,
)

logger = logging.getLogger(__name__)

CODE_EXPIRES_MINUTES = 15
CODE_MAX_ATTEMPTS = 5
COMPLETION_EXPIRES_MINUTES = 30

_INVALID_CODE = "Invalid or expired code."  # ONE answer: wrong == expired == dead


def _generate_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def _hash_code(code: str) -> str:
    # sha256, deliberately: the code space is tiny — the real lock is the
    # attempts counter + expiry, not hash strength. Fast beats bcrypt here.
    return hashlib.sha256(code.encode()).hexdigest()


async def verify_turnstile(token: str | None) -> bool:
    """Cloudflare Turnstile, FLAG pattern: secret absent = check skipped.
    Arming it = setting TURNSTILE_SECRET, zero deploy."""
    secret = get_settings().turnstile_secret
    if secret is None:
        return True
    if not token:
        return False
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data={"secret": secret, "response": token},
        )
        return bool(resp.status_code == 200 and resp.json().get("success"))


class SignupManager:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def request_code(self, payload: SignupRequest) -> None:
        """Stage 1. ALWAYS silent from the caller's viewpoint (the router
        returns the same 200): honeypot filled = nothing; known agent
        email = 'you already have an account' email; otherwise the code.
        A re-request KILLS the previous verification (one live per email)."""
        if payload.website:  # honeypot: bots fill hidden fields
            logger.info("signup honeypot tripped")
            return
        email = payload.email
        if await AgenciesRepository(self.db).get_agent_by_email(email) is not None:
            content = signup_existing_account_email(
                f"{get_settings().frontend_url}/login", payload.lang
            )
            send_email(email, content.subject, content.text, content.html)
            return
        # One LIVE verification per email: the re-request kills the old.
        await self.db.execute(delete(SignupVerification).where(SignupVerification.email == email))
        code = _generate_code()
        self.db.add(
            SignupVerification(
                email=email,
                lang=payload.lang,
                code_hash=_hash_code(code),
                expires_at=datetime.now(UTC) + timedelta(minutes=CODE_EXPIRES_MINUTES),
            )
        )
        await self.db.commit()
        content = signup_code_email(code, payload.lang)
        send_email(email, content.subject, content.text, content.html)

    async def verify_code(self, payload: SignupVerifyRequest) -> str:
        """Stage 2. Dead == expired == wrong: one answer, no oracle. The
        5th wrong attempt kills the code for good (a new request is the
        only way out)."""
        row = (
            await self.db.execute(
                select(SignupVerification).where(
                    SignupVerification.email == payload.email,
                    SignupVerification.consumed_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        now = datetime.now(UTC)
        if row is None or row.expires_at <= now or row.attempts >= CODE_MAX_ATTEMPTS:
            raise BadRequestError(_INVALID_CODE, code="signup.invalid_code")
        if _hash_code(payload.code) != row.code_hash:
            row.attempts += 1  # the 5th wrong attempt kills the code
            await self.db.commit()
            raise BadRequestError(_INVALID_CODE, code="signup.invalid_code")
        row.consumed_at = now
        row.completion_token = secrets.token_urlsafe(32)[:64]
        row.completion_expires_at = now + timedelta(minutes=COMPLETION_EXPIRES_MINUTES)
        await self.db.commit()
        return row.completion_token

    async def complete(self, payload: SignupCompleteRequest) -> TokenPairResponse:
        """Stage 3: ONE transaction through THE shared writer, then
        auto-login (TokenPairResponse) straight to the welcome screen."""
        row = (
            await self.db.execute(
                select(SignupVerification).where(
                    SignupVerification.completion_token == payload.completion_token
                )
            )
        ).scalar_one_or_none()
        now = datetime.now(UTC)
        if row is None or row.completion_expires_at is None or row.completion_expires_at <= now:
            raise BadRequestError("Invalid or expired signup session.", code="signup.invalid_token")
        # Belt (a race with another door creating the same email meanwhile).
        if await AgenciesRepository(self.db).get_agent_by_email(row.email) is not None:
            raise BadRequestError("Invalid or expired signup session.", code="signup.invalid_token")
        manager = AgenciesManager(self.db)
        # Sector is now chosen IN the signup form and posed atomically at
        # creation — no post-signup wall (which risked a 401 window). >= 1
        # mandatory; validated BEFORE any DB write, so a bad payload creates
        # nothing (no orphan agency without a sector).
        if not payload.sectors:
            raise ValidationError(
                "At least one sector is required.", code="signup.sectors_required"
            )
        sectors = manager._validate_sectors(payload.sectors)  # enum + dedup
        slug = base_slug = _slugify(payload.agency_name).strip("-")
        if not slug:
            raise ValidationError("Could not derive a slug from the agency name.")
        # Collision → suffix (the self-serve user never picks a slug).
        suffix = 2
        while await manager.repo.get_agency_by_slug(slug) is not None:
            slug = f"{base_slug}-{suffix}"
            suffix += 1
        agency, admin, _role = await manager._create_agency_core(
            name=payload.agency_name,
            slug=slug,
            default_language=payload.language,
            admin_email=row.email,
            admin_first_name=payload.first_name,
            admin_last_name=payload.last_name,
            password_hash=hash_password(payload.password),
            referral_code=payload.referral_code,
            # Sector chosen in the form → written with the agency, flag false
            # (nothing to re-ask; the post-signup wall is gone).
            sectors=sectors,
            sectors_onboarding_required=False,
        )
        # The verification is spent in the SAME transaction as the creation.
        await self.db.delete(row)
        pair = AuthManager(self.db).issue_token_pair(admin.id, Audience.AGENT)
        await manager._finalize_agency_creation(agency, admin)
        return pair
