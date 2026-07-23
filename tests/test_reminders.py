"""FEATURE 3 battery. THE ABSOLUTE INVARIANT carries its name below:
nothing is ever sent without human approval — a due TO_APPROVE crosses
a dispatch tick untouched. Mocks everywhere, zero real sends."""

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
from shared.models.client_case import ClientCase
from shared.models.rbac import Role
from shared.models.reminder import Reminder
from src.core import email
from src.reminders.reminders_jobs import create_auto_reminders, dispatch_due_reminders
from tests.plugins.agency_plugin import MakeAgency
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase, MakeExternalContact
from tests.plugins.expat_plugin import MakeExpatUser
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

    assert stats == {"due": 0, "sent": 0}
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
    assert stats == {"due": 1, "sent": 1}
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
    assert _run_dispatch(sync_session_local) == {"due": 0, "sent": 0}
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
    assert auto.status == "to_approve"  # NEVER more than proposed
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
    assert provider.status == "to_approve"  # NEVER auto-sent
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
    assert provider.status == "to_approve"  # NEVER auto-sent
    assert provider.recipient_external_id == contact.id
    assert provider.auto_threshold_days == 5  # the journey threshold, not the default 20
