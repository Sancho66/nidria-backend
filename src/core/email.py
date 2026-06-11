import logging
from dataclasses import dataclass

import resend

from src.core.config import get_settings

logger = logging.getLogger(__name__)


@dataclass
class OutboxEmail:
    to: str
    subject: str
    body: str


# Mock-mode sink, inspectable by tests (cleared per test by an autouse
# fixture). Real mode never touches it.
outbox: list[OutboxEmail] = []


def _is_mocked() -> bool:
    settings = get_settings()
    if settings.mock_email is not None:
        return settings.mock_email
    return settings.mock_services


def send_email(to: str, subject: str, body: str) -> None:
    """Plain-text transactional email. Mocked by default (MOCK_SERVICES /
    MOCK_EMAIL): logs + appends to `outbox` instead of calling Resend.
    Blocking — call via asyncio.to_thread from async code."""
    if _is_mocked():
        logger.info("MOCK email to=%s subject=%r", to, subject)
        outbox.append(OutboxEmail(to=to, subject=subject, body=body))
        return
    settings = get_settings()
    resend.api_key = settings.resend_api_key
    resend.Emails.send(
        {
            "from": settings.email_from,
            "to": [to],
            "subject": subject,
            "text": body,
        }
    )
