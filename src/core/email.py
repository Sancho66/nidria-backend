import logging
from dataclasses import dataclass

import resend

from src.core.config import get_settings

logger = logging.getLogger(__name__)


@dataclass
class OutboxEmail:
    to: str
    subject: str
    body: str  # plain-text part (multipart fallback)
    html: str | None = None


@dataclass
class PendingEmail:
    """A built-but-unsent email. Lets a caller (the CRM import) DEFER the
    send out of the request transaction: the manager appends pending mails
    to a sink and the router dispatches them on BackgroundTasks, so a
    200-row import never blocks the response on N synchronous sends."""

    to: str
    subject: str
    text: str
    html: str | None = None


# Mock-mode sink, inspectable by tests (cleared per test by an autouse
# fixture). Real mode never touches it.
outbox: list[OutboxEmail] = []


def _is_mocked() -> bool:
    settings = get_settings()
    if settings.mock_email is not None:
        return settings.mock_email
    return settings.mock_services


def send_email(to: str, subject: str, body: str, html: str | None = None) -> None:
    """Transactional email, multipart when `html` is given (text part is
    the fallback). Mocked by default (MOCK_SERVICES / MOCK_EMAIL): logs +
    appends to `outbox` instead of calling Resend. Blocking — call via
    asyncio.to_thread from async code."""
    if _is_mocked():
        logger.info("MOCK email to=%s subject=%r", to, subject)
        outbox.append(OutboxEmail(to=to, subject=subject, body=body, html=html))
        return
    settings = get_settings()
    resend.api_key = settings.resend_api_key
    payload: dict[str, object] = {
        "from": settings.email_from,
        "to": [to],
        "subject": subject,
        "text": body,
    }
    if html is not None:
        payload["html"] = html
    resend.Emails.send(payload)  # type: ignore[arg-type]
