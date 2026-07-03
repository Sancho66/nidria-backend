import logging
import re
from dataclasses import dataclass
from typing import Annotated
from urllib.parse import quote

import resend
from pydantic import AfterValidator, EmailStr

from src.core.config import get_settings

logger = logging.getLogger(__name__)


def normalize_email(email: str) -> str:
    """Canonical identity form: trimmed + lowercased. Applied at EVERY
    boundary that writes or looks up an ACCOUNT email (request schemas
    via NormalizedEmailStr, the CSV import pivot, the auth lookups) —
    combined with the lowercase data migration, the DB only ever holds
    and compares lowercase, so `Agent.email == input` stays an exact
    match. Prod incident: an account created 'Contact@x' was silently
    unreachable by forgot-password typed 'contact@x'."""
    return email.strip().lower()


# Drop-in replacement for EmailStr on IDENTITY emails (login, reset,
# admin/invitation/principal creation). NOT used for contact-card emails
# (external_contact): those are display/notification data, kept as typed.
NormalizedEmailStr = Annotated[EmailStr, AfterValidator(normalize_email)]


@dataclass
class OutboxEmail:
    to: str
    subject: str
    body: str  # plain-text part (multipart fallback)
    html: str | None = None
    sender: str | None = None  # None = the transactional email_from
    reply_to: str | None = None


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

# The seeded demo client (sample-case bloc 2): demo+<slug>@nidria.app.
# NOT a mailbox — nothing must EVER be sent to it (no activation, no
# reset, no thread notification, no reminder). Enforced at THE sink
# below, so every present and future send path is covered at once.
_DEMO_RECIPIENT_RE = re.compile(r"^demo\+[a-z0-9-]+@nidria\.app$")


def demo_expat_email(agency_slug: str) -> str:
    """Deterministic per-agency demo-client address (globally unique
    because the slug is). Must stay matched by `is_demo_recipient`."""
    return f"demo+{agency_slug}@nidria.app"


def is_demo_recipient(to: str) -> bool:
    return _DEMO_RECIPIENT_RE.match(normalize_email(to)) is not None


def _is_mocked() -> bool:
    settings = get_settings()
    if settings.mock_email is not None:
        return settings.mock_email
    return settings.mock_services


def space_link(frontend_url: str, path: str, agency_slug: str | None) -> str:
    """Client-space URL carrying the white-label context: every client
    email lands on the BRANDED login/activation (?agency=<slug>), never
    the naked /space pages. Slugs are [a-z0-9-] by construction; quoted
    anyway (clean encoding whatever a future slug holds)."""
    url = f"{frontend_url}{path}"
    if agency_slug:
        url = f"{url}?agency={quote(agency_slug, safe='')}"
    return url


def send_email(
    to: str,
    subject: str,
    body: str,
    html: str | None = None,
    *,
    sender: str | None = None,
    reply_to: str | None = None,
) -> None:
    """Transactional email, multipart when `html` is given (text part is
    the fallback). Mocked by default (MOCK_SERVICES / MOCK_EMAIL): logs +
    appends to `outbox` instead of calling Resend. Blocking — call via
    asyncio.to_thread from async code.

    `sender`/`reply_to` override the transactional From for BRAND mails
    (nurture: eric@nidria.com, same verified Resend domain — Cloudflare
    routes the replies to Eric's real inbox)."""
    if is_demo_recipient(to):
        logger.info("demo recipient, email suppressed to=%s subject=%r", to, subject)
        return
    if _is_mocked():
        logger.info("MOCK email to=%s subject=%r", to, subject)
        outbox.append(
            OutboxEmail(
                to=to, subject=subject, body=body, html=html, sender=sender, reply_to=reply_to
            )
        )
        return
    settings = get_settings()
    resend.api_key = settings.resend_api_key
    payload: dict[str, object] = {
        "from": sender or settings.email_from,
        "to": [to],
        "subject": subject,
        "text": body,
    }
    if reply_to is not None:
        payload["reply_to"] = [reply_to]
    if html is not None:
        payload["html"] = html
    response = resend.Emails.send(payload)  # type: ignore[arg-type]
    # The Resend message id is the correlation handle with their
    # dashboard (delivered / bounced / suppressed) — without it a
    # "mail never arrived" report is undiagnosable server-side.
    message_id = response.get("id") if isinstance(response, dict) else response
    logger.info("email sent via resend id=%s to=%s subject=%r", message_id, to, subject)
