"""FEATURE 3 battery. THE INVARIANT carries its name below: a due
TO_APPROVE crosses a dispatch tick untouched, whoever created it — that
part never moves, and a reminder a human WRITES is always born
TO_APPROVE. What the 13/08 lot changed is who decides for the AUTOMATIC
follow-ups: the agency, in its settings, default « elles partent seules »
(97 waiting in prod, oldest 17 days, zero ever sent). Mocks everywhere,
zero real sends."""

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
from shared.models.case_step_participant import CaseStepParticipant
from shared.models.case_step_progress import CaseStepProgress
from shared.models.case_step_requirement import CaseStepRequirement
from shared.models.client_case import ClientCase
from shared.models.expat_user import ExpatUser
from shared.models.journey import JourneyTemplate
from shared.models.rbac import Role
from shared.models.reminder import Reminder
from src.core import email
from src.core.rbac.permissions import Permission
from src.reminders.reminders_jobs import create_auto_reminders, dispatch_due_reminders
from tests.plugins.agency_plugin import MakeAgency
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase, MakeExternalContact
from tests.plugins.expat_plugin import MakeExpatUser
from tests.plugins.rbac_plugin import MakeRole
from tests.plugins.reminder_plugin import MakeMessageTemplate, MakeReminder

_NOW = datetime.now(UTC)
_PAST = _NOW - timedelta(hours=1)
_FUTURE = _NOW + timedelta(days=3)


@pytest.fixture
def rem_client(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def manager_agent(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["case_manager"])


@pytest_asyncio.fixture
async def case(manager_agent: Agent, make_client_case: MakeClientCase) -> ClientCase:
    return await make_client_case(agency_id=manager_agent.agency_id)


def _run_dispatch(session_local: sessionmaker[Session], dry_run: bool = False) -> dict:
    with session_local() as db:
        return dispatch_due_reminders(db, log=lambda _: None, dry_run=dry_run)


def _run_auto(session_local: sessionmaker[Session]) -> dict:
    with session_local() as db:
        return create_auto_reminders(db, log=lambda _: None)


# --- creation + interpolation -----------------------------------------------------


async def test_create_from_template_interpolates_client_name(
    rem_client: AsyncClient,
    manager_agent: Agent,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    make_message_template: MakeMessageTemplate,
    agent_headers: AuthHeaders,
) -> None:
    expat = await make_expat_user(first_name="Jean", last_name="Martin")
    case = await make_client_case(
        agency_id=manager_agent.agency_id, principal_expat_user_id=expat.id
    )
    template = await make_message_template(
        agency_id=manager_agent.agency_id, body="Bonjour {client_name}, des nouvelles ?"
    )
    response = await rem_client.post(
        f"/cases/{case.id}/reminders",
        headers=agent_headers(manager_agent),
        json={
            "channel": "mail",
            "scheduled_at": _FUTURE.isoformat(),
            "recipient_type": "expat",
            "message_template_id": str(template.id),
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["message_body"] == "Bonjour Jean Martin, des nouvelles ?"
    assert body["status"] == "to_approve"


async def test_days_left_projected_at_scheduled_at(
    rem_client: AsyncClient,
    manager_agent: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """estimated_days=15, step started today, send planned at J+10 →
    the approved text says 5 — exact AT SEND TIME, not at creation."""
    headers = agent_headers(manager_agent)
    template = (await rem_client.post("/journeys", headers=headers, json={"name": "T"})).json()
    step = (
        await rem_client.post(
            f"/journeys/{template['id']}/steps",
            headers=headers,
            json={"name": "Visa", "estimated_days": 15},
        )
    ).json()
    case = await make_client_case(agency_id=manager_agent.agency_id)
    timeline = (
        await rem_client.post(
            f"/cases/{case.id}/journey",
            headers=headers,
            json={"journey_template_id": template["id"]},
        )
    ).json()
    progress_id = timeline[0]["id"]
    assert step["id"] == timeline[0]["template_step_id"]
    started = await rem_client.patch(
        f"/cases/{case.id}/steps/{progress_id}", headers=headers, json={"status": "in_progress"}
    )
    assert started.status_code == 200

    response = await rem_client.post(
        f"/cases/{case.id}/reminders",
        headers=headers,
        json={
            "channel": "mail",
            "scheduled_at": (_NOW + timedelta(days=10)).isoformat(),
            "recipient_type": "expat",
            "step_progress_id": progress_id,
            "message_body": "Il reste {days_left} jours pour l'etape {step_name}.",
        },
    )
    assert response.status_code == 201
    assert response.json()["message_body"] == "Il reste 5 jours pour l'etape Visa."


async def test_unsolvable_variable_422_names_it(
    rem_client: AsyncClient, manager_agent: Agent, case: ClientCase, agent_headers: AuthHeaders
) -> None:
    response = await rem_client.post(
        f"/cases/{case.id}/reminders",
        headers=agent_headers(manager_agent),
        json={
            "channel": "mail",
            "scheduled_at": _FUTURE.isoformat(),
            "recipient_type": "expat",
            "message_body": "Etape {step_name} en attente.",
        },
    )
    assert response.status_code == 422
    assert "step_name" in response.json()["detail"]


async def test_recipient_validations(
    rem_client: AsyncClient,
    manager_agent: Agent,
    make_client_case: MakeClientCase,
    make_external_contact: MakeExternalContact,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(manager_agent)
    case = await make_client_case(agency_id=manager_agent.agency_id)
    other_case = await make_client_case(agency_id=manager_agent.agency_id)
    foreign_contact = await make_external_contact(case=other_case, email="x@y.com")
    no_mail_contact = await make_external_contact(case=case, email=None)

    base = {
        "channel": "mail",
        "scheduled_at": _FUTURE.isoformat(),
        "recipient_type": "external",
        "message_body": "Hello",
    }
    foreign = await rem_client.post(
        f"/cases/{case.id}/reminders",
        headers=headers,
        json={**base, "recipient_external_id": str(foreign_contact.id)},
    )
    assert foreign.status_code == 422
    no_email = await rem_client.post(
        f"/cases/{case.id}/reminders",
        headers=headers,
        json={**base, "recipient_external_id": str(no_mail_contact.id)},
    )
    assert no_email.status_code == 422


# --- THE INVARIANT ------------------------------------------------------------------


async def test_invariant_unapproved_reminder_never_sent_by_a_tick(
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    rbac_baseline: None,
    make_client_case: MakeClientCase,
    make_reminder: MakeReminder,
) -> None:
    """Eloïse's promise: a DUE reminder that nobody approved crosses a
    dispatch tick and NOTHING goes out."""
    case = await make_client_case()
    reminder = await make_reminder(case=case, status="to_approve", scheduled_at=_PAST)

    stats = _run_dispatch(sync_session_local)

    assert stats == {"due": 0, "sent": 0, "skipped_step_done": 0}
    await db_session.refresh(reminder)
    assert reminder.status == "to_approve"
    assert email.outbox == []


async def test_approved_due_is_dispatched_future_is_not(
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    rbac_baseline: None,
    make_client_case: MakeClientCase,
    make_reminder: MakeReminder,
) -> None:
    case = await make_client_case()
    due = await make_reminder(case=case, status="approved", scheduled_at=_PAST)
    future = await make_reminder(case=case, status="approved", scheduled_at=_FUTURE)

    stats = _run_dispatch(sync_session_local)
    assert stats == {"due": 1, "sent": 1, "skipped_step_done": 0}
    await db_session.refresh(due)
    await db_session.refresh(future)
    assert due.status == "sent"
    assert future.status == "approved"
    assert len(email.outbox) == 1
    # The interpolated message_body lands in BOTH multipart parts.
    sent_mail = email.outbox[0]
    assert due.message_body in sent_mail.body
    assert sent_mail.html is not None and due.message_body in sent_mail.html

    log = (
        await db_session.execute(
            select(ActivityLog).where(ActivityLog.action_type == "reminder.sent")
        )
    ).scalar_one()
    assert log.actor_type == "system"

    # Idempotence: a second tick is a no-op.
    assert _run_dispatch(sync_session_local) == {"due": 0, "sent": 0, "skipped_step_done": 0}
    assert len(email.outbox) == 1


async def test_in_app_dispatch_sends_no_mail(
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    rbac_baseline: None,
    make_client_case: MakeClientCase,
    make_reminder: MakeReminder,
) -> None:
    case = await make_client_case()
    reminder = await make_reminder(
        case=case, status="approved", scheduled_at=_PAST, channel="in_app"
    )
    _run_dispatch(sync_session_local)
    await db_session.refresh(reminder)
    assert reminder.status == "sent"  # the sent reminder IS the notif
    assert email.outbox == []


# --- whatsapp: manual send only -------------------------------------------------------


async def test_whatsapp_skipped_by_dispatcher_then_mark_sent(
    rem_client: AsyncClient,
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    manager_agent: Agent,
    case: ClientCase,
    make_reminder: MakeReminder,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(manager_agent)
    reminder = await make_reminder(
        case=case, status="approved", scheduled_at=_PAST, channel="whatsapp"
    )
    _run_dispatch(sync_session_local)
    await db_session.refresh(reminder)
    assert reminder.status == "approved"  # dispatcher never touches whatsapp

    # The agent reads the rendered text (GET mutates nothing)…
    detail = await rem_client.get(f"/reminders/{reminder.id}", headers=headers)
    assert detail.status_code == 200
    await db_session.refresh(reminder)
    assert reminder.status == "approved"

    # …then confirms the manual send.
    marked = await rem_client.post(f"/reminders/{reminder.id}/mark-sent", headers=headers)
    assert marked.status_code == 200
    assert marked.json()["status"] == "sent"


async def test_mark_sent_refused_on_wrong_channel_or_status(
    rem_client: AsyncClient,
    manager_agent: Agent,
    case: ClientCase,
    make_reminder: MakeReminder,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(manager_agent)
    mail_reminder = await make_reminder(case=case, status="approved", channel="mail")
    assert (
        await rem_client.post(f"/reminders/{mail_reminder.id}/mark-sent", headers=headers)
    ).status_code == 422
    pending_whatsapp = await make_reminder(case=case, status="to_approve", channel="whatsapp")
    assert (
        await rem_client.post(f"/reminders/{pending_whatsapp.id}/mark-sent", headers=headers)
    ).status_code == 409


# --- state machine ----------------------------------------------------------------------


async def test_approve_flow(
    rem_client: AsyncClient,
    manager_agent: Agent,
    case: ClientCase,
    make_reminder: MakeReminder,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(manager_agent)
    reminder = await make_reminder(case=case)
    approved = await rem_client.post(f"/reminders/{reminder.id}/approve", headers=headers)
    assert approved.status_code == 200
    body = approved.json()
    assert body["status"] == "approved"
    assert body["approved_by_agent_id"] == str(manager_agent.id)
    again = await rem_client.post(f"/reminders/{reminder.id}/approve", headers=headers)
    assert again.status_code == 409


async def test_editing_approved_returns_to_to_approve(
    rem_client: AsyncClient,
    manager_agent: Agent,
    case: ClientCase,
    make_reminder: MakeReminder,
    agent_headers: AuthHeaders,
) -> None:
    """The approval covers WHAT GOES OUT: any edit voids it."""
    headers = agent_headers(manager_agent)
    reminder = await make_reminder(case=case, status="to_approve")
    await rem_client.post(f"/reminders/{reminder.id}/approve", headers=headers)

    edited = await rem_client.patch(
        f"/reminders/{reminder.id}",
        headers=headers,
        json={"scheduled_at": _FUTURE.isoformat()},
    )
    assert edited.status_code == 200
    body = edited.json()
    assert body["status"] == "to_approve"
    assert body["approved_by_agent_id"] is None


async def test_sent_and_cancelled_are_immutable(
    rem_client: AsyncClient,
    manager_agent: Agent,
    case: ClientCase,
    make_reminder: MakeReminder,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(manager_agent)
    sent = await make_reminder(case=case, status="sent")
    assert (
        await rem_client.patch(f"/reminders/{sent.id}", headers=headers, json={"message_body": "x"})
    ).status_code == 409
    assert (
        await rem_client.post(f"/reminders/{sent.id}/cancel", headers=headers)
    ).status_code == 409

    cancellable = await make_reminder(case=case, status="approved")
    cancelled = await rem_client.post(f"/reminders/{cancellable.id}/cancel", headers=headers)
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"


# --- auto follow-ups (J+20 / J+30) ----------------------------------------------------------


async def _activate_client_space(db_session: AsyncSession, case: ClientCase) -> None:
    """The fixture's principal is created NON-activated; an auto follow-up is
    only ever created for a client who can actually read it (NID-23 follow-up
    — see test_auto_reminders_skip_a_client_who_never_activated). These
    threshold tests are about the CLOCK, so they start from an active space."""
    await db_session.execute(
        update(ExpatUser)
        .where(ExpatUser.id == case.principal_expat_user_id)
        .values(activated_at=datetime.now(UTC))
    )
    await db_session.commit()


async def _stalled_step(
    rem_client: AsyncClient,
    db_session: AsyncSession,
    agent: Agent,
    make_client_case: MakeClientCase,
    headers: dict[str, str],
    days: int,
    thresholds: tuple[int, int] | None = None,
) -> ClientCase:
    body: dict[str, object] = {"name": "T"}
    if thresholds is not None:
        body["auto_reminder_days_1"], body["auto_reminder_days_2"] = thresholds
    template = (await rem_client.post("/journeys", headers=headers, json=body)).json()
    await rem_client.post(
        f"/journeys/{template['id']}/steps", headers=headers, json={"name": "Stalled step"}
    )
    case = await make_client_case(agency_id=agent.agency_id)
    await _activate_client_space(db_session, case)
    await rem_client.post(
        f"/cases/{case.id}/journey",
        headers=headers,
        json={"journey_template_id": template["id"]},
    )
    await db_session.execute(
        update(CaseStepProgress)
        .where(CaseStepProgress.case_id == case.id)
        .values(updated_at=datetime.now(UTC) - timedelta(days=days))
    )
    await db_session.commit()
    return case


async def test_auto_threshold_created_once_over_two_ticks(
    rem_client: AsyncClient,
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    manager_agent: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(manager_agent)
    case = await _stalled_step(
        rem_client, db_session, manager_agent, make_client_case, headers, days=21
    )

    assert _run_auto(sync_session_local)["created"] == 1
    assert _run_auto(sync_session_local)["created"] == 0  # the unique at work

    reminders = (
        (await db_session.execute(select(Reminder).where(Reminder.case_id == case.id)))
        .scalars()
        .all()
    )
    assert len(reminders) == 1
    auto = reminders[0]
    assert auto.status == "approved"  # le régime par défaut : elle part seule
    assert auto.auto_threshold_days == 20

    log = (
        await db_session.execute(
            select(ActivityLog).where(ActivityLog.action_type == "reminder.auto_created")
        )
    ).scalar_one()
    assert log.actor_type == "system"

    # 31 days stalled → the J+30 tier joins, J+20 not duplicated.
    await db_session.execute(
        update(CaseStepProgress)
        .where(CaseStepProgress.case_id == case.id)
        .values(updated_at=datetime.now(UTC) - timedelta(days=31))
    )
    await db_session.commit()
    assert _run_auto(sync_session_local)["created"] == 1
    thresholds = (
        await db_session.execute(
            select(Reminder.auto_threshold_days).where(Reminder.case_id == case.id)
        )
    ).scalars()
    assert sorted(thresholds) == [20, 30]


async def test_auto_reminders_disabled_by_agency_settings(
    rem_client: AsyncClient,
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    make_agent: MakeAgent,
    make_agency: object,
    make_client_case: MakeClientCase,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    agency = await make_agency(settings={"auto_reminders_enabled": False})  # type: ignore[operator]
    agent = await make_agent(agency_id=agency.id, role=system_roles["case_manager"])
    await _stalled_step(
        rem_client, db_session, agent, make_client_case, agent_headers(agent), days=25
    )
    assert _run_auto(sync_session_local)["created"] == 0


async def test_auto_reminders_skip_demo_cases(
    rem_client: AsyncClient,
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    make_agent: MakeAgent,
    make_client_case: MakeClientCase,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """A demo dossier ([Exemple], is_demo) never enters the approval queue —
    two identical stalled cases, one flipped to is_demo, → only ONE reminder."""
    agent = await make_agent(role=system_roles["case_manager"])
    headers = agent_headers(agent)
    real = await _stalled_step(rem_client, db_session, agent, make_client_case, headers, days=25)
    demo = await _stalled_step(rem_client, db_session, agent, make_client_case, headers, days=25)
    await db_session.execute(
        update(ClientCase).where(ClientCase.id == demo.id).values(is_demo=True)
    )
    await db_session.commit()

    result = _run_auto(sync_session_local)
    assert result["created"] == 1  # only the non-demo case
    created = list(
        (await db_session.execute(select(Reminder).where(Reminder.case_id == real.id))).scalars()
    )
    assert len(created) == 1
    none_for_demo = list(
        (await db_session.execute(select(Reminder).where(Reminder.case_id == demo.id))).scalars()
    )
    assert none_for_demo == []


# --- calendar + permissions --------------------------------------------------------------------


async def test_calendar_filters_and_scoping(
    rem_client: AsyncClient,
    manager_agent: Agent,
    make_agent: MakeAgent,
    make_client_case: MakeClientCase,
    make_reminder: MakeReminder,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(manager_agent)
    case = await make_client_case(agency_id=manager_agent.agency_id)
    pending = await make_reminder(case=case, status="to_approve", scheduled_at=_NOW)
    await make_reminder(case=case, status="sent", scheduled_at=_NOW - timedelta(days=30))
    foreign_agent = await make_agent()
    foreign_case = await make_client_case(agency_id=foreign_agent.agency_id)
    await make_reminder(case=foreign_case, status="to_approve", scheduled_at=_NOW)

    response = await rem_client.get(
        "/reminders",
        headers=headers,
        params={
            "status": "to_approve",
            "scheduled_from": (_NOW - timedelta(days=1)).isoformat(),
            "scheduled_to": (_NOW + timedelta(days=1)).isoformat(),
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert [item["id"] for item in body["items"]] == [str(pending.id)]
    assert body["total"] == 1


async def test_viewer_cannot_create_reminders(
    rem_client: AsyncClient,
    make_agent: MakeAgent,
    case: ClientCase,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    viewer = await make_agent(agency_id=case.agency_id, role=system_roles["viewer"])
    response = await rem_client.post(
        f"/cases/{case.id}/reminders",
        headers=agent_headers(viewer),
        json={
            "channel": "mail",
            "scheduled_at": _FUTURE.isoformat(),
            "recipient_type": "expat",
            "message_body": "Hello",
        },
    )
    assert response.status_code == 403


# --- P2: provider auto follow-ups (same clock, agency language) -----------------------


async def _external_participant(
    db_session: AsyncSession, case: ClientCase, contact_id: uuid.UUID
) -> uuid.UUID:
    """Wire an external participant on the case's (single) step progress."""
    progress_id = (
        await db_session.execute(
            select(CaseStepProgress.id).where(CaseStepProgress.case_id == case.id)
        )
    ).scalar_one()
    db_session.add(
        CaseStepParticipant(
            case_step_progress_id=progress_id,
            type="external",
            external_id=contact_id,
            role="executant",
        )
    )
    await db_session.commit()
    return progress_id


async def test_auto_provider_j20_in_agency_language_client_untouched(
    rem_client: AsyncClient,
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    manager_agent: Agent,
    make_client_case: MakeClientCase,
    make_external_contact: MakeExternalContact,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(manager_agent)
    case = await _stalled_step(
        rem_client, db_session, manager_agent, make_client_case, headers, days=21
    )
    await db_session.execute(
        update(Agency).where(Agency.id == case.agency_id).values(default_language="en")
    )
    await db_session.commit()
    contact = await make_external_contact(case=case, email="notaire@example.com")
    await _external_participant(db_session, case, contact.id)

    assert _run_auto(sync_session_local)["created"] == 2  # client + provider, same tick
    rows = (
        (await db_session.execute(select(Reminder).where(Reminder.case_id == case.id)))
        .scalars()
        .all()
    )
    by_type = {r.recipient_type: r for r in rows}
    provider = by_type["external"]
    assert provider.status == "approved"  # part seule (régime par défaut, lot 13/08)
    assert provider.recipient_external_id == contact.id
    assert provider.auto_threshold_days == 20
    # The manual-flow language rule: AGENCY language (en), not the client's.
    assert "has not progressed" in provider.message_body
    # The client flow is UNTOUCHED: its row exists, in the client's language.
    assert "n'a pas progressé" in by_type["expat"].message_body


async def test_auto_provider_j30_joins_and_dedup(
    rem_client: AsyncClient,
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    manager_agent: Agent,
    make_client_case: MakeClientCase,
    make_external_contact: MakeExternalContact,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(manager_agent)
    case = await _stalled_step(
        rem_client, db_session, manager_agent, make_client_case, headers, days=21
    )
    contact = await make_external_contact(case=case, email="n@example.com")
    await _external_participant(db_session, case, contact.id)

    assert _run_auto(sync_session_local)["created"] == 2
    assert _run_auto(sync_session_local)["created"] == 0  # dedup: pending -> no doubles

    await db_session.execute(
        update(CaseStepProgress)
        .where(CaseStepProgress.case_id == case.id)
        .values(updated_at=datetime.now(UTC) - timedelta(days=31))
    )
    await db_session.commit()
    assert _run_auto(sync_session_local)["created"] == 2  # J+30 joins for BOTH
    provider_thresholds = (
        await db_session.execute(
            select(Reminder.auto_threshold_days).where(
                Reminder.case_id == case.id,
                Reminder.recipient_type == "external",
            )
        )
    ).scalars()
    assert sorted(provider_thresholds) == [20, 30]


async def test_auto_provider_without_email_escalates_to_owner(
    rem_client: AsyncClient,
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    manager_agent: Agent,
    make_client_case: MakeClientCase,
    make_external_contact: MakeExternalContact,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(manager_agent)
    case = await _stalled_step(
        rem_client, db_session, manager_agent, make_client_case, headers, days=21
    )
    await db_session.execute(
        update(ClientCase).where(ClientCase.id == case.id).values(owner_agent_id=manager_agent.id)
    )
    contact = await make_external_contact(case=case, email=None)  # unreachable
    await _external_participant(db_session, case, contact.id)
    await db_session.commit()
    _run_auto(sync_session_local)

    email.outbox.clear()
    # Lot 13/08 : la relance CLIENT part désormais seule au même tick. Ce test
    # porte sur le chemin PRESTATAIRE — on l'écarte pour garder une boîte
    # d'envoi (et un « sent » unique) lisibles.
    await db_session.execute(
        update(Reminder)
        .where(Reminder.recipient_type == "expat", Reminder.case_id == case.id)
        .values(status="cancelled")
    )
    await db_session.execute(
        update(Reminder)
        .where(Reminder.recipient_type == "external", Reminder.case_id == case.id)
        .values(status="approved", scheduled_at=datetime.now(UTC) - timedelta(hours=1))
    )
    await db_session.commit()
    _run_dispatch(sync_session_local)
    [mail] = email.outbox
    assert mail.to == manager_agent.email  # the case owner, not silence
    reminder = (
        await db_session.execute(
            select(Reminder).where(Reminder.case_id == case.id, Reminder.status == "sent")
        )
    ).scalar_one()
    assert reminder.recipient_type == "agent"  # re-routed (escalated_from mechanism)
    assert "Maitre Dupont" in mail.body  # the original provider is NAMED


async def test_auto_provider_foreign_case_contact_creates_nothing(
    rem_client: AsyncClient,
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    manager_agent: Agent,
    make_client_case: MakeClientCase,
    make_external_contact: MakeExternalContact,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(manager_agent)
    case = await _stalled_step(
        rem_client, db_session, manager_agent, make_client_case, headers, days=21
    )
    other_case = await make_client_case(agency_id=manager_agent.agency_id)
    foreign = await make_external_contact(case=other_case, email="x@example.com")
    await _external_participant(db_session, case, foreign.id)

    _run_auto(sync_session_local)
    external_rows = (
        await db_session.execute(select(Reminder).where(Reminder.recipient_type == "external"))
    ).scalars()
    assert list(external_rows) == []  # the case-contact validation, in SQL


async def test_auto_provider_respects_agency_toggle_and_tenancy(
    rem_client: AsyncClient,
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    manager_agent: Agent,
    make_agent: MakeAgent,
    make_agency: MakeAgency,
    make_client_case: MakeClientCase,
    make_external_contact: MakeExternalContact,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    # Agency A: provider follow-up created.
    headers = agent_headers(manager_agent)
    case_a = await _stalled_step(
        rem_client, db_session, manager_agent, make_client_case, headers, days=21
    )
    contact_a = await make_external_contact(case=case_a, email="a@example.com")
    await _external_participant(db_session, case_a, contact_a.id)

    # Agency B: toggle OFF — its stalled provider step creates NOTHING.
    agency_b = await make_agency(settings={"auto_reminders_enabled": False})
    agent_b = await make_agent(agency_id=agency_b.id, role=system_roles["case_manager"])
    headers_b = agent_headers(agent_b)
    case_b = await _stalled_step(
        rem_client, db_session, agent_b, make_client_case, headers_b, days=21
    )
    contact_b = await make_external_contact(case=case_b, email="b@example.com")
    await _external_participant(db_session, case_b, contact_b.id)

    _run_auto(sync_session_local)
    externals = (
        (await db_session.execute(select(Reminder).where(Reminder.recipient_type == "external")))
        .scalars()
        .all()
    )
    assert [r.case_id for r in externals] == [case_a.id]  # B's tenancy/toggle respected


async def test_auto_provider_escalated_line_still_blocks_next_tick(
    rem_client: AsyncClient,
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    manager_agent: Agent,
    make_client_case: MakeClientCase,
    make_external_contact: MakeExternalContact,
    agent_headers: AuthHeaders,
) -> None:
    """Fermeture (feu vert conditionnel): created -> ESCALATED at dispatch
    (rewritten agent, provenance kept) -> next tick -> ZERO re-creation
    for this (step, threshold, provider)."""
    headers = agent_headers(manager_agent)
    case = await _stalled_step(
        rem_client, db_session, manager_agent, make_client_case, headers, days=21
    )
    await db_session.execute(
        update(ClientCase).where(ClientCase.id == case.id).values(owner_agent_id=manager_agent.id)
    )
    contact = await make_external_contact(case=case, email=None)  # will escalate
    await _external_participant(db_session, case, contact.id)
    assert _run_auto(sync_session_local)["created"] == 2  # client + provider

    # Lot 13/08 : la relance CLIENT part désormais seule au même tick. Ce test
    # porte sur le chemin PRESTATAIRE — on l'écarte pour garder une boîte
    # d'envoi (et un « sent » unique) lisibles.
    await db_session.execute(
        update(Reminder)
        .where(Reminder.recipient_type == "expat", Reminder.case_id == case.id)
        .values(status="cancelled")
    )
    await db_session.execute(
        update(Reminder)
        .where(Reminder.recipient_type == "external", Reminder.case_id == case.id)
        .values(status="approved", scheduled_at=datetime.now(UTC) - timedelta(hours=1))
    )
    await db_session.commit()
    _run_dispatch(sync_session_local)
    escalated = (
        await db_session.execute(
            select(Reminder).where(Reminder.case_id == case.id, Reminder.status == "sent")
        )
    ).scalar_one()
    assert escalated.recipient_type == "agent"  # rewritten
    assert escalated.recipient_external_id == contact.id  # PROVENANCE KEPT

    # The closing assertion: the next tick recreates NOTHING.
    assert _run_auto(sync_session_local)["created"] == 0
    provider_rows = (
        (
            await db_session.execute(
                select(Reminder).where(
                    Reminder.case_id == case.id,
                    Reminder.recipient_external_id == contact.id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(provider_rows) == 1  # the escalated line, alone, blocks its threshold


# --- INVARIANT: preferred_channels never reroute the dispatch (email-only) -------------


async def test_preferred_whatsapp_client_still_gets_email_reminder(
    rem_client: AsyncClient,
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    manager_agent: Agent,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    make_reminder: MakeReminder,
) -> None:
    """THE invariant (preferred-channels lot): a client who prefers
    WhatsApp still receives the reminder by EMAIL — preferred_channels is
    display-only, the dispatch routing is untouched (100% email)."""
    from shared.models.case_person import CasePerson

    expat = await make_expat_user(first_name="Nadia", last_name="Client")
    case = await make_client_case(
        agency_id=manager_agent.agency_id, principal_expat_user_id=expat.id
    )
    # The principal prefers WhatsApp (+ phone), display-only.
    await db_session.execute(
        update(CasePerson)
        .where(CasePerson.case_id == case.id, CasePerson.kind == "principal")
        .values(preferred_channels=["whatsapp", "phone"], phone="+33611111111")
    )
    await db_session.commit()

    await make_reminder(case=case, status="approved", scheduled_at=_PAST, recipient_type="expat")
    email.outbox.clear()
    stats = _run_dispatch(sync_session_local)
    assert stats["sent"] == 1
    # Sent by EMAIL to the client's address — the whatsapp preference
    # changed NOTHING about the channel.
    assert [m.to for m in email.outbox] == [expat.email]


# --- NID-18: per-journey auto-reminder thresholds -------------------------------------------


async def test_per_journey_thresholds_override_the_default(
    rem_client: AsyncClient,
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    manager_agent: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """A journey carrying (10, 15) fires at J+10 / J+15 — NOT the system
    default (20, 30). At 11 days stalled the default would create nothing."""
    headers = agent_headers(manager_agent)
    case = await _stalled_step(
        rem_client,
        db_session,
        manager_agent,
        make_client_case,
        headers,
        days=11,
        thresholds=(10, 15),
    )

    assert _run_auto(sync_session_local)["created"] == 1  # J+10 crossed, default 20 would be 0
    assert _run_auto(sync_session_local)["created"] == 0  # idempotent
    [threshold] = (
        (
            await db_session.execute(
                select(Reminder.auto_threshold_days).where(Reminder.case_id == case.id)
            )
        )
        .scalars()
        .all()
    )
    assert threshold == 10

    # 16 days → the second journey tier (15) joins; 10 not duplicated.
    await db_session.execute(
        update(CaseStepProgress)
        .where(CaseStepProgress.case_id == case.id)
        .values(updated_at=datetime.now(UTC) - timedelta(days=16))
    )
    await db_session.commit()
    assert _run_auto(sync_session_local)["created"] == 1
    thresholds = (
        await db_session.execute(
            select(Reminder.auto_threshold_days).where(Reminder.case_id == case.id)
        )
    ).scalars()
    assert sorted(thresholds) == [10, 15]


async def test_journey_without_thresholds_falls_back_to_system_default(
    rem_client: AsyncClient,
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    manager_agent: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """No per-journey values (both NULL) → the chain resolves to [20, 30].
    Pins that pre-NID-18 journeys behave EXACTLY as before (additive column,
    zero behaviour change)."""
    headers = agent_headers(manager_agent)
    case = await _stalled_step(
        rem_client, db_session, manager_agent, make_client_case, headers, days=21
    )
    assert _run_auto(sync_session_local)["created"] == 1
    [threshold] = (
        (
            await db_session.execute(
                select(Reminder.auto_threshold_days).where(Reminder.case_id == case.id)
            )
        )
        .scalars()
        .all()
    )
    assert threshold == 20


async def test_agency_toggle_is_master_over_journey_thresholds(
    rem_client: AsyncClient,
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    make_agent: MakeAgent,
    make_agency: MakeAgency,
    make_client_case: MakeClientCase,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """Agency auto_reminders_enabled=False wins even with aggressive journey
    thresholds set — the master switch is unconditional."""
    agency = await make_agency(settings={"auto_reminders_enabled": False})
    agent = await make_agent(agency_id=agency.id, role=system_roles["case_manager"])
    await _stalled_step(
        rem_client,
        db_session,
        agent,
        make_client_case,
        agent_headers(agent),
        days=6,
        thresholds=(5, 10),
    )
    assert _run_auto(sync_session_local)["created"] == 0


@pytest.mark.parametrize(
    "pair",
    [
        {"auto_reminder_days_1": 30, "auto_reminder_days_2": 20},  # p2 <= p1
        {"auto_reminder_days_1": 20, "auto_reminder_days_2": 20},  # equal
        {"auto_reminder_days_1": 0, "auto_reminder_days_2": 10},  # below 1
        {"auto_reminder_days_1": -5, "auto_reminder_days_2": 10},  # negative
        {"auto_reminder_days_1": 10, "auto_reminder_days_2": 400},  # above 365
        {"auto_reminder_days_1": 10},  # partial pair (only first)
        {"auto_reminder_days_2": 15},  # partial pair (only second)
    ],
)
async def test_create_journey_rejects_invalid_threshold_pair(
    rem_client: AsyncClient,
    manager_agent: Agent,
    agent_headers: AuthHeaders,
    pair: dict[str, int],
) -> None:
    response = await rem_client.post(
        "/journeys", headers=agent_headers(manager_agent), json={"name": "Bad", **pair}
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "journey.auto_reminder_days_invalid"


async def test_update_journey_threshold_pair_semantics(
    rem_client: AsyncClient,
    manager_agent: Agent,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(manager_agent)
    template = (await rem_client.post("/journeys", headers=headers, json={"name": "T"})).json()
    tid = template["id"]

    # Set the pair.
    set_ok = await rem_client.patch(
        f"/journeys/{tid}",
        headers=headers,
        json={"auto_reminder_days_1": 10, "auto_reminder_days_2": 15},
    )
    assert set_ok.status_code == 200, set_ok.text
    assert (set_ok.json()["auto_reminder_days_1"], set_ok.json()["auto_reminder_days_2"]) == (
        10,
        15,
    )

    # A single field in the payload → partial pair → 422 (the other stays 10/15).
    partial = await rem_client.patch(
        f"/journeys/{tid}", headers=headers, json={"auto_reminder_days_1": 12}
    )
    assert partial.status_code == 422
    assert partial.json()["code"] == "journey.auto_reminder_days_invalid"

    # Both explicit null → clear to inherit.
    cleared = await rem_client.patch(
        f"/journeys/{tid}",
        headers=headers,
        json={"auto_reminder_days_1": None, "auto_reminder_days_2": None},
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["auto_reminder_days_1"] is None
    assert cleared.json()["auto_reminder_days_2"] is None

    # Neither field present → untouched (name-only edit keeps the cleared state).
    untouched = await rem_client.patch(f"/journeys/{tid}", headers=headers, json={"name": "T2"})
    assert untouched.status_code == 200
    assert untouched.json()["auto_reminder_days_1"] is None


async def test_clone_snapshots_journey_thresholds(
    rem_client: AsyncClient,
    manager_agent: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """clone_template copies the per-journey pair into the clone (snapshot)."""
    headers = agent_headers(manager_agent)
    source = (
        await rem_client.post(
            "/journeys",
            headers=headers,
            json={"name": "Src", "auto_reminder_days_1": 10, "auto_reminder_days_2": 15},
        )
    ).json()
    clone = await rem_client.post(f"/journeys/{source['id']}/clone", headers=headers, json={})
    assert clone.status_code == 201, clone.text
    body = clone.json()
    assert body["id"] != source["id"]
    assert (body["auto_reminder_days_1"], body["auto_reminder_days_2"]) == (10, 15)


async def test_provider_pass_honors_per_journey_thresholds(
    rem_client: AsyncClient,
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    manager_agent: Agent,
    make_client_case: MakeClientCase,
    make_external_contact: MakeExternalContact,
    agent_headers: AuthHeaders,
) -> None:
    """PIN (NID-18): the provider pass is DELIBERATELY kept — an external
    participant of a stalled step receives its own TO_APPROVE follow-up, at
    the JOURNEY's thresholds (5, 10), never auto-sent. If we ever drop the
    provider pass it must be an explicit choice, caught by this test, not a
    silent regression."""
    headers = agent_headers(manager_agent)
    case = await _stalled_step(
        rem_client, db_session, manager_agent, make_client_case, headers, days=6, thresholds=(5, 10)
    )
    contact = await make_external_contact(case=case, email="notaire@example.com")
    await _external_participant(db_session, case, contact.id)

    # J+5 crossed (6 days stalled): client + provider, both at threshold 5.
    assert _run_auto(sync_session_local)["created"] == 2
    provider = (
        (
            await db_session.execute(
                select(Reminder).where(
                    Reminder.case_id == case.id, Reminder.recipient_type == "external"
                )
            )
        )
        .scalars()
        .one()
    )
    assert provider.status == "approved"  # part seule (régime par défaut, lot 13/08)
    assert provider.recipient_external_id == contact.id
    assert provider.auto_threshold_days == 5  # the journey threshold, not the default 20


async def test_template_detail_exposes_journey_thresholds(
    rem_client: AsyncClient,
    db_session: AsyncSession,
    manager_agent: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """GET /journeys/{id} — the endpoint the EDITOR loads — is a SUPERSET of
    the list: the threshold pair AND the provenance (country, sector), posed
    values AND null. All four were served by the list but not here, forcing
    the front to filter the list by id to read them."""
    headers = agent_headers(manager_agent)

    # Posed values: the detail returns them, no list round-trip needed.
    posed = (
        await rem_client.post(
            "/journeys",
            headers=headers,
            json={"name": "Configured", "auto_reminder_days_1": 10, "auto_reminder_days_2": 15},
        )
    ).json()
    # Provenance is NOT settable through the API — clone_template carries the
    # country over, the onboarding gift the sector. Posed at the source, the
    # way those two paths do it.
    await db_session.execute(
        update(JourneyTemplate)
        .where(JourneyTemplate.id == uuid.UUID(posed["id"]))
        .values(country="PY", sector="immigration")
    )
    await db_session.commit()

    detail = await rem_client.get(f"/journeys/{posed['id']}", headers=headers)
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert (body["auto_reminder_days_1"], body["auto_reminder_days_2"]) == (10, 15)
    assert (body["country"], body["sector"]) == ("PY", "immigration")
    # The detail is a SUPERSET of the list: every shared key agrees.
    listed = next(
        t
        for t in (await rem_client.get("/journeys", headers=headers)).json()
        if t["id"] == posed["id"]
    )
    for key in ("auto_reminder_days_1", "auto_reminder_days_2", "country", "sector"):
        assert body[key] == listed[key], key

    # Hand-made journey: the keys are PRESENT and null (not absent) — the
    # editor distinguishes "inherits / no provenance" from "field not served".
    plain = (await rem_client.post("/journeys", headers=headers, json={"name": "Inherit"})).json()
    plain_detail = (await rem_client.get(f"/journeys/{plain['id']}", headers=headers)).json()
    assert plain_detail["auto_reminder_days_1"] is None
    assert plain_detail["auto_reminder_days_2"] is None
    assert plain_detail["country"] is None
    assert plain_detail["sector"] is None  # the adoption-signal discriminant


# --- NID-18 : une étape dont tout est fourni n'appelle plus le client ---------------


async def _stalled_step_with_document(
    rem_client: AsyncClient,
    db_session: AsyncSession,
    agent: Agent,
    make_client_case: MakeClientCase,
    headers: dict[str, str],
    days: int,
) -> tuple[ClientCase, uuid.UUID]:
    """Twin of _stalled_step, but the step carries ONE document requirement
    (whose provided state is the STORED status — the honest lever for
    "the client handed it over" / "the piece was removed")."""
    template = (await rem_client.post("/journeys", headers=headers, json={"name": "T"})).json()
    step = (
        await rem_client.post(
            f"/journeys/{template['id']}/steps", headers=headers, json={"name": "Stalled step"}
        )
    ).json()
    await rem_client.post(
        f"/journeys/{template['id']}/steps/{step['id']}/requirements",
        headers=headers,
        json={"kind": "document", "reference": "passeport", "scope": "principal"},
    )
    case = await make_client_case(agency_id=agent.agency_id)
    await _activate_client_space(db_session, case)
    timeline = (
        await rem_client.post(
            f"/cases/{case.id}/journey",
            headers=headers,
            json={"journey_template_id": template["id"]},
        )
    ).json()
    # Concrete requirements materialize at ACTIVATION, not at assignment.
    await rem_client.patch(
        f"/cases/{case.id}/steps/{timeline[0]['id']}",
        headers=headers,
        json={"status": "in_progress"},
    )
    requirement_id = (
        await db_session.execute(
            select(CaseStepRequirement.id)
            .join(
                CaseStepProgress,
                CaseStepProgress.id == CaseStepRequirement.case_step_progress_id,
            )
            .where(CaseStepProgress.case_id == case.id)
        )
    ).scalar_one()
    # Backdated LAST: materializing the requirements touches the rows.
    await db_session.execute(
        update(CaseStepProgress)
        .where(CaseStepProgress.case_id == case.id)
        .values(updated_at=datetime.now(UTC) - timedelta(days=days))
    )
    await db_session.commit()
    return case, requirement_id


async def _set_requirement_status(
    db_session: AsyncSession, requirement_id: uuid.UUID, status: str
) -> None:
    await db_session.execute(
        update(CaseStepRequirement)
        .where(CaseStepRequirement.id == requirement_id)
        .values(status=status)
    )
    await db_session.commit()


async def test_incomplete_stalled_step_still_reminds_the_client(
    rem_client: AsyncClient,
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    manager_agent: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """The control: something is STILL expected from the client, so the
    stalled step keeps producing its TO_APPROVE follow-up."""
    headers = agent_headers(manager_agent)
    case, _req = await _stalled_step_with_document(
        rem_client, db_session, manager_agent, make_client_case, headers, days=21
    )
    assert _run_auto(sync_session_local)["created"] == 1
    reminder = (
        (await db_session.execute(select(Reminder).where(Reminder.case_id == case.id)))
        .scalars()
        .one()
    )
    assert reminder.status == "approved"  # créée déjà approuvée, jamais envoyée hors dispatch


async def test_step_with_all_requirements_met_stops_client_reminders(
    rem_client: AsyncClient,
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    manager_agent: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """THE lot: everything expected has been provided — the ball is in the
    AGENCY's court (validation), not the client's. Chasing him there is
    noise in the approval queue, so the step leaves the client scan even
    though it is just as old (same 21 days as the control above)."""
    headers = agent_headers(manager_agent)
    case, requirement_id = await _stalled_step_with_document(
        rem_client, db_session, manager_agent, make_client_case, headers, days=21
    )
    await _set_requirement_status(db_session, requirement_id, "provided")

    assert _run_auto(sync_session_local)["created"] == 0
    assert (
        (await db_session.execute(select(Reminder).where(Reminder.case_id == case.id)))
        .scalars()
        .all()
    ) == []


async def test_removing_the_document_makes_the_step_eligible_again(
    rem_client: AsyncClient,
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    manager_agent: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """The exclusion is a LIVE read, never a latch: a piece removed puts the
    requirement back to non-provided, and the client is chased again."""
    headers = agent_headers(manager_agent)
    case, requirement_id = await _stalled_step_with_document(
        rem_client, db_session, manager_agent, make_client_case, headers, days=21
    )
    await _set_requirement_status(db_session, requirement_id, "provided")
    assert _run_auto(sync_session_local)["created"] == 0

    await _set_requirement_status(db_session, requirement_id, "pending")  # the piece is removed
    assert _run_auto(sync_session_local)["created"] == 1
    assert (
        len(
            (await db_session.execute(select(Reminder).where(Reminder.case_id == case.id)))
            .scalars()
            .all()
        )
        == 1
    )


async def test_provider_pass_untouched_by_a_fully_met_step(
    rem_client: AsyncClient,
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    manager_agent: Agent,
    make_client_case: MakeClientCase,
    make_external_contact: MakeExternalContact,
    agent_headers: AuthHeaders,
) -> None:
    """PIN: the all-met exclusion is CLIENT-ONLY. A provider's follow-up does
    not depend on the client's paperwork — the notary is still chased on a
    step where the client has handed everything over. Exactly ONE reminder,
    external, TO_APPROVE."""
    headers = agent_headers(manager_agent)
    case, requirement_id = await _stalled_step_with_document(
        rem_client, db_session, manager_agent, make_client_case, headers, days=21
    )
    contact = await make_external_contact(case=case, email="notaire@example.com")
    await _external_participant(db_session, case, contact.id)
    await _set_requirement_status(db_session, requirement_id, "provided")

    assert _run_auto(sync_session_local)["created"] == 1  # the provider one, not the client one
    reminder = (
        (await db_session.execute(select(Reminder).where(Reminder.case_id == case.id)))
        .scalars()
        .one()
    )
    assert reminder.recipient_type == "external"
    assert reminder.recipient_external_id == contact.id
    assert reminder.status == "approved"


# --- l'approbation des relances AUTOMATIQUES devient un choix (lot 13/08) ---------
#
# Constat prod du 13/08 : 97 relances automatiques en attente d'approbation sur
# deux agences, la plus vieille de 17 jours, ZÉRO jamais partie. La promesse
# d'Eloïse (« rien ne part sans approbation ») reste entière pour les rappels
# ÉCRITS À LA MAIN ; les automatiques, elles, partent seules par défaut.


async def _stalled(
    rem_client: AsyncClient,
    db_session: AsyncSession,
    agent: Agent,
    make_client_case: MakeClientCase,
    headers: dict[str, str],
) -> ClientCase:
    return await _stalled_step(rem_client, db_session, agent, make_client_case, headers, days=21)


async def test_by_default_an_auto_reminder_leaves_without_approval(
    rem_client: AsyncClient,
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    manager_agent: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """LA promesse du produit : l'agence qui n'a rien réglé voit ses relances
    partir. Créée APPROVED (aucun approbateur humain), puis envoyée par le
    dispatch — le seul chemin d'envoi, inchangé."""
    headers = agent_headers(manager_agent)
    case = await _stalled(rem_client, db_session, manager_agent, make_client_case, headers)

    assert _run_auto(sync_session_local)["created"] == 1
    reminder = (
        await db_session.execute(select(Reminder).where(Reminder.case_id == case.id))
    ).scalar_one()
    assert reminder.status == "approved"
    assert reminder.approved_by_agent_id is None  # personne n'a cliqué : c'est le régime

    assert _run_dispatch(sync_session_local)["sent"] == 1
    await db_session.refresh(reminder)
    assert reminder.status == "sent"

    log = (
        await db_session.execute(
            select(ActivityLog).where(ActivityLog.action_type == "reminder.auto_created")
        )
    ).scalar_one()
    assert log.details["approval"] == "auto"  # la trace dit QUEL régime a produit la ligne


async def test_an_agency_can_still_validate_each_one(
    rem_client: AsyncClient,
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    make_agent: MakeAgent,
    make_agency: MakeAgency,
    make_client_case: MakeClientCase,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """Le mode d'avant, conservé pour qui le veut : la relance attend, et le
    dispatch ne peut pas la prendre."""
    agency = await make_agency(settings={"auto_reminders_require_approval": True})
    agent = await make_agent(agency_id=agency.id, role=system_roles["case_manager"])
    headers = agent_headers(agent)
    case = await _stalled(rem_client, db_session, agent, make_client_case, headers)

    assert _run_auto(sync_session_local)["created"] == 1
    reminder = (
        await db_session.execute(select(Reminder).where(Reminder.case_id == case.id))
    ).scalar_one()
    assert reminder.status == "to_approve"
    assert _run_dispatch(sync_session_local)["sent"] == 0
    assert email.outbox == []


async def test_a_hand_written_reminder_always_needs_approval(
    rem_client: AsyncClient,
    db_session: AsyncSession,
    manager_agent: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """La promesse d'Eloïse, intacte : le réglage ne concerne QUE les
    automatiques. Un rappel écrit à la main naît toujours à approuver."""
    headers = agent_headers(manager_agent)
    case = await make_client_case(agency_id=manager_agent.agency_id)
    created = await rem_client.post(
        f"/cases/{case.id}/reminders",
        headers=headers,
        json={
            "channel": "mail",
            "scheduled_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            "recipient_type": "expat",
            "message_body": "Bonjour",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "to_approve"


async def test_the_setting_is_served_and_written(
    rem_client: AsyncClient,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    # Un réglage d'agence : c'est agency.manage qui l'écrit, pas le
    # case_manager (qui, lui, approuve et annule les rappels).
    admin = await make_agent(role=system_roles["admin"])
    headers = agent_headers(admin)
    assert (await rem_client.get("/agencies/me", headers=headers)).json()[
        "auto_reminders_require_approval"
    ] is False  # le défaut = la promesse

    patched = await rem_client.patch(
        "/agencies/me", headers=headers, json={"auto_reminders_require_approval": True}
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["auto_reminders_require_approval"] is True
    # Écrit dans settings sans rien y perdre (même discipline JSONB que les prefs).
    assert body["settings"]["auto_reminders_require_approval"] is True
    reread = (await rem_client.get("/agencies/me", headers=headers)).json()
    assert reread["auto_reminders_require_approval"] is True


async def test_the_waiting_backlog_never_leaves_retroactively(
    rem_client: AsyncClient,
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    manager_agent: Agent,
    make_client_case: MakeClientCase,
    make_reminder: MakeReminder,
    agent_headers: AuthHeaders,
) -> None:
    """Les 97 en attente ne partent PAS quand le réglage bascule : une relance
    de trois semaines (« l'étape n'a pas progressé depuis 20 jours » alors
    qu'il y en a 41) serait absurde. Elles restent où elles sont."""
    case = await make_client_case(agency_id=manager_agent.agency_id)
    old = datetime.now(UTC) - timedelta(days=17)
    waiting = [
        await make_reminder(case=case, status="to_approve", scheduled_at=old) for _ in range(3)
    ]

    _run_auto(sync_session_local)
    assert _run_dispatch(sync_session_local)["sent"] == 0
    assert email.outbox == []
    for reminder in waiting:
        await db_session.refresh(reminder)
        assert reminder.status == "to_approve"


async def test_bulk_cancel_clears_the_backlog_and_nothing_else(
    rem_client: AsyncClient,
    db_session: AsyncSession,
    manager_agent: Agent,
    make_agent: MakeAgent,
    make_agency: MakeAgency,
    make_client_case: MakeClientCase,
    make_reminder: MakeReminder,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """Le geste de sortie : annuler en masse. Seuls les rappels de SON agence
    et dans un statut annulable bougent ; les ids étrangers sont ignorés, sans
    404 qui confirmerait leur existence."""
    case = await make_client_case(agency_id=manager_agent.agency_id)
    waiting = [await make_reminder(case=case, status="to_approve") for _ in range(3)]
    already_sent = await make_reminder(case=case, status="sent")

    other_agency = await make_agency()
    other_agent = await make_agent(agency_id=other_agency.id, role=system_roles["case_manager"])
    other_case = await make_client_case(agency_id=other_agency.id)
    foreign = await make_reminder(case=other_case, status="to_approve")

    ids = [str(r.id) for r in (*waiting, already_sent, foreign)]
    response = await rem_client.post(
        "/reminders/bulk-cancel",
        headers=agent_headers(manager_agent),
        json={"reminder_ids": ids},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"examined": 5, "affected": 3}

    for reminder in waiting:
        await db_session.refresh(reminder)
        assert reminder.status == "cancelled"
    await db_session.refresh(already_sent)
    assert already_sent.status == "sent"  # un envoi ne s'annule pas
    await db_session.refresh(foreign)
    assert foreign.status == "to_approve"  # l'autre agence, intouchée
    assert other_agent.agency_id == other_agency.id

    # La trace reste PAR rappel — pas de trou « 3 rappels ont disparu ».
    logs = (
        (
            await db_session.execute(
                select(ActivityLog).where(ActivityLog.action_type == "reminder.cancelled")
            )
        )
        .scalars()
        .all()
    )
    assert len(logs) == 3
    assert all(log.details["bulk"] is True for log in logs)


async def test_bulk_cancel_is_gated_like_the_unit_cancel(
    rem_client: AsyncClient,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    viewer = await make_agent(role=system_roles["viewer"])
    refused = await rem_client.post(
        "/reminders/bulk-cancel",
        headers=agent_headers(viewer),
        json={"reminder_ids": [str(uuid.uuid4())]},
    )
    assert refused.status_code == 403, refused.text


# --- bulk approve + LA garde « étape terminée » --------------------------------------


async def _case_with_one_step(
    rem_client: AsyncClient,
    db_session: AsyncSession,
    agent: Agent,
    make_client_case: MakeClientCase,
    headers: dict[str, str],
    *,
    done: bool,
) -> tuple[ClientCase, uuid.UUID]:
    """A case running a one-step journey, that step DONE or still open."""
    template = (await rem_client.post("/journeys", headers=headers, json={"name": "T"})).json()
    await rem_client.post(
        f"/journeys/{template['id']}/steps", headers=headers, json={"name": "Passeport"}
    )
    case = await make_client_case(agency_id=agent.agency_id)
    await rem_client.post(
        f"/cases/{case.id}/journey",
        headers=headers,
        json={"journey_template_id": template["id"]},
    )
    progress_id = (
        await db_session.execute(
            select(CaseStepProgress.id).where(CaseStepProgress.case_id == case.id)
        )
    ).scalar_one()
    if done:
        await db_session.execute(
            update(CaseStepProgress).where(CaseStepProgress.id == progress_id).values(status="done")
        )
        await db_session.commit()
    return case, progress_id


async def test_bulk_approve_clears_the_backlog_and_nothing_else(
    rem_client: AsyncClient,
    db_session: AsyncSession,
    manager_agent: Agent,
    make_agent: MakeAgent,
    make_agency: MakeAgency,
    make_client_case: MakeClientCase,
    make_reminder: MakeReminder,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """Le miroir de bulk-cancel : l'agence qui veut que son passif PARTE.
    Seuls les rappels de SON agence encore `to_approve` bougent ; un déjà
    approuvé, un envoyé, un id étranger sont ignorés — sans 404 qui
    confirmerait leur existence."""
    case = await make_client_case(agency_id=manager_agent.agency_id)
    waiting = [await make_reminder(case=case, status="to_approve") for _ in range(3)]
    already_approved = await make_reminder(case=case, status="approved")
    already_sent = await make_reminder(case=case, status="sent")

    other_agency = await make_agency()
    other_agent = await make_agent(agency_id=other_agency.id, role=system_roles["case_manager"])
    other_case = await make_client_case(agency_id=other_agency.id)
    foreign = await make_reminder(case=other_case, status="to_approve")

    ids = [str(r.id) for r in (*waiting, already_approved, already_sent, foreign)]
    response = await rem_client.post(
        "/reminders/bulk-approve",
        headers=agent_headers(manager_agent),
        json={"reminder_ids": ids},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"examined": 6, "affected": 3, "skipped_step_done": 0}

    for reminder in waiting:
        await db_session.refresh(reminder)
        assert reminder.status == "approved"
        assert reminder.approved_by_agent_id == manager_agent.id  # qui a engagé l'agence
    await db_session.refresh(already_sent)
    assert already_sent.status == "sent"  # un envoi ne se ré-approuve pas
    await db_session.refresh(foreign)
    assert foreign.status == "to_approve"  # l'autre agence, intouchée
    assert other_agent.agency_id == other_agency.id

    # La trace reste PAR rappel — pas de trou « 3 rappels sont partis ».
    logs = (
        (
            await db_session.execute(
                select(ActivityLog).where(ActivityLog.action_type == "reminder.approved")
            )
        )
        .scalars()
        .all()
    )
    assert len(logs) == 3
    assert all(log.details["bulk"] is True for log in logs)
    assert all(log.details["approved_by"] == str(manager_agent.id) for log in logs)


async def test_bulk_approve_skips_the_reminders_whose_step_is_done(
    rem_client: AsyncClient,
    db_session: AsyncSession,
    manager_agent: Agent,
    make_client_case: MakeClientCase,
    make_reminder: MakeReminder,
    agent_headers: AuthHeaders,
) -> None:
    """LES 7 CAS DU CONSTAT : une relance « votre étape n'a pas progressé »
    sur une étape TERMINÉE est fausse, pas seulement tardive. En masse on
    l'écarte — et on le DIT (`skipped_step_done`) : le lot de 85 ne doit pas
    échouer en bloc parce que 7 étapes ont été validées entre-temps."""
    headers = agent_headers(manager_agent)
    live_case, live_step = await _case_with_one_step(
        rem_client, db_session, manager_agent, make_client_case, headers, done=False
    )
    done_case, done_step = await _case_with_one_step(
        rem_client, db_session, manager_agent, make_client_case, headers, done=True
    )
    on_live_step = await make_reminder(
        case=live_case, status="to_approve", step_progress_id=live_step
    )
    unlinked = await make_reminder(case=live_case, status="to_approve")
    on_done_step = await make_reminder(
        case=done_case, status="to_approve", step_progress_id=done_step
    )

    response = await rem_client.post(
        "/reminders/bulk-approve",
        headers=headers,
        json={"reminder_ids": [str(on_live_step.id), str(unlinked.id), str(on_done_step.id)]},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"examined": 3, "affected": 2, "skipped_step_done": 1}

    await db_session.refresh(on_live_step)
    assert on_live_step.status == "approved"
    await db_session.refresh(unlinked)
    assert unlinked.status == "approved"  # sans étape liée, rien ne prétend qu'elle stagne
    await db_session.refresh(on_done_step)
    assert on_done_step.status == "to_approve"  # écartée, pas annulée : bulk-cancel la solde
    assert on_done_step.approved_by_agent_id is None


async def test_unit_approve_refuses_a_reminder_whose_step_is_done(
    rem_client: AsyncClient,
    db_session: AsyncSession,
    manager_agent: Agent,
    make_client_case: MakeClientCase,
    make_reminder: MakeReminder,
    agent_headers: AuthHeaders,
) -> None:
    """À l'unité, la même règle REFUSE (409) : un geste explicite mérite une
    réponse explicite, pas un silence. Détacher l'étape rouvre la porte."""
    headers = agent_headers(manager_agent)
    case, done_step = await _case_with_one_step(
        rem_client, db_session, manager_agent, make_client_case, headers, done=True
    )
    reminder = await make_reminder(case=case, status="to_approve", step_progress_id=done_step)

    refused = await rem_client.post(f"/reminders/{reminder.id}/approve", headers=headers)
    assert refused.status_code == 409, refused.text
    assert "already done" in refused.json()["detail"]
    await db_session.refresh(reminder)
    assert reminder.status == "to_approve"

    # L'échappatoire nommée par le message : détacher l'étape.
    unlinked = await rem_client.patch(
        f"/reminders/{reminder.id}", headers=headers, json={"step_progress_id": None}
    )
    assert unlinked.status_code == 200, unlinked.text
    approved = await rem_client.post(f"/reminders/{reminder.id}/approve", headers=headers)
    assert approved.status_code == 200, approved.text


async def test_dispatch_cancels_instead_of_sending_on_a_done_step(
    rem_client: AsyncClient,
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    manager_agent: Agent,
    make_client_case: MakeClientCase,
    make_reminder: MakeReminder,
    agent_headers: AuthHeaders,
) -> None:
    """LE dernier chemin. Le régime par défaut crée les relances auto déjà
    APPROUVÉES : si l'étape est validée entre la création et l'échéance, le
    dispatch est le seul endroit qui peut encore l'attraper. Il ANNULE plutôt
    que d'envoyer — laisser la ligne « due » la ferait rejouer à chaque tick,
    la file morte dont on vient de sortir."""
    headers = agent_headers(manager_agent)
    case, done_step = await _case_with_one_step(
        rem_client, db_session, manager_agent, make_client_case, headers, done=True
    )
    false_one = await make_reminder(
        case=case, status="approved", scheduled_at=_PAST, step_progress_id=done_step
    )
    still_true = await make_reminder(case=case, status="approved", scheduled_at=_PAST)

    stats = _run_dispatch(sync_session_local)
    assert stats == {"due": 2, "sent": 1, "skipped_step_done": 1}
    assert len(email.outbox) == 1  # l'autre est bien partie

    await db_session.refresh(false_one)
    assert false_one.status == "cancelled"
    await db_session.refresh(still_true)
    assert still_true.status == "sent"

    # Rien ne disparaît en silence : la trace dit POURQUOI.
    log = (
        await db_session.execute(
            select(ActivityLog).where(ActivityLog.action_type == "reminder.cancelled")
        )
    ).scalar_one()
    assert log.actor_type == "system"
    assert log.details == {"reminder_id": str(false_one.id), "reason": "step_done"}

    # Et le tick suivant ne la rejoue pas.
    assert _run_dispatch(sync_session_local) == {"due": 0, "sent": 0, "skipped_step_done": 0}


async def test_bulk_approve_is_gated_like_the_unit_approve_not_like_the_cancel(
    rem_client: AsyncClient,
    make_agent: MakeAgent,
    make_role: MakeRole,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """La porte du lot n'est pas une porte dérobée : approuver 85, c'est
    approuver. Un agent qui ÉCRIT et ANNULE les rappels (reminder.create)
    mais n'a pas reminder.approve est refusé sur bulk-approve — et passe sur
    bulk-cancel, la preuve que c'est bien la gate qui parle."""
    viewer = await make_agent(role=system_roles["viewer"])
    refused = await rem_client.post(
        "/reminders/bulk-approve",
        headers=agent_headers(viewer),
        json={"reminder_ids": [str(uuid.uuid4())]},
    )
    assert refused.status_code == 403, refused.text

    writer_role = await make_role(permissions=[Permission.CASE_VIEW, Permission.REMINDER_CREATE])
    writer = await make_agent(role=writer_role)
    body = {"reminder_ids": [str(uuid.uuid4())]}
    assert (
        await rem_client.post("/reminders/bulk-approve", headers=agent_headers(writer), json=body)
    ).status_code == 403
    assert (
        await rem_client.post("/reminders/bulk-cancel", headers=agent_headers(writer), json=body)
    ).status_code == 200
