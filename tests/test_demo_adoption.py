"""Production adoption counts are scoped, real, paginated and read-only."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.rbac import Role
from src.admin.demo_adoption_manager import DemoAdoptionManager
from src.admin.demo_adoption_schema import DemoAdoptionQuery
from src.core.enums import Audience
from src.core.security import create_access_token
from src.demo_sandbox.measurement import demo_attribution
from tests.plugins.agency_plugin import MakeAgency
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeCasePerson, MakeClientCase
from tests.plugins.client_profile_plugin import MakeClientProfile
from tests.plugins.journey_plugin import MakeJourneyTemplate


@pytest.mark.asyncio
async def test_real_adoption_excludes_seeds_and_respects_global_filters(
    db_session: AsyncSession,
    make_agency: MakeAgency,
    make_journey_template: MakeJourneyTemplate,
    make_client_case: MakeClientCase,
    make_client_profile: MakeClientProfile,
    make_case_person: MakeCasePerson,
) -> None:
    now = datetime.now(UTC)
    reference = demo_attribution("a" * 32)
    active = await make_agency(
        name="Fictional active",
        utm_source="demo",
        utm_campaign="QA",
        utm_content=reference,
        created_at=now - timedelta(days=2),
    )
    empty = await make_agency(name="Fictional empty", utm_source="demo", utm_campaign="QA")
    await make_agency(name="Internal excluded", utm_source="demo", is_internal=True)
    await make_agency(name="Other source excluded", utm_source="landing", utm_content=reference)
    await make_journey_template(agency_id=active.id, origin="user")
    await make_journey_template(agency_id=active.id, origin="user", sector="formation")
    await make_journey_template(agency_id=empty.id, origin="seed")
    await make_journey_template(agency_id=empty.id, origin="user", is_sample=True)
    await make_client_case(agency_id=active.id)
    await make_client_case(agency_id=empty.id, deleted_at=now)
    sample = await make_client_case(agency_id=empty.id, is_demo=True)
    await make_client_profile(agency_id=empty.id, expat_user_id=sample.principal_expat_user_id)
    profile = await make_client_profile(agency_id=empty.id)
    await make_case_person(case=sample, client_profile_id=profile.id)
    await make_client_profile(agency_id=active.id)
    await db_session.execute(text("SET TRANSACTION READ ONLY"))
    manager = DemoAdoptionManager(db_session)
    result = await manager.read(DemoAdoptionQuery(page_size=1, sort="journeys"))
    assert result.total == 2
    assert result.totals.model_dump() == {
        "accounts": 2,
        "with_journeys": 1,
        "with_clients": 1,
        "with_cases": 1,
    }
    row = result.items[0]
    assert row.agency_id == active.id
    assert (row.journeys, row.clients, row.cases) == (2, 1, 1)
    assert row.has_demo_reference and row.last_creation_at is not None
    second = await manager.read(DemoAdoptionQuery(page_size=1, page=2, sort="journeys"))
    assert second.items[0].agency_id == empty.id
    assert (second.items[0].journeys, second.items[0].clients, second.items[0].cases) == (0, 0, 0)
    assert not second.items[0].has_demo_reference
    for kind in ("journeys", "clients", "cases"):
        filtered = await manager.read(DemoAdoptionQuery(activity=kind))  # type: ignore[arg-type]
        assert filtered.total == 1 and filtered.items[0].agency_id == active.id
    assert (await manager.read(DemoAdoptionQuery(activity="none"))).items[0].agency_id == empty.id
    assert (await manager.read(DemoAdoptionQuery(demo="a" * 32))).total == 1
    assert (await manager.read(DemoAdoptionQuery(demo="b" * 32))).total == 0
    assert (await manager.read(DemoAdoptionQuery(batch="other"))).total == 0
    assert (await manager.read(DemoAdoptionQuery(search="%"))).total == 0
    counts = await manager.repo.for_references([reference])
    assert counts == {reference: {"accounts": 1, "journeys": 2, "clients": 1, "cases": 1}}
    await db_session.rollback()


@pytest.mark.asyncio
@pytest.mark.usefixtures("rbac_baseline")
async def test_adoption_is_superadmin_only_and_strict(
    client: AsyncClient,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    root = await make_agent(role=system_roles["superadmin"])
    admin = await make_agent(role=system_roles["admin"])
    path = "/admin/demos/adoption"
    assert (await client.get(path)).status_code == 401
    assert (await client.get(path, headers=agent_headers(admin))).status_code == 403
    assert (
        await client.get(
            path,
            headers={
                "Authorization": "Bearer " + create_access_token(str(uuid.uuid4()), Audience.EXPAT)
            },
        )
    ).status_code == 401
    result = await client.get(
        path + "?page=1&page_size=1&activity=cases", headers=agent_headers(root)
    )
    assert result.status_code == 200 and result.json()["total"] == 0
    for query in ("page=0", "page_size=5000", "activity=bad", "sort=bad", "demo=bad", "extra=1"):
        assert (
            await client.get(path + "?" + query, headers=agent_headers(root))
        ).status_code == 422
