"""Nominal step assignment (wave C) — internal & external responsibles.
The critical axis is the B↔C coherence INVARIANT: a provider can never be
responsible for a step without dossier access — proven unreachable from
both directions. Plus: end-to-end (assign → name → the provider sees the
case and themselves), anti-staffing (internal name hidden from the client,
external provider name shown), and template default = internal only."""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.client_case import ClientCase
from shared.models.expat_user import ExpatUser
from shared.models.rbac import Role
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser


@pytest.fixture
def c_client(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"], first_name="Alice", last_name="Owner")


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
        email="robert@ext.com",
        first_name="Robert",
        last_name="Lawyer",
    )


@pytest_asyncio.fixture
async def expat(make_expat_user: MakeExpatUser) -> ExpatUser:
    return await make_expat_user(email="client@example.com")


async def _case_with_step(
    c_client: AsyncClient,
    ah: dict[str, str],
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
) -> tuple[ClientCase, str]:
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    tid = (await c_client.post("/journeys", headers=ah, json={"name": "T"})).json()["id"]
    await c_client.post(f"/journeys/{tid}/steps", headers=ah, json={"name": "Acte"})
    steps = (
        await c_client.post(
            f"/cases/{case.id}/journey", headers=ah, json={"journey_template_id": tid}
        )
    ).json()
    return case, steps[0]["id"]


async def _assign_to_case(
    c_client: AsyncClient, ah: dict[str, str], case_id: uuid.UUID, agent_id: uuid.UUID
) -> None:
    r = await c_client.post(
        f"/cases/{case_id}/external-assignments", headers=ah, json={"agent_id": str(agent_id)}
    )
    assert r.status_code == 201, r.text


# --- THE INVARIANT: no external responsible without dossier access -------------------


async def test_cannot_name_unassigned_external_responsible(
    c_client: AsyncClient,
    admin: Agent,
    external: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    ah = agent_headers(admin)
    case, pid = await _case_with_step(c_client, ah, admin, expat, make_client_case)
    # Direction (a): name an external NOT assigned to the case → 422.
    resp = await c_client.put(
        f"/cases/{case.id}/steps/{pid}/responsible",
        headers=ah,
        json={"responsible_type": "agent", "responsible_agent_id": str(external.id)},
    )
    assert resp.status_code == 422
    # The step stayed unassigned (no half-applied state).
    step = next(
        s
        for s in (await c_client.get(f"/cases/{case.id}", headers=ah)).json()["progress"]
        if s["id"] == pid
    )
    assert step["responsible_agent_id"] is None


async def test_cannot_unassign_external_still_responsible(
    c_client: AsyncClient,
    admin: Agent,
    external: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    ah = agent_headers(admin)
    case, pid = await _case_with_step(c_client, ah, admin, expat, make_client_case)
    await _assign_to_case(c_client, ah, case.id, external.id)
    named = await c_client.put(
        f"/cases/{case.id}/steps/{pid}/responsible",
        headers=ah,
        json={"responsible_type": "agent", "responsible_agent_id": str(external.id)},
    )
    assert named.status_code == 200
    # Direction (b): unassigning from the case while still responsible → 409.
    blocked = await c_client.delete(
        f"/cases/{case.id}/external-assignments/{external.id}", headers=ah
    )
    assert blocked.status_code == 409
    # Reassign the step away, THEN unassign succeeds.
    await c_client.put(
        f"/cases/{case.id}/steps/{pid}/responsible", headers=ah, json={"responsible_type": None}
    )
    removed = await c_client.delete(
        f"/cases/{case.id}/external-assignments/{external.id}", headers=ah
    )
    assert removed.status_code == 200


# --- end-to-end B + C ----------------------------------------------------------------


async def test_assigned_external_responsible_sees_case_and_self(
    c_client: AsyncClient,
    admin: Agent,
    external: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    ah, eh = agent_headers(admin), agent_headers(external)
    case, pid = await _case_with_step(c_client, ah, admin, expat, make_client_case)
    await _assign_to_case(c_client, ah, case.id, external.id)  # B
    await c_client.put(  # C
        f"/cases/{case.id}/steps/{pid}/responsible",
        headers=ah,
        json={"responsible_type": "agent", "responsible_agent_id": str(external.id)},
    )
    # The provider sees the case via the portal AND themselves responsible.
    detail = await c_client.get(f"/external/cases/{case.id}", headers=eh)
    assert detail.status_code == 200
    step = detail.json()["timeline"][0]
    assert step["responsible"] == {"type": "external", "name": "Robert Lawyer"}


# --- anti-staffing: internal name hidden, external name shown ------------------------


async def test_responsible_anti_staffing_internal_vs_external(
    c_client: AsyncClient,
    admin: Agent,
    external: Agent,
    expat: ExpatUser,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    make_client_case: MakeClientCase,
    expat_headers: AuthHeaders,
    agent_headers: AuthHeaders,
) -> None:
    ah, eh = agent_headers(admin), expat_headers(expat)
    case, pid = await _case_with_step(c_client, ah, admin, expat, make_client_case)
    internal = await make_agent(
        agency_id=admin.agency_id,
        role=system_roles["member"],
        first_name="Marie",
        last_name="Staff",
    )

    # Internal agent responsible → the client sees "agency", NO name.
    await c_client.put(
        f"/cases/{case.id}/steps/{pid}/responsible",
        headers=ah,
        json={"responsible_type": "agent", "responsible_agent_id": str(internal.id)},
    )
    client_step = (await c_client.get(f"/expat/cases/{case.id}", headers=eh)).json()["timeline"][0]
    assert client_step["responsible"] == {"type": "agency", "name": None}  # no staffing leak
    # But the AGENT timeline DOES resolve the internal name (internal tool).
    agent_step = next(
        s
        for s in (await c_client.get(f"/cases/{case.id}", headers=ah)).json()["progress"]
        if s["id"] == pid
    )
    assert agent_step["responsible_name"] == "Marie Staff"
    assert agent_step["responsible_is_external"] is False

    # External provider responsible → the client SEES the provider's name.
    await _assign_to_case(c_client, ah, case.id, external.id)
    await c_client.put(
        f"/cases/{case.id}/steps/{pid}/responsible",
        headers=ah,
        json={"responsible_type": "agent", "responsible_agent_id": str(external.id)},
    )
    client_step2 = (await c_client.get(f"/expat/cases/{case.id}", headers=eh)).json()["timeline"][0]
    assert client_step2["responsible"] == {"type": "external", "name": "Robert Lawyer"}


# --- template default copied at assignment (internal case) ---------------------------
# (Durable-external default + auto-assignment covered in the "REVISED MODEL"
# section below; the old "internal only" rule was reversed by A1.)


async def test_template_named_default_copies_to_case(
    c_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    ah = agent_headers(admin)
    internal = await make_agent(agency_id=admin.agency_id, role=system_roles["member"])
    tid = (await c_client.post("/journeys", headers=ah, json={"name": "T"})).json()["id"]
    await c_client.post(
        f"/journeys/{tid}/steps",
        headers=ah,
        json={"name": "S", "default_responsible_agent_id": str(internal.id)},
    )
    case = await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=expat.id)
    steps = (
        await c_client.post(
            f"/cases/{case.id}/journey", headers=ah, json={"journey_template_id": tid}
        )
    ).json()
    # The named internal default copied to the instance.
    assert steps[0]["responsible_type"] == "agent"
    assert steps[0]["responsible_agent_id"] == str(internal.id)

    # The agency can then OVERRIDE per case.
    other = await make_agent(agency_id=admin.agency_id, role=system_roles["member"])
    overridden = await c_client.put(
        f"/cases/{case.id}/steps/{steps[0]['id']}/responsible",
        headers=ah,
        json={"responsible_type": "agent", "responsible_agent_id": str(other.id)},
    )
    assert overridden.status_code == 200
    assert overridden.json()["responsible_agent_id"] == str(other.id)


# --- gate: assignment requires case.edit ---------------------------------------------


async def test_responsible_assignment_requires_case_edit(
    c_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    ah = agent_headers(admin)
    case, pid = await _case_with_step(c_client, ah, admin, expat, make_client_case)
    viewer = await make_agent(agency_id=admin.agency_id, role=system_roles["viewer"])
    denied = await c_client.put(
        f"/cases/{case.id}/steps/{pid}/responsible",
        headers=agent_headers(viewer),  # case.view only, no case.edit
        json={"responsible_type": "expat"},
    )
    assert denied.status_code == 403


# --- REVISED MODEL: durable external as template default + auto-assignment -----------


async def _template_with_default(
    c_client: AsyncClient, ah: dict[str, str], default_agent_id: str, n_steps: int = 1
) -> str:
    tid = (await c_client.post("/journeys", headers=ah, json={"name": "T"})).json()["id"]
    for i in range(n_steps):
        r = await c_client.post(
            f"/journeys/{tid}/steps",
            headers=ah,
            json={"name": f"S{i}", "default_responsible_agent_id": default_agent_id},
        )
        assert r.status_code == 201, r.text
    return tid


async def test_a1_external_durable_accepted_as_template_default(
    c_client: AsyncClient,
    admin: Agent,
    external: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    external_role: Role,
    agent_headers: AuthHeaders,
) -> None:
    ah = agent_headers(admin)
    tid = (await c_client.post("/journeys", headers=ah, json={"name": "T"})).json()["id"]
    # Durable external of THIS agency → accepted (revised model).
    ok_ext = await c_client.post(
        f"/journeys/{tid}/steps",
        headers=ah,
        json={"name": "ExtDefault", "default_responsible_agent_id": str(external.id)},
    )
    assert ok_ext.status_code == 201
    assert ok_ext.json()["default_responsible_agent_id"] == str(external.id)
    # Internal still accepted.
    internal = await make_agent(agency_id=admin.agency_id, role=system_roles["member"])
    ok_int = await c_client.post(
        f"/journeys/{tid}/steps",
        headers=ah,
        json={"name": "IntDefault", "default_responsible_agent_id": str(internal.id)},
    )
    assert ok_int.status_code == 201
    # Another agency's external → rejected (agency scope holds).
    stranger = await make_agent(role=external_role, is_external=True)  # new agency
    ko = await c_client.post(
        f"/journeys/{tid}/steps",
        headers=ah,
        json={"name": "Stranger", "default_responsible_agent_id": str(stranger.id)},
    )
    assert ko.status_code == 422


async def test_a2_external_default_auto_assigns_case(
    c_client: AsyncClient,
    admin: Agent,
    external: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    ah, eh = agent_headers(admin), agent_headers(external)
    tid = await _template_with_default(c_client, ah, str(external.id))
    case = await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=expat.id)
    steps = (
        await c_client.post(
            f"/cases/{case.id}/journey", headers=ah, json={"journey_template_id": tid}
        )
    ).json()
    # The default copied to the instance…
    assert steps[0]["responsible_agent_id"] == str(external.id)
    # …AND the external was auto-assigned → it sees the case via the portal.
    assert (await c_client.get(f"/external/cases/{case.id}", headers=eh)).status_code == 200
    # …and the agency lists it among the case's assigned externals.
    assigned = (await c_client.get(f"/cases/{case.id}/external-assignments", headers=ah)).json()
    assert str(external.id) in {a["agent_id"] for a in assigned}


async def test_a2_idempotent_one_assignment_for_multi_step_default(
    c_client: AsyncClient,
    admin: Agent,
    external: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    ah = agent_headers(admin)
    tid = await _template_with_default(
        c_client, ah, str(external.id), n_steps=3
    )  # 3 steps, same external
    case = await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=expat.id)
    await c_client.post(f"/cases/{case.id}/journey", headers=ah, json={"journey_template_id": tid})
    assigned = (await c_client.get(f"/cases/{case.id}/external-assignments", headers=ah)).json()
    # ONE row for the external despite defaulting on 3 steps.
    assert [a["agent_id"] for a in assigned].count(str(external.id)) == 1


async def test_a2_backfill_assigns_existing_cases(
    c_client: AsyncClient,
    admin: Agent,
    external: Agent,
    expat: ExpatUser,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    ah = agent_headers(admin)
    # Template assigned to TWO live cases, no external step yet.
    tid = (await c_client.post("/journeys", headers=ah, json={"name": "T"})).json()["id"]
    await c_client.post(f"/journeys/{tid}/steps", headers=ah, json={"name": "S1"})
    case1 = await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=expat.id)
    expat2 = await make_expat_user(email="c2@example.com")
    case2 = await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=expat2.id)
    for c in (case1, case2):
        await c_client.post(f"/cases/{c.id}/journey", headers=ah, json={"journey_template_id": tid})

    # Add a step defaulting to the external → backfilled onto both cases.
    added = await c_client.post(
        f"/journeys/{tid}/steps",
        headers=ah,
        json={"name": "ExtStep", "default_responsible_agent_id": str(external.id)},
    )
    assert added.status_code == 201
    for c in (case1, case2):
        assigned = (await c_client.get(f"/cases/{c.id}/external-assignments", headers=ah)).json()
        assert str(external.id) in {a["agent_id"] for a in assigned}


async def test_a2_coherence_auto_assigned_external_cannot_be_unassigned_while_responsible(
    c_client: AsyncClient,
    admin: Agent,
    external: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """The auto-assigned external is also the step's responsible — the
    wave-C 409 guards the removal (no incoherent state via the new path)."""
    ah = agent_headers(admin)
    tid = await _template_with_default(c_client, ah, str(external.id))
    case = await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=expat.id)
    await c_client.post(f"/cases/{case.id}/journey", headers=ah, json={"journey_template_id": tid})
    blocked = await c_client.delete(
        f"/cases/{case.id}/external-assignments/{external.id}", headers=ah
    )
    assert blocked.status_code == 409
