"""Canvas layout storage (visual editor, MVP-1) — pure-presentation node
positions on journey_template. PUT replaces the blob; GET exposes it;
foreign/stale step ids are dropped server-side so the blob never rots.
Nothing here touches journey logic."""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient

from shared.models.agent import Agent
from shared.models.rbac import Role
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent


@pytest.fixture
def cl_client(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


@pytest_asyncio.fixture
async def member(admin: Agent, make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    """case.edit but NOT journey.configure."""
    return await make_agent(agency_id=admin.agency_id, role=system_roles["member"])


async def _template_with_steps(
    client: AsyncClient, headers: dict[str, str], names: list[str]
) -> tuple[str, list[str]]:
    tid = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()["id"]
    ids = []
    for name in names:
        ids.append(
            (
                await client.post(f"/journeys/{tid}/steps", headers=headers, json={"name": name})
            ).json()["id"]
        )
    return tid, ids


# --- PUT then GET round-trips the blob -----------------------------------------------


async def test_canvas_layout_put_then_get(
    cl_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    tid, [s1, s2] = await _template_with_steps(cl_client, headers, ["A", "B"])

    # Initially null (never opened in canvas).
    detail = (await cl_client.get(f"/journeys/{tid}", headers=headers)).json()
    assert detail["canvas_layout"] is None

    resp = await cl_client.put(
        f"/journeys/{tid}/canvas-layout",
        headers=headers,
        json={"positions": {s1: {"x": 10.5, "y": 20.0}, s2: {"x": 300, "y": 40}}},
    )
    assert resp.status_code == 200, resp.text
    saved = resp.json()
    assert saved[s1] == {"x": 10.5, "y": 20.0}
    assert saved[s2] == {"x": 300.0, "y": 40.0}

    # Exposed in the template detail.
    detail = (await cl_client.get(f"/journeys/{tid}", headers=headers)).json()
    assert detail["canvas_layout"][s1] == {"x": 10.5, "y": 20.0}
    assert detail["canvas_layout"][s2] == {"x": 300.0, "y": 40.0}


async def test_canvas_layout_replaces_whole_blob(
    cl_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """PUT replaces (not merges): a second PUT with only s1 drops s2."""
    headers = agent_headers(admin)
    tid, [s1, s2] = await _template_with_steps(cl_client, headers, ["A", "B"])
    await cl_client.put(
        f"/journeys/{tid}/canvas-layout",
        headers=headers,
        json={"positions": {s1: {"x": 1, "y": 1}, s2: {"x": 2, "y": 2}}},
    )
    resp = await cl_client.put(
        f"/journeys/{tid}/canvas-layout",
        headers=headers,
        json={"positions": {s1: {"x": 9, "y": 9}}},
    )
    saved = resp.json()
    assert set(saved.keys()) == {s1}  # s2 gone — full replace
    assert saved[s1] == {"x": 9.0, "y": 9.0}


# --- foreign / stale ids dropped (blob never rots) -----------------------------------


async def test_foreign_step_ids_dropped(
    cl_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """A position keyed by a step id NOT in this template is silently
    dropped — the blob only ever holds the template's own step ids."""
    headers = agent_headers(admin)
    tid, [s1] = await _template_with_steps(cl_client, headers, ["A"])
    ghost = str(uuid.uuid4())
    resp = await cl_client.put(
        f"/journeys/{tid}/canvas-layout",
        headers=headers,
        json={"positions": {s1: {"x": 1, "y": 1}, ghost: {"x": 5, "y": 5}}},
    )
    saved = resp.json()
    assert set(saved.keys()) == {s1}  # ghost dropped


async def test_deleted_step_pruned_on_next_put(
    cl_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """After a step is deleted, a re-save drops its stale position."""
    headers = agent_headers(admin)
    tid, [s1, s2] = await _template_with_steps(cl_client, headers, ["A", "B"])
    await cl_client.put(
        f"/journeys/{tid}/canvas-layout",
        headers=headers,
        json={"positions": {s1: {"x": 1, "y": 1}, s2: {"x": 2, "y": 2}}},
    )
    await cl_client.delete(f"/journeys/{tid}/steps/{s2}", headers=headers)
    # Re-save with the front's stale view (still includes s2) → s2 dropped.
    resp = await cl_client.put(
        f"/journeys/{tid}/canvas-layout",
        headers=headers,
        json={"positions": {s1: {"x": 1, "y": 1}, s2: {"x": 2, "y": 2}}},
    )
    assert set(resp.json().keys()) == {s1}


# --- gate + scoping ------------------------------------------------------------------


async def test_canvas_layout_gate_journey_configure(
    cl_client: AsyncClient, admin: Agent, member: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    tid, [s1] = await _template_with_steps(cl_client, headers, ["A"])
    denied = await cl_client.put(
        f"/journeys/{tid}/canvas-layout",
        headers=agent_headers(member),  # lacks journey.configure
        json={"positions": {s1: {"x": 1, "y": 1}}},
    )
    assert denied.status_code == 403


async def test_canvas_layout_foreign_template_404(
    cl_client: AsyncClient,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    tid, [s1] = await _template_with_steps(cl_client, headers, ["A"])
    other_admin = await make_agent(role=system_roles["admin"])
    denied = await cl_client.put(
        f"/journeys/{tid}/canvas-layout",
        headers=agent_headers(other_admin),
        json={"positions": {s1: {"x": 1, "y": 1}}},
    )
    assert denied.status_code == 404
