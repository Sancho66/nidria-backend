"""Reminder dispatch mail carries its agency context (multi-agency
inspection §6 fix).

Covers: (a) a reminder on an agency-A dossier → subject/intro name A,
branded ?agency=slug-A link; (b) Jean with dossiers at TWO agencies →
each reminder mail carries ITS OWN agency context; (c) the recipient
language follows the client rule (stored preferred_lang, else EN) and
the agency-written body stays untouched."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session, sessionmaker

from shared.models.client_case import ClientCase
from shared.models.expat_user import ExpatUser
from src.core import email
from src.reminders.reminders_jobs import dispatch_due_reminders
from tests.plugins.agency_plugin import MakeAgency
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser
from tests.plugins.reminder_plugin import MakeReminder

pytestmark = pytest.mark.usefixtures("rbac_baseline")

_PAST = datetime.now(UTC) - timedelta(hours=1)


def _dispatch(sync_session_local: sessionmaker[Session]) -> dict:
    with sync_session_local() as db:
        return dispatch_due_reminders(db, log=lambda _line: None)


async def _case_at(
    make_agency: MakeAgency,
    make_client_case: MakeClientCase,
    expat: ExpatUser,
    *,
    slug: str,
    name: str,
) -> ClientCase:
    agency = await make_agency(slug=slug, name=name)
    return await make_client_case(agency_id=agency.id, principal_expat_user_id=expat.id)


async def test_reminder_mail_carries_its_agency(
    sync_session_local: sessionmaker[Session],
    make_agency: MakeAgency,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    make_reminder: MakeReminder,
) -> None:
    expat = await make_expat_user(email="client-a@example.com")
    case = await _case_at(
        make_agency, make_client_case, expat, slug="agence-alpha", name="Agence Alpha"
    )
    await make_reminder(
        case=case, status="approved", scheduled_at=_PAST, message_body="Pensez au passeport."
    )

    stats = _dispatch(sync_session_local)
    assert stats["sent"] == 1
    [sent] = email.outbox
    assert sent.to == "client-a@example.com"
    assert sent.subject == "Nidria : Rappel de Agence Alpha"
    assert "Agence Alpha vous envoie un rappel concernant votre dossier" in sent.body
    assert "?agency=agence-alpha" in sent.body  # branded client-space link
    assert "Pensez au passeport." in sent.body  # the agency-written body, untouched


async def test_two_agencies_two_contexts(
    sync_session_local: sessionmaker[Session],
    make_agency: MakeAgency,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    make_reminder: MakeReminder,
) -> None:
    jean = await make_expat_user(email="jean@example.com")
    case_a = await _case_at(
        make_agency, make_client_case, jean, slug="agence-alpha", name="Agence Alpha"
    )
    case_b = await _case_at(
        make_agency, make_client_case, jean, slug="agence-beta", name="Agence Beta"
    )
    await make_reminder(
        case=case_a, status="approved", scheduled_at=_PAST, message_body="Rappel A."
    )
    await make_reminder(
        case=case_b, status="approved", scheduled_at=_PAST, message_body="Rappel B."
    )

    stats = _dispatch(sync_session_local)
    assert stats["sent"] == 2
    by_body = {("Rappel A." in m.body): m for m in email.outbox}
    mail_a, mail_b = by_body[True], by_body[False]
    assert mail_a.subject == "Nidria : Rappel de Agence Alpha"
    assert "?agency=agence-alpha" in mail_a.body
    assert "Agence Beta" not in mail_a.subject and "agence-beta" not in mail_a.body
    assert mail_b.subject == "Nidria : Rappel de Agence Beta"
    assert "?agency=agence-beta" in mail_b.body
    assert "Rappel B." in mail_b.body


async def test_recipient_language_follows_the_client_rule(
    sync_session_local: sessionmaker[Session],
    make_agency: MakeAgency,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    make_reminder: MakeReminder,
) -> None:
    # Stored preference → that language (the client rule, unchanged).
    ivan = await make_expat_user(email="ivan@example.com", preferred_lang="ru")
    case = await _case_at(
        make_agency, make_client_case, ivan, slug="agence-alpha", name="Agence Alpha"
    )
    await make_reminder(
        case=case, status="approved", scheduled_at=_PAST, message_body="Не забудьте паспорт."
    )
    _dispatch(sync_session_local)
    [sent] = email.outbox
    assert sent.subject == "Nidria: Напоминание от Agence Alpha"
    assert "Не забудьте паспорт." in sent.body
