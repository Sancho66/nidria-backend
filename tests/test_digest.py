"""Le digest d'avancement (dernière pièce notifications) : weekly le bon
jour, daily chaque jour, le curseur (jamais deux fois, jamais de mail
vide), la liste blanche stricte, la langue du membre, les exclusions
(is_internal / off / dossier fermé), et l'effectif servi au front."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, sessionmaker

from shared.models.activity import ActivityLog
from shared.models.agency import Agency
from shared.models.case_person import CasePerson
from shared.models.case_step_progress import CaseStepProgress
from shared.models.client_case import ClientCase
from shared.models.expat_user import ExpatUser
from shared.models.rbac import Role
from src.core import email
from src.digest.digest_job import run_notification_digest
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser
from tests.plugins.journey_plugin import MakeJourneyTemplate, MakeTemplateStep

MONDAY = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)  # un lundi
TUESDAY = MONDAY + timedelta(days=1)


def _run(sync_session_local: sessionmaker[Session], now: datetime) -> dict:
    with sync_session_local() as db:
        return run_notification_digest(db, log=lambda _l: None, now=now)


@pytest_asyncio.fixture
async def principal(make_expat_user: MakeExpatUser) -> ExpatUser:
    return await make_expat_user(email="digest-pere@example.com", preferred_lang="fr")


async def _set_digest(db: AsyncSession, agency_id, mode: str) -> None:
    agency = await db.get(Agency, agency_id)
    assert agency is not None
    agency.settings = {
        **(agency.settings or {}),
        "notification_prefs": {"client": {"progress_digest": mode}},
    }
    await db.commit()


async def _case_with_activity(
    db: AsyncSession,
    make_journey_template: MakeJourneyTemplate,
    make_template_step: MakeTemplateStep,
    make_client_case: MakeClientCase,
    principal: ExpatUser,
    *,
    agency_id=None,
    when: datetime | None = None,
) -> ClientCase:
    """Un dossier avec 1 étape terminée + 1 démarrée + 1 doc validé + du
    BRUIT INTERNE (note, action nominative) dans la fenêtre."""
    case = await make_client_case(
        **({"agency_id": agency_id} if agency_id else {}),
        principal_expat_user_id=principal.id,
    )
    template = await make_journey_template(agency_id=case.agency_id)
    s1 = await make_template_step(template=template, name="Depot du dossier")
    s2 = await make_template_step(template=template, name="Traduction")
    p1 = CaseStepProgress(case_id=case.id, template_step_id=s1.id, status="done")
    p2 = CaseStepProgress(case_id=case.id, template_step_id=s2.id, status="in_progress")
    db.add_all([p1, p2])
    await db.flush()
    when = when or datetime.now(UTC)
    events = [
        ActivityLog(
            case_id=case.id,
            actor_type="agent",
            actor_id=None,
            action_type="step.completed",
            details={"step_progress_id": str(p1.id)},
        ),
        ActivityLog(
            case_id=case.id,
            actor_type="agent",
            actor_id=None,
            action_type="step.started",
            details={"step_progress_id": str(p2.id)},
        ),
        ActivityLog(
            case_id=case.id,
            actor_type="agent",
            actor_id=None,
            action_type="document.validated",
            details={"document_id": str(uuid.uuid4()), "old": "incomplete", "new": "ok"},
        ),
        # LE BRUIT : jamais dans le digest (liste blanche)
        ActivityLog(
            case_id=case.id,
            actor_type="agent",
            actor_id=None,
            action_type="case.note_added",
            details={"body": "note interne sensible"},
        ),
        ActivityLog(
            case_id=case.id,
            actor_type="agent",
            actor_id=None,
            action_type="reminder.sent",
            details={"reminder_id": str(uuid.uuid4())},
        ),
    ]
    db.add_all(events)
    await db.commit()
    for event in events:
        await db.execute(
            update(ActivityLog).where(ActivityLog.id == event.id).values(created_at=when)
        )
    await db.commit()
    return case


async def test_weekly_sends_only_on_monday_and_whitelist_holds(
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    make_journey_template: MakeJourneyTemplate,
    make_template_step: MakeTemplateStep,
    make_client_case: MakeClientCase,
    principal: ExpatUser,
) -> None:
    case = await _case_with_activity(
        db_session,
        make_journey_template,
        make_template_step,
        make_client_case,
        principal,
        when=MONDAY - timedelta(days=2),
    )
    await _set_digest(db_session, case.agency_id, "weekly")
    # mardi : rien (weekly attend lundi)
    email.outbox.clear()
    _run(sync_session_local, TUESDAY - timedelta(days=7))
    assert [m for m in email.outbox if m.to == principal.email] == []
    # lundi : LE digest — et la liste blanche tient
    email.outbox.clear()
    stats = _run(sync_session_local, MONDAY)
    assert stats["mails"] >= 1
    [sent] = [m for m in email.outbox if m.to == principal.email]
    assert "Depot du dossier" in sent.body and "Traduction" in sent.body
    assert "1 document" in sent.body or "document(s)" in sent.body
    assert "note interne sensible" not in sent.body  # le bruit ne fuit JAMAIS
    assert "reminder" not in sent.body.lower()


async def test_daily_and_cursor_no_duplicate_no_empty(
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    make_journey_template: MakeJourneyTemplate,
    make_template_step: MakeTemplateStep,
    make_client_case: MakeClientCase,
    principal: ExpatUser,
) -> None:
    case = await _case_with_activity(
        db_session,
        make_journey_template,
        make_template_step,
        make_client_case,
        principal,
        when=TUESDAY - timedelta(hours=3),
    )
    await _set_digest(db_session, case.agency_id, "daily")
    # daily envoie un mardi
    email.outbox.clear()
    stats = _run(sync_session_local, TUESDAY)
    assert stats["mails"] == 1
    # re-run le lendemain SANS activite : ni doublon ni mail vide
    email.outbox.clear()
    stats = _run(sync_session_local, TUESDAY + timedelta(days=1))
    assert stats["mails"] == 0
    assert email.outbox == []


async def test_member_language_and_own_copy(
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    make_journey_template: MakeJourneyTemplate,
    make_template_step: MakeTemplateStep,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    principal: ExpatUser,
) -> None:
    """Claire (EN, avec acces) recoit SA copie en anglais ; le pere en FR."""
    case = await _case_with_activity(
        db_session,
        make_journey_template,
        make_template_step,
        make_client_case,
        principal,
        when=TUESDAY - timedelta(hours=2),
    )
    claire = await make_expat_user(email="digest-claire@example.com", preferred_lang="en")
    db_session.add(
        CasePerson(case_id=case.id, kind="family", full_name="Claire", expat_user_id=claire.id)
    )
    await db_session.commit()
    await _set_digest(db_session, case.agency_id, "daily")
    email.outbox.clear()
    _run(sync_session_local, TUESDAY)
    pere = next(m for m in email.outbox if m.to == principal.email)
    claire_mail = next(m for m in email.outbox if m.to == "digest-claire@example.com")
    assert "avancé" in pere.subject  # FR
    assert "moved forward" in claire_mail.subject  # EN — SA langue
    assert "Completed:" in claire_mail.body


async def test_exclusions_internal_off_closed(
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    make_journey_template: MakeJourneyTemplate,
    make_template_step: MakeTemplateStep,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    principal: ExpatUser,
) -> None:
    # interne : jamais
    case_a = await _case_with_activity(
        db_session,
        make_journey_template,
        make_template_step,
        make_client_case,
        principal,
        when=TUESDAY - timedelta(hours=2),
    )
    await _set_digest(db_session, case_a.agency_id, "daily")
    await db_session.execute(
        update(Agency).where(Agency.id == case_a.agency_id).values(is_internal=True)
    )
    # off : rien
    p2 = await make_expat_user(email="digest-off@example.com")
    case_b = await _case_with_activity(
        db_session,
        make_journey_template,
        make_template_step,
        make_client_case,
        p2,
        when=TUESDAY - timedelta(hours=2),
    )
    await _set_digest(db_session, case_b.agency_id, "off")
    # dossier ferme : rien
    p3 = await make_expat_user(email="digest-closed@example.com")
    case_c = await _case_with_activity(
        db_session,
        make_journey_template,
        make_template_step,
        make_client_case,
        p3,
        when=TUESDAY - timedelta(hours=2),
    )
    await _set_digest(db_session, case_c.agency_id, "daily")
    await db_session.execute(
        update(ClientCase).where(ClientCase.id == case_c.id).values(status="closed")
    )
    await db_session.commit()
    email.outbox.clear()
    stats = _run(sync_session_local, TUESDAY)
    assert stats["mails"] == 0
    assert email.outbox == []


@pytest.mark.usefixtures("rbac_baseline")
async def test_effective_prefs_served_on_agencies_me(
    client: AsyncClient,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """Le front supprime son miroir CLIENT_DEFAULTS : GET /agencies/me sert
    l'effectif (defauts fusionnes)."""
    admin = await make_agent(role=system_roles["admin"])
    body = (await client.get("/agencies/me", headers=agent_headers(admin))).json()
    assert body["notification_prefs"] == {
        "requirement_request": "on",
        "comments": "grouped",
        "reminders": "on",
        "progress_digest": "weekly",
    }
