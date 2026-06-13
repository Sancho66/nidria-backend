"""Step requirements (NEW WAVE 1/4) — model + materialization + derived
completion, READ-ONLY agency side. No client write, no active auto→DONE.

Covers: materialization (principal + each_person, frozen on later person
add, idempotent on reopen), derived status (no requirement-provision
code runs — pure read of case_person), all_requirements_met, the lock
untouched, agency_validation never self-closes, CRUD gate."""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.case_step_requirement import CaseStepRequirement
from shared.models.rbac import Role
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeCasePerson, MakeClientCase


@pytest.fixture
def sr_client(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


@pytest_asyncio.fixture
async def member(admin: Agent, make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    """case.edit but NOT journey.configure (member lacks it)."""
    return await make_agent(agency_id=admin.agency_id, role=system_roles["member"])


async def _template_with_step(
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


async def _add_req(
    client: AsyncClient, headers: dict[str, str], tid: str, sid: str, **body: object
) -> dict:
    r = await client.post(f"/journeys/{tid}/steps/{sid}/requirements", headers=headers, json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def _activate_first_step(
    client: AsyncClient, headers: dict[str, str], case_id: str, tid: str
) -> dict:
    """Assign the journey and move its single step to in_progress.
    Returns that step's timeline entry."""
    steps = (
        await client.post(
            f"/cases/{case_id}/journey", headers=headers, json={"journey_template_id": tid}
        )
    ).json()
    progress_id = steps[0]["id"]
    started = await client.patch(
        f"/cases/{case_id}/steps/{progress_id}", headers=headers, json={"status": "in_progress"}
    )
    assert started.status_code == 200
    return started.json()


# --- CRUD gate -----------------------------------------------------------------------


async def test_requirement_crud_gate_journey_configure(
    sr_client: AsyncClient, admin: Agent, member: Agent, agent_headers: AuthHeaders
) -> None:
    tid, sid = await _template_with_step(sr_client, agent_headers(admin))
    # member has case.edit but not journey.configure → 403 on define.
    denied = await sr_client.post(
        f"/journeys/{tid}/steps/{sid}/requirements",
        headers=agent_headers(member),
        json={"kind": "base_field", "reference": "passport_number", "scope": "principal"},
    )
    assert denied.status_code == 403


async def test_requirement_validation(
    sr_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    tid, sid = await _template_with_step(sr_client, headers)
    # Unknown base field → 422.
    bad = await sr_client.post(
        f"/journeys/{tid}/steps/{sid}/requirements",
        headers=headers,
        json={"kind": "base_field", "reference": "email", "scope": "principal"},
    )
    assert bad.status_code == 422
    # Unknown custom-field key → 422.
    bad_cf = await sr_client.post(
        f"/journeys/{tid}/steps/{sid}/requirements",
        headers=headers,
        json={"kind": "custom_field", "reference": "ghost", "scope": "principal"},
    )
    assert bad_cf.status_code == 422


# --- materialization -----------------------------------------------------------------


async def test_materialization_principal_and_each_person_frozen(
    sr_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_client_case: MakeClientCase,
    make_case_person: MakeCasePerson,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    tid, sid = await _template_with_step(sr_client, headers)
    await _add_req(
        sr_client,
        headers,
        tid,
        sid,
        kind="base_field",
        reference="passport_number",
        scope="principal",
    )
    await _add_req(
        sr_client,
        headers,
        tid,
        sid,
        kind="base_field",
        reference="date_of_birth",
        scope="each_person",
    )

    # Case with principal + 2 family = 3 persons at activation.
    case = await make_client_case(agency_id=admin.agency_id)
    await make_case_person(case=case, full_name="Spouse")
    await make_case_person(case=case, full_name="Child")

    entry = await _activate_first_step(sr_client, headers, str(case.id), tid)
    # 1 (principal scope) + 3 (each_person × 3 persons) = 4 concrete reqs.
    assert len(entry["requirements"]) == 4
    principal_reqs = [r for r in entry["requirements"] if r["reference"] == "passport_number"]
    assert len(principal_reqs) == 1  # principal scope → exactly one
    each = [r for r in entry["requirements"] if r["reference"] == "date_of_birth"]
    assert len(each) == 3

    # FROZEN: add a 4th person AFTER activation → no new concrete req.
    await make_case_person(case=case, full_name="LateComer")
    detail = await sr_client.get(f"/cases/{case.id}", headers=headers)
    step = next(s for s in detail.json()["progress"] if s["id"] == entry["id"])
    assert len(step["requirements"]) == 4  # unchanged

    # Idempotent on reopen: complete (skip — has pending reqs but
    # agency_validation lets the agency close) then reopen → no dup.
    await sr_client.patch(
        f"/cases/{case.id}/steps/{entry['id']}", headers=headers, json={"status": "done"}
    )
    await sr_client.patch(
        f"/cases/{case.id}/steps/{entry['id']}", headers=headers, json={"status": "in_progress"}
    )
    rows = (
        (
            await db_session.execute(
                select(CaseStepRequirement).where(
                    CaseStepRequirement.case_step_progress_id == uuid.UUID(entry["id"])
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 4  # reopen materialized nothing new


# --- derived status ------------------------------------------------------------------


async def test_status_derived_from_person_value(
    sr_client: AsyncClient,
    admin: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """No requirement-provision endpoint exists: status is purely
    derived from case_person at read time."""
    headers = agent_headers(admin)
    tid, sid = await _template_with_step(sr_client, headers)
    await _add_req(
        sr_client,
        headers,
        tid,
        sid,
        kind="base_field",
        reference="passport_number",
        scope="principal",
    )
    case = await make_client_case(agency_id=admin.agency_id)
    entry = await _activate_first_step(sr_client, headers, str(case.id), tid)
    principal_person_id = entry["requirements"][0]["person_id"]

    # Initially pending (passport empty), step not all-met.
    assert entry["requirements"][0]["status"] == "pending"
    assert entry["all_requirements_met"] is False

    # Fill passport via the EXISTING person PATCH — nothing else.
    await sr_client.patch(
        f"/cases/{case.id}/persons/{principal_person_id}",
        headers=headers,
        json={"passport_number": "AB12345"},
    )
    detail = await sr_client.get(f"/cases/{case.id}", headers=headers)
    step = next(s for s in detail.json()["progress"] if s["id"] == entry["id"])
    assert step["requirements"][0]["status"] == "provided"  # derived, no copy
    assert step["all_requirements_met"] is True


async def test_status_derived_custom_field(
    sr_client: AsyncClient,
    admin: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    # Define a custom field, then require it.
    await sr_client.post(
        "/agencies/me/custom-fields",
        headers=headers,
        json={"key": "visa_number", "label": "Visa", "field_type": "text"},
    )
    tid, sid = await _template_with_step(sr_client, headers)
    await _add_req(
        sr_client,
        headers,
        tid,
        sid,
        kind="custom_field",
        reference="visa_number",
        scope="principal",
    )
    case = await make_client_case(agency_id=admin.agency_id)
    entry = await _activate_first_step(sr_client, headers, str(case.id), tid)
    person_id = entry["requirements"][0]["person_id"]
    assert entry["requirements"][0]["status"] == "pending"

    await sr_client.patch(
        f"/cases/{case.id}/persons/{person_id}",
        headers=headers,
        json={"custom_fields": {"visa_number": "V-9"}},
    )
    detail = await sr_client.get(f"/cases/{case.id}", headers=headers)
    step = next(s for s in detail.json()["progress"] if s["id"] == entry["id"])
    assert step["requirements"][0]["status"] == "provided"


# --- lock + completion_mode ----------------------------------------------------------


async def test_lock_unaffected_by_requirements(
    sr_client: AsyncClient,
    admin: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """A step with a prerequisite cannot be started while the prereq is
    unfinished — requirements don't change the lock (which gates the
    transition before any materialization)."""
    headers = agent_headers(admin)
    tid = (await sr_client.post("/journeys", headers=headers, json={"name": "T"})).json()["id"]
    s1 = (
        await sr_client.post(f"/journeys/{tid}/steps", headers=headers, json={"name": "S1"})
    ).json()
    s2 = (
        await sr_client.post(f"/journeys/{tid}/steps", headers=headers, json={"name": "S2"})
    ).json()
    await sr_client.put(
        f"/journeys/{tid}/steps/{s2['id']}/prerequisites",
        headers=headers,
        json={"prerequisite_step_ids": [s1["id"]]},
    )
    case = await make_client_case(agency_id=admin.agency_id)
    steps = (
        await sr_client.post(
            f"/cases/{case.id}/journey", headers=headers, json={"journey_template_id": tid}
        )
    ).json()
    s2_progress = next(s for s in steps if s["template_step_id"] == s2["id"])
    blocked = await sr_client.patch(
        f"/cases/{case.id}/steps/{s2_progress['id']}",
        headers=headers,
        json={"status": "in_progress"},
    )
    assert blocked.status_code == 409  # lock holds


async def test_agency_validation_never_self_closes(
    sr_client: AsyncClient,
    admin: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """agency_validation: even with all requirements provided, the step
    stays in_progress (exposed ready-to-validate via all_requirements_met),
    never auto-closes in this wave."""
    headers = agent_headers(admin)
    tid, sid = await _template_with_step(sr_client, headers, completion_mode="agency_validation")
    await _add_req(
        sr_client,
        headers,
        tid,
        sid,
        kind="base_field",
        reference="passport_number",
        scope="principal",
    )
    case = await make_client_case(agency_id=admin.agency_id)
    entry = await _activate_first_step(sr_client, headers, str(case.id), tid)
    person_id = entry["requirements"][0]["person_id"]
    await sr_client.patch(
        f"/cases/{case.id}/persons/{person_id}",
        headers=headers,
        json={"passport_number": "AB12345"},
    )
    detail = await sr_client.get(f"/cases/{case.id}", headers=headers)
    step = next(s for s in detail.json()["progress"] if s["id"] == entry["id"])
    assert step["all_requirements_met"] is True
    assert step["status"] == "in_progress"  # NOT done — no active auto-complete
    assert step["completion_mode"] == "agency_validation"


# --- reorder (same convention as steps/order) ----------------------------------------


async def _three_reqs(
    client: AsyncClient, headers: dict[str, str], tid: str, sid: str
) -> list[dict]:
    return [
        await _add_req(
            client, headers, tid, sid, kind="base_field", reference=ref, scope="principal"
        )
        for ref in ("passport_number", "date_of_birth", "nationality")
    ]


async def test_reorder_requirements_changes_order_and_renumbers(
    sr_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    tid, sid = await _template_with_step(sr_client, headers)
    r1, r2, r3 = await _three_reqs(sr_client, headers, tid, sid)

    resp = await sr_client.put(
        f"/journeys/{tid}/steps/{sid}/requirements/order",
        headers=headers,
        json={"requirement_ids": [r3["id"], r1["id"], r2["id"]]},
    )
    assert resp.status_code == 200, resp.text
    ordered = resp.json()
    assert [x["id"] for x in ordered] == [r3["id"], r1["id"], r2["id"]]
    assert [x["position"] for x in ordered] == [0, 1, 2]  # dense 0..n-1

    # The GET listing (ordered by position) reflects it; count unchanged.
    listed = (
        await sr_client.get(f"/journeys/{tid}/steps/{sid}/requirements", headers=headers)
    ).json()
    assert [x["id"] for x in listed] == [r3["id"], r1["id"], r2["id"]]
    assert len(listed) == 3


async def test_reorder_requirements_is_idempotent(
    sr_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    tid, sid = await _template_with_step(sr_client, headers)
    r1, r2, r3 = await _three_reqs(sr_client, headers, tid, sid)
    order = {"requirement_ids": [r2["id"], r3["id"], r1["id"]]}
    first = await sr_client.put(
        f"/journeys/{tid}/steps/{sid}/requirements/order", headers=headers, json=order
    )
    second = await sr_client.put(
        f"/journeys/{tid}/steps/{sid}/requirements/order", headers=headers, json=order
    )
    assert first.json() == second.json()  # same order, same positions, no drift


async def test_reorder_requirements_gate_journey_configure(
    sr_client: AsyncClient, admin: Agent, member: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    tid, sid = await _template_with_step(sr_client, headers)
    r1, r2, r3 = await _three_reqs(sr_client, headers, tid, sid)
    denied = await sr_client.put(
        f"/journeys/{tid}/steps/{sid}/requirements/order",
        headers=agent_headers(member),  # case.edit but not journey.configure
        json={"requirement_ids": [r3["id"], r2["id"], r1["id"]]},
    )
    assert denied.status_code == 403


async def test_reorder_requirements_rejects_foreign_id(
    sr_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """A requirement belonging to ANOTHER step makes the set mismatch →
    422, never silently applied (no cross-step/agency leak)."""
    headers = agent_headers(admin)
    tid, sid = await _template_with_step(sr_client, headers)
    r1, r2, _ = await _three_reqs(sr_client, headers, tid, sid)
    # A second step in the same template with its own requirement.
    other_step = (
        await sr_client.post(f"/journeys/{tid}/steps", headers=headers, json={"name": "Other"})
    ).json()
    foreign = await _add_req(
        sr_client,
        headers,
        tid,
        other_step["id"],
        kind="base_field",
        reference="phone",
        scope="principal",
    )
    # Swapping one of sid's ids for the foreign one → not exactly sid's set.
    bad = await sr_client.put(
        f"/journeys/{tid}/steps/{sid}/requirements/order",
        headers=headers,
        json={"requirement_ids": [r1["id"], r2["id"], foreign["id"]]},
    )
    assert bad.status_code == 422
    # Partial list (missing one) also rejected.
    short = await sr_client.put(
        f"/journeys/{tid}/steps/{sid}/requirements/order",
        headers=headers,
        json={"requirement_ids": [r1["id"], r2["id"]]},
    )
    assert short.status_code == 422


async def test_reorder_requirements_foreign_template_404(
    sr_client: AsyncClient,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """Another agency's template is invisible: reordering it as our admin
    → 404 (template scoped to the agent's agency)."""
    headers = agent_headers(admin)
    tid, sid = await _template_with_step(sr_client, headers)
    r1, r2, r3 = await _three_reqs(sr_client, headers, tid, sid)

    other_admin = await make_agent(role=system_roles["admin"])  # different agency
    other_headers = agent_headers(other_admin)
    denied = await sr_client.put(
        f"/journeys/{tid}/steps/{sid}/requirements/order",
        headers=other_headers,
        json={"requirement_ids": [r3["id"], r2["id"], r1["id"]]},
    )
    assert denied.status_code == 404
