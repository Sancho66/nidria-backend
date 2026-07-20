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
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, sessionmaker

from shared.models.activity import ActivityLog
from shared.models.agent import Agent
from shared.models.auth_tokens import PasswordResetToken
from shared.models.case_step_progress import CaseStepProgress
from shared.models.external_contact import ExternalContact
from shared.models.invitation import AgentInvitation
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
    # P2 (2026-07-20): the external FK is KEPT as provenance — the auto-pass
    # idempotence matches on it (an escalated line still blocks its threshold).
    assert reminder.recipient_external_id == contact.id

    log = (
        await db_session.execute(
            select(ActivityLog).where(ActivityLog.action_type == "reminder.escalated")
        )
    ).scalar_one()
    assert log.details["escalated_from"] == "Notaire Muet"


# --- (a) GET /agencies/me/external-contacts ------------------------------------------


async def test_directory_list_shows_nature_and_isolates(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    external_role: Role,
    make_agency: MakeAgency,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    account = await _account(make_agent, external_role, admin.agency_id, "acc@a.io")
    await _contact(db_session, admin.agency_id, "Designe", agent_id=account.id)
    await _contact(db_session, admin.agency_id, "Sans acces")
    other = await make_agency(name="Other")
    await _contact(db_session, other.id, "Etranger")  # another agency

    rows = (await client.get("/agencies/me/external-contacts", headers=headers)).json()
    by_name = {r["name"]: r for r in rows}
    assert by_name["Sans acces"]["agent_id"] is None
    assert by_name["Designe"]["agent_id"] == str(account.id)
    assert by_name["Designe"]["agent_role"] == external_role.name
    assert "Etranger" not in by_name  # cross-agency never appears

    # 403 without agent.manage
    member = await make_agent(role=system_roles["member"], email="member@a.io")
    assert (
        await client.get("/agencies/me/external-contacts", headers=agent_headers(member))
    ).status_code == 403


# --- (b) invite an existing contact — the id NEVER changes (the invariant) -----------


async def test_invite_keeps_contact_id_and_assignment(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    external_role: Role,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    contact = await _contact(db_session, admin.agency_id, "Notaire Nicolas")
    contact_id = contact.id

    # An assignment posed BEFORE the invitation: a template participant.
    tid = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()["id"]
    step = (
        await client.post(
            f"/journeys/{tid}/steps",
            headers=headers,
            json={"name": "S", "completion_mode": "agency_validation"},
        )
    ).json()
    part = (
        await client.post(
            f"/journeys/{tid}/steps/{step['id']}/participants",
            headers=headers,
            json={"type": "external", "external_id": str(contact_id), "role": "executant"},
        )
    ).json()
    assert part["external_id"] == str(contact_id) and part["name"] == "Notaire Nicolas"

    # Invite the contact → 201.
    inv = await client.post(
        f"/agencies/me/external-contacts/{contact_id}/invite",
        headers=headers,
        json={"email": "nicolas@x.io", "role_id": str(external_role.id)},
    )
    assert inv.status_code == 201, inv.text

    # Accept the invitation → the Agent is created, the contact is DESIGNATED.
    token = (
        await db_session.execute(
            select(AgentInvitation.token).where(AgentInvitation.id == uuid.UUID(inv.json()["id"]))
        )
    ).scalar_one()
    acc = await client.post(
        "/agencies/invitations/accept",
        json={"token": token, "password": "password123", "first_name": "N", "last_name": "N"},
    )
    assert acc.status_code == 200, acc.text

    # INVARIANT: the contact id is UNCHANGED, its agent_id is now set, and the
    # participant posed before still points to the SAME contact.
    agent_id_after = (
        await db_session.execute(
            select(ExternalContact.agent_id).where(ExternalContact.id == contact_id)
        )
    ).scalar_one()
    assert agent_id_after is not None
    part_ext = (
        await db_session.execute(
            select(JourneyStepParticipant.external_id).where(
                JourneyStepParticipant.id == uuid.UUID(part["id"])
            )
        )
    ).scalar_one()
    assert part_ext == contact_id  # assignment never repointed


async def test_invite_contact_with_account_is_409(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    external_role: Role,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
) -> None:
    account = await _account(make_agent, external_role, admin.agency_id, "acc@a.io")
    contact = await _contact(db_session, admin.agency_id, "Deja compte", agent_id=account.id)
    r = await client.post(
        f"/agencies/me/external-contacts/{contact.id}/invite",
        headers=agent_headers(admin),
        json={"email": "new@x.io", "role_id": str(external_role.id)},
    )
    assert r.status_code == 409, r.text


async def test_invite_email_taken_is_409(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    external_role: Role,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
) -> None:
    await make_agent(
        agency_id=admin.agency_id, role=external_role, is_external=True, email="taken@x.io"
    )
    contact = await _contact(db_session, admin.agency_id, "Nouveau")
    r = await client.post(
        f"/agencies/me/external-contacts/{contact.id}/invite",
        headers=agent_headers(admin),
        json={"email": "taken@x.io", "role_id": str(external_role.id)},
    )
    assert r.status_code == 409, r.text


# --- (c) a participant with both ids → a READABLE error, never a raw 500 -------------


async def test_participant_both_ids_is_readable_error(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    contact = await _contact(db_session, admin.agency_id, "Ambigu")
    tid = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()["id"]
    step = (
        await client.post(
            f"/journeys/{tid}/steps",
            headers=headers,
            json={"name": "S", "completion_mode": "agency_validation"},
        )
    ).json()
    r = await client.post(
        f"/journeys/{tid}/steps/{step['id']}/participants",
        headers=headers,
        json={
            "type": "external",
            "agent_id": str(admin.id),
            "external_id": str(contact.id),
            "role": "executant",
        },
    )
    assert r.status_code in (400, 422), r.text  # readable, NOT a 500 IntegrityError


# --- accept_invitation: the SHARED path stays safe for the 3 cases -------------------

_ACCEPT = {"password": "password123", "first_name": "N", "last_name": "N"}


async def _contact_count(db_session: AsyncSession) -> int:
    return (
        await db_session.execute(select(func.count()).select_from(ExternalContact))
    ).scalar_one()


async def test_accept_internal_member_invitation_is_unchanged(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """Case 1: an INTERNAL member accept is untouched — no external_contact,
    no agent_id designation, is_external False."""
    inv = await client.post(
        "/agencies/me/invitations",
        headers=agent_headers(admin),
        json={"email": "member@x.io", "role_id": str(system_roles["member"].id)},
    )
    assert inv.status_code == 201, inv.text
    token = (
        await db_session.execute(
            select(AgentInvitation.token).where(AgentInvitation.id == uuid.UUID(inv.json()["id"]))
        )
    ).scalar_one()

    acc = await client.post("/agencies/invitations/accept", json={"token": token, **_ACCEPT})
    assert acc.status_code == 200, acc.text
    assert await _contact_count(db_session) == 0  # NO external_contact created
    new_agent = (
        await db_session.execute(select(Agent).where(Agent.email == "member@x.io"))
    ).scalar_one()
    assert new_agent.is_external is False


async def test_accept_legacy_external_invitation_without_contact_works(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    external_role: Role,
) -> None:
    """Case 2: an EXTERNAL invitation IN FLIGHT before the migration
    (external_contact_id NULL) accepts fine — the fallback creates the Agent
    (and a directory contact), never crashes."""
    token = uuid.uuid4().hex
    db_session.add(
        AgentInvitation(
            agency_id=admin.agency_id,
            email="legacy@x.io",
            role_id=external_role.id,
            token=token,
            expires_at=datetime.now(UTC) + timedelta(days=7),
            invited_by_agent_id=admin.id,
            external_contact_id=None,  # in-flight legacy invite
        )
    )
    await db_session.commit()

    acc = await client.post("/agencies/invitations/accept", json={"token": token, **_ACCEPT})
    assert acc.status_code == 200, acc.text
    agent = (
        await db_session.execute(select(Agent).where(Agent.email == "legacy@x.io"))
    ).scalar_one()
    assert agent.is_external is True
    # Fallback created a directory contact linked to the new account.
    linked = (
        await db_session.execute(
            select(func.count())
            .select_from(ExternalContact)
            .where(ExternalContact.agent_id == agent.id)
        )
    ).scalar_one()
    assert linked == 1


async def test_accept_new_external_invitation_links_existing_contact(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    external_role: Role,
    agent_headers: AuthHeaders,
) -> None:
    """Case 3: an EXTERNAL invitation with external_contact_id set links the
    EXISTING contact (agent_id posed) — no duplicate row."""
    contact = await _contact(db_session, admin.agency_id, "Notaire Lie")
    inv = await client.post(
        f"/agencies/me/external-contacts/{contact.id}/invite",
        headers=agent_headers(admin),
        json={"email": "lie@x.io", "role_id": str(external_role.id)},
    )
    assert inv.status_code == 201, inv.text
    token = (
        await db_session.execute(
            select(AgentInvitation.token).where(AgentInvitation.id == uuid.UUID(inv.json()["id"]))
        )
    ).scalar_one()

    acc = await client.post("/agencies/invitations/accept", json={"token": token, **_ACCEPT})
    assert acc.status_code == 200, acc.text
    assert await _contact_count(db_session) == 1  # the SAME row, no duplicate
    agent_id_after = (
        await db_session.execute(
            select(ExternalContact.agent_id).where(ExternalContact.id == contact.id)
        )
    ).scalar_one()
    assert agent_id_after is not None


# --- agent_id at invite + access_state + the SECURITY gates ---------------------------


async def _invite_provider(
    client: AsyncClient,
    db_session: AsyncSession,
    headers: dict[str, str],
    external_role: Role,
    *,
    name: str,
    email_addr: str,
) -> str:
    """Invite a brand-new provider (creates the Agent at invite, PENDING).
    Returns the invitation token."""
    inv = await client.post(
        "/agencies/me/external-invitations",
        headers=headers,
        json={"name": name, "email": email_addr, "role_id": str(external_role.id)},
    )
    assert inv.status_code == 201, inv.text
    return (
        await db_session.execute(
            select(AgentInvitation.token).where(AgentInvitation.id == uuid.UUID(inv.json()["id"]))
        )
    ).scalar_one()


async def _row(client: AsyncClient, headers: dict[str, str], name: str) -> dict:
    rows = (await client.get("/agencies/me/external-contacts", headers=headers)).json()
    return next(r for r in rows if r["name"] == name)


async def test_invite_poses_agent_id_and_invited_state_with_mail(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    external_role: Role,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    await _invite_provider(
        client, db_session, headers, external_role, name="Notaire X", email_addr="nx@x.io"
    )
    row = await _row(client, headers, "Notaire X")
    assert row["access_state"] == "invited"  # honest immediately, not a false "none"
    assert row["agent_id"] is not None  # agent_id posed AT INVITE
    assert row["invited_at"] is not None
    assert any(m.to == "nx@x.io" for m in email.outbox)  # mail parti, un geste


async def test_login_pending_provider_refused_indistinguishable(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    external_role: Role,
    agent_headers: AuthHeaders,
) -> None:
    await _invite_provider(
        client, db_session, agent_headers(admin), external_role, name="P", email_addr="p@x.io"
    )
    pending = await client.post(
        "/auth/agent/login", json={"email": "p@x.io", "password": "whatever12"}
    )
    unknown = await client.post(
        "/auth/agent/login", json={"email": "nobody@x.io", "password": "whatever12"}
    )
    assert pending.status_code == 401
    # Indistinguishable from an unknown account / wrong password.
    assert pending.status_code == unknown.status_code and pending.json() == unknown.json()


async def test_forgot_password_pending_provider_sends_no_mail(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    external_role: Role,
    agent_headers: AuthHeaders,
) -> None:
    await _invite_provider(
        client, db_session, agent_headers(admin), external_role, name="Q", email_addr="q@x.io"
    )
    email.outbox.clear()  # drop the invite mail
    pending = await client.post("/auth/agent/forgot-password", json={"email": "q@x.io"})
    unknown = await client.post("/auth/agent/forgot-password", json={"email": "nobody@x.io"})
    assert pending.status_code == 200 and pending.json() == unknown.json()  # identical
    assert email.outbox == []  # PROVE no mail went out, not just the code


async def test_reset_password_forced_token_pending_refused(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    external_role: Role,
    agent_headers: AuthHeaders,
) -> None:
    await _invite_provider(
        client, db_session, agent_headers(admin), external_role, name="R", email_addr="r@x.io"
    )
    agent = (await db_session.execute(select(Agent).where(Agent.email == "r@x.io"))).scalar_one()
    tok = uuid.uuid4().hex
    db_session.add(
        PasswordResetToken(
            actor_type="agent",
            actor_id=agent.id,
            token=tok,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    await db_session.commit()
    r = await client.post(
        "/auth/agent/reset-password", json={"token": tok, "password": "newpass123"}
    )
    assert r.status_code == 400  # a stray token cannot activate a pending provider


async def test_accept_activates_reopens_login_and_forgot(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    external_role: Role,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    token = await _invite_provider(
        client, db_session, headers, external_role, name="S", email_addr="s@x.io"
    )
    contact_id_before = (
        await db_session.execute(select(ExternalContact.id).where(ExternalContact.name == "S"))
    ).scalar_one()

    acc = await client.post(
        "/agencies/invitations/accept",
        json={"token": token, "password": "realpass123", "first_name": "Sam", "last_name": "Pro"},
    )
    assert acc.status_code == 200, acc.text

    row = await _row(client, headers, "S")
    assert row["access_state"] == "active" and row["invited_at"] is None
    assert uuid.UUID(row["id"]) == contact_id_before  # id NEVER changes

    login = await client.post(
        "/auth/agent/login", json={"email": "s@x.io", "password": "realpass123"}
    )
    assert login.status_code == 200, login.text  # login reopened

    email.outbox.clear()
    fp = await client.post("/auth/agent/forgot-password", json={"email": "s@x.io"})
    assert fp.status_code == 200
    assert any(m.to == "s@x.io" for m in email.outbox)  # forgot-password reopened


async def test_internal_invite_has_no_pre_accept_hole(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """Non-regression: the INTERNAL flow creates the Agent at ACCEPT, so before
    acceptance NO account exists — forgot-password is silent, no mail, no hole.
    (This is the answer to 'does internal allow forgot-password before accept':
    no, because there is no account yet.)"""
    inv = await client.post(
        "/agencies/me/invitations",
        headers=agent_headers(admin),
        json={"email": "m@x.io", "role_id": str(system_roles["member"].id)},
    )
    assert inv.status_code == 201, inv.text
    no_agent = (
        await db_session.execute(select(Agent).where(Agent.email == "m@x.io"))
    ).scalar_one_or_none()
    assert no_agent is None  # NO agent before accept (unlike the external flow)

    email.outbox.clear()
    fp = await client.post("/auth/agent/forgot-password", json={"email": "m@x.io"})
    assert fp.status_code == 200
    assert email.outbox == []  # silent, no mail (no account to reset)
