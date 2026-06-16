"""Case-level step requirements (sections chantier, vague C1) — a step
may require a client_case column (country/address), evaluated LIVE
against client_case. READ + projection + completion only (no client
write — that's C2).

Covers: CRUD (declaration) + gate + scoping + validation + dup, the
projection on the agency/expat/external faces (value for agency/expat,
STRIPPED for external, no crash on person_id=None), completion folding (a
pending case-req blocks auto-complete; filling client_case via the agency
PATCH completes it), and non-regression of person-only steps."""

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


@pytest.fixture
def cr_client(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


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
        agency_id=admin.agency_id, role=external_role, is_external=True, email="lawyer@ext.com"
    )


async def _template_step(
    client: AsyncClient, headers: dict[str, str], completion_mode: str = "agency_validation"
) -> tuple[str, str]:
    tid = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()["id"]
    step = (
        await client.post(
            f"/journeys/{tid}/steps",
            headers=headers,
            json={"name": "Collecte", "completion_mode": completion_mode},
        )
    ).json()
    return tid, step["id"]


async def _declare(
    client: AsyncClient, headers: dict[str, str], tid: str, sid: str, **body: object
) -> dict:
    r = await client.post(
        f"/journeys/{tid}/steps/{sid}/case-requirements", headers=headers, json=body
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _activate(client: AsyncClient, headers: dict[str, str], case_id: str, tid: str) -> dict:
    steps = (
        await client.post(
            f"/cases/{case_id}/journey", headers=headers, json={"journey_template_id": tid}
        )
    ).json()
    pid = steps[0]["id"]
    started = await client.patch(
        f"/cases/{case_id}/steps/{pid}", headers=headers, json={"status": "in_progress"}
    )
    assert started.status_code == 200
    return started.json()


# --- CRUD (declaration) --------------------------------------------------------------


async def test_case_requirement_crud(
    cr_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    tid, sid = await _template_step(cr_client, headers)

    created = await _declare(cr_client, headers, tid, sid, case_field="origin_country")
    assert created["case_field"] == "origin_country"
    assert created["step_id"] == sid

    listed = (
        await cr_client.get(f"/journeys/{tid}/steps/{sid}/case-requirements", headers=headers)
    ).json()
    assert [r["id"] for r in listed] == [created["id"]]

    removed = await cr_client.delete(
        f"/journeys/{tid}/steps/{sid}/case-requirements/{created['id']}", headers=headers
    )
    assert removed.status_code == 200
    assert (
        await cr_client.get(f"/journeys/{tid}/steps/{sid}/case-requirements", headers=headers)
    ).json() == []


async def test_case_requirement_reorder(
    cr_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    tid, sid = await _template_step(cr_client, headers)
    a = await _declare(cr_client, headers, tid, sid, case_field="origin_country")
    b = await _declare(cr_client, headers, tid, sid, case_field="dest_country")
    resp = await cr_client.put(
        f"/journeys/{tid}/steps/{sid}/case-requirements/order",
        headers=headers,
        json={"case_requirement_ids": [b["id"], a["id"]]},
    )
    assert resp.status_code == 200
    assert [r["id"] for r in resp.json()] == [b["id"], a["id"]]
    assert [r["position"] for r in resp.json()] == [0, 1]
    # Incomplete set → 422.
    bad = await cr_client.put(
        f"/journeys/{tid}/steps/{sid}/case-requirements/order",
        headers=headers,
        json={"case_requirement_ids": [a["id"]]},
    )
    assert bad.status_code == 422


async def test_case_requirement_gate_and_validation(
    cr_client: AsyncClient, admin: Agent, member: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    tid, sid = await _template_step(cr_client, headers)
    # member lacks journey.configure → 403.
    denied = await cr_client.post(
        f"/journeys/{tid}/steps/{sid}/case-requirements",
        headers=agent_headers(member),
        json={"case_field": "origin_country"},
    )
    assert denied.status_code == 403
    # Not a collectable case field → 422.
    bad = await cr_client.post(
        f"/journeys/{tid}/steps/{sid}/case-requirements",
        headers=headers,
        json={"case_field": "passport_number"},  # a person field
    )
    assert bad.status_code == 422
    # Duplicate → 409.
    await _declare(cr_client, headers, tid, sid, case_field="origin_country")
    dup = await cr_client.post(
        f"/journeys/{tid}/steps/{sid}/case-requirements",
        headers=headers,
        json={"case_field": "origin_country"},
    )
    assert dup.status_code == 409


async def test_case_requirement_foreign_template_404(
    cr_client: AsyncClient,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    tid, sid = await _template_step(cr_client, headers)
    other_admin = await make_agent(role=system_roles["admin"])
    denied = await cr_client.post(
        f"/journeys/{tid}/steps/{sid}/case-requirements",
        headers=agent_headers(other_admin),
        json={"case_field": "origin_country"},
    )
    assert denied.status_code == 404


# --- projection (agency) -------------------------------------------------------------


async def test_projection_agency_live_value(
    cr_client: AsyncClient,
    admin: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    tid, sid = await _template_step(cr_client, headers)
    await _declare(cr_client, headers, tid, sid, case_field="origin_country")
    # origin_country starts EMPTY so the case-req is pending (the plugin
    # defaults it to "FR").
    case = await make_client_case(agency_id=admin.agency_id, origin_country=None)
    entry = await _activate(cr_client, headers, str(case.id), tid)

    # The case-req appears live, pending, value None — no concrete row.
    creq = next(r for r in entry["requirements"] if r["target"] == "case")
    assert creq["reference"] == "origin_country"
    assert creq["person_id"] is None
    assert creq["scope"] is None
    assert creq["status"] == "pending"
    assert creq["value"] is None
    assert entry["all_requirements_met"] is False

    # Agency fills the value on client_case (the existing case PATCH).
    await cr_client.patch(f"/cases/{case.id}", headers=headers, json={"origin_country": "FR"})
    detail = await cr_client.get(f"/cases/{case.id}", headers=headers)
    step = next(s for s in detail.json()["progress"] if s["id"] == entry["id"])
    creq = next(r for r in step["requirements"] if r["target"] == "case")
    assert creq["status"] == "provided"  # derived live from client_case
    assert creq["value"] == "FR"
    assert step["all_requirements_met"] is True


# --- completion folding --------------------------------------------------------------


async def test_case_requirement_blocks_then_allows_auto_completion(
    cr_client: AsyncClient,
    admin: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """An auto step with a pending case-req does NOT self-complete; filling
    the client_case value (agency PATCH) recomputes → auto→DONE."""
    headers = agent_headers(admin)
    tid, sid = await _template_step(cr_client, headers, completion_mode="auto")
    await _declare(cr_client, headers, tid, sid, case_field="dest_country")
    case = await make_client_case(agency_id=admin.agency_id, dest_country=None)
    entry = await _activate(cr_client, headers, str(case.id), tid)
    assert entry["status"] == "in_progress"  # case-req pending → not auto-done

    await cr_client.patch(f"/cases/{case.id}", headers=headers, json={"dest_country": "PY"})
    detail = await cr_client.get(f"/cases/{case.id}", headers=headers)
    step = next(s for s in detail.json()["progress"] if s["id"] == entry["id"])
    assert step["status"] == "done"  # auto-completed once the case-req is met


async def test_person_only_step_unchanged(
    cr_client: AsyncClient,
    admin: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """Non-regression: a step with NO case-req behaves exactly as before —
    no spurious case-req in the projection."""
    headers = agent_headers(admin)
    tid, sid = await _template_step(cr_client, headers)
    await cr_client.post(
        f"/journeys/{tid}/steps/{sid}/requirements",
        headers=headers,
        json={"kind": "base_field", "reference": "passport_number", "scope": "principal"},
    )
    case = await make_client_case(agency_id=admin.agency_id)
    entry = await _activate(cr_client, headers, str(case.id), tid)
    assert all(r["target"] == "person" for r in entry["requirements"])
    assert len(entry["requirements"]) == 1


# --- external RGPD -------------------------------------------------------------------


async def _assign_external(
    client: AsyncClient, ah: dict[str, str], case_id: object, agent_id: object
) -> None:
    r = await client.post(
        f"/cases/{case_id}/external-assignments", headers=ah, json={"agent_id": str(agent_id)}
    )
    assert r.status_code in (200, 201), r.text


async def test_external_sees_case_requirement_without_value(
    cr_client: AsyncClient,
    admin: Agent,
    external: Agent,
    expat_user: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """RGPD: a case-req appears on the external timeline (status/reference)
    but NEVER its value — and person_id=None must not crash the portal."""
    ah, h = agent_headers(admin), agent_headers(external)
    tid, sid = await _template_step(cr_client, ah)
    await _declare(cr_client, ah, tid, sid, case_field="origin_country")
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat_user.id, origin_country="FR"
    )
    await _activate(cr_client, ah, str(case.id), tid)
    await _assign_external(cr_client, ah, case.id, external.id)

    detail = (await cr_client.get(f"/external/cases/{case.id}", headers=h)).json()
    reqs = detail["timeline"][0]["requirements"]
    creq = next(r for r in reqs if r["reference"] == "origin_country")
    assert creq["status"] == "provided"  # the external knows it's done
    assert "value" not in creq  # but NEVER the value (origin_country=FR hidden)
