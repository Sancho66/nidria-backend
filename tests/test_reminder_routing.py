"""Rappels ROUTÉS vers la personne concernée (2026-07-18, promesse
Nicolas) : l'exigence qui cible UN membre avec accès part au membre (dans
SA langue) ; membre sans accès ou étape générale -> le principal, jamais
les deux ; les relances auto suivent par construction (même writer de
dispatch) ; l'écran d'approbation dit le destinataire réel."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, sessionmaker

from shared.models.agent import Agent
from shared.models.case_person import CasePerson
from shared.models.case_step_progress import CaseStepProgress
from shared.models.case_step_requirement import CaseStepRequirement
from shared.models.client_case import ClientCase
from shared.models.expat_user import ExpatUser
from shared.models.rbac import Role
from src.core import email
from src.reminders.reminders_jobs import dispatch_due_reminders
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser
from tests.plugins.journey_plugin import MakeJourneyTemplate, MakeTemplateStep
from tests.plugins.reminder_plugin import MakeReminder

_PAST = datetime.now(UTC) - timedelta(hours=1)


def _dispatch(sync_session_local: sessionmaker[Session]) -> dict:
    with sync_session_local() as db:
        return dispatch_due_reminders(db, log=lambda _line: None)


@pytest_asyncio.fixture
async def principal(make_expat_user: MakeExpatUser) -> ExpatUser:
    return await make_expat_user(email="pere@example.com", first_name="Paul", last_name="Martin")


async def _routed_setup(
    db: AsyncSession,
    make_journey_template: MakeJourneyTemplate,
    make_template_step: MakeTemplateStep,
    case: ClientCase,
    principal: ExpatUser,
    *,
    member_expat: ExpatUser | None,
    principal_pending: bool = False,
) -> uuid.UUID:
    """Une étape IN_PROGRESS avec exigences matérialisées : celle du
    principal PROVIDED (sauf principal_pending), celle du membre pending.
    Retourne le step_progress_id."""
    template = await make_journey_template(agency_id=case.agency_id)
    step = await make_template_step(template=template, name="Pièce d'identité")
    progress = CaseStepProgress(case_id=case.id, template_step_id=step.id, status="in_progress")
    # le principal existe deja (cree avec le dossier) — on le lit
    from sqlalchemy import select

    person_principal = (
        await db.execute(
            select(CasePerson).where(CasePerson.case_id == case.id, CasePerson.kind == "principal")
        )
    ).scalar_one()
    person_member = CasePerson(
        case_id=case.id,
        kind="family",
        full_name="Claire Martin",
        relationship="fille",
        expat_user_id=member_expat.id if member_expat is not None else None,
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
                status="pending" if principal_pending else "provided",
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
    return progress.id


async def test_targeted_member_gets_the_reminder_alone(
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    make_journey_template: MakeJourneyTemplate,
    make_template_step: MakeTemplateStep,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    make_reminder: MakeReminder,
    principal: ExpatUser,
) -> None:
    """La pièce attendue est celle de Claire -> le rappel part à Claire,
    SEULE (le père ne reçoit rien)."""
    member = await make_expat_user(email="claire@example.com", first_name="Claire")
    case = await make_client_case(principal_expat_user_id=principal.id)
    pid = await _routed_setup(
        db_session,
        make_journey_template,
        make_template_step,
        case,
        principal,
        member_expat=member,
    )
    await make_reminder(case=case, status="approved", scheduled_at=_PAST, step_progress_id=pid)
    email.outbox.clear()
    stats = _dispatch(sync_session_local)
    assert stats["sent"] == 1
    [sent] = email.outbox
    assert sent.to == "claire@example.com"  # le membre, jamais le principal


async def test_member_without_access_falls_back_to_principal(
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    make_journey_template: MakeJourneyTemplate,
    make_template_step: MakeTemplateStep,
    make_client_case: MakeClientCase,
    make_reminder: MakeReminder,
    principal: ExpatUser,
) -> None:
    case = await make_client_case(principal_expat_user_id=principal.id)
    pid = await _routed_setup(
        db_session,
        make_journey_template,
        make_template_step,
        case,
        principal,
        member_expat=None,  # Claire sans compte
    )
    await make_reminder(case=case, status="approved", scheduled_at=_PAST, step_progress_id=pid)
    email.outbox.clear()
    _dispatch(sync_session_local)
    [sent] = email.outbox
    assert sent.to == "pere@example.com"  # fallback principal, jamais les deux


async def test_general_step_goes_to_principal(
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    make_journey_template: MakeJourneyTemplate,
    make_template_step: MakeTemplateStep,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    make_reminder: MakeReminder,
    principal: ExpatUser,
) -> None:
    """Deux personnes concernées (multi-personnes) -> le principal, comme
    aujourd'hui. Et un rappel SANS étape -> le principal aussi."""
    member = await make_expat_user(email="claire2@example.com")
    case = await make_client_case(principal_expat_user_id=principal.id)
    pid = await _routed_setup(
        db_session,
        make_journey_template,
        make_template_step,
        case,
        principal,
        member_expat=member,
        principal_pending=True,  # les DEUX exigences pending -> multi-personnes
    )
    await make_reminder(case=case, status="approved", scheduled_at=_PAST, step_progress_id=pid)
    await make_reminder(case=case, status="approved", scheduled_at=_PAST)  # sans étape
    email.outbox.clear()
    _dispatch(sync_session_local)
    assert {m.to for m in email.outbox} == {"pere@example.com"}
    assert len(email.outbox) == 2


async def test_member_language_is_hers(
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    make_journey_template: MakeJourneyTemplate,
    make_template_step: MakeTemplateStep,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    make_reminder: MakeReminder,
    principal: ExpatUser,
) -> None:
    """Claire est anglophone, le père francophone : le mail part dans LA
    langue de Claire (le resolve par utilisateur, pas celle du principal)."""
    member = await make_expat_user(email="claire-en@example.com", preferred_lang="en")
    case = await make_client_case(principal_expat_user_id=principal.id)
    pid = await _routed_setup(
        db_session,
        make_journey_template,
        make_template_step,
        case,
        principal,
        member_expat=member,
    )
    await make_reminder(case=case, status="approved", scheduled_at=_PAST, step_progress_id=pid)
    email.outbox.clear()
    _dispatch(sync_session_local)
    [sent] = email.outbox
    assert sent.to == "claire-en@example.com"
    assert "Reminder" in sent.subject  # gabarit EN, pas "Rappel"


async def test_auto_reminder_routes_the_same(
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    make_journey_template: MakeJourneyTemplate,
    make_template_step: MakeTemplateStep,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    make_reminder: MakeReminder,
    principal: ExpatUser,
) -> None:
    """Une relance auto (J+20) approuvée est un Reminder comme un autre :
    le MÊME writer de dispatch la route vers le membre ciblé."""
    member = await make_expat_user(email="claire-auto@example.com")
    case = await make_client_case(principal_expat_user_id=principal.id)
    pid = await _routed_setup(
        db_session,
        make_journey_template,
        make_template_step,
        case,
        principal,
        member_expat=member,
    )
    await make_reminder(
        case=case,
        status="approved",
        scheduled_at=_PAST,
        step_progress_id=pid,
        auto_threshold_days=20,
    )
    email.outbox.clear()
    _dispatch(sync_session_local)
    [sent] = email.outbox
    assert sent.to == "claire-auto@example.com"


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


@pytest.mark.usefixtures("rbac_baseline")
async def test_approval_screen_says_the_real_recipient(
    client: AsyncClient,
    db_session: AsyncSession,
    make_journey_template: MakeJourneyTemplate,
    make_template_step: MakeTemplateStep,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    make_reminder: MakeReminder,
    principal: ExpatUser,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Le contrat expose le destinataire résolu : « sera envoyé à Claire
    Martin » — et le principal pour un rappel général."""
    member = await make_expat_user(email="claire-ui@example.com")
    case = await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=principal.id)
    pid = await _routed_setup(
        db_session,
        make_journey_template,
        make_template_step,
        case,
        principal,
        member_expat=member,
    )
    targeted = await make_reminder(
        case=case, status="to_approve", scheduled_at=_PAST, step_progress_id=pid
    )
    general = await make_reminder(case=case, status="to_approve", scheduled_at=_PAST)
    h = agent_headers(admin)
    r1 = (await client.get(f"/reminders/{targeted.id}", headers=h)).json()
    assert r1["resolved_recipient"] == "Claire Martin"
    r2 = (await client.get(f"/reminders/{general.id}", headers=h)).json()
    assert r2["resolved_recipient"] == "Paul Martin"
