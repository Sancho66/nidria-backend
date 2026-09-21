"""Per-operation invitation choice, including deferred and scheduler paths."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, sessionmaker

from shared.models.activity import ActivityLog
from shared.models.case_person import CasePerson
from shared.models.case_step_progress import CaseStepProgress
from shared.models.client_case import ClientCase
from shared.models.expat_user import ExpatUser
from shared.models.invitation import CaseInvitation
from shared.models.usage import UsageEvent
from src.cases.activation_jobs import send_activation_reminders
from src.cases.cases_manager import CasesManager
from src.cases.cases_schema import CaseCreateRequest
from src.core import email
from src.core.database import get_db
from src.main import app

pytestmark = pytest.mark.usefixtures("rbac_baseline")


def payload(address: str = "isolated@example.com", **kwargs: Any) -> dict[str, Any]:
    return {"first_name": "Synthetic", "last_name": "Client", "email": address, **kwargs}


@pytest.mark.parametrize("identity", ["new", "inactive", "active"])
@pytest.mark.parametrize("option", [False, True, "absent"])
async def test_creation_choice(
    client,
    db_session,
    make_agent,
    system_roles,
    agent_headers,
    make_expat_user,
    make_journey_template,
    make_template_step,
    identity,
    option,
):
    agent = await make_agent(role=system_roles["admin"])
    existing = None
    if identity != "new":
        existing = await make_expat_user(
            email="isolated@example.com", activated=identity == "active"
        )
    previous_activation = existing.activated_at if existing else None
    previous_password = existing.password_hash if existing else None
    template = await make_journey_template(agency_id=agent.agency_id)
    await make_template_step(template=template)
    data = payload(journey_template_id=str(template.id))
    if option != "absent":
        data["send_invitation"] = option
    response = await client.post("/cases", headers=agent_headers(agent), json=data)
    assert response.status_code == 201, response.text
    case_id = uuid.UUID(response.json()["id"])
    case = await db_session.get(ClientCase, case_id)
    assert case.agency_id == agent.agency_id
    assert case.journey_template_id == template.id
    principal = (
        await db_session.scalars(select(CasePerson).where(CasePerson.case_id == case_id))
    ).one()
    expat = await db_session.get(ExpatUser, principal.expat_user_id)
    assert expat.activated_at == previous_activation
    assert expat.password_hash == previous_password
    assert principal.client_profile_id is not None
    assert (
        len(
            (
                await db_session.scalars(
                    select(CaseStepProgress).where(CaseStepProgress.case_id == case_id)
                )
            ).all()
        )
        == 1
    )
    expected = 0 if option is False else 1
    assert len(email.outbox) == expected
    assert (
        len(
            (
                await db_session.scalars(
                    select(CaseInvitation).where(CaseInvitation.case_id == case_id)
                )
            ).all()
        )
        == expected
    )
    actions = (
        await db_session.scalars(
            select(ActivityLog.action_type).where(ActivityLog.case_id == case_id)
        )
    ).all()
    assert "case.created" in actions
    assert ("case.invitation_sent" in actions) == bool(expected)
    events = (
        await db_session.scalars(select(UsageEvent.event_type).where(UsageEvent.case_id == case_id))
    ).all()
    assert "case.created" in events
    assert ("case.client_invited" in events) == bool(expected)


@pytest.mark.parametrize("invalid", ["false", "true", "", None, 0, 1, [], {}])
async def test_strict_boolean(client, db_session, make_agent, system_roles, agent_headers, invalid):
    agent = await make_agent(role=system_roles["admin"])
    response = await client.post(
        "/cases", headers=agent_headers(agent), json=payload(send_invitation=invalid)
    )
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"][-1] == "send_invitation"
    assert await db_session.scalar(select(func.count()).select_from(ClientCase)) == 0
    assert not email.outbox


async def test_deferred_and_103_batch_never_reappear(
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    make_agent,
    system_roles,
    make_journey_template,
    make_template_step,
):
    agent = await make_agent(role=system_roles["admin"])
    template = await make_journey_template(agency_id=agent.agency_id)
    await make_template_step(template=template)
    pending: list[email.PendingEmail] = []
    for number in range(103):
        await CasesManager(db_session).create_case(
            agent,
            CaseCreateRequest(
                **payload(
                    f"synthetic-{number}@example.com",
                    send_invitation=False,
                    journey_template_id=template.id,
                )
            ),
            email_sink=pending,
        )
    assert await db_session.scalar(select(func.count()).select_from(ClientCase)) == 103
    assert await db_session.scalar(select(func.count()).select_from(CasePerson)) == 103
    assert await db_session.scalar(select(func.count()).select_from(CaseStepProgress)) == 103
    assert await db_session.scalar(select(func.count()).select_from(CaseInvitation)) == 0
    assert pending == []
    assert email.outbox == []
    for _ in range(2):
        with sync_session_local() as db:
            assert send_activation_reminders(db, log=lambda _: None)["sent"] == 0
    # A later explicit creation for the SAME identity still invites normally.
    await CasesManager(db_session).create_case(
        agent,
        CaseCreateRequest(**payload("synthetic-0@example.com")),
        email_sink=pending,
    )
    assert len(pending) == 1
    assert not email.outbox
    await db_session.execute(
        update(CaseInvitation).values(created_at=datetime.now(UTC) - timedelta(days=4))
    )
    await db_session.commit()
    with sync_session_local() as db:
        assert send_activation_reminders(db, log=lambda _: None)["sent"] == 1
    assert len(email.outbox) == 1


async def test_concurrent_agencies_do_not_share_choice(
    client,
    async_engine,
    make_agent,
    system_roles,
    agent_headers,
    db_session,
):
    agents = [await make_agent(role=system_roles["admin"]) for _ in range(2)]
    assert agents[0].agency_id != agents[1].agency_id
    factory = async_sessionmaker(async_engine, expire_on_commit=False)

    async def isolated_session():
        async with factory() as db:
            yield db

    previous = app.dependency_overrides[get_db]
    app.dependency_overrides[get_db] = isolated_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            results = await asyncio.gather(
                *[
                    ac.post(
                        "/cases",
                        headers=agent_headers(agent),
                        json=payload(
                            f"concurrent-{index}@example.com",
                            send_invitation=bool(index),
                        ),
                    )
                    for index, agent in enumerate(agents)
                ]
            )
    finally:
        app.dependency_overrides[get_db] = previous
    assert [r.status_code for r in results] == [201, 201]
    assert [r.json()["agency_id"] for r in results] == [str(a.agency_id) for a in agents]
    assert len(email.outbox) == 1
    assert email.outbox[0].to == "concurrent-1@example.com"
    assert await db_session.scalar(select(func.count()).select_from(CaseInvitation)) == 1


async def test_family_creation_respects_form_choice(
    client,
    db_session,
    make_agent,
    system_roles,
    agent_headers,
):
    agent = await make_agent(role=system_roles["admin"])
    headers = agent_headers(agent)
    response = await client.post("/cases", headers=headers, json=payload(send_invitation=False))
    case_id = response.json()["id"]
    member = await client.post(
        f"/cases/{case_id}/persons",
        headers=headers,
        json={
            "full_name": "Synthetic Member",
            "relationship": "spouse",
            "email": "synthetic-member@example.com",
            "send_invitation": False,
        },
    )
    assert member.status_code == 201, member.text
    assert await db_session.scalar(select(func.count()).select_from(ExpatUser)) == 2
    assert await db_session.scalar(select(func.count()).select_from(CaseInvitation)) == 0
    assert not email.outbox
