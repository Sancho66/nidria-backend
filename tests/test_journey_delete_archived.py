"""Journey-template deletion vs ARCHIVED (soft-deleted) cases.

Product rule: an ACTIVE case blocks deletion (409); an ARCHIVED case must
NOT — it is auto-detached (journey_template_id → NULL, its step instances of
THIS template purged) in the same transaction as the delete. Any unanticipated
FK violation degrades to a clean 409, never a 500. The detach is strictly
scoped to the target template (never another template / agency).

NB: the endpoint shares the test session; after its commit/rollback the
pre-created ORM objects are stale/expired, so assertions read mutated values
through fresh SCALAR selects keyed by ids captured up front (never by touching
the stale objects' attributes).
"""

import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.case_step_progress import CaseStepProgress
from shared.models.client_case import ClientCase
from shared.models.journey import JourneyTemplate
from shared.models.rbac import Role
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.journey_plugin import MakeJourneyTemplate, MakeTemplateStep


@pytest.fixture
def journeys_client(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def configurer(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    """case_manager: holds journey.configure without being admin."""
    return await make_agent(role=system_roles["case_manager"])


async def _add_progress(db: AsyncSession, case_id: uuid.UUID, step_id: uuid.UUID) -> None:
    db.add(CaseStepProgress(case_id=case_id, template_step_id=step_id))
    await db.commit()


async def _progress_count(db: AsyncSession, case_id: uuid.UUID) -> int:
    stmt = (
        select(func.count())
        .select_from(CaseStepProgress)
        .where(CaseStepProgress.case_id == case_id)
    )
    return (await db.execute(stmt)).scalar_one()


async def _template_exists(db: AsyncSession, template_id: uuid.UUID) -> bool:
    stmt = (
        select(func.count()).select_from(JourneyTemplate).where(JourneyTemplate.id == template_id)
    )
    return (await db.execute(stmt)).scalar_one() > 0


async def _case_link(db: AsyncSession, case_id: uuid.UUID) -> tuple[uuid.UUID | None, object]:
    """(journey_template_id, deleted_at) read fresh from the DB."""
    stmt = select(ClientCase.journey_template_id, ClientCase.deleted_at).where(
        ClientCase.id == case_id
    )
    return (await db.execute(stmt)).one()  # type: ignore[return-value]


# --- (a) no case at all → deleted -------------------------------------------------


async def test_delete_template_without_cases(
    journeys_client: AsyncClient,
    configurer: Agent,
    make_journey_template: MakeJourneyTemplate,
    agent_headers: AuthHeaders,
) -> None:
    template = await make_journey_template(agency_id=configurer.agency_id)
    template_id = template.id
    resp = await journeys_client.delete(
        f"/journeys/{template_id}", headers=agent_headers(configurer)
    )
    assert resp.status_code == 200
    assert (
        await journeys_client.get(f"/journeys/{template_id}", headers=agent_headers(configurer))
    ).status_code == 404


# --- (b) an ACTIVE case blocks → 409, not deleted --------------------------------


async def test_active_case_blocks_deletion(
    journeys_client: AsyncClient,
    configurer: Agent,
    make_journey_template: MakeJourneyTemplate,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    template = await make_journey_template(agency_id=configurer.agency_id)
    template_id = template.id
    await make_client_case(agency_id=configurer.agency_id, journey_template_id=template_id)
    resp = await journeys_client.delete(
        f"/journeys/{template_id}", headers=agent_headers(configurer)
    )
    assert resp.status_code == 409
    assert "active case(s)" in resp.json()["detail"]
    assert (
        await journeys_client.get(f"/journeys/{template_id}", headers=agent_headers(configurer))
    ).status_code == 200


# --- (c) an ARCHIVED case is detached, template deleted --------------------------


async def test_archived_case_is_detached_and_template_deleted(
    journeys_client: AsyncClient,
    configurer: Agent,
    make_journey_template: MakeJourneyTemplate,
    make_template_step: MakeTemplateStep,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    db_session: AsyncSession,
) -> None:
    template = await make_journey_template(agency_id=configurer.agency_id)
    template_id = template.id
    step = await make_template_step(template=template)
    archived = await make_client_case(
        agency_id=configurer.agency_id,
        journey_template_id=template_id,
        deleted_at=datetime.now(UTC),
    )
    archived_id = archived.id
    await _add_progress(db_session, archived_id, step.id)
    assert await _progress_count(db_session, archived_id) == 1

    resp = await journeys_client.delete(
        f"/journeys/{template_id}", headers=agent_headers(configurer)
    )
    assert resp.status_code == 200

    assert not await _template_exists(db_session, template_id)  # template gone
    journey_template_id, deleted_at = await _case_link(db_session, archived_id)
    assert journey_template_id is None  # detached
    assert deleted_at is not None  # still archived (case survives)
    assert await _progress_count(db_session, archived_id) == 0  # instances purged


# --- (d) orphan case_step_progress (the ex-500 path) → clean 409 -----------------


async def test_orphan_progress_yields_clean_409_not_500(
    journeys_client: AsyncClient,
    configurer: Agent,
    make_journey_template: MakeJourneyTemplate,
    make_template_step: MakeTemplateStep,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    db_session: AsyncSession,
) -> None:
    template = await make_journey_template(agency_id=configurer.agency_id)
    template_id = template.id
    step = await make_template_step(template=template)
    # A case NOT linked to this template (journey_template_id stays NULL) but
    # carrying a step instance of it — the detach (archived-linked only) won't
    # touch it, so the delete hits the case_step_progress RESTRICT at commit.
    orphan = await make_client_case(agency_id=configurer.agency_id)
    orphan_id = orphan.id
    assert orphan.journey_template_id is None
    await _add_progress(db_session, orphan_id, step.id)

    resp = await journeys_client.delete(
        f"/journeys/{template_id}", headers=agent_headers(configurer)
    )
    assert resp.status_code == 409  # clean conflict, NOT a 500
    assert "still in use" in resp.json()["detail"]
    # Rollback kept everything: template + the instance still present.
    assert await _template_exists(db_session, template_id)
    assert await _progress_count(db_session, orphan_id) == 1


# --- (e) the detach never touches another template / agency ----------------------


async def test_detach_is_scoped_to_target_template_and_agency(
    journeys_client: AsyncClient,
    configurer: Agent,
    make_journey_template: MakeJourneyTemplate,
    make_template_step: MakeTemplateStep,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    db_session: AsyncSession,
) -> None:
    # Target template (configurer's agency) + an archived case on it.
    target = await make_journey_template(agency_id=configurer.agency_id)
    target_id = target.id
    target_step = await make_template_step(template=target)
    target_case = await make_client_case(
        agency_id=configurer.agency_id,
        journey_template_id=target_id,
        deleted_at=datetime.now(UTC),
    )
    await _add_progress(db_session, target_case.id, target_step.id)

    # A SEPARATE template in ANOTHER agency, with its own archived case +
    # instance — must be left fully intact.
    other = await make_journey_template()  # auto-creates a different agency
    other_id = other.id
    other_step = await make_template_step(template=other)
    other_case = await make_client_case(
        agency_id=other.agency_id,
        journey_template_id=other_id,
        deleted_at=datetime.now(UTC),
    )
    other_case_id = other_case.id
    await _add_progress(db_session, other_case_id, other_step.id)

    resp = await journeys_client.delete(f"/journeys/{target_id}", headers=agent_headers(configurer))
    assert resp.status_code == 200

    # The other agency's data is untouched.
    assert await _template_exists(db_session, other_id)
    other_link, _ = await _case_link(db_session, other_case_id)
    assert other_link == other_id  # still linked
    assert await _progress_count(db_session, other_case_id) == 1  # instance intact
