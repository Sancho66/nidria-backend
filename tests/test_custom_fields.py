"""Custom fields (DÉGEL 2) battery: definition CRUD, per-type
validation with readable errors, RGPD isolation (definition AND value),
orphan value after archive, the field.manage vs case.edit permission
split, immutable key/type, the bounded-required rule (point 1), and the
GET /cases exposure + PDF."""

import pytest
import pytest_asyncio
from httpx import AsyncClient

from shared.models.agent import Agent
from shared.models.rbac import Role
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase


@pytest.fixture
def cf_client(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


@pytest_asyncio.fixture
async def member(admin: Agent, make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    """Same agency, member: case.edit but NOT field.manage."""
    return await make_agent(agency_id=admin.agency_id, role=system_roles["member"])


async def _define(client: AsyncClient, headers: dict[str, str], **overrides: object) -> dict:
    payload = {"key": "visa_number", "label": "Numéro de visa", "field_type": "text", **overrides}
    response = await client.post("/agencies/me/custom-fields", headers=headers, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


# --- definition CRUD -----------------------------------------------------------------


async def test_create_list_update_archive(
    cf_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    created = await _define(cf_client, headers, position=1)
    assert created["field_type"] == "text" and created["archived_at"] is None

    listing = await cf_client.get("/agencies/me/custom-fields", headers=headers)
    assert [d["key"] for d in listing.json()] == ["visa_number"]

    renamed = await cf_client.patch(
        f"/agencies/me/custom-fields/{created['id']}",
        headers=headers,
        json={"label": "Visa #", "required": True},
    )
    assert renamed.status_code == 200
    assert renamed.json()["label"] == "Visa #" and renamed.json()["required"] is True

    archived = await cf_client.post(
        f"/agencies/me/custom-fields/{created['id']}/archive", headers=headers
    )
    assert archived.status_code == 200 and archived.json()["archived_at"] is not None
    # Archived fields drop out of the default listing.
    assert (await cf_client.get("/agencies/me/custom-fields", headers=headers)).json() == []
    with_archived = await cf_client.get(
        "/agencies/me/custom-fields?include_archived=true", headers=headers
    )
    assert [d["key"] for d in with_archived.json()] == ["visa_number"]


async def test_duplicate_key_409(
    cf_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    await _define(cf_client, headers)
    dup = await cf_client.post(
        "/agencies/me/custom-fields",
        headers=headers,
        json={"key": "visa_number", "label": "Other", "field_type": "text"},
    )
    assert dup.status_code == 409


async def test_select_requires_options(
    cf_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    bad = await cf_client.post(
        "/agencies/me/custom-fields",
        headers=agent_headers(admin),
        json={"key": "permit", "label": "Permis", "field_type": "select"},
    )
    assert bad.status_code == 422


# --- permission split ----------------------------------------------------------------


async def test_field_manage_gate(
    cf_client: AsyncClient, admin: Agent, member: Agent, agent_headers: AuthHeaders
) -> None:
    # member (case.edit, no field.manage) cannot DEFINE…
    denied = await cf_client.post(
        "/agencies/me/custom-fields",
        headers=agent_headers(member),
        json={"key": "x", "label": "X", "field_type": "text"},
    )
    assert denied.status_code == 403
    # …but CAN read the definitions (case.view) to render the form.
    listing = await cf_client.get("/agencies/me/custom-fields", headers=agent_headers(member))
    assert listing.status_code == 200


# --- per-type validation on the person PATCH -----------------------------------------


async def test_each_type_valid_and_invalid(
    cf_client: AsyncClient,
    admin: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    await _define(cf_client, headers, key="vnum", label="N° visa", field_type="text")
    await _define(cf_client, headers, key="age", label="Âge", field_type="number")
    await _define(cf_client, headers, key="expiry", label="Expiration", field_type="date")
    await _define(cf_client, headers, key="vip", label="VIP", field_type="boolean")
    await _define(
        cf_client,
        headers,
        key="permit",
        label="Permis",
        field_type="select",
        options=["temp", "perm"],
    )
    await _define(
        cf_client,
        headers,
        key="langs",
        label="Langues",
        field_type="multi_select",
        options=["fr", "en", "ru"],
    )

    case = await make_client_case(agency_id=admin.agency_id)
    person_id = (await cf_client.get(f"/cases/{case.id}", headers=headers)).json()[
        "principal_person_id"
    ]

    ok = await cf_client.patch(
        f"/cases/{case.id}/persons/{person_id}",
        headers=headers,
        json={
            "custom_fields": {
                "vnum": "AB123",
                "age": "42",
                "expiry": "2027-01-15",
                "vip": "true",
                "permit": "temp",
                "langs": ["fr", "ru"],
            }
        },
    )
    assert ok.status_code == 200
    cf = ok.json()["custom_fields"]
    assert cf == {
        "vnum": "AB123",
        "age": 42,
        "expiry": "2027-01-15",
        "vip": True,
        "permit": "temp",
        "langs": ["fr", "ru"],
    }

    # A bad value per type → 422 with a readable, accumulated message.
    bad = await cf_client.patch(
        f"/cases/{case.id}/persons/{person_id}",
        headers=headers,
        json={"custom_fields": {"age": "not-a-number", "expiry": "nope"}},
    )
    assert bad.status_code == 422
    detail = bad.json()["detail"]
    assert "Âge" in detail and "Expiration" in detail  # both reported


async def test_multi_select_out_of_options_422(
    cf_client: AsyncClient,
    admin: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    await _define(
        cf_client,
        headers,
        key="langs",
        label="Langues",
        field_type="multi_select",
        options=["fr", "en"],
    )
    case = await make_client_case(agency_id=admin.agency_id)
    person_id = (await cf_client.get(f"/cases/{case.id}", headers=headers)).json()[
        "principal_person_id"
    ]
    bad = await cf_client.patch(
        f"/cases/{case.id}/persons/{person_id}",
        headers=headers,
        json={"custom_fields": {"langs": ["fr", "de"]}},
    )
    assert bad.status_code == 422
    assert "Langues" in bad.json()["detail"] and "de" in bad.json()["detail"]


async def test_unknown_or_archived_key_422(
    cf_client: AsyncClient,
    admin: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    field = await _define(cf_client, headers, key="vnum", label="N° visa", field_type="text")
    case = await make_client_case(agency_id=admin.agency_id)
    person_id = (await cf_client.get(f"/cases/{case.id}", headers=headers)).json()[
        "principal_person_id"
    ]
    # Unknown key.
    unknown = await cf_client.patch(
        f"/cases/{case.id}/persons/{person_id}",
        headers=headers,
        json={"custom_fields": {"ghost": "x"}},
    )
    assert unknown.status_code == 422 and "ghost" in unknown.json()["detail"]
    # Archived key → strict 422 too.
    await cf_client.post(f"/agencies/me/custom-fields/{field['id']}/archive", headers=headers)
    archived = await cf_client.patch(
        f"/cases/{case.id}/persons/{person_id}",
        headers=headers,
        json={"custom_fields": {"vnum": "AB123"}},
    )
    assert archived.status_code == 422 and "vnum" in archived.json()["detail"]


# --- the bounded-required rule (point 1) ---------------------------------------------


async def test_required_added_later_does_not_block_unrelated_edit(
    cf_client: AsyncClient,
    admin: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """A required field created AFTER a person exists must not block a
    PATCH that doesn't mention it — editing the phone stays possible."""
    headers = agent_headers(admin)
    case = await make_client_case(agency_id=admin.agency_id)
    person_id = (await cf_client.get(f"/cases/{case.id}", headers=headers)).json()[
        "principal_person_id"
    ]
    # The person already exists with custom_fields={}. Now add a required field.
    await _define(cf_client, headers, key="visa", label="Visa", field_type="text", required=True)

    # Editing an unrelated (civil-status) field — no custom_fields key in
    # the payload → not blocked by the retroactive required.
    edit = await cf_client.patch(
        f"/cases/{case.id}/persons/{person_id}",
        headers=headers,
        json={"phone": "+33600000000"},
    )
    assert edit.status_code == 200

    # Explicitly sending the required field EMPTY → blocked.
    blocked = await cf_client.patch(
        f"/cases/{case.id}/persons/{person_id}",
        headers=headers,
        json={"custom_fields": {"visa": ""}},
    )
    assert blocked.status_code == 422 and "Visa" in blocked.json()["detail"]


# --- RGPD isolation ------------------------------------------------------------------


async def test_definition_and_value_isolated_across_agencies(
    cf_client: AsyncClient,
    admin: Agent,
    make_agent: MakeAgent,
    make_client_case: MakeClientCase,
    make_expat_user: object,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """Agency A defines a field + sets a value on a shared expat's case.
    Agency B (sharing the same expat) sees neither the definition nor
    the value."""
    headers_a = agent_headers(admin)
    field = await _define(cf_client, headers_a, key="vnum", label="N° visa", field_type="text")

    shared_expat = await make_expat_user()  # type: ignore[operator]
    case_a = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=shared_expat.id
    )
    detail_a = (await cf_client.get(f"/cases/{case_a.id}", headers=headers_a)).json()
    await cf_client.patch(
        f"/cases/{case_a.id}/persons/{detail_a['principal_person_id']}",
        headers=headers_a,
        json={"custom_fields": {"vnum": "SECRET-A"}},
    )

    # Agency B: own agency, same shared expat as principal.
    admin_b = await make_agent(role=system_roles["admin"])  # other agency
    headers_b = agent_headers(admin_b)
    # B's definition list does NOT contain A's field.
    defs_b = await cf_client.get("/agencies/me/custom-fields", headers=headers_b)
    assert field["id"] not in {d["id"] for d in defs_b.json()}
    # B's case on the same expat: no value exposed (different case_person).
    case_b = await make_client_case(
        agency_id=admin_b.agency_id, principal_expat_user_id=shared_expat.id
    )
    detail_b = (await cf_client.get(f"/cases/{case_b.id}", headers=headers_b)).json()
    principal_b = next(p for p in detail_b["persons"] if p["kind"] == "principal")
    assert principal_b["custom_fields"] == {}
    assert detail_b["custom_field_definitions"] == []


# --- orphan value after archive ------------------------------------------------------


async def test_orphan_value_after_archive_no_crash(
    cf_client: AsyncClient,
    admin: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """A value saved, then its definition archived: the GET must not
    crash and must NOT expose the orphan (but it stays in the DB)."""
    headers = agent_headers(admin)
    field = await _define(cf_client, headers, key="vnum", label="N° visa", field_type="text")
    case = await make_client_case(agency_id=admin.agency_id)
    detail = (await cf_client.get(f"/cases/{case.id}", headers=headers)).json()
    person_id = detail["principal_person_id"]
    await cf_client.patch(
        f"/cases/{case.id}/persons/{person_id}",
        headers=headers,
        json={"custom_fields": {"vnum": "AB123"}},
    )
    # Archive the definition.
    await cf_client.post(f"/agencies/me/custom-fields/{field['id']}/archive", headers=headers)
    # GET still works; the orphan value is hidden.
    after = await cf_client.get(f"/cases/{case.id}", headers=headers)
    assert after.status_code == 200
    principal = next(p for p in after.json()["persons"] if p["kind"] == "principal")
    assert principal["custom_fields"] == {}
    assert after.json()["custom_field_definitions"] == []


# --- GET /cases exposure + PDF -------------------------------------------------------


async def test_detail_exposes_definitions_and_values(
    cf_client: AsyncClient,
    admin: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    await _define(cf_client, headers, key="vnum", label="N° visa", field_type="text", position=2)
    await _define(cf_client, headers, key="vip", label="VIP", field_type="boolean", position=1)
    case = await make_client_case(agency_id=admin.agency_id)
    detail = (await cf_client.get(f"/cases/{case.id}", headers=headers)).json()
    # Definitions embedded, ordered by position.
    assert [d["key"] for d in detail["custom_field_definitions"]] == ["vip", "vnum"]

    person_id = detail["principal_person_id"]
    await cf_client.patch(
        f"/cases/{case.id}/persons/{person_id}",
        headers=headers,
        json={"custom_fields": {"vnum": "AB123", "vip": "true"}},
    )
    pdf = await cf_client.get(f"/cases/{case.id}/export", headers=headers)
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")


# --- unarchive (resurrection) --------------------------------------------------------


async def test_unarchive_resurrects_field_and_orphan_value(
    cf_client: AsyncClient,
    admin: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """define → set value → archive (value hidden) → unarchive → the
    value reappears, exposed and validable again."""
    headers = agent_headers(admin)
    field = await _define(cf_client, headers, key="vnum", label="N° visa", field_type="text")
    case = await make_client_case(agency_id=admin.agency_id)
    person_id = (await cf_client.get(f"/cases/{case.id}", headers=headers)).json()[
        "principal_person_id"
    ]
    await cf_client.patch(
        f"/cases/{case.id}/persons/{person_id}",
        headers=headers,
        json={"custom_fields": {"vnum": "AB123"}},
    )

    # Archive → value hidden, definition gone from the form.
    await cf_client.post(f"/agencies/me/custom-fields/{field['id']}/archive", headers=headers)
    archived_detail = (await cf_client.get(f"/cases/{case.id}", headers=headers)).json()
    principal = next(p for p in archived_detail["persons"] if p["kind"] == "principal")
    assert principal["custom_fields"] == {}
    assert archived_detail["custom_field_definitions"] == []

    # Unarchive → field active again, the kept JSONB value re-exposed.
    resurrected = await cf_client.post(
        f"/agencies/me/custom-fields/{field['id']}/unarchive", headers=headers
    )
    assert resurrected.status_code == 200 and resurrected.json()["archived_at"] is None
    after = (await cf_client.get(f"/cases/{case.id}", headers=headers)).json()
    principal = next(p for p in after["persons"] if p["kind"] == "principal")
    assert principal["custom_fields"] == {"vnum": "AB123"}  # value resurrected
    assert [d["key"] for d in after["custom_field_definitions"]] == ["vnum"]

    # The resurrected field is validable again on a PATCH.
    revalidate = await cf_client.patch(
        f"/cases/{case.id}/persons/{person_id}",
        headers=headers,
        json={"custom_fields": {"vnum": "CD456"}},
    )
    assert revalidate.status_code == 200
    assert revalidate.json()["custom_fields"]["vnum"] == "CD456"


async def test_unarchive_idempotent_on_active_field(
    cf_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    field = await _define(cf_client, headers, key="vnum", label="N° visa", field_type="text")
    # Already active → no-op, not an error (symmetric with archive).
    response = await cf_client.post(
        f"/agencies/me/custom-fields/{field['id']}/unarchive", headers=headers
    )
    assert response.status_code == 200 and response.json()["archived_at"] is None


async def test_unarchive_gate_field_manage(
    cf_client: AsyncClient, admin: Agent, member: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    field = await _define(cf_client, headers, key="vnum", label="N° visa", field_type="text")
    await cf_client.post(f"/agencies/me/custom-fields/{field['id']}/archive", headers=headers)
    # member has case.edit but NOT field.manage → 403.
    denied = await cf_client.post(
        f"/agencies/me/custom-fields/{field['id']}/unarchive", headers=agent_headers(member)
    )
    assert denied.status_code == 403
