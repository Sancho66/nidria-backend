"""Saved views battery (ported from Prism): CRUD + duplicate names,
agent/agency scoping, is_shared (visible to the agency, owner-only
mutations), per-agent default, customizable "All" (204 absence, upsert,
idempotent reset, sentinel guard), columns catalog."""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient

from shared.models.agent import Agent
from shared.models.rbac import Role
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent


@pytest.fixture
def views_client(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def member(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["member"])


@pytest_asyncio.fixture
async def colleague(member: Agent, make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    """Second agent of the SAME agency."""
    return await make_agent(agency_id=member.agency_id, role=system_roles["member"])


def _payload(**overrides: object) -> dict[str, object]:
    return {
        "name": "Dossiers Paraguay",
        "filters": {"dest_country": "PY", "status": ["in_progress"]},
        "columns": ["principal", "status", "owner"],
        "column_sizing": {"principal": 220},
        "sort_by": "created_at",
        "sort_order": "desc",
        **overrides,
    }


# --- columns catalog ---------------------------------------------------------------


async def test_columns_catalog(
    views_client: AsyncClient, member: Agent, agent_headers: AuthHeaders
) -> None:
    response = await views_client.get("/cases/columns", headers=agent_headers(member))
    assert response.status_code == 200
    columns = response.json()["columns"]
    by_key = {c["key"]: c for c in columns}
    assert by_key["principal"]["locked"] is True
    assert by_key["status"]["default"] is True
    assert by_key["source"]["default"] is False
    assert all({"key", "label", "type", "default", "locked"} <= set(c) for c in columns)


async def test_columns_catalog_requires_token(views_client: AsyncClient) -> None:
    assert (await views_client.get("/cases/columns")).status_code == 401


# --- CRUD ---------------------------------------------------------------------------


async def test_create_and_list_view(
    views_client: AsyncClient, member: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(member)
    created = await views_client.post("/views", headers=headers, json=_payload())
    assert created.status_code == 201
    body = created.json()
    assert body["name"] == "Dossiers Paraguay"
    assert body["entity"] == "cases"
    assert body["filters"] == {"dest_country": "PY", "status": ["in_progress"]}
    assert body["columns"] == ["principal", "status", "owner"]
    assert body["column_sizing"] == {"principal": 220}
    assert body["sort_by"] == "created_at" and body["sort_order"] == "desc"
    assert body["is_mine"] is True
    assert body["is_shared"] is False

    listing = await views_client.get("/views?entity=cases", headers=headers)
    assert listing.status_code == 200
    assert [v["id"] for v in listing.json()] == [body["id"]]


async def test_duplicate_names_allowed(
    views_client: AsyncClient, member: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(member)
    assert (await views_client.post("/views", headers=headers, json=_payload())).status_code == 201
    assert (await views_client.post("/views", headers=headers, json=_payload())).status_code == 201


async def test_create_with_default_all_entity_422(
    views_client: AsyncClient, member: Agent, agent_headers: AuthHeaders
) -> None:
    response = await views_client.post(
        "/views", headers=agent_headers(member), json=_payload(entity="cases_all")
    )
    assert response.status_code == 422


async def test_update_is_full_replace_on_provided_fields(
    views_client: AsyncClient, member: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(member)
    view = (await views_client.post("/views", headers=headers, json=_payload())).json()
    patched = await views_client.patch(
        f"/views/{view['id']}",
        headers=headers,
        json={"filters": {"q": "martin"}, "columns": None, "name": "Renommée"},
    )
    assert patched.status_code == 200
    body = patched.json()
    assert body["name"] == "Renommée"
    assert body["filters"] == {"q": "martin"}  # replaced, not merged
    assert body["columns"] is None
    assert body["sort_by"] == "created_at"  # untouched field survives


async def test_delete_own_view(
    views_client: AsyncClient, member: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(member)
    view = (await views_client.post("/views", headers=headers, json=_payload())).json()
    assert (await views_client.delete(f"/views/{view['id']}", headers=headers)).status_code == 204
    assert (await views_client.get("/views", headers=headers)).json() == []


# --- scoping & sharing ---------------------------------------------------------------


async def test_private_views_invisible_to_other_agents_and_agencies(
    views_client: AsyncClient,
    member: Agent,
    colleague: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    view = (
        await views_client.post("/views", headers=agent_headers(member), json=_payload())
    ).json()

    # Same agency, other agent: a PRIVATE view is invisible…
    assert (await views_client.get("/views", headers=agent_headers(colleague))).json() == []
    # …and unreachable for mutation (404: scoped lookup, no leak).
    foreign_agent = await make_agent(role=system_roles["viewer"])  # other agency
    for actor in (colleague, foreign_agent):
        patched = await views_client.patch(
            f"/views/{view['id']}", headers=agent_headers(actor), json={"name": "hack"}
        )
        assert patched.status_code in (403, 404)
    # Cross-agency is strictly 404 (existence never leaks).
    cross = await views_client.patch(
        f"/views/{view['id']}", headers=agent_headers(foreign_agent), json={"name": "x"}
    )
    assert cross.status_code == 404


async def test_shared_view_visible_but_owner_only_mutations(
    views_client: AsyncClient,
    member: Agent,
    colleague: Agent,
    agent_headers: AuthHeaders,
) -> None:
    view = (
        await views_client.post(
            "/views", headers=agent_headers(member), json=_payload(is_shared=True)
        )
    ).json()

    # The colleague SEES it…
    listing = (await views_client.get("/views", headers=agent_headers(colleague))).json()
    assert [v["id"] for v in listing] == [view["id"]]
    assert listing[0]["is_mine"] is False
    assert listing[0]["agent_name"]

    # …but cannot mutate it (owner-only).
    patched = await views_client.patch(
        f"/views/{view['id']}", headers=agent_headers(colleague), json={"name": "hijack"}
    )
    assert patched.status_code == 403
    deleted = await views_client.delete(f"/views/{view['id']}", headers=agent_headers(colleague))
    assert deleted.status_code == 403


# --- default views --------------------------------------------------------------------


async def test_set_default_unsets_previous(
    views_client: AsyncClient, member: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(member)
    v1 = (await views_client.post("/views", headers=headers, json=_payload(name="V1"))).json()
    v2 = (await views_client.post("/views", headers=headers, json=_payload(name="V2"))).json()

    assert (
        await views_client.post(f"/views/{v1['id']}/set-default", headers=headers)
    ).status_code == 200
    second = await views_client.post(f"/views/{v2['id']}/set-default", headers=headers)
    assert second.status_code == 200 and second.json()["is_default"] is True

    listing = {v["id"]: v for v in (await views_client.get("/views", headers=headers)).json()}
    assert listing[v1["id"]]["is_default"] is False
    assert listing[v2["id"]]["is_default"] is True

    unset = await views_client.post(f"/views/{v2['id']}/unset-default", headers=headers)
    assert unset.status_code == 200 and unset.json()["is_default"] is False


async def test_set_default_on_shared_view_allowed_private_forbidden(
    views_client: AsyncClient,
    member: Agent,
    colleague: Agent,
    agent_headers: AuthHeaders,
) -> None:
    shared = (
        await views_client.post(
            "/views", headers=agent_headers(member), json=_payload(is_shared=True)
        )
    ).json()
    response = await views_client.post(
        f"/views/{shared['id']}/set-default", headers=agent_headers(colleague)
    )
    assert response.status_code == 200  # own-or-shared rule (Prism)


# --- customizable "All" ----------------------------------------------------------------


async def test_default_all_flow(
    views_client: AsyncClient, member: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(member)
    # Absence is a normal state: 204, not 404.
    assert (
        await views_client.get("/views/default-all?entity=cases_all", headers=headers)
    ).status_code == 204

    first = await views_client.put(
        "/views/default-all?entity=cases_all",
        headers=headers,
        json={"filters": {"status": ["in_progress"]}, "sort_by": "created_at"},
    )
    assert first.status_code == 200
    body = first.json()
    assert body["is_default_all"] is True
    assert body["name"] == "__all__:cases_all"  # server-controlled sentinel

    # Second save lands on the SAME row (upsert, partial unique index).
    second = await views_client.put(
        "/views/default-all?entity=cases_all",
        headers=headers,
        json={"filters": {"q": "martin"}},
    )
    assert second.status_code == 200
    assert second.json()["id"] == body["id"]
    assert second.json()["filters"] == {"q": "martin"}

    # Named-views listing NEVER shows the "All" rows.
    assert (await views_client.get("/views", headers=headers)).json() == []

    # Generic CRUD refuses the sentinel row.
    guarded = await views_client.patch(
        f"/views/{body['id']}", headers=headers, json={"name": "sneaky"}
    )
    assert guarded.status_code == 422

    # Reset is idempotent: 204 with and without an existing row.
    assert (
        await views_client.delete("/views/default-all?entity=cases_all", headers=headers)
    ).status_code == 204
    assert (
        await views_client.get("/views/default-all?entity=cases_all", headers=headers)
    ).status_code == 204
    assert (
        await views_client.delete("/views/default-all?entity=cases_all", headers=headers)
    ).status_code == 204


async def test_default_all_is_per_agent(
    views_client: AsyncClient,
    member: Agent,
    colleague: Agent,
    agent_headers: AuthHeaders,
) -> None:
    await views_client.put(
        "/views/default-all?entity=cases_all",
        headers=agent_headers(member),
        json={"filters": {"q": "x"}},
    )
    # The colleague's "All" is untouched.
    assert (
        await views_client.get(
            "/views/default-all?entity=cases_all", headers=agent_headers(colleague)
        )
    ).status_code == 204


async def test_default_all_invalid_entity_and_forbidden_fields(
    views_client: AsyncClient, member: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(member)
    bad_entity = await views_client.put(
        "/views/default-all?entity=cases", headers=headers, json={"filters": {}}
    )
    assert bad_entity.status_code == 422
    # extra="forbid": excluded fields are a 422, not a silent drop.
    bad_field = await views_client.put(
        "/views/default-all?entity=cases_all",
        headers=headers,
        json={"filters": {}, "is_shared": True},
    )
    assert bad_field.status_code == 422


async def test_view_id_route_rejects_unknown_view(
    views_client: AsyncClient, member: Agent, agent_headers: AuthHeaders
) -> None:
    response = await views_client.patch(
        f"/views/{uuid.uuid4()}", headers=agent_headers(member), json={"name": "x"}
    )
    assert response.status_code == 404
