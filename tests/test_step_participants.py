""" "Action à réaliser par" 1 → N (responsible refonte) — template CRUD,
snapshot to new dossiers, and the read projection with anti-staffing.

Validator and gating are NOT touched here — only the "qui fait" participants.
"""

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.expat_user import ExpatUser
from shared.models.rbac import Role
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser


@pytest.fixture
def pc(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"], first_name="Alice", last_name="Admin")


@pytest_asyncio.fixture
async def member(admin: Agent, make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    """case.edit but NOT journey.configure."""
    return await make_agent(agency_id=admin.agency_id, role=system_roles["member"])


@pytest_asyncio.fixture
async def external_role(db_session: AsyncSession, rbac_baseline: None) -> Role:
    return (
        await db_session.execute(
            select(Role).where(Role.is_external.is_(True), Role.name == "external_lawyer")
        )
    ).scalar_one()


@pytest_asyncio.fixture
async def external(make_agent: MakeAgent, admin: Agent, external_role: Role) -> Agent:
    return await make_agent(
        agency_id=admin.agency_id,
        role=external_role,
        is_external=True,
        email="prov@ext.com",
        first_name="Robert",
        last_name="Lawyer",
    )


@pytest_asyncio.fixture
async def expat(make_expat_user: MakeExpatUser) -> ExpatUser:
    return await make_expat_user(email="client@example.com", first_name="Marie", last_name="Curie")


async def _template_step(pc: AsyncClient, ah: dict[str, str]) -> tuple[str, str]:
    tid = (await pc.post("/journeys", headers=ah, json={"name": "T"})).json()["id"]
    sid = (await pc.post(f"/journeys/{tid}/steps", headers=ah, json={"name": "Collecte"})).json()[
        "id"
    ]
    return tid, sid


# --- template CRUD + validation + gate -----------------------------------------------


async def test_participant_crud_and_validation(
    pc: AsyncClient, admin: Agent, member: Agent, agent_headers: AuthHeaders
) -> None:
    ah = agent_headers(admin)
    tid, sid = await _template_step(pc, ah)
    base = f"/journeys/{tid}/steps/{sid}/participants"

    # Add an expat participant (executant) + an internal-agent participant.
    r1 = await pc.post(base, headers=ah, json={"type": "expat", "role": "executant"})
    assert r1.status_code == 201, r1.text
    assert r1.json()["type"] == "expat" and r1.json()["agent_id"] is None
    r2 = await pc.post(
        base,
        headers=ah,
        json={"type": "agent", "agent_id": str(admin.id), "role": "contributor"},
    )
    assert r2.status_code == 201

    listed = (await pc.get(base, headers=ah)).json()
    assert {p["role"] for p in listed} == {"executant", "contributor"}
    # Embedded in the template detail.
    detail = (await pc.get(f"/journeys/{tid}", headers=ah)).json()
    step = next(s for s in detail["steps"] if s["id"] == sid)
    assert len(step["participants"]) == 2

    # Delete one → list shrinks.
    rm = await pc.delete(f"{base}/{r1.json()['id']}", headers=ah)
    assert rm.status_code == 200
    assert len((await pc.get(base, headers=ah)).json()) == 1

    # Gate: a member without journey.configure cannot add.
    denied = await pc.post(
        base, headers=agent_headers(member), json={"type": "expat", "role": "informed"}
    )
    assert denied.status_code == 403

    # Validation: external type rejected at template; expat+agent_id rejected;
    # an unknown role (e.g. 'validator') rejected by the closed enum.
    assert (
        await pc.post(
            base,
            headers=ah,
            json={"type": "external", "agent_id": str(admin.id), "role": "executant"},
        )
    ).status_code == 422
    assert (
        await pc.post(
            base, headers=ah, json={"type": "expat", "agent_id": str(admin.id), "role": "executant"}
        )
    ).status_code == 422
    assert (
        await pc.post(base, headers=ah, json={"type": "expat", "role": "validator"})
    ).status_code == 422


# --- snapshot to a new dossier + anti-staffing read ----------------------------------


async def test_participants_snapshot_and_antistaffing(
    pc: AsyncClient,
    admin: Agent,
    external: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    ah = agent_headers(admin)
    tid, sid = await _template_step(pc, ah)
    base = f"/journeys/{tid}/steps/{sid}/participants"
    # Three participants: client (expat), internal agent, external provider.
    await pc.post(base, headers=ah, json={"type": "expat", "role": "provides_documents"})
    await pc.post(
        base, headers=ah, json={"type": "agent", "agent_id": str(admin.id), "role": "contributor"}
    )
    await pc.post(
        base, headers=ah, json={"type": "agent", "agent_id": str(external.id), "role": "executant"}
    )

    # New dossier → snapshot copies the 3 participants onto the instance step.
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    steps = (
        await pc.post(f"/cases/{case.id}/journey", headers=ah, json={"journey_template_id": tid})
    ).json()
    pid = steps[0]["id"]

    # AGENCY timeline: 3 participants, resolved names + is_external flags.
    agency_step = (await pc.get(f"/cases/{case.id}/steps", headers=ah)).json()[0]
    parts = {p["role"]: p for p in agency_step["participants"]}
    assert set(parts) == {"provides_documents", "contributor", "executant"}
    assert parts["contributor"]["is_external"] is False
    assert parts["executant"]["is_external"] is True
    assert parts["executant"]["name"] == "Robert Lawyer"

    # EXPAT timeline: anti-staffing — internal agent → "agency" (no name),
    # external → name, the client → "you".
    expat_step = (await pc.get(f"/expat/cases/{case.id}", headers=expat_headers(expat))).json()[
        "timeline"
    ][0]
    eparts = {p["role"]: p for p in expat_step["participants"]}
    assert eparts["contributor"] == {"role": "contributor", "type": "agency", "name": None}
    assert eparts["executant"] == {"role": "executant", "type": "external", "name": "Robert Lawyer"}
    assert eparts["provides_documents"]["type"] == "you"

    # EXTERNAL timeline: the external provider was AUTO-ASSIGNED to the case
    # (executant participant is_external) → it can read the case, with the
    # same anti-staffing (internal agent name hidden).
    h = agent_headers(external)
    ext_detail = await pc.get(f"/external/cases/{case.id}", headers=h)
    assert ext_detail.status_code == 200  # proves ensure_external_assignment ran
    ext_step = ext_detail.json()["timeline"][0]
    xparts = {p["role"]: p for p in ext_step["participants"]}
    assert xparts["contributor"] == {"role": "contributor", "type": "agency", "name": None}
    assert xparts["executant"]["type"] == "external"
    _ = pid
