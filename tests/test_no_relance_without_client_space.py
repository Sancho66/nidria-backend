"""Ne pas relancer un client qui n'est jamais entré (suite de NID-23).

Constat : les relances auto et les digests partaient vers des clients dont
l'espace n'a jamais été activé — on les invitait à consulter un espace
inaccessible (5 dossiers en prod le 24/07). Les deux passes CLIENT excluent
désormais ces destinataires, via la MÊME dérivation que le badge de la fiche
(`client_space_is_active`), et le comptage l'écrit dans les stats du run :
une relance qui n'a pas lieu ne disparaît pas en silence.

Le pass PRESTATAIRE n'est pas concerné : un prestataire n'a pas d'espace
client, sa relance ne dépend d'aucune activation.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, sessionmaker

from shared.models.activity import ActivityLog
from shared.models.agent import Agent
from shared.models.case_step_progress import CaseStepProgress
from shared.models.client_case import ClientCase
from shared.models.expat_user import ExpatUser
from shared.models.rbac import Role
from shared.models.reminder import Reminder
from src.core import email
from src.digest.digest_job import run_notification_digest
from src.reminders.reminders_jobs import create_auto_reminders
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase, MakeExternalContact
from tests.plugins.expat_plugin import MakeExpatUser

pytestmark = pytest.mark.usefixtures("rbac_baseline")


@pytest_asyncio.fixture
async def manager(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["case_manager"])


def _run_auto(session_local: sessionmaker[Session]) -> dict:
    with session_local() as db:
        return create_auto_reminders(db, log=lambda _: None)


async def _stalled_case(
    client: AsyncClient,
    db: AsyncSession,
    agent: Agent,
    make_client_case: MakeClientCase,
    principal: ExpatUser,
    headers: dict[str, str],
    days: int = 25,
) -> ClientCase:
    """Un dossier de cet agent, parcours assigné, étape immobile depuis
    `days` jours — donc candidat au seuil J+20."""
    template = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()
    await client.post(
        f"/journeys/{template['id']}/steps", headers=headers, json={"name": "Etape immobile"}
    )
    case = await make_client_case(agency_id=agent.agency_id, principal_expat_user_id=principal.id)
    await client.post(
        f"/cases/{case.id}/journey",
        headers=headers,
        json={"journey_template_id": template["id"]},
    )
    await db.execute(
        update(CaseStepProgress)
        .where(CaseStepProgress.case_id == case.id)
        .values(updated_at=datetime.now(UTC) - timedelta(days=days))
    )
    await db.commit()
    return case


# --- (1) relances auto : le client non activé n'est jamais relancé -------------------


async def test_auto_reminders_skip_a_client_who_never_activated(
    client: AsyncClient,
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    manager: Agent,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    principal = await make_expat_user(activated=False, email="jamais-entre@example.com")
    case = await _stalled_case(
        client, db_session, manager, make_client_case, principal, agent_headers(manager)
    )

    stats = _run_auto(sync_session_local)

    assert stats["created"] == 0
    assert stats["skipped_no_client_space"] == 1  # compté, jamais silencieux
    rows = (
        (await db_session.execute(select(Reminder).where(Reminder.case_id == case.id)))
        .scalars()
        .all()
    )
    assert rows == []


async def test_auto_reminders_run_normally_for_an_activated_client(
    client: AsyncClient,
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    manager: Agent,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    principal = await make_expat_user(activated=True, email="entre@example.com")
    case = await _stalled_case(
        client, db_session, manager, make_client_case, principal, agent_headers(manager)
    )

    stats = _run_auto(sync_session_local)

    assert stats["created"] == 1
    assert stats["skipped_no_client_space"] == 0
    reminder = (
        await db_session.execute(select(Reminder).where(Reminder.case_id == case.id))
    ).scalar_one()
    assert reminder.status == "to_approve"  # l'invariant d'Eloïse, intact
    assert reminder.auto_threshold_days == 20


async def test_relances_resume_once_the_client_activates(
    client: AsyncClient,
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    manager: Agent,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """Le cycle complet : lien expiré donc aucune relance, l'agence renvoie
    l'invitation, le client active — et les relances reprennent sans que rien
    d'autre ne bouge (même dossier, même étape immobile, même seuil)."""
    headers = agent_headers(manager)
    principal = await make_expat_user(activated=False, email="renvoi@example.com")
    case = await _stalled_case(client, db_session, manager, make_client_case, principal, headers)

    assert _run_auto(sync_session_local)["created"] == 0

    # Renvoi d'invitation par l'agence (le geste NID-23), puis activation.
    person_id = (
        await db_session.execute(
            select(ClientCase.principal_expat_user_id).where(ClientCase.id == case.id)
        )
    ).scalar_one()
    assert person_id == principal.id
    await db_session.execute(
        update(ExpatUser).where(ExpatUser.id == principal.id).values(activated_at=datetime.now(UTC))
    )
    await db_session.commit()

    stats = _run_auto(sync_session_local)
    assert stats["created"] == 1
    assert stats["skipped_no_client_space"] == 0
    log = (
        await db_session.execute(
            select(ActivityLog).where(
                ActivityLog.case_id == case.id,
                ActivityLog.action_type == "reminder.auto_created",
            )
        )
    ).scalar_one()
    assert log.actor_type == "system"


# --- (2) le pass prestataire n'est PAS concerné --------------------------------------


async def test_provider_follow_up_is_untouched_by_the_client_space_rule(
    client: AsyncClient,
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    manager: Agent,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    make_external_contact: MakeExternalContact,
    agent_headers: AuthHeaders,
) -> None:
    """Un prestataire n'a pas d'espace client : sa relance part même quand le
    client du dossier n'a jamais activé le sien. Une seule relance créée —
    celle du prestataire, pas celle du client."""
    from shared.models.case_step_participant import CaseStepParticipant

    principal = await make_expat_user(activated=False, email="client-absent@example.com")
    case = await _stalled_case(
        client, db_session, manager, make_client_case, principal, agent_headers(manager)
    )
    contact = await make_external_contact(case=case, email="notaire@example.com")
    progress_id = (
        await db_session.execute(
            select(CaseStepProgress.id).where(CaseStepProgress.case_id == case.id)
        )
    ).scalar_one()
    db_session.add(
        CaseStepParticipant(
            case_step_progress_id=progress_id,
            type="external",
            external_id=contact.id,
            role="executant",
        )
    )
    await db_session.commit()

    stats = _run_auto(sync_session_local)

    assert stats["created"] == 1
    assert stats["skipped_no_client_space"] == 1
    reminder = (
        await db_session.execute(select(Reminder).where(Reminder.case_id == case.id))
    ).scalar_one()
    assert reminder.recipient_type == "external"
    assert reminder.recipient_external_id == contact.id


# --- (3) digest client : même règle, même trace --------------------------------------


MONDAY = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)  # le digest weekly tire le lundi


async def _digest_case(
    db: AsyncSession,
    make_client_case: MakeClientCase,
    principal: ExpatUser,
    agency_id: uuid.UUID,
) -> ClientCase:
    """Un dossier avec un évènement digestible DANS la fenêtre du run."""
    case = await make_client_case(agency_id=agency_id, principal_expat_user_id=principal.id)
    db.add(
        ActivityLog(
            case_id=case.id,
            actor_type="agent",
            actor_id=None,
            action_type="document.validated",
            details={"new": "ok"},
            created_at=MONDAY - timedelta(hours=1),
        )
    )
    await db.commit()
    return case


async def test_digest_skips_recipients_without_an_active_space(
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    manager: Agent,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
) -> None:
    absent = await make_expat_user(activated=False, email="digest-absent@example.com")
    present = await make_expat_user(activated=True, email="digest-present@example.com")
    await _digest_case(db_session, make_client_case, absent, manager.agency_id)
    await _digest_case(db_session, make_client_case, present, manager.agency_id)
    email.outbox.clear()

    with sync_session_local() as db:
        stats = run_notification_digest(db, log=lambda _: None, now=MONDAY)

    assert stats["skipped_no_client_space"] == 1
    assert [m.to for m in email.outbox] == [present.email]
    assert stats["mails"] == 1
