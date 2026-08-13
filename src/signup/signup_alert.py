"""INTERNAL alert on every self-serve agency signup (demande Eric, 13/08).

NOT the onboarding mail. That one goes TO the agency at J+10 min through the
scheduler; this one goes to US, immediately, inside the signup request — so
acquisition is traced and the agency can be called back the same day. Two
mails, two audiences, two flags (`signup_alert_sent_at` vs
`onboarding_email_sent_at`), so replaying one never suppresses the other.

BEST-EFFORT, always: `notify_signup` swallows everything. A mail provider
outage must never cost a signup — the account is already committed when we
get here, and the caller has a token to return.

THE GUARDS ARE NOT REDEFINED HERE. `mail_enabled` and `is_excluded_address`
are imported from the onboarding job, so "test account" has exactly ONE
definition in the codebase; adding an internal domain still means editing one
env var and nothing else.
"""

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from src.agencies.onboarding_email_job import is_excluded_address, mail_enabled
from src.core.config import Settings, get_settings
from src.core.email import send_email

logger = logging.getLogger(__name__)

_UNKNOWN = "inconnue"


def _fmt_when(moment: datetime) -> str:
    """Explicit timezone, always. The team reads this mail from several
    countries — a bare local time would be ambiguous, UTC never is."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%d/%m/%Y à %H:%M UTC")


async def _describe_source(db: AsyncSession, agency: Agency) -> str:
    """The ONLY acquisition signal the signup captures today is the referral
    code (`referral_code` on the form → `referred_by_agency_id`). There is no
    UTM and no referrer anywhere in the signup path, so for every other route
    we say `inconnue` rather than guess: a wrong source is worse than none.
    Capturing UTMs needs a front lot (read them on /signup, carry them to the
    POST, store them on the agency)."""
    if agency.referred_by_agency_id is None:
        return _UNKNOWN
    sponsor = (
        await db.execute(select(Agency).where(Agency.id == agency.referred_by_agency_id))
    ).scalar_one_or_none()
    if sponsor is None:
        return "parrainage (agence marraine introuvable)"
    return f"parrainage par {sponsor.name} ({sponsor.slug})"


def _compose(agency: Agency, admin: Agent, source: str, settings: Settings) -> tuple[str, str]:
    """Sober on purpose: an internal alert is read in three seconds. Plain
    text only — no HTML part, no layout. Mail clients auto-link the bare URL,
    which is what "cliquable" needs here."""
    created = agency.created_at or datetime.now(UTC)
    # There is no per-agency page in the superadmin today (/admin/agencies is
    # a list; its search is component state, not a URL param, so no deep link
    # is possible yet). The list defaults to created_at DESC, so the new
    # agency IS the first row — the link lands on it in practice.
    console = f"{settings.frontend_url.rstrip('/')}/admin/agencies"
    lines = [
        f"Agence     : {agency.name}",
        f"Créateur   : {admin.first_name} {admin.last_name} <{admin.email}>",
        f"Date       : {_fmt_when(created)}",
        f"Pays       : {agency.country or 'non renseigné'}",
        f"Langue     : {agency.default_language or 'non renseignée'}",
        f"Source     : {source}",
        "",
        f"Fiche agence : {console}",
    ]
    return f"Nouvelle inscription — {agency.name}", "\n".join(lines)


def _recipients(settings: Settings) -> list[str]:
    return [r.strip() for r in settings.signup_alert_recipients if r.strip()]


async def notify_signup(db: AsyncSession, agency: Agency, admin: Agent) -> bool:
    """Fire the internal alert. Returns True when a mail actually went out.

    NEVER raises: the signup is already committed by the time we are called.
    """
    try:
        settings = get_settings()
        if agency.signup_alert_sent_at is not None:
            return False  # idempotence: one alert per agency, replay-proof
        recipients = _recipients(settings)
        if not recipients:
            return False
        # The three guards of the onboarding lot, reused verbatim.
        if not mail_enabled(settings):
            return False
        if agency.is_internal:
            return False
        if is_excluded_address(admin.email, settings):
            return False

        source = await _describe_source(db, agency)
        subject, body = _compose(agency, admin, source, settings)
        sent = 0
        for recipient in recipients:
            try:
                # send_email is blocking (Resend over HTTP) and we are on the
                # request's event loop — off to a thread, per its contract.
                await asyncio.to_thread(send_email, recipient, subject, body)
                sent += 1
            except Exception:
                logger.exception("signup alert failed for %s -> %s", agency.slug, recipient)
        if sent == 0:
            # Flag stays NULL — the doctrine of the onboarding lot: pose it
            # AFTER a real send, never before.
            return False
        agency.signup_alert_sent_at = datetime.now(UTC)
        await db.commit()
        return True
    except Exception:
        logger.exception("signup alert crashed for agency %s", getattr(agency, "slug", "?"))
        return False
