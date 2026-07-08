"""DELETE /agencies/{id} — HARD agency deletion (Groupe C, superadmin).

Covers: (a) exact name → agency + all its content + storage blobs gone;
(b) wrong name → 422 agency.name_mismatch, nothing deleted; (c) active
non-demo cases without force → 409 agency.has_active_cases, with force →
deleted; (d) a demo-only agency deletes without force; (e) THE isolation
test — a multi-agency expat: deleting A keeps the global account, their
session, and their cases at B; (f) a non-superadmin admin → 403;
(g) the audit trace is written."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agency_deletion_log import AgencyDeletionLog
from shared.models.agent import Agent
from shared.models.auth_tokens import RefreshToken
from shared.models.client_case import ClientCase
from shared.models.document import Document
from shared.models.expat_user import ExpatUser
from shared.models.journey import JourneyStepAttachment, JourneyTemplate, JourneyTemplateStep
from shared.models.rbac import Role
from src.core import storage
from tests.plugins.agency_plugin import MakeAgency
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser

pytestmark = pytest.mark.usefixtures("rbac_baseline")


@pytest_asyncio.fixture
async def superadmin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    # Its OWN home agency (never the target) — survives the deletion.
    return await make_agent(role=system_roles["superadmin"], email="root@platform.io")


async def _agent_with_avatar(db: AsyncSession, agency: Agency, role: Role, email: str) -> Agent:
    agent = Agent(
        agency_id=agency.id,
        role_id=role.id,
        email=email,
        first_name="A",
        last_name="B",
        password_hash="x",
        avatar_path=f"avatars/agent/{uuid.uuid4()}.jpg",
    )
    db.add(agent)
    await db.flush()
    storage.mock_store[agent.avatar_path] = b"avatar"
    return agent


async def _stuffed_agency(
    db: AsyncSession,
    make_agency: MakeAgency,
    make_client_case: MakeClientCase,
    system_roles: dict[str, Role],
    *,
    expat: ExpatUser,
    demo: bool = False,
) -> Agency:
    """An agency with the full ownership graph + storage blobs, for the
    cascade + purge assertions."""
    agency = await make_agency(name="Cible", logo_path="logos/agency/logo.png")
    storage.mock_store["logos/agency/logo.png"] = b"logo"
    agent = await _agent_with_avatar(db, agency, system_roles["admin"], "admin@cible.io")

    # A journey template with a step and a stored attachment.
    template = JourneyTemplate(agency_id=agency.id, name="Parcours")
    db.add(template)
    await db.flush()
    step = JourneyTemplateStep(template_id=template.id, name="Etape", position=0)
    db.add(step)
    await db.flush()
    att_path = f"templates/{template.id}/steps/{step.id}/a.pdf"
    db.add(
        JourneyStepAttachment(step_id=step.id, filename="a.pdf", storage_path=att_path, position=0)
    )
    storage.mock_store[att_path] = b"attachment"

    # A client case (demo or real) with a stored document.
    case = await make_client_case(
        agency_id=agency.id,
        principal_expat_user_id=expat.id,
        owner_agent_id=agent.id,
        is_demo=demo,
        status="in_progress",
    )
    doc_path = f"{case.id}/{uuid.uuid4()}/passport.pdf"
    db.add(
        Document(
            case_id=case.id,
            filename="passport.pdf",
            storage_path=doc_path,
            uploaded_by_type="expat",
            uploaded_by_id=expat.id,
        )
    )
    storage.mock_store[doc_path] = b"doc"
    # An agent refresh token (agency-scoped, must be purged).
    db.add(
        RefreshToken(
            actor_type="agent",
            actor_id=agent.id,
            expires_at=datetime.now(UTC) + timedelta(days=7),
        )
    )
    await db.commit()
    return agency


def _delete(client: AsyncClient, headers: dict[str, str], agency_id, **body):
    return client.request(
        "DELETE", f"/agencies/{agency_id}", headers=headers, json={"confirm_name": "Cible", **body}
    )


# --- (a) + (g) full delete, storage purged, trace written ----------------------------


async def test_hard_delete_removes_everything_and_writes_trace(
    client: AsyncClient,
    db_session: AsyncSession,
    superadmin: Agent,
    make_agency: MakeAgency,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    expat = await make_expat_user(email="marie@x.io")
    agency = await _stuffed_agency(
        db_session, make_agency, make_client_case, system_roles, expat=expat
    )
    blobs_before = set(storage.mock_store)
    assert len(blobs_before) == 4  # logo, avatar, attachment, document
    headers = agent_headers(superadmin)  # capture before expire_all (ORM object)

    # A real (non-demo) case exists → needs force.
    response = await _delete(client, headers, agency.id, force=True)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {"agency_id": str(agency.id), "name": "Cible", "deleted_cases_count": 1}

    db_session.expunge_all()
    assert await db_session.get(Agency, agency.id) is None
    # Everything agency-scoped is gone.
    assert (
        await db_session.execute(select(func.count(Agent.id)).where(Agent.agency_id == agency.id))
    ).scalar_one() == 0
    assert (
        await db_session.execute(
            select(func.count(ClientCase.id)).where(ClientCase.agency_id == agency.id)
        )
    ).scalar_one() == 0
    assert (
        await db_session.execute(
            select(func.count(JourneyTemplate.id)).where(JourneyTemplate.agency_id == agency.id)
        )
    ).scalar_one() == 0
    assert (
        await db_session.execute(select(func.count(Document.id)))
    ).scalar_one() == 0  # cascaded with the case
    # The agent's refresh token (no FK, agency-scoped) was purged by hand.
    assert (await db_session.execute(select(func.count(RefreshToken.jti)))).scalar_one() == 0

    # Storage: every agency blob purged, no orphan.
    assert storage.mock_store == {}

    # (g) the audit trace survives (no FK to the gone agency).
    trace = (
        await db_session.execute(
            select(AgencyDeletionLog).where(AgencyDeletionLog.deleted_agency_id == agency.id)
        )
    ).scalar_one()
    assert trace.agency_name == "Cible" and trace.deleted_cases_count == 1
    assert trace.performed_by_agent_id == superadmin.id
    assert trace.performed_by_email == "root@platform.io"


# --- (b) wrong name → 422, nothing deleted -------------------------------------------


async def test_wrong_confirm_name_is_422_and_deletes_nothing(
    client: AsyncClient,
    db_session: AsyncSession,
    superadmin: Agent,
    make_agency: MakeAgency,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    expat = await make_expat_user(email="m@x.io")
    agency = await _stuffed_agency(
        db_session, make_agency, make_client_case, system_roles, expat=expat
    )
    response = await client.request(
        "DELETE",
        f"/agencies/{agency.id}",
        headers=agent_headers(superadmin),
        json={"confirm_name": "Mauvais nom", "force": True},
    )  # single call, no expire before it
    assert response.status_code == 422
    assert response.json()["code"] == "agency.name_mismatch"
    db_session.expunge_all()
    assert await db_session.get(Agency, agency.id) is not None  # untouched
    assert len(storage.mock_store) == 4


# --- (c) active non-demo cases guardrail (409 / force) -------------------------------


async def test_active_cases_block_without_force_then_delete_with_force(
    client: AsyncClient,
    db_session: AsyncSession,
    superadmin: Agent,
    make_agency: MakeAgency,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    expat = await make_expat_user(email="m2@x.io")
    agency = await _stuffed_agency(
        db_session, make_agency, make_client_case, system_roles, expat=expat
    )
    headers = agent_headers(superadmin)
    blocked = await _delete(client, headers, agency.id)  # no force
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "agency.has_active_cases"
    assert blocked.json()["params"] == {"count": 1}
    db_session.expunge_all()
    assert await db_session.get(Agency, agency.id) is not None  # still there

    forced = await _delete(client, headers, agency.id, force=True)
    assert forced.status_code == 200
    db_session.expunge_all()
    assert await db_session.get(Agency, agency.id) is None


# --- (d) a demo-only agency deletes without force ------------------------------------


async def test_demo_only_agency_deletes_without_force(
    client: AsyncClient,
    db_session: AsyncSession,
    superadmin: Agent,
    make_agency: MakeAgency,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    expat = await make_expat_user(email="m3@x.io")
    agency = await _stuffed_agency(
        db_session, make_agency, make_client_case, system_roles, expat=expat, demo=True
    )
    response = await _delete(client, agent_headers(superadmin), agency.id)  # no force
    assert response.status_code == 200, response.text
    assert response.json()["deleted_cases_count"] == 1  # the demo case removed
    db_session.expunge_all()
    assert await db_session.get(Agency, agency.id) is None


# --- (e) THE isolation test: multi-agency expat --------------------------------------


async def test_deleting_agency_a_keeps_expat_account_and_agency_b(
    client: AsyncClient,
    db_session: AsyncSession,
    superadmin: Agent,
    make_agency: MakeAgency,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    expat = await make_expat_user(email="shared@x.io")
    agency_a = await _stuffed_agency(
        db_session, make_agency, make_client_case, system_roles, expat=expat
    )
    # The SAME expat is principal of a case at agency B, and holds a
    # global session (refresh token).
    agency_b = await make_agency(name="Agence B")
    case_b = await make_client_case(
        agency_id=agency_b.id, principal_expat_user_id=expat.id, status="in_progress"
    )
    db_session.add(
        RefreshToken(
            actor_type="expat",
            actor_id=expat.id,
            expires_at=datetime.now(UTC) + timedelta(days=7),
        )
    )
    await db_session.commit()

    response = await _delete(client, agent_headers(superadmin), agency_a.id, force=True)
    assert response.status_code == 200, response.text
    db_session.expunge_all()
    # A is gone; B is intact.
    assert await db_session.get(Agency, agency_a.id) is None
    assert await db_session.get(Agency, agency_b.id) is not None
    assert await db_session.get(ClientCase, case_b.id) is not None
    # The global expat account survives (orphan-of-A but still valid).
    assert await db_session.get(ExpatUser, expat.id) is not None
    # The expat's global session is NOT revoked (still works for B).
    expat_tokens = (
        await db_session.execute(
            select(func.count(RefreshToken.jti)).where(
                RefreshToken.actor_type == "expat", RefreshToken.actor_id == expat.id
            )
        )
    ).scalar_one()
    assert expat_tokens == 1


# --- (f) a non-superadmin admin is refused (403) ------------------------------------


async def test_agency_admin_cannot_delete(
    client: AsyncClient,
    db_session: AsyncSession,
    make_agency: MakeAgency,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    expat = await make_expat_user(email="m4@x.io")
    agency = await _stuffed_agency(
        db_session, make_agency, make_client_case, system_roles, expat=expat
    )
    # A full admin OF THAT agency: holds every internal permission but
    # NOT agency.create (platform-only) → 403 by the matrix.
    admin = await make_agent(agency_id=agency.id, role=system_roles["admin"])
    headers = agent_headers(admin)
    response = await _delete(client, headers, agency.id, force=True)
    assert response.status_code == 403
    db_session.expunge_all()
    assert await db_session.get(Agency, agency.id) is not None  # untouched
