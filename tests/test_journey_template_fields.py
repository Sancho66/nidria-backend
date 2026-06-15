"""Per-template field collection (NEW WAVE 1) — the explicit list of
fields a template collects at case CREATION, calque of step requirements
one level up (attached to the template, not a step).

Covers: CRUD + reorder (two-phase dense renumber, exact-set 422), the
journey.configure gate, cross-agency scoping (404), reference validation
(base whitelist / active custom definition / document rejected),
duplicate → 409, is_archived flagged at read for a custom archived AFTER
attachment, exposure both via GET /fields and embedded in the template
detail, and ZERO value write (no case_person impact)."""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.case_person import CasePerson
from shared.models.journey import JourneyTemplateField
from shared.models.rbac import Role
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase


@pytest.fixture
def tf_client(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
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


async def _add_field(
    client: AsyncClient, headers: dict[str, str], tid: str, **body: object
) -> dict:
    r = await client.post(f"/journeys/{tid}/fields", headers=headers, json=body)
    assert r.status_code == 201, r.text
    return r.json()


# --- CRUD ----------------------------------------------------------------------------


async def test_field_crud_full_cycle(
    tf_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    tid = await _template(tf_client, headers)

    created = await _add_field(
        tf_client,
        headers,
        tid,
        kind="base_field",
        reference="passport_number",
        required_at_creation=True,
    )
    assert created["kind"] == "base_field"
    assert created["reference"] == "passport_number"
    assert created["required_at_creation"] is True
    assert created["position"] == 0
    assert created["is_archived"] is False
    # base field carries no resolved render metadata.
    assert created["label"] is None
    assert created["field_type"] is None
    assert created["options"] is None

    listed = (await tf_client.get(f"/journeys/{tid}/fields", headers=headers)).json()
    assert [f["id"] for f in listed] == [created["id"]]

    removed = await tf_client.delete(f"/journeys/{tid}/fields/{created['id']}", headers=headers)
    assert removed.status_code == 200
    after = (await tf_client.get(f"/journeys/{tid}/fields", headers=headers)).json()
    assert after == []


async def test_field_custom_resolves_render_metadata(
    tf_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    await tf_client.post(
        "/agencies/me/custom-fields",
        headers=headers,
        json={
            "key": "visa_type",
            "label": "Visa type",
            "field_type": "select",
            "options": ["work", "student"],
        },
    )
    tid = await _template(tf_client, headers)
    field = await _add_field(tf_client, headers, tid, kind="custom_field", reference="visa_type")
    assert field["label"] == "Visa type"
    assert field["field_type"] == "select"
    assert field["options"] == ["work", "student"]
    assert field["is_archived"] is False


# --- embedded in the template detail -------------------------------------------------


async def test_fields_embedded_in_template_detail(
    tf_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    tid = await _template(tf_client, headers)
    f1 = await _add_field(tf_client, headers, tid, kind="base_field", reference="passport_number")
    f2 = await _add_field(tf_client, headers, tid, kind="base_field", reference="date_of_birth")

    detail = (await tf_client.get(f"/journeys/{tid}", headers=headers)).json()
    assert "fields" in detail
    assert [f["id"] for f in detail["fields"]] == [f1["id"], f2["id"]]


# --- gate ----------------------------------------------------------------------------


async def test_field_write_gate_journey_configure(
    tf_client: AsyncClient, admin: Agent, member: Agent, agent_headers: AuthHeaders
) -> None:
    """member has case.edit but not journey.configure → 403 on every write."""
    headers = agent_headers(admin)
    tid = await _template(tf_client, headers)
    created = await _add_field(tf_client, headers, tid, kind="base_field", reference="phone")
    member_headers = agent_headers(member)

    denied_add = await tf_client.post(
        f"/journeys/{tid}/fields",
        headers=member_headers,
        json={"kind": "base_field", "reference": "nationality"},
    )
    assert denied_add.status_code == 403
    denied_order = await tf_client.put(
        f"/journeys/{tid}/fields/order",
        headers=member_headers,
        json={"field_ids": [created["id"]]},
    )
    assert denied_order.status_code == 403
    denied_delete = await tf_client.delete(
        f"/journeys/{tid}/fields/{created['id']}", headers=member_headers
    )
    assert denied_delete.status_code == 403


async def test_field_read_gate_journey_configure(
    tf_client: AsyncClient, admin: Agent, member: Agent, agent_headers: AuthHeaders
) -> None:
    """The dedicated GET /fields is bound to journey.configure (same as
    the requirements listing) — member is denied."""
    headers = agent_headers(admin)
    tid = await _template(tf_client, headers)
    denied = await tf_client.get(f"/journeys/{tid}/fields", headers=agent_headers(member))
    assert denied.status_code == 403


# --- scoping -------------------------------------------------------------------------


async def test_field_foreign_template_404(
    tf_client: AsyncClient,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """Another agency's template is invisible: operating on it → 404."""
    headers = agent_headers(admin)
    tid = await _template(tf_client, headers)
    field = await _add_field(tf_client, headers, tid, kind="base_field", reference="phone")

    other_admin = await make_agent(role=system_roles["admin"])  # different agency
    other_headers = agent_headers(other_admin)

    assert (
        await tf_client.get(f"/journeys/{tid}/fields", headers=other_headers)
    ).status_code == 404
    add = await tf_client.post(
        f"/journeys/{tid}/fields",
        headers=other_headers,
        json={"kind": "base_field", "reference": "nationality"},
    )
    assert add.status_code == 404
    delete = await tf_client.delete(f"/journeys/{tid}/fields/{field['id']}", headers=other_headers)
    assert delete.status_code == 404


async def test_field_foreign_field_id_404(
    tf_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """A field id of ANOTHER template (same agency) is not in this
    template → 404 on delete, no cross-template leak."""
    headers = agent_headers(admin)
    t1 = await _template(tf_client, headers, name="T1")
    t2 = await _template(tf_client, headers, name="T2")
    foreign = await _add_field(tf_client, headers, t2, kind="base_field", reference="phone")
    denied = await tf_client.delete(f"/journeys/{t1}/fields/{foreign['id']}", headers=headers)
    assert denied.status_code == 404


# --- validation ----------------------------------------------------------------------


async def test_field_validation(
    tf_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    tid = await _template(tf_client, headers)

    # base field outside the collectable whitelist → 422.
    bad_base = await tf_client.post(
        f"/journeys/{tid}/fields",
        headers=headers,
        json={"kind": "base_field", "reference": "email"},
    )
    assert bad_base.status_code == 422

    # custom field with no definition → 422.
    bad_custom = await tf_client.post(
        f"/journeys/{tid}/fields",
        headers=headers,
        json={"kind": "custom_field", "reference": "ghost"},
    )
    assert bad_custom.status_code == 422

    # document is a requirement, not a creation field → 422.
    bad_doc = await tf_client.post(
        f"/journeys/{tid}/fields",
        headers=headers,
        json={"kind": "document", "reference": "passport_scan"},
    )
    assert bad_doc.status_code == 422


async def test_field_custom_archived_at_creation_rejected(
    tf_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """A custom definition archived BEFORE attachment cannot be added
    (no active definition) → 422, same rule as requirements."""
    headers = agent_headers(admin)
    cf = (
        await tf_client.post(
            "/agencies/me/custom-fields",
            headers=headers,
            json={"key": "old_field", "label": "Old", "field_type": "text"},
        )
    ).json()
    await tf_client.post(f"/agencies/me/custom-fields/{cf['id']}/archive", headers=headers)
    tid = await _template(tf_client, headers)
    bad = await tf_client.post(
        f"/journeys/{tid}/fields",
        headers=headers,
        json={"kind": "custom_field", "reference": "old_field"},
    )
    assert bad.status_code == 422


async def test_field_duplicate_409(
    tf_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """UNIQUE(template_id, kind, reference): the same field twice on one
    template → clean 409."""
    headers = agent_headers(admin)
    tid = await _template(tf_client, headers)
    await _add_field(tf_client, headers, tid, kind="base_field", reference="passport_number")
    dup = await tf_client.post(
        f"/journeys/{tid}/fields",
        headers=headers,
        json={"kind": "base_field", "reference": "passport_number"},
    )
    assert dup.status_code == 409


# --- is_archived flagged at read (archived AFTER attachment) --------------------------


async def test_field_custom_archived_after_attachment_stays_flagged(
    tf_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """Added while active, then the definition is archived: the field row
    stays in the list, flagged is_archived=true (mirrors requirements)."""
    headers = agent_headers(admin)
    cf = (
        await tf_client.post(
            "/agencies/me/custom-fields",
            headers=headers,
            json={"key": "temp_visa", "label": "Temp visa", "field_type": "text"},
        )
    ).json()
    tid = await _template(tf_client, headers)
    field = await _add_field(tf_client, headers, tid, kind="custom_field", reference="temp_visa")
    assert field["is_archived"] is False

    await tf_client.post(f"/agencies/me/custom-fields/{cf['id']}/archive", headers=headers)

    listed = (await tf_client.get(f"/journeys/{tid}/fields", headers=headers)).json()
    assert len(listed) == 1  # the row stays
    assert listed[0]["is_archived"] is True
    # also reflected embedded in the template detail.
    detail = (await tf_client.get(f"/journeys/{tid}", headers=headers)).json()
    assert detail["fields"][0]["is_archived"] is True


# --- reorder (same convention as steps/order, requirements/order) --------------------


async def _three_fields(client: AsyncClient, headers: dict[str, str], tid: str) -> list[dict]:
    return [
        await _add_field(client, headers, tid, kind="base_field", reference=ref)
        for ref in ("passport_number", "date_of_birth", "nationality")
    ]


async def test_reorder_fields_changes_order_and_renumbers(
    tf_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    tid = await _template(tf_client, headers)
    f1, f2, f3 = await _three_fields(tf_client, headers, tid)

    resp = await tf_client.put(
        f"/journeys/{tid}/fields/order",
        headers=headers,
        json={"field_ids": [f3["id"], f1["id"], f2["id"]]},
    )
    assert resp.status_code == 200, resp.text
    ordered = resp.json()
    assert [x["id"] for x in ordered] == [f3["id"], f1["id"], f2["id"]]
    assert [x["position"] for x in ordered] == [0, 1, 2]  # dense 0..n-1

    listed = (await tf_client.get(f"/journeys/{tid}/fields", headers=headers)).json()
    assert [x["id"] for x in listed] == [f3["id"], f1["id"], f2["id"]]
    assert len(listed) == 3


async def test_reorder_fields_is_idempotent(
    tf_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    tid = await _template(tf_client, headers)
    f1, f2, f3 = await _three_fields(tf_client, headers, tid)
    order = {"field_ids": [f2["id"], f3["id"], f1["id"]]}
    first = await tf_client.put(f"/journeys/{tid}/fields/order", headers=headers, json=order)
    second = await tf_client.put(f"/journeys/{tid}/fields/order", headers=headers, json=order)
    assert first.json() == second.json()  # no drift


async def test_reorder_fields_rejects_incomplete_or_foreign_set(
    tf_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    tid = await _template(tf_client, headers)
    f1, f2, _ = await _three_fields(tf_client, headers, tid)
    # A field from another template makes the set mismatch → 422.
    other = await _template(tf_client, headers, name="Other")
    foreign = await _add_field(tf_client, headers, other, kind="base_field", reference="phone")
    bad = await tf_client.put(
        f"/journeys/{tid}/fields/order",
        headers=headers,
        json={"field_ids": [f1["id"], f2["id"], foreign["id"]]},
    )
    assert bad.status_code == 422
    # Partial list (missing one) also rejected.
    short = await tf_client.put(
        f"/journeys/{tid}/fields/order",
        headers=headers,
        json={"field_ids": [f1["id"], f2["id"]]},
    )
    assert short.status_code == 422


# --- no value write ------------------------------------------------------------------


async def test_field_attachment_never_writes_a_value(
    tf_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """Wave 1 is template configuration only: attaching/reordering fields
    touches no case_person (custom_fields stay {}). Values land in wave 2."""
    headers = agent_headers(admin)
    case = await make_client_case(agency_id=admin.agency_id)
    tid = await _template(tf_client, headers)
    await _add_field(tf_client, headers, tid, kind="base_field", reference="passport_number")

    persons = (
        (await db_session.execute(select(CasePerson).where(CasePerson.case_id == case.id)))
        .scalars()
        .all()
    )
    for person in persons:
        assert person.custom_fields == {}
        assert person.passport_number is None

    # And the field row itself exists exactly once, no value column.
    count = (
        await db_session.execute(
            select(func.count())
            .select_from(JourneyTemplateField)
            .where(JourneyTemplateField.template_id == uuid.UUID(tid))
        )
    ).scalar_one()
    assert count == 1
