"""Per-template CASE-field collection (option b) — countries a template
collects at case creation, kept on client_case (SEPARATE from the
person-field mechanism). Calque of test_journey_template_fields.

Covers: CRUD + reorder (exact-set 422, dense renumber), the
journey.configure gate, cross-agency / foreign-id scoping (404),
validation (case_field outside COLLECTABLE_CASE_FIELDS → 422, duplicate →
409), and exposure embedded in the template detail."""

import pytest
import pytest_asyncio
from httpx import AsyncClient

from shared.models.agent import Agent
from shared.models.rbac import Role
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent


@pytest.fixture
def cf_client(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


@pytest_asyncio.fixture
async def member(admin: Agent, make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    """case.edit but NOT journey.configure (member lacks it)."""
    return await make_agent(agency_id=admin.agency_id, role=system_roles["member"])


async def _template(client: AsyncClient, headers: dict[str, str], name: str = "T") -> str:
    return (await client.post("/journeys", headers=headers, json={"name": name})).json()["id"]


async def _add(client: AsyncClient, headers: dict[str, str], tid: str, **body: object) -> dict:
    r = await client.post(f"/journeys/{tid}/case-fields", headers=headers, json=body)
    assert r.status_code == 201, r.text
    return r.json()


# --- CRUD ----------------------------------------------------------------------------


async def test_case_field_crud_full_cycle(
    cf_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    tid = await _template(cf_client, headers)

    created = await _add(
        cf_client, headers, tid, case_field="origin_country", required_at_creation=True
    )
    assert created["case_field"] == "origin_country"
    assert created["required_at_creation"] is True
    assert created["position"] == 0

    listed = (await cf_client.get(f"/journeys/{tid}/case-fields", headers=headers)).json()
    assert [c["id"] for c in listed] == [created["id"]]

    # PATCH the required toggle.
    patched = await cf_client.patch(
        f"/journeys/{tid}/case-fields/{created['id']}",
        headers=headers,
        json={"required_at_creation": False},
    )
    assert patched.status_code == 200
    assert patched.json()["required_at_creation"] is False

    removed = await cf_client.delete(
        f"/journeys/{tid}/case-fields/{created['id']}", headers=headers
    )
    assert removed.status_code == 200
    after = (await cf_client.get(f"/journeys/{tid}/case-fields", headers=headers)).json()
    assert after == []


async def test_case_fields_embedded_in_template_detail(
    cf_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    tid = await _template(cf_client, headers)
    c1 = await _add(cf_client, headers, tid, case_field="origin_country")
    c2 = await _add(cf_client, headers, tid, case_field="dest_country")

    detail = (await cf_client.get(f"/journeys/{tid}", headers=headers)).json()
    assert "case_fields" in detail
    assert [c["id"] for c in detail["case_fields"]] == [c1["id"], c2["id"]]
    # The person-field list stays a SEPARATE key (the two planes don't mix).
    assert "fields" in detail


# --- gate ----------------------------------------------------------------------------


async def test_case_field_write_gate_journey_configure(
    cf_client: AsyncClient, admin: Agent, member: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    tid = await _template(cf_client, headers)
    created = await _add(cf_client, headers, tid, case_field="origin_country")
    mh = agent_headers(member)

    denied_add = await cf_client.post(
        f"/journeys/{tid}/case-fields", headers=mh, json={"case_field": "dest_country"}
    )
    assert denied_add.status_code == 403
    denied_order = await cf_client.put(
        f"/journeys/{tid}/case-fields/order", headers=mh, json={"case_field_ids": [created["id"]]}
    )
    assert denied_order.status_code == 403
    denied_patch = await cf_client.patch(
        f"/journeys/{tid}/case-fields/{created['id']}",
        headers=mh,
        json={"required_at_creation": True},
    )
    assert denied_patch.status_code == 403
    denied_delete = await cf_client.delete(
        f"/journeys/{tid}/case-fields/{created['id']}", headers=mh
    )
    assert denied_delete.status_code == 403


async def test_case_field_read_gate_journey_configure(
    cf_client: AsyncClient, admin: Agent, member: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    tid = await _template(cf_client, headers)
    denied = await cf_client.get(f"/journeys/{tid}/case-fields", headers=agent_headers(member))
    assert denied.status_code == 403


# --- scoping -------------------------------------------------------------------------


async def test_case_field_foreign_template_404(
    cf_client: AsyncClient,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    tid = await _template(cf_client, headers)
    cf = await _add(cf_client, headers, tid, case_field="origin_country")

    other_admin = await make_agent(role=system_roles["admin"])
    oh = agent_headers(other_admin)

    assert (await cf_client.get(f"/journeys/{tid}/case-fields", headers=oh)).status_code == 404
    add = await cf_client.post(
        f"/journeys/{tid}/case-fields", headers=oh, json={"case_field": "dest_country"}
    )
    assert add.status_code == 404
    delete = await cf_client.delete(f"/journeys/{tid}/case-fields/{cf['id']}", headers=oh)
    assert delete.status_code == 404


async def test_case_field_foreign_id_404(
    cf_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """A case-field id of ANOTHER template (same agency) → 404, no leak."""
    headers = agent_headers(admin)
    t1 = await _template(cf_client, headers, name="T1")
    t2 = await _template(cf_client, headers, name="T2")
    foreign = await _add(cf_client, headers, t2, case_field="origin_country")
    denied = await cf_client.delete(f"/journeys/{t1}/case-fields/{foreign['id']}", headers=headers)
    assert denied.status_code == 404


# --- validation ----------------------------------------------------------------------


async def test_case_field_validation(
    cf_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    tid = await _template(cf_client, headers)

    # Outside COLLECTABLE_CASE_FIELDS → 422 (e.g. a person field, or junk).
    bad = await cf_client.post(
        f"/journeys/{tid}/case-fields", headers=headers, json={"case_field": "passport_number"}
    )
    assert bad.status_code == 422
    junk = await cf_client.post(
        f"/journeys/{tid}/case-fields", headers=headers, json={"case_field": "ghost"}
    )
    assert junk.status_code == 422


async def test_full_address_fields_declarable(
    cf_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """Vague B: street/city/postal_code (origin + dest) join the countries
    as collectable case-fields — all 8 declarable, no 422."""
    headers = agent_headers(admin)
    tid = await _template(cf_client, headers)
    for ref in (
        "origin_country",
        "origin_street",
        "origin_city",
        "origin_postal_code",
        "dest_country",
        "dest_street",
        "dest_city",
        "dest_postal_code",
    ):
        created = await _add(cf_client, headers, tid, case_field=ref)
        assert created["case_field"] == ref


async def test_case_field_duplicate_409(
    cf_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    tid = await _template(cf_client, headers)
    await _add(cf_client, headers, tid, case_field="origin_country")
    dup = await cf_client.post(
        f"/journeys/{tid}/case-fields", headers=headers, json={"case_field": "origin_country"}
    )
    assert dup.status_code == 409


# --- reorder -------------------------------------------------------------------------


async def test_reorder_case_fields_changes_order_and_renumbers(
    cf_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    tid = await _template(cf_client, headers)
    c1 = await _add(cf_client, headers, tid, case_field="origin_country")
    c2 = await _add(cf_client, headers, tid, case_field="dest_country")

    resp = await cf_client.put(
        f"/journeys/{tid}/case-fields/order",
        headers=headers,
        json={"case_field_ids": [c2["id"], c1["id"]]},
    )
    assert resp.status_code == 200, resp.text
    ordered = resp.json()
    assert [c["id"] for c in ordered] == [c2["id"], c1["id"]]
    assert [c["position"] for c in ordered] == [0, 1]  # dense 0..n-1


async def test_reorder_case_fields_rejects_incomplete_or_foreign_set(
    cf_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    tid = await _template(cf_client, headers)
    c1 = await _add(cf_client, headers, tid, case_field="origin_country")
    await _add(cf_client, headers, tid, case_field="dest_country")
    # A case field from another template makes the set mismatch → 422.
    other = await _template(cf_client, headers, name="Other")
    foreign = await _add(cf_client, headers, other, case_field="origin_country")
    bad = await cf_client.put(
        f"/journeys/{tid}/case-fields/order",
        headers=headers,
        json={"case_field_ids": [c1["id"], foreign["id"]]},
    )
    assert bad.status_code == 422
    # Partial list (missing one) also rejected.
    short = await cf_client.put(
        f"/journeys/{tid}/case-fields/order",
        headers=headers,
        json={"case_field_ids": [c1["id"]]},
    )
    assert short.status_code == 422
