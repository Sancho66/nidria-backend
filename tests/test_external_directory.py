"""Provider directory (external_contact agency scope) — the merge condition.

- 4 ISOLATION tests on the portal designation: a directory contact
  designates an Agent (external_contact.agent_id); the account then sees
  exactly the cases where that contact is responsible, and nothing else.
- a TEMPLATE participant type=external propagates to cases by reference at
  assignment (no account) and the step shows the contact name.
- the participant CHECK refuses agent_id AND external_id together.
- the hard constraint (no token / no invitation / no seat) and the reminder
  escalation to the owner (a reminder never dies in silence).
"""

import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, sessionmaker

from shared.models.activity import ActivityLog
from shared.models.agent import Agent
from shared.models.case_step_progress import CaseStepProgress
from shared.models.external_contact import ExternalContact
from shared.models.journey import JourneyStepParticipant
from shared.models.rbac import Role
from shared.models.reminder import Reminder
from src.core import email
from src.reminders.reminders_jobs import dispatch_due_reminders
from tests.plugins.agency_plugin import MakeAgency
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase, MakeExternalContact
from tests.plugins.expat_plugin import MakeExpatUser

pytestmark = pytest.mark.usefixtures("rbac_baseline")

_PAST = datetime(2020, 1, 1, tzinfo=UTC)


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


@pytest_asyncio.fixture
async def external_role(db_session: AsyncSession) -> Role:
    return (
        await db_session.execute(
            select(Role).where(Role.is_external.is_(True), Role.name == "external_lawyer")
        )
    ).scalar_one()


@pytest_asyncio.fixture
async def expat(make_expat_user: MakeExpatUser) -> Agent:
    return await make_expat_user(email="client@x.io")


# --- helpers -------------------------------------------------------------------------


async def _contact(
    db_session: AsyncSession, agency_id: uuid.UUID, name: str, agent_id: uuid.UUID | None = None
) -> ExternalContact:
    c = ExternalContact(
        agency_id=agency_id, case_id=None, name=name, type="notary", agent_id=agent_id
    )
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)
    return c


async def _account(
    make_agent: MakeAgent, external_role: Role, agency_id: uuid.UUID, email_addr: str
) -> Agent:
    return await make_agent(
        agency_id=agency_id, role=external_role, is_external=True, email=email_addr
    )


async def _case_with_step(
    client: AsyncClient, headers: dict[str, str], admin: Agent, expat: Agent, make_client_case
) -> tuple[uuid.UUID, uuid.UUID]:
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    tid = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()["id"]
    await client.post(
        f"/journeys/{tid}/steps",
        headers=headers,
        json={"name": "S", "completion_mode": "agency_validation"},
    )
    steps = (
        await client.post(
            f"/cases/{case.id}/journey", headers=headers, json={"journey_template_id": tid}
        )
    ).json()
    return case.id, uuid.UUID(steps[0]["id"])


async def _set_responsible(db_session: AsyncSession, pid: uuid.UUID, contact_id: uuid.UUID) -> None:
    await db_session.execute(
        update(CaseStepProgress)
        .where(CaseStepProgress.id == pid)
        .values(
            responsible_type="external",
            responsible_agent_id=None,
            responsible_external_id=contact_id,
        )
    )
    await db_session.commit()


async def _my_case_ids(client: AsyncClient, account_headers: dict[str, str]) -> set[str]:
    resp = await client.get("/external/cases", headers=account_headers)
    assert resp.status_code == 200, resp.text
    return {c["id"] for c in resp.json()}


# --- 1. designated account sees its cases and nothing else ---------------------------


async def test_designated_account_sees_its_three_cases(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    external_role: Role,
    expat: Agent,
    make_agent: MakeAgent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    account = await _account(make_agent, external_role, admin.agency_id, "notary@a.io")
    contact = await _contact(db_session, admin.agency_id, "Notaire A", agent_id=account.id)

    my_cases = set()
    for _ in range(3):
        cid, pid = await _case_with_step(client, headers, admin, expat, make_client_case)
        await _set_responsible(db_session, pid, contact.id)
        my_cases.add(str(cid))

    assert await _my_case_ids(client, agent_headers(account)) == my_cases


# --- 2. evasion: no case of another contact of the same agency -----------------------


async def test_account_does_not_see_another_contacts_case(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    external_role: Role,
    expat: Agent,
    make_agent: MakeAgent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    account_a = await _account(make_agent, external_role, admin.agency_id, "a@a.io")
    contact_a = await _contact(db_session, admin.agency_id, "Notaire A", agent_id=account_a.id)
    contact_b = await _contact(db_session, admin.agency_id, "Notaire B")  # designates no one

    cid_a, pid_a = await _case_with_step(client, headers, admin, expat, make_client_case)
    await _set_responsible(db_session, pid_a, contact_a.id)
    cid_b, pid_b = await _case_with_step(client, headers, admin, expat, make_client_case)
    await _set_responsible(db_session, pid_b, contact_b.id)

    seen = await _my_case_ids(client, agent_headers(account_a))
    assert str(cid_a) in seen
    assert str(cid_b) not in seen  # contact_b's case is invisible to account_a


# --- 3. cross-tenant: nothing of another agency, even via a designation --------------


async def test_cross_tenant_designation_grants_nothing(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    external_role: Role,
    expat: Agent,
    make_agency: MakeAgency,
    make_agent: MakeAgent,
    make_client_case: MakeClientCase,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    account_a = await _account(make_agent, external_role, admin.agency_id, "a@a.io")

    # Agency B, with its OWN case, and a contact that (maliciously) designates
    # account_a of agency A.
    agency_b = await make_agency(name="B")
    admin_b = await make_agent(
        agency_id=agency_b.id, role=system_roles["admin"], email="adminb@b.io"
    )
    contact_b = await _contact(db_session, agency_b.id, "Notaire B", agent_id=account_a.id)
    cid_b, pid_b = await _case_with_step(
        client, agent_headers(admin_b), admin_b, expat, make_client_case
    )
    await _set_responsible(db_session, pid_b, contact_b.id)

    # account_a (agency A) sees NOTHING of agency B, designation notwithstanding.
    assert str(cid_b) not in await _my_case_ids(client, agent_headers(account_a))


# --- 4. a contact WITHOUT agent_id grants visibility to no one -----------------------


async def test_contact_without_agent_id_grants_nothing(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    external_role: Role,
    expat: Agent,
    make_agent: MakeAgent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    orphan_contact = await _contact(db_session, admin.agency_id, "Sans acces")  # agent_id NULL
    cid, pid = await _case_with_step(client, headers, admin, expat, make_client_case)
    await _set_responsible(db_session, pid, orphan_contact.id)

    # A fresh external account (no designation) sees nothing at all.
    lonely = await _account(make_agent, external_role, admin.agency_id, "lonely@a.io")
    assert await _my_case_ids(client, agent_headers(lonely)) == set()


# --- 5. a template participant type=external propagates to cases (no account) ---------


async def test_template_external_participant_propagates_to_case(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    expat: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    contact = await _contact(db_session, admin.agency_id, "Maitre Nicolas")  # no account

    tid = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()["id"]
    step = (
        await client.post(
            f"/journeys/{tid}/steps",
            headers=headers,
            json={"name": "S", "completion_mode": "agency_validation"},
        )
    ).json()
    # Template participant type=external (the front will wire this via import;
    # here we set the state directly).
    db_session.add(
        JourneyStepParticipant(
            step_id=uuid.UUID(step["id"]), type="external", external_id=contact.id, role="executant"
        )
    )
    await db_session.commit()

    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    await client.post(
        f"/cases/{case.id}/journey", headers=headers, json={"journey_template_id": tid}
    )

    steps = (await client.get(f"/cases/{case.id}/steps", headers=headers)).json()
    participants = steps[0]["participants"]
    external_ps = [p for p in participants if p["type"] == "external"]
    assert external_ps and external_ps[0]["name"] == "Maitre Nicolas"  # named, no account


# --- 6. a participant can never carry BOTH agent_id and external_id -------------------


async def test_participant_cannot_hold_both_ids(
    db_session: AsyncSession,
    admin: Agent,
) -> None:
    contact = await _contact(db_session, admin.agency_id, "Ambigu")
    db_session.add(
        JourneyStepParticipant(
            step_id=uuid.uuid4(),  # FK won't matter — the CHECK fails first at flush
            type="external",
            agent_id=admin.id,
            external_id=contact.id,
            role="executant",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


# --- hard constraint: a directory contact is no account ------------------------------


async def test_directory_contact_is_not_an_account(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    created = await client.post(
        "/agencies/me/external-contacts",
        headers=headers,
        json={"name": "Annuaire Un", "type": "notary"},
    )
    assert created.status_code == 201, created.text

    # Not an account: no agent_id, not in external-members, not in invitations.
    contact = (
        await db_session.execute(
            select(ExternalContact).where(ExternalContact.name == "Annuaire Un")
        )
    ).scalar_one()
    assert contact.agent_id is None and contact.case_id is None

    members = (await client.get("/agencies/me/external-members", headers=headers)).json()
    assert all(m.get("email") != "Annuaire Un" for m in members)
    invites = (await client.get("/agencies/me/invitations", headers=headers)).json()
    assert invites == [] or all("Annuaire Un" not in str(i) for i in invites)

    # Duplicate directory name → 409.
    dup = await client.post(
        "/agencies/me/external-contacts",
        headers=headers,
        json={"name": "annuaire un"},  # case-insensitive collision
    )
    assert dup.status_code == 409, dup.text


# --- reminder escalation: an unreachable contact routes to the owner -----------------


def _run_dispatch(session_local: sessionmaker[Session]) -> dict:
    with session_local() as db:
        return dispatch_due_reminders(db, log=lambda _: None, dry_run=False)


async def test_unreachable_contact_reminder_escalates_to_owner(
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    admin: Agent,
    expat: Agent,
    make_client_case: MakeClientCase,
    make_external_contact: MakeExternalContact,
) -> None:
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    # A contact with NO email → unreachable by mail.
    contact = await make_external_contact(case=case, name="Notaire Muet", email=None)
    reminder = Reminder(
        case_id=case.id,
        channel="mail",
        scheduled_at=_PAST,
        status="approved",
        recipient_type="external",
        recipient_external_id=contact.id,
        message_body="Merci de fournir la declaration 260Z.",
    )
    db_session.add(reminder)
    await db_session.commit()

    stats = _run_dispatch(sync_session_local)
    assert stats == {"due": 1, "sent": 1}

    # The mail went to the OWNER, not the contact; it wraps the original body
    # and names the contact.
    assert len(email.outbox) == 1
    sent = email.outbox[0]
    assert sent.to == admin.email
    assert "Merci de fournir la declaration 260Z." in sent.body
    assert "Notaire Muet" in sent.body

    await db_session.refresh(reminder)
    assert reminder.status == "sent"
    assert reminder.recipient_type == "agent"  # re-routed to the owner
    assert reminder.recipient_external_id is None

    log = (
        await db_session.execute(
            select(ActivityLog).where(ActivityLog.action_type == "reminder.escalated")
        )
    ).scalar_one()
    assert log.details["escalated_from"] == "Notaire Muet"
