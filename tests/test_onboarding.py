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


async def test_fresh_agency_has_four_unchecked_steps(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    body = await _state(client, agent_headers(admin))
    assert body["dismissed"] is False
    steps = _by_key(body)
    # review_client_terms added by the lot 13/08 (conditions au nom de l'agence).
    assert set(steps) == {
        "create_journey",
        "open_case",
        "view_as_client",
        "review_client_terms",
    }
    assert all(not s["done"] and s["done_at"] is None for s in steps.values())


# --- (b) each real gesture checks its key ---------------------------------------------


async def test_real_gestures_check_their_keys(
    client: AsyncClient, db_session: AsyncSession, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)

    created = await client.post("/journeys", headers=headers, json={"name": "Mon parcours"})
    assert created.status_code == 201
    tid = created.json()["id"]
    steps = _by_key(await _state(client, headers))
    assert steps["create_journey"]["done"] is True
    assert steps["create_journey"]["done_at"] is not None
    assert steps["open_case"]["done"] is False

    made = await client.post(
        "/cases",
        headers=headers,
        json={
            "first_name": "Jean",
            "last_name": "Client",
            "email": "jean@example.com",
            "journey_template_id": tid,
        },
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


@pytest.mark.usefixtures("sector_templates")
async def test_demo_checks_nothing_by_creation_but_consultation_checks_open_case(
    client: AsyncClient, db_session: AsyncSession, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    agency = await db_session.get(Agency, admin.agency_id)
    assert agency is not None
    # The demo case now rides a cloned SECTOR journey — the agency needs a
    # sector for the gift to exist (fixtures create bare agencies).
    agency.sectors = ["immigration"]
    await db_session.commit()
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


# --- (e) note Eric 26/07 : le seed ne coche jamais, meme via le backfill de boot ------


@pytest.mark.usefixtures("sector_templates")
async def test_boot_backfill_never_stamps_the_milestone_from_the_seeded_gift(
    client: AsyncClient, db_session: AsyncSession, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """LA fuite constatee : backfill_usage_milestones tourne A CHAQUE BOOT et
    stampait premier_parcours_cree depuis min(created_at) de TOUS les
    templates — clones sectoriels offerts inclus. Une agence neuve seedee se
    reveillait l'etape 1 cochee. Le filtre origin='user' la ferme."""
    from shared.models.usage import AgencyUsageMilestone
    from src.usage.usage_backfill import backfill_usage_milestones

    headers = agent_headers(admin)
    agency = await db_session.get(Agency, admin.agency_id)
    assert agency is not None
    agency.sectors = ["immigration"]
    await db_session.commit()
    assert await seed_demo_case(db_session, agency, admin) is not None

    # Le boot d'apres : le backfill tourne — et ne stampe RIEN pour cette
    # agence (ses seuls parcours sont d'origine seed).
    await backfill_usage_milestones(db_session)
    milestone = (
        await db_session.execute(
            select(AgencyUsageMilestone).where(
                AgencyUsageMilestone.agency_id == agency.id,
                AgencyUsageMilestone.key == "premier_parcours_cree",
            )
        )
    ).scalar_one_or_none()
    assert milestone is None
    steps = _by_key(await _state(client, headers))
    assert steps["create_journey"]["done"] is False


@pytest.mark.usefixtures("sector_templates")
async def test_library_clone_checks_create_journey(
    client: AsyncClient, db_session: AsyncSession, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """Un import VOLONTAIRE depuis la bibliotheque est une action reelle :
    le clone nait origin='user' et coche l'etape — seul le seed automatique
    est exclu."""
    from shared.models.journey import JourneyTemplate

    headers = agent_headers(admin)
    sample = (
        (
            await db_session.execute(
                select(JourneyTemplate).where(
                    JourneyTemplate.agency_id.is_(None), JourneyTemplate.is_sample.is_(True)
                )
            )
        )
        .scalars()
        .first()
    )
    assert sample is not None, "the harness seeds the library at boot"
    assert sample.origin == "seed"

    r = await client.post(f"/journeys/{sample.id}/clone", headers=headers, json={})
    assert r.status_code == 201, r.text
    clone = await db_session.get(JourneyTemplate, uuid.UUID(r.json()["id"]))
    assert clone is not None and clone.origin == "user"

    steps = _by_key(await _state(client, headers))
    assert steps["create_journey"]["done"] is True


async def test_existing_agency_handmade_history_still_checks(
    client: AsyncClient, db_session: AsyncSession, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """Temoin retro : une agence d'AVANT le milestone (aucun usage_event)
    avec un parcours fait main (origin='user', le defaut) — le backfill de
    boot stampe, l'etape est cochee. L'histoire reelle ne se decoche pas."""
    from shared.models.journey import JourneyTemplate
    from src.usage.usage_backfill import backfill_usage_milestones

    headers = agent_headers(admin)
    db_session.add(JourneyTemplate(agency_id=admin.agency_id, name="Parcours historique"))
    await db_session.commit()

    await backfill_usage_milestones(db_session)
    steps = _by_key(await _state(client, headers))
    assert steps["create_journey"]["done"] is True
