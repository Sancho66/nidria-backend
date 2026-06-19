"""Library samples (BLOC 2) — read-only. A sample is agency-less
(agency_id IS NULL + is_sample) and lives in GET /journeys/library, NEVER in
the agency's own GET /journeys. The clone-source resolver accepts a sample or
the agency's own template, but not another agency's. No write, no clone here
(samples are inserted directly — the seed is a later block)."""

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.journey import JourneyTemplate
from shared.models.rbac import Role
from src.core.exceptions import NotFoundError
from src.journeys.journeys_manager import JourneysManager
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent


@pytest.fixture
def lib_client(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


async def _sample(
    db_session: AsyncSession, name: str = "Setup Espagne (sample)"
) -> JourneyTemplate:
    s = JourneyTemplate(agency_id=None, is_sample=True, name=name)
    db_session.add(s)
    await db_session.commit()
    return s


async def test_library_lists_samples_agency_list_excludes_them(
    lib_client: AsyncClient,
    admin: Agent,
    db_session: AsyncSession,
    agent_headers: AuthHeaders,
) -> None:
    ah = agent_headers(admin)
    own = (await lib_client.post("/journeys", headers=ah, json={"name": "Mon parcours"})).json()
    sample = await _sample(db_session)

    # Agency list: the agency's own template, NEVER the sample.
    agency_ids = {t["id"] for t in (await lib_client.get("/journeys", headers=ah)).json()}
    assert own["id"] in agency_ids
    assert str(sample.id) not in agency_ids

    # Library list: the sample, NEVER the agency's own template.
    lib_ids = {t["id"] for t in (await lib_client.get("/journeys/library", headers=ah)).json()}
    assert str(sample.id) in lib_ids
    assert own["id"] not in lib_ids


async def test_library_route_is_not_captured_as_template_id(
    lib_client: AsyncClient, admin: Agent, db_session: AsyncSession, agent_headers: AuthHeaders
) -> None:
    # /journeys/library must resolve to the list, not GET /journeys/{id}.
    await _sample(db_session)
    resp = await lib_client.get("/journeys/library", headers=agent_headers(admin))
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_clone_source_resolver_accepts_sample_rejects_foreign(
    lib_client: AsyncClient,
    admin: Agent,
    db_session: AsyncSession,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
) -> None:
    mgr = JourneysManager(db_session)
    sample = await _sample(db_session)
    # A sample is a valid clone source for any agency.
    assert (await mgr.get_clone_source(admin, sample.id)).id == sample.id

    # Another agency's template is NOT a valid source → 404.
    other = await make_agent(role=system_roles["admin"])
    foreign = JourneyTemplate(agency_id=other.agency_id, is_sample=False, name="Foreign")
    db_session.add(foreign)
    await db_session.commit()
    with pytest.raises(NotFoundError):
        await mgr.get_clone_source(admin, foreign.id)
    # …but the agency's OWN template is.
    assert (await mgr.get_clone_source(other, foreign.id)).id == foreign.id
