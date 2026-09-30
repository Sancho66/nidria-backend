"""« La langue du client » (lot B, 30/09) — the agency sets THE CLIENT'S
LANGUAGE on its record of the client (client_profile.preferred_lang), and
that value drives every client send of THIS agency and the client space's
default display. Rule validated by Alexandre.

Proves:
- the resolver order: agency record → account → agency language → "en";
- the batched readers (async AND sync) read the record of THIS agency only;
- each send family takes the RECORD's language when it differs from the
  account's: invitation (principal + member), manual reminder, automatic
  follow-up routed to a member (body + translated step name), grouped
  follow-ups, digest;
- creation defaults: no language → the agency's (never "fr" hardcoded),
  existing account kept but the record written, explicit value written and
  traced, unsupported value refused (422), case from a profile, CRM import;
- the activation link carries ?lang= and its token still activates;
- `client_lang` served to the client space, per agency;
- the agency screens serve the EFFECTIVE language (the one the mails use).
"""

import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, sessionmaker

from shared.models.activity import ActivityLog
from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.case_person import CasePerson
from shared.models.case_step_progress import CaseStepProgress
from shared.models.case_step_requirement import CaseStepRequirement
from shared.models.client_case import ClientCase
from shared.models.client_profile import ClientProfile
from shared.models.expat_user import ExpatUser
from shared.models.journey import JourneyTemplate
from shared.models.rbac import Role
from shared.models.reminder import Reminder
from src.core import email
from src.core.client_lang import client_langs, client_langs_sync
from src.core.email_templates import auto_reminder_body, reminder_email
from src.core.i18n import resolve_notification_lang_client, resolve_step_name_for_notif
from src.digest.digest_job import run_notification_digest
from src.imports.case_import_manager import CaseImportManager
from src.imports.case_import_schema import CaseImportRequest
from src.reminders.reminders_jobs import create_auto_reminders, dispatch_due_reminders
from tests.plugins.agency_plugin import MakeAgency
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser
from tests.plugins.journey_plugin import MakeJourneyTemplate, MakeTemplateStep
from tests.plugins.reminder_plugin import MakeMessageTemplate, MakeReminder

pytestmark = pytest.mark.usefixtures("rbac_baseline")

_PAST = datetime.now(UTC) - timedelta(hours=1)
_ID_STEP = {"fr": "Pièce d'identité", "ru": "Удостоверение личности", "es": "Documento"}


@pytest_asyncio.fixture
async def agency_es(make_agency: MakeAgency) -> Agency:
    """A Spanish-speaking agency: its language is the default of its clients."""
    return await make_agency(name="Agencia Sol", default_language="es")


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role], agency_es: Agency) -> Agent:
    return await make_agent(agency_id=agency_es.id, role=system_roles["admin"])


async def _record(
    db: AsyncSession, agency_id: uuid.UUID, expat_id: uuid.UUID | None, lang: str | None, **kw
) -> ClientProfile:
    """The agency's record of a client, carrying the language IT set."""
    profile = ClientProfile(agency_id=agency_id, expat_user_id=expat_id, preferred_lang=lang, **kw)
    db.add(profile)
    await db.commit()
    return profile


def _html_lang(mail: email.OutboxEmail) -> str:
    assert mail.html is not None
    found = re.search(r'<html lang="([a-z]{2})"', mail.html)
    assert found is not None
    return found.group(1)


def _case_payload(addr: str, **extra: object) -> dict[str, object]:
    return {"first_name": "Ana", "last_name": "Ruiz", "email": addr, **extra}


# --- 1. the resolver ----------------------------------------------------------------


def test_resolver_order_record_then_account_then_agency_then_english() -> None:
    r = resolve_notification_lang_client
    assert r("fr", profile_lang="es", agency_default="it") == "es"  # the record wins
    assert r("fr", profile_lang=None, agency_default="it") == "fr"  # then the account
    # A language the product does not speak: English, never the agency's (NOTIF-1).
    assert r("de", profile_lang=None, agency_default="it") == "en"
    assert r("de", profile_lang="xx", agency_default=None) == "en"
    assert r(None, profile_lang=None, agency_default="it") == "it"  # nothing known: the agency
    assert r(None, profile_lang=None, agency_default=None) == "en"  # last resort
    assert r(None, profile_lang="PT-br", agency_default="fr") == "pt"  # normalized
    # No agency context (the expat password reset): the historical meaning.
    assert r("ko") == "en"
    assert r("hu") == "hu"


def test_step_names_fall_back_to_the_agency_language_before_french() -> None:
    """Audit n°21: the notification chain now matches the timeline's —
    recipient → AGENCY → fr → scalar."""
    blob = {"fr": "Dépôt", "es": "Depósito"}
    assert resolve_step_name_for_notif(blob, "Dépôt", "hu", "es") == "Depósito"
    assert resolve_step_name_for_notif(blob, "Dépôt", "hu") == "Dépôt"  # legacy callers
    assert resolve_step_name_for_notif({}, "Dépôt", "hu", "es") == "Dépôt"


async def test_batched_readers_read_this_agency_record_only(
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    make_agency: MakeAgency,
    make_expat_user: MakeExpatUser,
    agency_es: Agency,
) -> None:
    other = await make_agency(name="Magyar", default_language="hu")
    with_record = await make_expat_user(preferred_lang="fr")
    plain = await make_expat_user(preferred_lang="fr")
    legacy = await make_expat_user(preferred_lang="de")  # outside the product
    await _record(db_session, agency_es.id, with_record.id, "ru")
    await _record(db_session, other.id, plain.id, "pt")  # ANOTHER agency's record
    accounts = {with_record.id: "fr", plain.id: "fr", legacy.id: "de"}
    expected = {with_record.id: "ru", plain.id: "fr", legacy.id: "en"}

    assert await client_langs(db_session, agency_es.id, accounts) == expected
    with sync_session_local() as db:
        assert client_langs_sync(db, agency_es.id, accounts, agency_default="es") == expected
    # The other agency sees ITS record, not the Spanish one.
    assert (await client_langs(db_session, other.id, {plain.id: "fr"})) == {plain.id: "pt"}
    assert await client_langs(db_session, agency_es.id, {}) == {}


# --- 2. creation defaults -------------------------------------------------------------


async def test_case_without_language_speaks_the_agency_language(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agency_es: Agency,
    agent_headers: AuthHeaders,
) -> None:
    """No language in the form → the AGENCY's language (it used to be a
    hardcoded "fr"): on the new account, on the agency record, in the mail —
    and the activation link carries ?lang= without breaking its token."""
    r = await client.post(
        "/cases", headers=agent_headers(admin), json=_case_payload("ana.new@example.com")
    )
    assert r.status_code == 201, r.text
    account = (
        await db_session.execute(select(ExpatUser).where(ExpatUser.email == "ana.new@example.com"))
    ).scalar_one()
    assert account.preferred_lang == "es"
    profile = (
        await db_session.execute(
            select(ClientProfile).where(ClientProfile.expat_user_id == account.id)
        )
    ).scalar_one()
    assert profile.preferred_lang == "es"
    [sent] = email.outbox
    assert _html_lang(sent) == "es"
    link = re.search(r"/space/activate/([^?\s\"]+)\?agency=([a-z0-9-]+)&lang=es", sent.body)
    assert link is not None, sent.body
    assert link.group(2) == agency_es.slug
    assert f"/space/login?agency={agency_es.slug}&lang=es" in sent.body
    activated = await client.post(
        "/auth/expat/activate", json={"token": link.group(1), "password": "new-password-1"}
    )
    assert activated.status_code == 200, activated.text


async def test_existing_account_keeps_its_language_and_the_record_takes_the_form_value(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agency_es: Agency,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
) -> None:
    """The account is GLOBAL (other agencies): never rewritten. The value the
    agency chose lands on ITS record, and the mail follows the record."""
    ivan = await make_expat_user(email="ivan@example.com", preferred_lang="ru")
    r = await client.post(
        "/cases",
        headers=agent_headers(admin),
        json=_case_payload(ivan.email, preferred_lang="pt"),
    )
    assert r.status_code == 201, r.text
    await db_session.refresh(ivan)
    assert ivan.preferred_lang == "ru"  # untouched
    profile = (
        await db_session.execute(
            select(ClientProfile).where(
                ClientProfile.agency_id == agency_es.id, ClientProfile.expat_user_id == ivan.id
            )
        )
    ).scalar_one()
    assert profile.preferred_lang == "pt"
    [sent] = email.outbox
    assert _html_lang(sent) == "pt"
    assert f"/space/login?agency={agency_es.slug}&lang=pt" in sent.body


async def test_existing_account_without_form_value_is_recorded_with_its_own_language(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agency_es: Agency,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
) -> None:
    """No form value, no record yet: the chain gives the ACCOUNT's language
    (before the agency's), and the empty record receives it."""
    olga = await make_expat_user(email="olga@example.com", preferred_lang="ru")
    r = await client.post("/cases", headers=agent_headers(admin), json=_case_payload(olga.email))
    assert r.status_code == 201, r.text
    profile = (
        await db_session.execute(
            select(ClientProfile).where(ClientProfile.expat_user_id == olga.id)
        )
    ).scalar_one()
    assert profile.preferred_lang == "ru"
    assert _html_lang(email.outbox[-1]) == "ru"


async def test_existing_record_wins_without_form_value_and_an_explicit_one_rewrites_it(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agency_es: Agency,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
) -> None:
    marco = await make_expat_user(email="marco@example.com", preferred_lang="fr")
    profile = await _record(db_session, agency_es.id, marco.id, "it")
    headers = agent_headers(admin)

    r = await client.post("/cases", headers=headers, json=_case_payload(marco.email))
    assert r.status_code == 201, r.text
    await db_session.refresh(profile)
    assert profile.preferred_lang == "it"  # kept: the agency already chose
    assert _html_lang(email.outbox[-1]) == "it"

    email.outbox.clear()
    r = await client.post(
        "/cases", headers=headers, json=_case_payload(marco.email, preferred_lang="hu")
    )
    assert r.status_code == 201, r.text
    await db_session.refresh(profile)
    assert profile.preferred_lang == "hu"  # explicit → rewritten…
    assert _html_lang(email.outbox[-1]) == "hu"
    traces = (
        (
            await db_session.execute(
                select(ActivityLog).where(
                    ActivityLog.case_id == uuid.UUID(r.json()["id"]),
                    ActivityLog.action_type == "profile.updated",
                )
            )
        )
        .scalars()
        .all()
    )
    assert [t.details["fields"] for t in traces] == [["preferred_lang"]]  # …and traced


async def test_unsupported_language_is_refused(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """A language outside the 7 would be stored then mailed in another one."""
    r = await client.post(
        "/cases",
        headers=agent_headers(admin),
        json=_case_payload("klaus@example.com", preferred_lang="de"),
    )
    assert r.status_code == 422


async def test_case_from_a_profile_no_longer_forces_french(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agency_es: Agency,
    agent_headers: AuthHeaders,
) -> None:
    """« Nouvelle démarche » from a record WITHOUT account: it used to seed
    the account in "fr" whatever the record said. Now: the record's language,
    else the agency's."""
    headers = agent_headers(admin)
    with_lang = await _record(
        db_session,
        agency_es.id,
        None,
        "ru",
        first_name="Pavel",
        last_name="Orlov",
        email="pavel@example.com",
    )
    without_lang = await _record(
        db_session,
        agency_es.id,
        None,
        None,
        first_name="Lucia",
        last_name="Diaz",
        email="lucia@example.com",
    )
    for profile, expected in ((with_lang, "ru"), (without_lang, "es")):
        email.outbox.clear()
        r = await client.post(f"/client-profiles/{profile.id}/cases", headers=headers, json={})
        assert r.status_code == 200, r.text
        account = (
            await db_session.execute(select(ExpatUser).where(ExpatUser.email == profile.email))
        ).scalar_one()
        assert account.preferred_lang == expected
        assert _html_lang(email.outbox[-1]) == expected


async def test_crm_import_takes_the_agency_language(
    db_session: AsyncSession,
    admin: Agent,
    make_journey_template: MakeJourneyTemplate,
    make_template_step: MakeTemplateStep,
) -> None:
    """The CRM import cannot carry a language: its rows used to fall on the
    schema's "fr". Now the agency's language, on the account and the mail."""
    template = await make_journey_template(agency_id=admin.agency_id)
    await make_template_step(template=template)
    request = CaseImportRequest(
        journey_template_id=template.id,
        mapping={"Email": "email", "First": "first_name", "Last": "last_name"},
        csv_text="Email,First,Last\nimported@example.com,Imp,Orted\n",
    )
    report, pending = await CaseImportManager(db_session).run_import(admin, request)
    assert report.created_count == 1
    account = (
        await db_session.execute(select(ExpatUser).where(ExpatUser.email == "imported@example.com"))
    ).scalar_one()
    assert account.preferred_lang == "es"
    [mail] = pending
    assert mail.html is not None and '<html lang="es"' in mail.html


# --- 3. each send family takes the RECORD's language ----------------------------------


async def test_member_invitation_follows_the_agency_record(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agency_es: Agency,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
) -> None:
    case = await make_client_case(agency_id=agency_es.id)
    member = await make_expat_user(
        activated=False, email="member-ru@example.com", preferred_lang="fr"
    )
    await _record(db_session, agency_es.id, member.id, "ru")
    r = await client.post(
        f"/cases/{case.id}/persons",
        headers=agent_headers(admin),
        json={"full_name": "Member Ru", "relationship": "spouse", "email": member.email},
    )
    assert r.status_code == 201, r.text
    [sent] = email.outbox
    assert sent.to == member.email
    assert _html_lang(sent) == "ru"  # the record, not the account's "fr"
    assert "&lang=ru" in sent.body
    # The agency screen states that same language.
    assert r.json()["preferred_lang"] == "ru"


async def test_manual_reminder_goes_out_in_the_record_language(
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    agency_es: Agency,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    make_reminder: MakeReminder,
) -> None:
    principal = await make_expat_user(email="manual@example.com", preferred_lang="fr")
    await _record(db_session, agency_es.id, principal.id, "it")
    case = await make_client_case(agency_id=agency_es.id, principal_expat_user_id=principal.id)
    await make_reminder(case=case, status="approved", scheduled_at=_PAST, message_body="Ciao")
    with sync_session_local() as db:
        dispatch_due_reminders(db, log=lambda _line: None)
    [sent] = email.outbox
    assert sent.subject == reminder_email(agency_es.name, "Ciao", None, "it").subject
    assert _html_lang(sent) == "it"


async def test_manual_reminder_is_frozen_in_the_record_language(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agency_es: Agency,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    make_message_template: MakeMessageTemplate,
    agent_headers: AuthHeaders,
) -> None:
    """At creation (the freeze), a template's variant is picked for the
    reader — the reader's language is now the agency record's."""
    principal = await make_expat_user(email="freeze@example.com", preferred_lang="fr")
    await _record(db_session, agency_es.id, principal.id, "it")
    case = await make_client_case(agency_id=agency_es.id, principal_expat_user_id=principal.id)
    template = await make_message_template(
        agency_id=agency_es.id, body="Bonjour", body_i18n={"it": "Buongiorno"}
    )
    created = await client.post(
        f"/cases/{case.id}/reminders",
        headers=agent_headers(admin),
        json={
            "channel": "mail",
            "scheduled_at": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
            "recipient_type": "expat",
            "message_template_id": str(template.id),
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["message_body"] == "Buongiorno"


async def _stalled_member_step(
    db: AsyncSession,
    make_journey_template: MakeJourneyTemplate,
    make_template_step: MakeTemplateStep,
    make_client_case: MakeClientCase,
    agency: Agency,
    principal: ExpatUser,
    member: ExpatUser,
) -> tuple[ClientCase, uuid.UUID]:
    """A stalled step (21 days) whose only pending piece is the MEMBER's —
    the follow-up routes to her, not to the principal."""
    template: JourneyTemplate = await make_journey_template(agency_id=agency.id)
    step = await make_template_step(
        template=template, name=_ID_STEP["fr"], name_i18n=dict(_ID_STEP)
    )
    case = await make_client_case(
        agency_id=agency.id,
        principal_expat_user_id=principal.id,
        journey_template_id=template.id,
    )
    progress = CaseStepProgress(case_id=case.id, template_step_id=step.id, status="in_progress")
    person_principal = (
        await db.execute(
            select(CasePerson).where(CasePerson.case_id == case.id, CasePerson.kind == "principal")
        )
    ).scalar_one()
    person_member = CasePerson(
        case_id=case.id,
        kind="family",
        full_name="Nadia",
        relationship="spouse",
        expat_user_id=member.id,
    )
    db.add_all([progress, person_member])
    await db.flush()
    db.add_all(
        [
            CaseStepRequirement(
                case_step_progress_id=progress.id,
                person_id=person_principal.id,
                kind="document",
                reference="Passeport",
                scope="each_person",
                status="provided",
            ),
            CaseStepRequirement(
                case_step_progress_id=progress.id,
                person_id=person_member.id,
                kind="document",
                reference="Passeport",
                scope="each_person",
                status="pending",
            ),
        ]
    )
    await db.commit()
    await db.execute(
        update(CaseStepProgress)
        .where(CaseStepProgress.id == progress.id)
        .values(updated_at=datetime.now(UTC) - timedelta(days=21))
    )
    await db.commit()
    return case, progress.id


async def test_auto_follow_up_is_written_for_the_routed_member_in_her_record_language(
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    agency_es: Agency,
    make_journey_template: MakeJourneyTemplate,
    make_template_step: MakeTemplateStep,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
) -> None:
    """Audit n°5: the body used to be frozen in the PRINCIPAL's language with
    the RAW step name, while the envelope went to the member in hers. Now:
    one language, the routed member's (her agency record: ru), and the step
    name translated in it."""
    principal = await make_expat_user(email="pere-auto@example.com", preferred_lang="fr")
    member = await make_expat_user(email="nadia@example.com", preferred_lang="fr")
    await _record(db_session, agency_es.id, member.id, "ru")
    case, _ = await _stalled_member_step(
        db_session,
        make_journey_template,
        make_template_step,
        make_client_case,
        agency_es,
        principal,
        member,
    )
    with sync_session_local() as db:
        assert create_auto_reminders(db, log=lambda _line: None)["created"] >= 1
    reminders = (
        (await db_session.execute(select(Reminder).where(Reminder.case_id == case.id)))
        .scalars()
        .all()
    )
    assert reminders
    for reminder in reminders:
        assert reminder.message_body == auto_reminder_body(
            _ID_STEP["ru"], reminder.auto_threshold_days or 0, "ru"
        )
    with sync_session_local() as db:
        dispatch_due_reminders(db, log=lambda _line: None)
    [sent] = email.outbox
    assert sent.to == member.email  # routed to her…
    assert _html_lang(sent) == "ru"  # …the envelope in her language…
    assert _ID_STEP["ru"] in sent.body  # …and the body too, step name translated
    assert _ID_STEP["fr"] not in sent.body


async def test_grouped_follow_ups_list_translated_step_names(
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    agency_es: Agency,
    make_journey_template: MakeJourneyTemplate,
    make_template_step: MakeTemplateStep,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    make_reminder: MakeReminder,
) -> None:
    principal = await make_expat_user(email="grouped@example.com", preferred_lang="fr")
    await _record(db_session, agency_es.id, principal.id, "ru")
    template = await make_journey_template(agency_id=agency_es.id)
    s1 = await make_template_step(template=template, name="Dépôt", name_i18n={"ru": "Подача"})
    s2 = await make_template_step(template=template, name="Visa", name_i18n={"ru": "Виза"})
    case = await make_client_case(agency_id=agency_es.id, principal_expat_user_id=principal.id)
    p1 = CaseStepProgress(case_id=case.id, template_step_id=s1.id, status="in_progress")
    p2 = CaseStepProgress(case_id=case.id, template_step_id=s2.id, status="in_progress")
    db_session.add_all([p1, p2])
    await db_session.commit()
    for progress in (p1, p2):
        await make_reminder(
            case=case,
            status="approved",
            scheduled_at=_PAST,
            step_progress_id=progress.id,
            auto_threshold_days=20,
        )
    with sync_session_local() as db:
        dispatch_due_reminders(db, log=lambda _line: None)
    [sent] = email.outbox
    assert _html_lang(sent) == "ru"
    assert "· Подача" in sent.body and "· Виза" in sent.body
    assert "Dépôt" not in sent.body


async def test_digest_follows_the_agency_record(
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    agency_es: Agency,
    make_journey_template: MakeJourneyTemplate,
    make_template_step: MakeTemplateStep,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
) -> None:
    principal = await make_expat_user(email="digest-lang@example.com", preferred_lang="en")
    await _record(db_session, agency_es.id, principal.id, "pt")
    case = await make_client_case(agency_id=agency_es.id, principal_expat_user_id=principal.id)
    template = await make_journey_template(agency_id=agency_es.id)
    step = await make_template_step(
        template=template, name="Dépôt", name_i18n={"es": "Depósito", "pt": "Entrega"}
    )
    progress = CaseStepProgress(case_id=case.id, template_step_id=step.id, status="done")
    db_session.add(progress)
    await db_session.flush()
    db_session.add(
        ActivityLog(
            case_id=case.id,
            actor_type="agent",
            actor_id=None,
            action_type="step.completed",
            details={"step_progress_id": str(progress.id)},
        )
    )
    agency_es.settings = {"notification_prefs": {"client": {"progress_digest": "daily"}}}
    await db_session.commit()
    with sync_session_local() as db:
        run_notification_digest(db, log=lambda _line: None, now=datetime.now(UTC))
    [sent] = email.outbox
    assert sent.to == principal.email
    assert _html_lang(sent) == "pt"  # the record, not the account's "en"
    assert "Entrega" in sent.body


# --- 4. what the CLIENT and the AGENCY are served --------------------------------------


async def test_client_space_is_served_the_language_each_agency_set(
    client: AsyncClient,
    db_session: AsyncSession,
    agency_es: Agency,
    make_agency: MakeAgency,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    expat_headers: AuthHeaders,
) -> None:
    """Per dossier: agency A set "pt" for this client → the space opens in
    pt there; agency B set nothing → the account's language."""
    hungarian = await make_agency(name="Magyar", default_language="hu")
    expat = await make_expat_user(email="two-agencies@example.com", preferred_lang="fr")
    await _record(db_session, agency_es.id, expat.id, "pt")
    case_a = await make_client_case(agency_id=agency_es.id, principal_expat_user_id=expat.id)
    case_b = await make_client_case(agency_id=hungarian.id, principal_expat_user_id=expat.id)
    headers = expat_headers(expat)

    listing = (await client.get("/expat/cases", headers=headers)).json()
    langs = {item["id"]: item["client_lang"] for item in listing}
    assert langs == {str(case_a.id): "pt", str(case_b.id): "fr"}
    detail = (await client.get(f"/expat/cases/{case_a.id}", headers=headers)).json()
    assert detail["client_lang"] == "pt"

    # An account language outside the product falls on English (NOTIF-1),
    # not on the agency's.
    await db_session.execute(
        update(ExpatUser).where(ExpatUser.id == expat.id).values(preferred_lang="de")
    )
    await db_session.commit()
    detail_b = (await client.get(f"/expat/cases/{case_b.id}", headers=headers)).json()
    assert detail_b["client_lang"] == "en"


async def test_agency_screens_state_the_language_the_mails_use(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agency_es: Agency,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
) -> None:
    principal = await make_expat_user(email="screen@example.com", preferred_lang="fr")
    profile = await _record(db_session, agency_es.id, principal.id, "pt")
    case = await make_client_case(agency_id=agency_es.id, principal_expat_user_id=principal.id)
    headers = agent_headers(admin)

    listing = (await client.get("/cases", headers=headers)).json()
    [item] = [i for i in listing["items"] if i["id"] == str(case.id)]
    assert item["principal"]["preferred_lang"] == "pt"
    detail = (await client.get(f"/cases/{case.id}", headers=headers)).json()
    [person] = [p for p in detail["persons"] if p["kind"] == "principal"]
    assert person["preferred_lang"] == "pt"
    fiche = (await client.get(f"/client-profiles/{profile.id}", headers=headers)).json()
    assert fiche["preferred_lang"] == "pt"


async def test_language_filters_match_the_language_the_listing_shows(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agency_es: Agency,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
) -> None:
    """The listing SERVES the effective language (the agency's record first):
    the simple filter and the advanced principal_preferred_lang must select
    on that same value, or « pt » shows in the column and the « pt » filter
    misses the case."""
    import json
    from urllib.parse import quote

    recorded = await make_expat_user(email="rec@example.com", preferred_lang="fr")
    await _record(db_session, agency_es.id, recorded.id, "pt")
    plain = await make_expat_user(email="plain@example.com", preferred_lang="pt")
    french = await make_expat_user(email="fr@example.com", preferred_lang="fr")
    case_rec = await make_client_case(agency_id=agency_es.id, principal_expat_user_id=recorded.id)
    case_plain = await make_client_case(agency_id=agency_es.id, principal_expat_user_id=plain.id)
    case_fr = await make_client_case(agency_id=agency_es.id, principal_expat_user_id=french.id)
    headers = agent_headers(admin)

    async def ids(query: str) -> set[str]:
        response = await client.get(f"/cases?{query}", headers=headers)
        assert response.status_code == 200, response.text
        return {item["id"] for item in response.json()["items"]}

    ours = {str(case_rec.id), str(case_plain.id), str(case_fr.id)}
    assert await ids("preferred_lang=pt") & ours == {str(case_rec.id), str(case_plain.id)}
    assert await ids("preferred_lang=fr") & ours == {str(case_fr.id)}
    tree = json.dumps(
        {
            "conditions": [{"field": "principal_preferred_lang", "operator": "eq", "value": "pt"}],
            "groups": [],
        }
    )
    assert await ids(f"filters={quote(tree)}") & ours == {str(case_rec.id), str(case_plain.id)}
