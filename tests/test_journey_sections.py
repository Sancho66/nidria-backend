"""Journey sections (sections chantier, vague A) — freely-named ordered
groups of creation fields on a template. PURELY ADDITIVE: section_id is
nullable (NULL = unsectioned bucket), flat fields[]/case_fields[] kept,
existing flat templates keep working with zero data migration.

Covers: section CRUD + reorder, the journey.configure gate, cross-agency
scoping, SET NULL on delete (fields survive in the NULL bucket), field↔
section assignment via PATCH (foreign template → 422, null → bucket), the
grouped detail (sections[] + unsectioned), and the legacy flat path."""

import pytest
import pytest_asyncio
from httpx import AsyncClient

from shared.models.agent import Agent
from shared.models.rbac import Role
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent


@pytest.fixture
def sec_client(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


@pytest_asyncio.fixture
async def member(admin: Agent, make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    """case.edit but NOT journey.configure."""
    return await make_agent(agency_id=admin.agency_id, role=system_roles["member"])


async def _template(client: AsyncClient, headers: dict[str, str], name: str = "T") -> str:
    return (await client.post("/journeys", headers=headers, json={"name": name})).json()["id"]


async def _section(client: AsyncClient, headers: dict[str, str], tid: str, **body: object) -> dict:
    r = await client.post(f"/journeys/{tid}/sections", headers=headers, json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def _add_field(
    client: AsyncClient, headers: dict[str, str], tid: str, **body: object
) -> dict:
    r = await client.post(f"/journeys/{tid}/fields", headers=headers, json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def _add_case_field(
    client: AsyncClient, headers: dict[str, str], tid: str, **body: object
) -> dict:
    r = await client.post(f"/journeys/{tid}/case-fields", headers=headers, json=body)
    assert r.status_code == 201, r.text
    return r.json()


# --- CRUD ----------------------------------------------------------------------------


async def test_section_crud_full_cycle(
    sec_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    tid = await _template(sec_client, headers)

    created = await _section(sec_client, headers, tid, name="État civil", description="Infos")
    assert created["name"] == "État civil"
    assert created["description"] == "Infos"
    assert created["position"] == 0

    listed = (await sec_client.get(f"/journeys/{tid}/sections", headers=headers)).json()
    assert [s["id"] for s in listed] == [created["id"]]

    patched = await sec_client.patch(
        f"/journeys/{tid}/sections/{created['id']}",
        headers=headers,
        json={"name": "Identité"},
    )
    assert patched.status_code == 200
    assert patched.json()["name"] == "Identité"
    assert patched.json()["description"] == "Infos"  # untouched

    removed = await sec_client.delete(f"/journeys/{tid}/sections/{created['id']}", headers=headers)
    assert removed.status_code == 200
    assert (await sec_client.get(f"/journeys/{tid}/sections", headers=headers)).json() == []


async def test_section_reorder_dense_renumber(
    sec_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    tid = await _template(sec_client, headers)
    s1 = await _section(sec_client, headers, tid, name="A")
    s2 = await _section(sec_client, headers, tid, name="B")
    s3 = await _section(sec_client, headers, tid, name="C")

    resp = await sec_client.put(
        f"/journeys/{tid}/sections/order",
        headers=headers,
        json={"section_ids": [s3["id"], s1["id"], s2["id"]]},
    )
    assert resp.status_code == 200, resp.text
    ordered = resp.json()
    assert [s["id"] for s in ordered] == [s3["id"], s1["id"], s2["id"]]
    assert [s["position"] for s in ordered] == [0, 1, 2]


async def test_section_reorder_rejects_incomplete_set(
    sec_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    tid = await _template(sec_client, headers)
    s1 = await _section(sec_client, headers, tid, name="A")
    await _section(sec_client, headers, tid, name="B")
    short = await sec_client.put(
        f"/journeys/{tid}/sections/order", headers=headers, json={"section_ids": [s1["id"]]}
    )
    assert short.status_code == 422


# --- gate + scoping ------------------------------------------------------------------


async def test_section_write_gate_journey_configure(
    sec_client: AsyncClient, admin: Agent, member: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    tid = await _template(sec_client, headers)
    section = await _section(sec_client, headers, tid, name="A")
    mh = agent_headers(member)

    assert (
        await sec_client.post(f"/journeys/{tid}/sections", headers=mh, json={"name": "X"})
    ).status_code == 403
    assert (
        await sec_client.patch(
            f"/journeys/{tid}/sections/{section['id']}", headers=mh, json={"name": "X"}
        )
    ).status_code == 403
    assert (
        await sec_client.delete(f"/journeys/{tid}/sections/{section['id']}", headers=mh)
    ).status_code == 403


async def test_section_foreign_template_404(
    sec_client: AsyncClient,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    tid = await _template(sec_client, headers)
    section = await _section(sec_client, headers, tid, name="A")
    other_admin = await make_agent(role=system_roles["admin"])
    oh = agent_headers(other_admin)

    assert (await sec_client.get(f"/journeys/{tid}/sections", headers=oh)).status_code == 404
    assert (
        await sec_client.delete(f"/journeys/{tid}/sections/{section['id']}", headers=oh)
    ).status_code == 404


# --- SET NULL on delete (fields survive in the bucket) -------------------------------


async def test_delete_section_sets_fields_null_never_lost(
    sec_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """Deleting a section must NOT delete its fields — both planes fall
    back to the unsectioned bucket (ON DELETE SET NULL)."""
    headers = agent_headers(admin)
    tid = await _template(sec_client, headers)
    section = await _section(sec_client, headers, tid, name="Adresse")
    field = await _add_field(
        sec_client, headers, tid, kind="base_field", reference="passport_number"
    )
    case_field = await _add_case_field(sec_client, headers, tid, case_field="origin_country")
    # Assign both to the section.
    await sec_client.patch(
        f"/journeys/{tid}/fields/{field['id']}", headers=headers, json={"section_id": section["id"]}
    )
    await sec_client.patch(
        f"/journeys/{tid}/case-fields/{case_field['id']}",
        headers=headers,
        json={"section_id": section["id"]},
    )

    await sec_client.delete(f"/journeys/{tid}/sections/{section['id']}", headers=headers)

    detail = (await sec_client.get(f"/journeys/{tid}", headers=headers)).json()
    assert detail["sections"] == []
    # Both fields survive, back in the NULL bucket.
    assert [f["id"] for f in detail["unsectioned"]["fields"]] == [field["id"]]
    assert [c["id"] for c in detail["unsectioned"]["case_fields"]] == [case_field["id"]]
    assert detail["unsectioned"]["fields"][0]["section_id"] is None
    # Flat lists still hold everything.
    assert len(detail["fields"]) == 1
    assert len(detail["case_fields"]) == 1


# --- field <-> section assignment ----------------------------------------------------


async def test_assign_field_to_foreign_template_section_422(
    sec_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """A section_id of ANOTHER template → 422 (a field's section must
    belong to the same template)."""
    headers = agent_headers(admin)
    t1 = await _template(sec_client, headers, name="T1")
    t2 = await _template(sec_client, headers, name="T2")
    foreign_section = await _section(sec_client, headers, t2, name="Foreign")
    field = await _add_field(sec_client, headers, t1, kind="base_field", reference="phone")
    bad = await sec_client.patch(
        f"/journeys/{t1}/fields/{field['id']}",
        headers=headers,
        json={"section_id": foreign_section["id"]},
    )
    assert bad.status_code == 422


async def test_assign_then_clear_field_section(
    sec_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    tid = await _template(sec_client, headers)
    section = await _section(sec_client, headers, tid, name="État civil")
    field = await _add_field(sec_client, headers, tid, kind="base_field", reference="nationality")

    assigned = await sec_client.patch(
        f"/journeys/{tid}/fields/{field['id']}",
        headers=headers,
        json={"section_id": section["id"]},
    )
    assert assigned.json()["section_id"] == section["id"]
    detail = (await sec_client.get(f"/journeys/{tid}", headers=headers)).json()
    assert [f["id"] for f in detail["sections"][0]["fields"]] == [field["id"]]
    assert detail["unsectioned"]["fields"] == []

    # Clear (section_id=null) → back to the bucket; required_at_creation untouched.
    cleared = await sec_client.patch(
        f"/journeys/{tid}/fields/{field['id']}", headers=headers, json={"section_id": None}
    )
    assert cleared.json()["section_id"] is None


async def test_required_toggle_still_works_without_section(
    sec_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """The existing required-only PATCH (front calls it) is unaffected by
    the new optional section_id."""
    headers = agent_headers(admin)
    tid = await _template(sec_client, headers)
    field = await _add_field(sec_client, headers, tid, kind="base_field", reference="phone")
    resp = await sec_client.patch(
        f"/journeys/{tid}/fields/{field['id']}",
        headers=headers,
        json={"required_at_creation": True},
    )
    assert resp.status_code == 200
    assert resp.json()["required_at_creation"] is True
    assert resp.json()["section_id"] is None  # untouched


# --- legacy flat path (no migration, product still works) ----------------------------


async def test_legacy_flat_template_has_empty_sections_and_full_bucket(
    sec_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """A template with fields but NO sections (the legacy state): sections
    empty, every field in the unsectioned bucket, flat lists intact."""
    headers = agent_headers(admin)
    tid = await _template(sec_client, headers)
    f = await _add_field(sec_client, headers, tid, kind="base_field", reference="passport_number")
    c = await _add_case_field(sec_client, headers, tid, case_field="dest_country")

    detail = (await sec_client.get(f"/journeys/{tid}", headers=headers)).json()
    assert detail["sections"] == []
    assert [x["id"] for x in detail["unsectioned"]["fields"]] == [f["id"]]
    assert [x["id"] for x in detail["unsectioned"]["case_fields"]] == [c["id"]]
    # Flat keys (what the existing front reads) untouched.
    assert [x["id"] for x in detail["fields"]] == [f["id"]]
    assert [x["id"] for x in detail["case_fields"]] == [c["id"]]
