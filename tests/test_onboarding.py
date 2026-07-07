"""Activation checklist (GET /agencies/me/onboarding + dismiss).

Covers: (a) fresh agency: 3 steps false, not dismissed; (b) each REAL
gesture checks its key: journey creation (editor AND AI import - the
milestone fix), case creation, view-as-client; (c) the demo seed checks
NOTHING by creation (gift journey excluded, demo case emits no signal),
but CONSULTING the demo through view-as-client checks open_case (the
closest existing trace - a plain GET leaves none by design); (d) the
dismiss persists, no un-dismiss."""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.client_case import ClientCase
from shared.models.rbac import Role
from src.agencies.demo_case_seed import seed_demo_case
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent

pytestmark = pytest.mark.usefixtures("rbac_baseline")


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


def _by_key(body: dict) -> dict[str, dict]:
    return {s["key"]: s for s in body["steps"]}


async def _state(client: AsyncClient, headers: dict[str, str]) -> dict:
    response = await client.get("/agencies/me/onboarding", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


# --- (a) fresh agency ----------------------------------------------------------------


async def test_fresh_agency_has_three_unchecked_steps(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    body = await _state(client, agent_headers(admin))
    assert body["dismissed"] is False
    steps = _by_key(body)
    assert set(steps) == {"create_journey", "open_case", "view_as_client"}
    assert all(not s["done"] and s["done_at"] is None for s in steps.values())


# --- (b) each real gesture checks its key ---------------------------------------------


async def test_real_gestures_check_their_keys(
    client: AsyncClient, db_session: AsyncSession, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)

    created = await client.post("/journeys", headers=headers, json={"name": "Mon parcours"})
    assert created.status_code == 201
    steps = _by_key(await _state(client, headers))
    assert steps["create_journey"]["done"] is True
    assert steps["create_journey"]["done_at"] is not None
    assert steps["open_case"]["done"] is False

    made = await client.post(
        "/cases",
        headers=headers,
        json={"first_name": "Jean", "last_name": "Client", "email": "jean@example.com"},
    )
    assert made.status_code == 201
    steps = _by_key(await _state(client, headers))
    assert steps["open_case"]["done"] is True
    assert steps["view_as_client"]["done"] is False

    case = await db_session.get(ClientCase, uuid.UUID(made.json()["id"]))
    assert case is not None
    seen = await client.post(
        f"/expat-users/{case.principal_expat_user_id}/impersonate", headers=headers
    )
    assert seen.status_code == 200, seen.text
    steps = _by_key(await _state(client, headers))
    assert steps["view_as_client"]["done"] is True


async def test_ai_import_checks_create_journey(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """The milestone fix: journey.imported_from_ai now folds
    premier_parcours_cree."""
    headers = agent_headers(admin)
    payload = {
        "version": 1,
        "parcours": {"nom": {"fr": "Importé"}, "etapes": [{"ref": "e1", "nom": {"fr": "E"}}]},
    }
    imported = await client.post("/journeys/import", headers=headers, json=payload)
    assert imported.status_code == 200, imported.text
    steps = _by_key(await _state(client, headers))
    assert steps["create_journey"]["done"] is True


# --- (c) the demo: creation checks nothing, consultation checks open_case ------------


async def test_demo_checks_nothing_by_creation_but_consultation_checks_open_case(
    client: AsyncClient, db_session: AsyncSession, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    agency = await db_session.get(Agency, admin.agency_id)
    assert agency is not None
    demo_case = await seed_demo_case(db_session, agency, admin)
    assert demo_case is not None and demo_case.is_demo

    # The seeded gift journey and demo case check NOTHING.
    steps = _by_key(await _state(client, headers))
    assert steps["create_journey"]["done"] is False  # gift template excluded
    assert steps["open_case"]["done"] is False
    assert steps["view_as_client"]["done"] is False

    # CONSULTING the demo (voir comme le client) is the closest existing
    # trace: it checks open_case AND view_as_client.
    demo = (
        await db_session.execute(select(ClientCase).where(ClientCase.id == demo_case.id))
    ).scalar_one()
    seen = await client.post(
        f"/expat-users/{demo.principal_expat_user_id}/impersonate", headers=headers
    )
    assert seen.status_code == 200, seen.text
    steps = _by_key(await _state(client, headers))
    assert steps["open_case"]["done"] is True
    assert steps["view_as_client"]["done"] is True
    assert steps["create_journey"]["done"] is False  # still untouched


# --- (d) dismiss persists --------------------------------------------------------------


async def test_dismiss_persists(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    dismissed = await client.post("/agencies/me/onboarding/dismiss", headers=headers)
    assert dismissed.status_code == 200, dismissed.text
    assert dismissed.json()["dismissed"] is True

    body = await _state(client, headers)
    assert body["dismissed"] is True  # persisted; no un-dismiss endpoint exists
