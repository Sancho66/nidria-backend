"""INTERNAL alert on every self-serve agency signup (demande Eric, 13/08).

NOT the onboarding mail. That one goes TO the agency at J+10 min through the
scheduler; this one goes to US, immediately, inside the signup request — so
acquisition is traced and the agency can be called back the same day. Two
mails, two audiences, two flags (`signup_alert_sent_at` vs
`onboarding_email_sent_at`), so replaying one never suppresses the other.

BEST-EFFORT, always: `notify_signup` swallows everything. A mail provider
outage must never cost a signup — the account is already committed when we
get here, and the caller has a token to return.

THE "TEST ACCOUNT" DEFINITION IS NOT REDEFINED HERE: `is_excluded_address` is
imported from the onboarding job, so both mails exclude exactly the same
addresses and adding an internal domain is still one env var.

The ON/OFF SWITCH, however, is our own (`signup_alert_enabled`). Sharing the
onboarding one meant that muting the agency's welcome mail would silently
blind the team to every new signup — two unrelated decisions on one lever.
Same idiom, separate lever.
"""

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from src.agencies.onboarding_email_job import is_excluded_address
from src.core.config import Settings, get_settings
from src.core.email import send_email

logger = logging.getLogger(__name__)

_UNKNOWN = "inconnue"


def alert_enabled(settings: Settings) -> bool:
    """`bool | None` like every other switch here: None = derive from the
    environment (production only), True/False force it. A local seed of 20
    agencies therefore sends ZERO alerts without anyone remembering a flag."""
    if settings.signup_alert_enabled is not None:
        return settings.signup_alert_enabled
    return settings.environment.strip().lower() == "production"


def _fmt_when(moment: datetime) -> str:
    """Explicit timezone, always. The team reads this mail from several
    countries — a bare local time would be ambiguous, UTC never is."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%d/%m/%Y à %H:%M UTC")


async def _describe_source(db: AsyncSession, agency: Agency) -> str:
    """D'OÙ vient l'inscription — l'information la plus utile de ce mail.

    Ordre de préférence : la campagne (UTM, capturée à l'arrivée sur /signup
    et portée jusqu'ici), puis le referrer nu, puis le parrainage, puis
    `inconnue`. On ne devine JAMAIS : une source fausse est pire qu'absente,
    et une inscription directe est un fait, pas un trou."""
    campaign = [
        f"{label} {value.strip()}"
        for label, value in (
            ("source", agency.utm_source),
            ("support", agency.utm_medium),
            ("campagne", agency.utm_campaign),
        )
        if value and value.strip()
    ]
    if campaign:
        described = " · ".join(campaign)
        if agency.referrer and agency.referrer.strip():
            described += f" (venu de {agency.referrer.strip()})"
        return described
    if agency.referrer and agency.referrer.strip():
        return f"venu de {agency.referrer.strip()}"
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
        if not alert_enabled(settings):
            return False
        if agency.is_internal:
            return False
        # THE shared definition of "test account" — the onboarding job's own
        # matcher, on its own config list. One list, both mails.
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
