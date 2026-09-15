"""INTERNAL billing incident alert — to US, never to a client.

Born with the kept-price guard (lot pricing 14/09): when a seat push finds
that a price a running subscription bills on is no longer active in Paddle,
the push must FAIL LOUD (stable error code, this mail) and never fall back
to the env's fresh price (a silent repricing) nor skip in silence.

Same idiom as the signup alert: recipients are the team's config list
(`signup_alert_recipients`), the switch is its own lever
(`billing_alert_enabled`, None = production only). BEST-EFFORT: this mail
never raises — the caller raises its own error right after."""

import asyncio
import logging

from src.core.config import Settings, get_settings
from src.core.email import send_email

logger = logging.getLogger(__name__)


def billing_alert_enabled(settings: Settings) -> bool:
    if settings.billing_alert_enabled is not None:
        return settings.billing_alert_enabled
    return settings.environment.strip().lower() == "production"


async def notify_billing_incident(subject: str, body: str) -> int:
    """Send the incident to every configured recipient; returns how many
    mails went out (0 when disabled or unconfigured). Never raises."""
    try:
        settings = get_settings()
        recipients = [r.strip() for r in settings.signup_alert_recipients if r.strip()]
        if not recipients or not billing_alert_enabled(settings):
            return 0
        sent = 0
        for recipient in recipients:
            try:
                await asyncio.to_thread(send_email, recipient, f"[Nidria billing] {subject}", body)
                sent += 1
            except Exception:
                logger.exception("billing incident alert failed -> %s", recipient)
        return sent
    except Exception:
        logger.exception("billing incident alert could not be composed")
        return 0
