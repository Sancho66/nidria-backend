"""Journey templates battery: agency-scoped CRUD, append/reorder/dense
renumbering of positions, declarative prerequisites with full-graph
cycle detection, and the option-A contract (free edit of assigned
templates, 409 on destructive ops)."""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient

from shared.models.agent import Agent
from shared.models.rbac import Role
from tests.plugins.agency_plugin import MakeAgency
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.journey_plugin import MakeJourneyTemplate


@pytest.fixture
def journeys_client(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def configurer(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    """case_manager: holds journey.configure without being admin."""
    return await make_agent(role=system_roles["case_manager"])


async def _create_template_with_steps(
    client: AsyncClient,
    headers: dict[str, str],
    step_names: list[str],
) -> tuple[str, list[str]]:
    template = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()
    step_ids = []
    for name in step_names:
        response = await client.post(
            f"/journeys/{template['id']}/steps", headers=headers, json={"name": name}
        )
        assert response.status_code == 201
        step_ids.append(response.json()["id"])
    return template["id"], step_ids


# --- Template CRUD ---------------------------------------------------------------


async def test_create_template_as_case_manager(
    journeys_client: AsyncClient, configurer: Agent, agent_headers: AuthHeaders
) -> None:
    response = await journeys_client.post(
        "/journeys", headers=agent_headers(configurer), json={"name": "Paraguay PR"}
    )
    assert response.status_code == 201
    assert response.json()["name"] == "Paraguay PR"


async def test_create_template_member_403(
    journeys_client: AsyncClient,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    member = await make_agent(role=system_roles["member"])
    response = await journeys_client.post(
        "/journeys", headers=agent_headers(member), json={"name": "Nope"}
    )
    assert response.status_code == 403


async def test_list_templates_scoped_to_agency(
    journeys_client: AsyncClient,
    configurer: Agent,
    make_journey_template: MakeJourneyTemplate,
    make_agency: MakeAgency,
    agent_headers: AuthHeaders,
) -> None:
    mine = await make_journey_template(agency_id=configurer.agency_id, name="Mine")
    other_agency = await make_agency()
    await make_journey_template(agency_id=other_agency.id, name="Theirs")
    response = await journeys_client.get("/journeys", headers=agent_headers(configurer))
    assert response.status_code == 200
    assert [t["id"] for t in response.json()] == [str(mine.id)]


async def test_member_can_read_templates(
    journeys_client: AsyncClient,
    make_agent: MakeAgent,
    make_journey_template: MakeJourneyTemplate,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    member = await make_agent(role=system_roles["member"])
    template = await make_journey_template(agency_id=member.agency_id)
    listing = await journeys_client.get("/journeys", headers=agent_headers(member))
    assert listing.status_code == 200
    detail = await journeys_client.get(f"/journeys/{template.id}", headers=agent_headers(member))
    assert detail.status_code == 200


async def test_get_foreign_template_404(
    journeys_client: AsyncClient,
    configurer: Agent,
    make_journey_template: MakeJourneyTemplate,
    make_agency: MakeAgency,
    agent_headers: AuthHeaders,
) -> None:
    other_agency = await make_agency()
    foreign = await make_journey_template(agency_id=other_agency.id)
    response = await journeys_client.get(
        f"/journeys/{foreign.id}", headers=agent_headers(configurer)
    )
    assert response.status_code == 404


async def test_patch_template_name(
    journeys_client: AsyncClient,
    configurer: Agent,
    make_journey_template: MakeJourneyTemplate,
    agent_headers: AuthHeaders,
) -> None:
    template = await make_journey_template(agency_id=configurer.agency_id, name="Old")
    response = await journeys_client.patch(
        f"/journeys/{template.id}", headers=agent_headers(configurer), json={"name": "New"}
    )
    assert response.status_code == 200
    assert response.json()["name"] == "New"


async def test_delete_unassigned_template(
    journeys_client: AsyncClient,
    configurer: Agent,
    make_journey_template: MakeJourneyTemplate,
    agent_headers: AuthHeaders,
) -> None:
    template = await make_journey_template(agency_id=configurer.agency_id)
    response = await journeys_client.delete(
        f"/journeys/{template.id}", headers=agent_headers(configurer)
    )
    assert response.status_code == 200
    assert (
        await journeys_client.get(f"/journeys/{template.id}", headers=agent_headers(configurer))
    ).status_code == 404


async def test_delete_assigned_template_409_clear_error(
    journeys_client: AsyncClient,
    configurer: Agent,
    make_journey_template: MakeJourneyTemplate,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    template = await make_journey_template(agency_id=configurer.agency_id)
    await make_client_case(agency_id=configurer.agency_id, journey_template_id=template.id)
    response = await journeys_client.delete(
        f"/journeys/{template.id}", headers=agent_headers(configurer)
    )
    assert response.status_code == 409
    assert "assigned to 1 case(s)" in response.json()["detail"]


# --- Steps: append, edit, delete, reorder ---------------------------------------------


async def test_steps_are_appended_in_order(
    journeys_client: AsyncClient, configurer: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(configurer)
    template_id, _ = await _create_template_with_steps(
        journeys_client, headers, ["Visa", "Bank", "Housing"]
    )
    detail = (await journeys_client.get(f"/journeys/{template_id}", headers=headers)).json()
    assert [(s["name"], s["position"]) for s in detail["steps"]] == [
        ("Visa", 0),
        ("Bank", 1),
        ("Housing", 2),
    ]


async def test_patch_step_fields(
    journeys_client: AsyncClient, configurer: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(configurer)
    template_id, step_ids = await _create_template_with_steps(journeys_client, headers, ["Visa"])
    response = await journeys_client.patch(
        f"/journeys/{template_id}/steps/{step_ids[0]}",
        headers=headers,
        json={"estimated_days": 15, "default_responsible_type": "expat"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["estimated_days"] == 15
    assert body["default_responsible_type"] == "expat"
    assert body["name"] == "Visa"  # untouched


async def test_delete_middle_step_renumbers_dense(
    journeys_client: AsyncClient, configurer: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(configurer)
    template_id, step_ids = await _create_template_with_steps(
        journeys_client, headers, ["A", "B", "C"]
    )
    response = await journeys_client.delete(
        f"/journeys/{template_id}/steps/{step_ids[1]}", headers=headers
    )
    assert response.status_code == 200
    detail = (await journeys_client.get(f"/journeys/{template_id}", headers=headers)).json()
    assert [(s["name"], s["position"]) for s in detail["steps"]] == [("A", 0), ("C", 1)]


async def test_delete_step_of_assigned_template_409(
    journeys_client: AsyncClient,
    configurer: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(configurer)
    template_id, step_ids = await _create_template_with_steps(journeys_client, headers, ["A", "B"])
    await make_client_case(
        agency_id=configurer.agency_id, journey_template_id=uuid.UUID(template_id)
    )
    response = await journeys_client.delete(
        f"/journeys/{template_id}/steps/{step_ids[0]}", headers=headers
    )
    assert response.status_code == 409
    assert "cannot be deleted" in response.json()["detail"]


async def test_reorder_steps(
    journeys_client: AsyncClient, configurer: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(configurer)
    template_id, step_ids = await _create_template_with_steps(
        journeys_client, headers, ["A", "B", "C"]
    )
    new_order = [step_ids[2], step_ids[0], step_ids[1]]
    response = await journeys_client.put(
        f"/journeys/{template_id}/steps/order", headers=headers, json={"step_ids": new_order}
    )
    assert response.status_code == 200
    assert [(s["name"], s["position"]) for s in response.json()] == [
        ("C", 0),
        ("A", 1),
        ("B", 2),
    ]


async def test_reorder_rejects_wrong_id_set(
    journeys_client: AsyncClient, configurer: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(configurer)
    template_id, step_ids = await _create_template_with_steps(journeys_client, headers, ["A", "B"])
    missing = await journeys_client.put(
        f"/journeys/{template_id}/steps/order",
        headers=headers,
        json={"step_ids": [step_ids[0]]},
    )
    assert missing.status_code == 422
    foreign = await journeys_client.put(
        f"/journeys/{template_id}/steps/order",
        headers=headers,
        json={"step_ids": [step_ids[0], str(uuid.uuid4())]},
    )
    assert foreign.status_code == 422


# --- Prerequisites ------------------------------------------------------------------------


async def test_set_prerequisites_ok(
    journeys_client: AsyncClient, configurer: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(configurer)
    template_id, step_ids = await _create_template_with_steps(
        journeys_client, headers, ["A", "B", "C"]
    )
    response = await journeys_client.put(
        f"/journeys/{template_id}/steps/{step_ids[2]}/prerequisites",
        headers=headers,
        json={"prerequisite_step_ids": [step_ids[0], step_ids[1]]},
    )
    assert response.status_code == 200
    assert sorted(response.json()["prerequisite_step_ids"]) == sorted([step_ids[0], step_ids[1]])


async def test_prerequisite_must_belong_to_same_template(
    journeys_client: AsyncClient, configurer: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(configurer)
    template_a, steps_a = await _create_template_with_steps(journeys_client, headers, ["A1"])
    _, steps_b = await _create_template_with_steps(journeys_client, headers, ["B1"])
    response = await journeys_client.put(
        f"/journeys/{template_a}/steps/{steps_a[0]}/prerequisites",
        headers=headers,
        json={"prerequisite_step_ids": [steps_b[0]]},
    )
    assert response.status_code == 422


async def test_self_prerequisite_rejected(
    journeys_client: AsyncClient, configurer: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(configurer)
    template_id, step_ids = await _create_template_with_steps(journeys_client, headers, ["A"])
    response = await journeys_client.put(
        f"/journeys/{template_id}/steps/{step_ids[0]}/prerequisites",
        headers=headers,
        json={"prerequisite_step_ids": [step_ids[0]]},
    )
    assert response.status_code == 422


async def test_direct_cycle_rejected(
    journeys_client: AsyncClient, configurer: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(configurer)
    template_id, step_ids = await _create_template_with_steps(journeys_client, headers, ["A", "B"])
    ok = await journeys_client.put(
        f"/journeys/{template_id}/steps/{step_ids[1]}/prerequisites",
        headers=headers,
        json={"prerequisite_step_ids": [step_ids[0]]},
    )
    assert ok.status_code == 200
    cycle = await journeys_client.put(
        f"/journeys/{template_id}/steps/{step_ids[0]}/prerequisites",
        headers=headers,
        json={"prerequisite_step_ids": [step_ids[1]]},
    )
    assert cycle.status_code == 422
    assert "cycle" in cycle.json()["detail"]


async def test_transitive_cycle_rejected(
    journeys_client: AsyncClient, configurer: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(configurer)
    template_id, step_ids = await _create_template_with_steps(
        journeys_client, headers, ["A", "B", "C"]
    )
    # B requires A; C requires B; then A requires C → A→C→B→A.
    for step, prereq in [(1, 0), (2, 1)]:
        ok = await journeys_client.put(
            f"/journeys/{template_id}/steps/{step_ids[step]}/prerequisites",
            headers=headers,
            json={"prerequisite_step_ids": [step_ids[prereq]]},
        )
        assert ok.status_code == 200
    cycle = await journeys_client.put(
        f"/journeys/{template_id}/steps/{step_ids[0]}/prerequisites",
        headers=headers,
        json={"prerequisite_step_ids": [step_ids[2]]},
    )
    assert cycle.status_code == 422


async def test_editing_prerequisites_of_assigned_template_allowed(
    journeys_client: AsyncClient,
    configurer: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """THE option-A test: an assigned template stays freely editable —
    locking is evaluated dynamically (enforced at step 10)."""
    headers = agent_headers(configurer)
    template_id, step_ids = await _create_template_with_steps(journeys_client, headers, ["A", "B"])
    await make_client_case(
        agency_id=configurer.agency_id, journey_template_id=uuid.UUID(template_id)
    )
    response = await journeys_client.put(
        f"/journeys/{template_id}/steps/{step_ids[1]}/prerequisites",
        headers=headers,
        json={"prerequisite_step_ids": [step_ids[0]]},
    )
    assert response.status_code == 200
    # Adding a step to an assigned template is allowed too (backfill
    # of progress rows lands at step 10).
    add = await journeys_client.post(
        f"/journeys/{template_id}/steps", headers=headers, json={"name": "C"}
    )
    assert add.status_code == 201


async def test_required_documents_editable_and_exposed(
    journeys_client: AsyncClient,
    configurer: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """Step 15: free-label required documents — editable via the
    EXISTING step endpoints, exposed in template detail AND in the
    agent timeline. Informative only: no upload-based lock."""
    headers = agent_headers(configurer)
    template = (await journeys_client.post("/journeys", headers=headers, json={"name": "T"})).json()
    created = await journeys_client.post(
        f"/journeys/{template['id']}/steps",
        headers=headers,
        json={
            "name": "Depot",
            "required_documents": ["Casier judiciaire apostillé", "Acte de naissance"],
        },
    )
    assert created.status_code == 201
    assert created.json()["required_documents"] == [
        "Casier judiciaire apostillé",
        "Acte de naissance",
    ]

    patched = await journeys_client.patch(
        f"/journeys/{template['id']}/steps/{created.json()['id']}",
        headers=headers,
        json={"required_documents": ["Casier judiciaire apostillé"]},
    )
    assert patched.status_code == 200
    assert patched.json()["required_documents"] == ["Casier judiciaire apostillé"]

    detail = (await journeys_client.get(f"/journeys/{template['id']}", headers=headers)).json()
    assert detail["steps"][0]["required_documents"] == ["Casier judiciaire apostillé"]

    # Agent timeline (projection) reads it straight from the template.
    case = await make_client_case(agency_id=configurer.agency_id)
    assign = await journeys_client.post(
        f"/cases/{case.id}/journey", headers=headers, json={"journey_template_id": template["id"]}
    )
    assert assign.status_code == 201
    assert assign.json()[0]["required_documents"] == ["Casier judiciaire apostillé"]
