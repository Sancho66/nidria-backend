"""Deep clone (BLOC 3) — POST /journeys/{id}/clone. The point that matters:
SOURCE/CLONE ISOLATION. Cloning a sample produces a 100% independent copy
(every id remapped); mutating the clone never touches the source, and no
source id survives in the clone (steps, prerequisites, canvas keys).

The sample is built through the real editor API, then flipped to a library
sample (agency_id NULL + is_sample) — the seed is a later block."""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.journey import JourneyTemplate, JourneyTemplateStep, StepPrerequisite
from shared.models.rbac import Role
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent


@pytest.fixture
def cl(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


async def _make_sample(
    cl: AsyncClient, ah: dict[str, str], db_session: AsyncSession
) -> tuple[str, list[str]]:
    """Build a full tree via the API (S1 requires S0, a participant, a
    section, a field, a canvas), then FLIP the template to a library sample."""
    tid = (await cl.post("/journeys", headers=ah, json={"name": "Setup Espagne"})).json()["id"]
    s0 = (await cl.post(f"/journeys/{tid}/steps", headers=ah, json={"name": "S0"})).json()["id"]
    s1 = (await cl.post(f"/journeys/{tid}/steps", headers=ah, json={"name": "S1"})).json()["id"]
    await cl.put(
        f"/journeys/{tid}/steps/{s1}/prerequisites",
        headers=ah,
        json={"prerequisite_step_ids": [s0]},
    )
    await cl.post(
        f"/journeys/{tid}/steps/{s0}/participants",
        headers=ah,
        json={"type": "expat", "role": "executant"},
    )
    await cl.post(f"/journeys/{tid}/sections", headers=ah, json={"name": "Identité"})
    await cl.post(
        f"/journeys/{tid}/fields",
        headers=ah,
        json={"kind": "base_field", "reference": "passport_number"},
    )
    await cl.put(
        f"/journeys/{tid}/canvas-layout",
        headers=ah,
        json={"positions": {s0: {"x": 1.0, "y": 2.0}, s1: {"x": 3.0, "y": 4.0}}},
    )
    # Flip to a library sample (agency-less). A single UPDATE.
    tpl = await db_session.get(JourneyTemplate, uuid.UUID(tid))
    assert tpl is not None
    tpl.agency_id = None
    tpl.is_sample = True
    await db_session.commit()
    return tid, [s0, s1]


async def test_clone_is_independent_and_remaps_every_id(
    cl: AsyncClient, admin: Agent, db_session: AsyncSession, agent_headers: AuthHeaders
) -> None:
    ah = agent_headers(admin)
    src_id, src_steps = await _make_sample(cl, ah, db_session)
    src_step_ids = set(src_steps)

    resp = await cl.post(f"/journeys/{src_id}/clone", headers=ah, json={})
    assert resp.status_code == 201, resp.text
    clone_id = resp.json()["id"]
    assert clone_id != src_id
    assert resp.json()["name"] == "Setup Espagne (copie)"

    detail = (await cl.get(f"/journeys/{clone_id}", headers=ah)).json()
    steps = {s["name"]: s for s in detail["steps"]}
    clone_step_ids = {s["id"] for s in detail["steps"]}

    # Steps preserved, ids ALL new (no source id survives).
    assert set(steps) == {"S0", "S1"}
    assert clone_step_ids.isdisjoint(src_step_ids)
    # Prerequisite remapped to the CLONE's S0 (never the source's).
    assert steps["S1"]["prerequisite_step_ids"] == [steps["S0"]["id"]]
    assert src_step_ids.isdisjoint(set(steps["S1"]["prerequisite_step_ids"]))
    # Participant + section + field copied.
    assert [p["role"] for p in steps["S0"]["participants"]] == ["executant"]
    assert len(detail["sections"]) == 1
    assert len(detail["fields"]) == 1
    # Canvas keys are the CLONE step ids, none from the source.
    assert set(detail["canvas_layout"]) == clone_step_ids
    assert set(detail["canvas_layout"]).isdisjoint(src_step_ids)

    # Clone is in the agency list (owned, not a sample); the sample is not.
    agency_ids = {t["id"] for t in (await cl.get("/journeys", headers=ah)).json()}
    assert clone_id in agency_ids
    assert src_id not in agency_ids


async def test_mutating_clone_never_touches_the_source(
    cl: AsyncClient, admin: Agent, db_session: AsyncSession, agent_headers: AuthHeaders
) -> None:
    ah = agent_headers(admin)
    src_id, [s0, s1] = await _make_sample(cl, ah, db_session)

    clone_id = (await cl.post(f"/journeys/{src_id}/clone", headers=ah, json={})).json()["id"]
    clone = {
        s["name"]: s for s in (await cl.get(f"/journeys/{clone_id}", headers=ah)).json()["steps"]
    }

    # Mutate the CLONE: rename S0, clear S1's prerequisites.
    await cl.patch(
        f"/journeys/{clone_id}/steps/{clone['S0']['id']}", headers=ah, json={"name": "RENAMED"}
    )
    await cl.put(
        f"/journeys/{clone_id}/steps/{clone['S1']['id']}/prerequisites",
        headers=ah,
        json={"prerequisite_step_ids": []},
    )

    # The SOURCE sample is UNCHANGED (re-read from the DB).
    names = (
        (
            await db_session.execute(
                select(JourneyTemplateStep.name).where(
                    JourneyTemplateStep.template_id == uuid.UUID(src_id)
                )
            )
        )
        .scalars()
        .all()
    )
    assert set(names) == {"S0", "S1"}  # S0 not renamed
    prereqs = (
        (
            await db_session.execute(
                select(StepPrerequisite).where(StepPrerequisite.step_id == uuid.UUID(s1))
            )
        )
        .scalars()
        .all()
    )
    assert len(prereqs) == 1  # prerequisite still there
    assert str(prereqs[0].prerequisite_step_id) == s0


async def test_clone_without_body_uses_default_name(
    cl: AsyncClient, admin: Agent, db_session: AsyncSession, agent_headers: AuthHeaders
) -> None:
    """The body is optional: POST with NO body (and with {}) → 200 + default
    name. No 422 'field required'."""
    ah = agent_headers(admin)
    src_id, _ = await _make_sample(cl, ah, db_session)

    no_body = await cl.post(f"/journeys/{src_id}/clone", headers=ah)  # no json at all
    assert no_body.status_code == 201, no_body.text
    assert no_body.json()["name"] == "Setup Espagne (copie)"

    empty = await cl.post(f"/journeys/{src_id}/clone", headers=ah, json={})
    assert empty.status_code == 201
    assert empty.json()["name"] == "Setup Espagne (copie)"

    named = await cl.post(f"/journeys/{src_id}/clone", headers=ah, json={"name": "Mon parcours"})
    assert named.status_code == 201
    assert named.json()["name"] == "Mon parcours"


async def test_clone_relaunch_creates_a_new_copy(
    cl: AsyncClient, admin: Agent, db_session: AsyncSession, agent_headers: AuthHeaders
) -> None:
    ah = agent_headers(admin)
    src_id, _ = await _make_sample(cl, ah, db_session)
    a = (await cl.post(f"/journeys/{src_id}/clone", headers=ah, json={})).json()["id"]
    b = (await cl.post(f"/journeys/{src_id}/clone", headers=ah, json={})).json()["id"]
    assert a != b  # a clone is a copy on demand, not a dedup


async def test_clone_foreign_template_is_404(
    cl: AsyncClient,
    admin: Agent,
    db_session: AsyncSession,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    other = await make_agent(role=system_roles["admin"])
    foreign = JourneyTemplate(id=uuid.uuid4(), agency_id=other.agency_id, name="Foreign")
    db_session.add(foreign)
    await db_session.commit()
    resp = await cl.post(f"/journeys/{foreign.id}/clone", headers=agent_headers(admin), json={})
    assert resp.status_code == 404  # get_clone_source rejects another agency
