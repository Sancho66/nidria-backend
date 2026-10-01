"""Error i18n, wave E2 — the refusals an AGENT reads in Settings and on the
side sheets: roles & member role, saved views, custom-field definitions,
case costs, the case journal, the agency export.

Each refusal keeps its English `detail` (the fallback, byte-identical for
logs) and gains a stable `code` + `params` the front translates. Params
carry what a sentence needs and nothing technical: a role is named by the
name the agency gave it (a system role's name is a key the screen labels
itself, so it is never served), a field by its label in the request
language, never by its key; no id ever travels in params.

Also pinned: the raises left on their CATEGORY on purpose (never shown on
a screen), so a later wave does not « fix » them by reflex."""

import uuid
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.custom_field import CustomFieldDefinition
from shared.models.rbac import Permission as PermissionRow
from shared.models.rbac import Role
from src.core.exceptions import NotFoundError, ValidationError
from src.core.rbac.permissions import Permission
from src.custom_fields.custom_fields_validation import validate_and_merge
from src.export.export_manager import ExportManager
from tests.plugins.agency_plugin import MakeAgency
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.rbac_plugin import MakeRole

FIELDS = "/agencies/me/custom-fields"


@pytest.fixture
def ec_client(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


@pytest_asyncio.fixture
async def perm_ids(rbac_baseline: None, db_session: AsyncSession) -> dict[str, str]:
    rows = (await db_session.execute(select(PermissionRow))).scalars().all()
    return {row.key: str(row.id) for row in rows}


def _in(headers: dict[str, str], lang: str) -> dict[str, str]:
    return {**headers, "Accept-Language": lang}


def _envelope(response: Any) -> tuple[str, dict[str, Any], str]:
    body = response.json()
    return body["code"], body["params"], body["detail"]


# --- roles & member role ---------------------------------------------------------------


async def test_every_role_lookup_that_misses_shares_role_not_found(
    ec_client: AsyncClient,
    admin: Agent,
    make_agency: MakeAgency,
    make_agent: MakeAgent,
    make_role: MakeRole,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """One entity, one code, whatever the door: unknown id, another
    agency's custom role (no existence leak), and the platform-reserved
    role on the member assignment (a 422 there, same sentence)."""
    headers = agent_headers(admin)
    foreign = await make_role(
        permissions=[Permission.CASE_VIEW], agency_id=(await make_agency()).id
    )
    ghost = uuid.uuid4()
    colleague = await make_agent(agency_id=admin.agency_id)
    responses = [
        await ec_client.get(f"/agencies/me/roles/{ghost}", headers=headers),
        await ec_client.patch(
            f"/agencies/me/roles/{foreign.id}", headers=headers, json={"name": "x"}
        ),
        await ec_client.delete(f"/agencies/me/roles/{ghost}", headers=headers),
        await ec_client.post(
            f"/agencies/me/roles/{ghost}/duplicate", headers=headers, json={"name": "copy"}
        ),
        await ec_client.put(
            f"/agencies/me/members/{colleague.id}/role",
            headers=headers,
            json={"role_id": str(foreign.id)},
        ),
        await ec_client.put(
            f"/agencies/me/members/{colleague.id}/role",
            headers=headers,
            json={"role_id": str(system_roles["superadmin"].id)},
        ),
    ]
    assert [r.status_code for r in responses] == [404, 404, 404, 404, 422, 422]
    for response in responses:
        code, params, _ = _envelope(response)
        assert (code, params) == ("role.not_found", {})


async def test_role_name_taken_names_the_name_typed(
    ec_client: AsyncClient, admin: Agent, make_role: MakeRole, agent_headers: AuthHeaders
) -> None:
    """Create, rename, duplicate: the same guard, the same code, and the
    name the agent typed (their own words, never a key)."""
    headers = agent_headers(admin)
    await make_role(permissions=[], agency_id=admin.agency_id, name="Superviseurs")
    other = await make_role(permissions=[], agency_id=admin.agency_id, name="Stagiaires")
    responses = [
        await ec_client.post(
            "/agencies/me/roles",
            headers=headers,
            json={"name": "Superviseurs", "permission_ids": []},
        ),
        await ec_client.patch(
            f"/agencies/me/roles/{other.id}", headers=headers, json={"name": "Superviseurs"}
        ),
        await ec_client.post(
            f"/agencies/me/roles/{other.id}/duplicate",
            headers=headers,
            json={"name": "Superviseurs"},
        ),
    ]
    for response in responses:
        assert response.status_code == 409, response.text
        code, params, detail = _envelope(response)
        assert (code, params) == ("role.name_taken", {"name": "Superviseurs"})
        assert detail == "A role named 'Superviseurs' already exists in this agency."


async def test_beyond_ceiling_keeps_the_permission_keys_out_of_params(
    ec_client: AsyncClient,
    make_agency: MakeAgency,
    make_agent: MakeAgent,
    make_role: MakeRole,
    perm_ids: dict[str, str],
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """Both delegation paths (a matrix, an assignment) answer one code;
    the offending keys stay in the English detail only."""
    agency = await make_agency()
    ceiling = await make_role(
        permissions=[Permission.AGENT_MANAGE, Permission.ROLE_MANAGE, Permission.CASE_VIEW],
        agency_id=agency.id,
    )
    manager = await make_agent(agency_id=agency.id, role=ceiling)
    colleague = await make_agent(agency_id=agency.id)
    headers = agent_headers(manager)
    matrix = await ec_client.post(
        "/agencies/me/roles",
        headers=headers,
        json={"name": "Trop large", "permission_ids": [perm_ids["job.manage"]]},
    )
    assign = await ec_client.put(
        f"/agencies/me/members/{colleague.id}/role",
        headers=headers,
        json={"role_id": str(system_roles["admin"].id)},
    )
    for response in (matrix, assign):
        assert response.status_code == 403, response.text
        code, params, detail = _envelope(response)
        assert (code, params) == ("role.beyond_ceiling", {})
        assert "job.manage" in detail  # the detail still names the keys


async def test_system_role_refusals(
    ec_client: AsyncClient,
    admin: Agent,
    make_agent: MakeAgent,
    make_role: MakeRole,
    system_roles: dict[str, Role],
    perm_ids: dict[str, str],
    agent_headers: AuthHeaders,
) -> None:
    """Delete a system role (locked), edit one whose name a plain custom
    role already holds (clone_name_taken), assign one masked by its clone
    (masked_by_clone): no param — a system role's name is a key, the clone
    id stays in the detail."""
    headers = agent_headers(admin)
    locked = await ec_client.delete(
        f"/agencies/me/roles/{system_roles['member'].id}", headers=headers
    )
    assert locked.status_code == 403
    assert _envelope(locked)[:2] == ("role.system_locked", {})

    await make_role(permissions=[], agency_id=admin.agency_id, name="viewer")
    clash = await ec_client.patch(
        f"/agencies/me/roles/{system_roles['viewer'].id}", headers=headers, json={"name": "x"}
    )
    assert clash.status_code == 409
    assert _envelope(clash)[:2] == ("role.clone_name_taken", {})

    edited = await ec_client.put(
        f"/agencies/me/roles/{system_roles['member'].id}/permissions",
        headers=headers,
        json={"permission_ids": [perm_ids["case.view"]]},
    )
    clone_id = edited.json()["id"]
    colleague = await make_agent(agency_id=admin.agency_id)
    masked = await ec_client.put(
        f"/agencies/me/members/{colleague.id}/role",
        headers=headers,
        json={"role_id": str(system_roles["member"].id)},
    )
    assert masked.status_code == 409
    code, params, detail = _envelope(masked)
    assert (code, params) == ("role.masked_by_clone", {})
    assert clone_id in detail


async def test_role_in_use_names_the_role_and_counts_its_wearers(
    ec_client: AsyncClient,
    admin: Agent,
    make_agent: MakeAgent,
    make_role: MakeRole,
    agent_headers: AuthHeaders,
) -> None:
    role = await make_role(
        permissions=[Permission.CASE_VIEW], agency_id=admin.agency_id, name="Auditeurs"
    )
    for _ in range(2):
        await make_agent(agency_id=admin.agency_id, role=role)
    response = await ec_client.delete(f"/agencies/me/roles/{role.id}", headers=agent_headers(admin))
    assert response.status_code == 409
    code, params, detail = _envelope(response)
    assert (code, params) == ("role.in_use", {"name": "Auditeurs", "count": 2})
    assert detail == "Role is assigned to 2 agent(s)."


async def test_member_role_assignment_refusals(
    ec_client: AsyncClient,
    admin: Agent,
    make_agency: MakeAgency,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    own = await ec_client.put(
        f"/agencies/me/members/{admin.id}/role",
        headers=headers,
        json={"role_id": str(system_roles["member"].id)},
    )
    assert own.status_code == 403
    assert _envelope(own)[:2] == ("role.own_role_locked", {})

    stranger = await make_agent(agency_id=(await make_agency()).id)
    missing = await ec_client.put(
        f"/agencies/me/members/{stranger.id}/role",
        headers=headers,
        json={"role_id": str(system_roles["member"].id)},
    )
    assert missing.status_code == 404
    assert _envelope(missing)[:2] == ("member.not_found", {})

    colleague = await make_agent(agency_id=admin.agency_id)
    external = await ec_client.put(
        f"/agencies/me/members/{colleague.id}/role",
        headers=headers,
        json={"role_id": str(system_roles["external_lawyer"].id)},
    )
    assert external.status_code == 422
    assert _envelope(external)[:2] == ("role.external_not_assignable", {})


async def test_last_manager_on_the_role_doors(
    ec_client: AsyncClient,
    make_agency: MakeAgency,
    make_agent: MakeAgent,
    make_role: MakeRole,
    system_roles: dict[str, Role],
    perm_ids: dict[str, str],
    agent_headers: AuthHeaders,
) -> None:
    """The anti-lockout on the two role gestures that can trip it: a matrix
    edit dropping agent.manage, and the deletion of a clone that falls its
    only manager back to a matrix without it. (The member flows reuse the
    guard; their own screen may answer it under a member code.)"""
    agency = await make_agency()
    solo = await make_role(
        permissions=[Permission.AGENT_MANAGE, Permission.ROLE_MANAGE, Permission.CASE_VIEW],
        agency_id=agency.id,
        name="Gérant",
    )
    actor = await make_agent(agency_id=agency.id, role=solo)
    matrix = await ec_client.put(
        f"/agencies/me/roles/{solo.id}/permissions",
        headers=agent_headers(actor),
        json={"permission_ids": [perm_ids["role.manage"], perm_ids["case.view"]]},
    )

    other = await make_agency()
    admin = await make_agent(agency_id=other.id, role=system_roles["admin"])
    helper = await make_agent(agency_id=other.id, role=system_roles["admin"])
    member_keys = [perm_ids[key] for key in ("case.view", "agent.manage", "role.manage")]
    clone_id = (
        await ec_client.put(
            f"/agencies/me/roles/{system_roles['member'].id}/permissions",
            headers=agent_headers(admin),
            json={"permission_ids": member_keys},
        )
    ).json()["id"]
    for target, wearer, role_id in (
        (helper, admin, clone_id),
        (admin, helper, str(system_roles["viewer"].id)),
    ):
        moved = await ec_client.put(
            f"/agencies/me/members/{target.id}/role",
            headers=agent_headers(wearer),
            json={"role_id": role_id},
        )
        assert moved.status_code == 200, moved.text
    unmask = await ec_client.delete(f"/agencies/me/roles/{clone_id}", headers=agent_headers(helper))

    for response in (matrix, unmask):
        assert response.status_code == 409, response.text
        code, params, detail = _envelope(response)
        assert (code, params) == ("role.last_manager", {})
        assert "without any manager" in detail


# --- saved views ------------------------------------------------------------------------


async def test_saved_view_refusals(
    ec_client: AsyncClient,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """Unknown view (any door), another agent's view (owner-only), and a
    default on a view that is neither yours nor shared. No param: the view
    may be someone's PRIVATE one, its name is not ours to serve."""
    owner = await make_agent(role=system_roles["member"])
    other = await make_agent(agency_id=owner.agency_id, role=system_roles["member"])
    shared = await ec_client.post(
        "/views", headers=agent_headers(owner), json={"name": "Partagée", "is_shared": True}
    )
    private = await ec_client.post(
        "/views", headers=agent_headers(owner), json={"name": "Privée", "is_shared": False}
    )
    assert shared.status_code == 201 and private.status_code == 201
    headers = agent_headers(other)

    ghost = uuid.uuid4()
    for response in (
        await ec_client.patch(f"/views/{ghost}", headers=headers, json={"name": "x"}),
        await ec_client.post(f"/views/{ghost}/set-default", headers=headers),
        await ec_client.post(f"/views/{ghost}/unset-default", headers=headers),
    ):
        assert response.status_code == 404
        assert _envelope(response)[:2] == ("view.not_found", {})

    not_owner = await ec_client.patch(
        f"/views/{shared.json()['id']}", headers=headers, json={"name": "x"}
    )
    assert not_owner.status_code == 403
    assert _envelope(not_owner)[:2] == ("view.not_owner", {})

    for action in ("set-default", "unset-default"):
        response = await ec_client.post(f"/views/{private.json()['id']}/{action}", headers=headers)
        assert response.status_code == 403
        assert _envelope(response)[:2] == ("view.not_shared", {})


# --- custom-field definitions ------------------------------------------------------------


async def test_custom_field_key_exists_names_the_field_in_the_request_language(
    ec_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """The definition in place, named as the reader sees it, and whether it
    is archived (the remedy differs) — never its key, never its id."""
    headers = agent_headers(admin)
    body = {
        "key": "passport_no",
        "label": "N° de passeport",
        "label_i18n": {"fr": "N° de passeport", "en": "Passport number"},
        "field_type": "text",
        "scope": "person",
    }
    created = await ec_client.post(FIELDS, headers=headers, json=body)
    assert created.status_code == 201, created.text

    again = await ec_client.post(FIELDS, headers=_in(headers, "en"), json=body)
    assert again.status_code == 409
    code, params, detail = _envelope(again)
    assert (code, params) == (
        "custom_field.key_exists",
        {"label": "Passport number", "archived": False},
    )
    assert detail == "A custom field with key 'passport_no' already exists."

    archived = await ec_client.post(f"{FIELDS}/{created.json()['id']}/archive", headers=headers)
    assert archived.status_code == 200
    again = await ec_client.post(FIELDS, headers=_in(headers, "fr"), json=body)
    assert _envelope(again)[:2] == (
        "custom_field.key_exists",
        {"label": "N° de passeport", "archived": True},
    )


async def test_custom_field_not_found_on_every_door(
    ec_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    ghost = uuid.uuid4()
    for response in (
        await ec_client.patch(f"{FIELDS}/{ghost}", headers=headers, json={"label": "x"}),
        await ec_client.post(f"{FIELDS}/{ghost}/archive", headers=headers),
        await ec_client.post(f"{FIELDS}/{ghost}/unarchive", headers=headers),
    ):
        assert response.status_code == 404
        assert _envelope(response)[:2] == ("custom_field.not_found", {})


@pytest.mark.parametrize(("lang", "label"), [("en", "VAT number"), ("hu", "Adószám")])
async def test_company_preset_is_named_in_the_request_language(
    ec_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders, lang: str, label: str
) -> None:
    """`company_field.key_reserved` used to name the preset in French
    whatever the screen's language."""
    response = await ec_client.post(
        FIELDS,
        headers=_in(agent_headers(admin), lang),
        json={
            "label": "VAT number",
            "field_type": "text",
            "scope": "company",
            "profile_section": "identity",
        },
    )
    assert response.status_code == 409
    code, params, _ = _envelope(response)
    assert code == "company_field.key_reserved"
    assert params["label"] == label


# --- case costs, journal, export -----------------------------------------------------------


async def _case_with_step(
    client: AsyncClient,
    headers: dict[str, str],
    make_client_case: MakeClientCase,
    agency_id: uuid.UUID,
) -> tuple[uuid.UUID, str]:
    tid = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()["id"]
    await client.post(f"/journeys/{tid}/steps", headers=headers, json={"name": "Étape"})
    case = await make_client_case(agency_id=agency_id)
    timeline = (
        await client.post(
            f"/cases/{case.id}/journey", headers=headers, json={"journey_template_id": tid}
        )
    ).json()
    return case.id, timeline[0]["id"]


async def test_case_cost_refusals(
    ec_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """The case, the step, the REAL cost line (not the journey's planned
    cost, `cost.not_found`), and the decimals refusal now naming the
    currency and its cap."""
    await db_session.execute(
        update(Agency).where(Agency.id == admin.agency_id).values(currency="EUR")
    )
    await db_session.commit()
    headers = agent_headers(admin)
    case_id, pid = await _case_with_step(ec_client, headers, make_client_case, admin.agency_id)
    ghost = uuid.uuid4()

    no_case = await ec_client.get(f"/cases/{ghost}/costs", headers=headers)
    assert no_case.status_code == 404
    assert _envelope(no_case)[:2] == ("case.not_found", {})

    no_step = await ec_client.post(
        f"/cases/{case_id}/steps/{ghost}/costs",
        headers=headers,
        json={"amount": "10.00", "label": "Timbre"},
    )
    assert no_step.status_code == 404
    assert _envelope(no_step)[:2] == ("progress.step_not_found", {})

    for response in (
        await ec_client.patch(
            f"/cases/{case_id}/costs/{ghost}", headers=headers, json={"label": "x"}
        ),
        await ec_client.delete(f"/cases/{case_id}/costs/{ghost}", headers=headers),
    ):
        assert response.status_code == 404
        assert _envelope(response)[:2] == ("cost.line_not_found", {})

    decimals = await ec_client.post(
        f"/cases/{case_id}/steps/{pid}/costs",
        headers=headers,
        json={"amount": "10.505", "label": "Timbre"},
    )
    assert decimals.status_code == 422
    code, params, detail = _envelope(decimals)
    assert (code, params) == ("cost.amount_decimals", {"currency": "EUR", "max_decimals": 2})
    assert detail == "EUR allows at most 2 decimal place(s)."


async def test_case_journal_unknown_case(
    ec_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    response = await ec_client.get(f"/cases/{uuid.uuid4()}/activity", headers=agent_headers(admin))
    assert response.status_code == 404
    assert response.json() == {"detail": "Case not found.", "code": "case.not_found", "params": {}}


async def test_export_of_a_vanished_agency(db_session: AsyncSession) -> None:
    """Unreachable through HTTP (the agent's agency exists by FK); the code
    is the shared `agency.not_found`, already translated."""
    ghost = Agent(agency_id=uuid.uuid4(), role_id=uuid.uuid4())
    with pytest.raises(NotFoundError) as caught:
        await ExportManager(db_session).build_agency_export(ghost)
    assert (caught.value.code, caught.value.params) == ("agency.not_found", {})


# --- left on their category, on purpose -------------------------------------------------------


def test_an_unknown_field_type_keeps_the_category() -> None:
    """Only a plain ValueError reaches the aggregate without a code — a
    field type the enum cannot produce: an integrity net, not a screen."""
    definition = CustomFieldDefinition(key="odd", label="Odd", field_type="bogus", required=False)
    with pytest.raises(ValidationError) as caught:
        validate_and_merge([definition], {}, {"odd": "x"})
    assert (caught.value.code, caught.value.params) == ("validation_error", {})


async def test_unknown_permission_ids_keep_the_category(
    ec_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """The ids come from GET /permissions (an insert-only catalogue): only a
    hand-built request misses — the detail names the ids for the logs."""
    ghost = str(uuid.uuid4())
    response = await ec_client.post(
        "/agencies/me/roles",
        headers=agent_headers(admin),
        json={"name": "x", "permission_ids": [ghost]},
    )
    assert response.status_code == 422
    code, params, detail = _envelope(response)
    assert (code, params) == ("validation_error", {})
    assert ghost in detail
