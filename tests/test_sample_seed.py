"""PY-1 library sample (BLOC 4) — seeded idempotently, appears in
GET /journeys/library, ABSENT from the agency's GET /journeys, and is
CLONABLE. The seed runs at boot in prod; here it is invoked directly (the
test client does not run the lifespan)."""

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.journey import JourneyStepParticipant, JourneyTemplate, JourneyTemplateStep
from shared.models.rbac import Role
from src.journeys.sample_seed import (
    _SAMPLES,
    PY1_COUNTRY,
    PY1_NAME,
    seed_sample_journeys,
)
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent

# An agency-doer step of PY-1 (role None in the spec → "the agency in general").
_PY1_AGENCY_STEP = "Dépôt du dossier à l'immigration (DNM)"


@pytest.fixture
def sd(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


async def test_py1_sample_seeded_library_only_and_clonable(
    sd: AsyncClient, admin: Agent, db_session: AsyncSession, agent_headers: AuthHeaders
) -> None:
    ah = agent_headers(admin)
    await seed_sample_journeys(db_session)
    # Idempotent: a second run creates no duplicate.
    await seed_sample_journeys(db_session)

    # In the LIBRARY, exactly once.
    library = (await sd.get("/journeys/library", headers=ah)).json()
    py1 = [t for t in library if t["name"] == PY1_NAME]
    assert len(py1) == 1
    py1_id = py1[0]["id"]

    # ABSENT from the agency's own list (agency_id NULL excluded).
    agency = (await sd.get("/journeys", headers=ah)).json()
    assert PY1_NAME not in {t["name"] for t in agency}

    # CLONABLE into the agency.
    clone = await sd.post(f"/journeys/{py1_id}/clone", headers=ah, json={})
    assert clone.status_code == 201, clone.text
    clone_id = clone.json()["id"]
    detail = (await sd.get(f"/journeys/{clone_id}", headers=ah)).json()
    steps = sorted(detail["steps"], key=lambda s: s["position"])
    assert len(steps) == 6
    # Validator = the agency on every step.
    assert all(s["default_validated_by_type"] == "agent" for s in steps)
    # AND chain: each step (after the first) requires the previous.
    assert steps[1]["prerequisite_step_ids"] == [steps[0]["id"]]

    # The clone is now an OWNED agency template (appears in the agency list).
    agency_after = {t["id"] for t in (await sd.get("/journeys", headers=ah)).json()}
    assert clone_id in agency_after


async def test_py1_step2_translator_is_client_participant_plus_note(
    sd: AsyncClient, admin: Agent, db_session: AsyncSession, agent_headers: AuthHeaders
) -> None:
    """Step 2 (sworn translation): on an agency-less sample only the CLIENT is
    a nameable participant; the translator (a provider) is a content_note "to
    assign on the dossier" — the second participant materializes on the clone
    when the agency names a provider."""
    ah = agent_headers(admin)
    await seed_sample_journeys(db_session)
    py1_id = next(
        t["id"]
        for t in (await sd.get("/journeys/library", headers=ah)).json()
        if t["name"] == PY1_NAME
    )
    clone_id = (await sd.post(f"/journeys/{py1_id}/clone", headers=ah, json={})).json()["id"]
    detail = (await sd.get(f"/journeys/{clone_id}", headers=ah)).json()
    step2 = next(s for s in detail["steps"] if s["name"].startswith("Traduction"))

    # ONE participant — the client — with a distinct role.
    assert [(p["type"], p["role"]) for p in step2["participants"]] == [
        ("expat", "provides_documents")
    ]
    # The translator is documented in the content_note (to assign on the dossier).
    assert step2["content_note"] is not None
    assert "assigner au dossier" in step2["content_note"]


async def test_agency_step_is_agency_in_general_participant_and_cloned(
    sd: AsyncClient, admin: Agent, db_session: AsyncSession, agent_headers: AuthHeaders
) -> None:
    """An "À réaliser par : Agence" step (role None in the spec) becomes a
    participant type=agent with agent_id NULL = "the agency in general". The
    clone preserves it (agent_id stays NULL on the owned template)."""
    ah = agent_headers(admin)
    await seed_sample_journeys(db_session)
    py1_id = next(
        t["id"]
        for t in (await sd.get("/journeys/library", headers=ah)).json()
        if t["name"] == PY1_NAME
    )
    clone_id = (await sd.post(f"/journeys/{py1_id}/clone", headers=ah, json={})).json()["id"]
    detail = (await sd.get(f"/journeys/{clone_id}", headers=ah)).json()
    agency_step = next(s for s in detail["steps"] if s["name"] == _PY1_AGENCY_STEP)

    # The agency is the doer — a type=agent participant with NO named member.
    assert [(p["type"], p["agent_id"], p["role"]) for p in agency_step["participants"]] == [
        ("agent", None, "executant")
    ]


async def test_reconcile_backfills_agency_participant_on_existing_sample(
    sd: AsyncClient, admin: Agent, db_session: AsyncSession, agent_headers: AuthHeaders
) -> None:
    """Prod-migration path: a sample seeded BEFORE the agency participant
    existed (agency steps participant-less). Re-running the seed BACKFILLS the
    "agency in general" participant on those steps, idempotently."""
    await seed_sample_journeys(db_session)
    tpl_id = (
        await db_session.execute(
            select(JourneyTemplate.id).where(
                JourneyTemplate.is_sample.is_(True), JourneyTemplate.name == PY1_NAME
            )
        )
    ).scalar_one()
    step_ids = (
        (
            await db_session.execute(
                select(JourneyTemplateStep.id).where(JourneyTemplateStep.template_id == tpl_id)
            )
        )
        .scalars()
        .all()
    )
    # Simulate the OLD shape: drop every agent (agency) participant.
    await db_session.execute(
        delete(JourneyStepParticipant).where(
            JourneyStepParticipant.step_id.in_(step_ids),
            JourneyStepParticipant.type == "agent",
        )
    )
    await db_session.commit()

    # Re-seed → reconcile backfills the missing agency doers. A further re-seed
    # adds no duplicate (idempotent). A sample is not reachable via GET
    # /journeys/{id} (agency-scoped), so assert directly on the DB.
    await seed_sample_journeys(db_session)
    await seed_sample_journeys(db_session)
    agency_step_id = (
        await db_session.execute(
            select(JourneyTemplateStep.id).where(
                JourneyTemplateStep.template_id == tpl_id,
                JourneyTemplateStep.name == _PY1_AGENCY_STEP,
            )
        )
    ).scalar_one()
    parts = (
        (
            await db_session.execute(
                select(JourneyStepParticipant).where(
                    JourneyStepParticipant.step_id == agency_step_id
                )
            )
        )
        .scalars()
        .all()
    )
    # Exactly ONE agency-in-general participant on the step — backfilled, not duplicated.
    assert [(p.type, p.agent_id) for p in parts] == [("agent", None)]


async def test_py1_country_in_library_and_copied_by_clone(
    sd: AsyncClient, admin: Agent, db_session: AsyncSession, agent_headers: AuthHeaders
) -> None:
    ah = agent_headers(admin)
    await seed_sample_journeys(db_session)

    # country="PY" surfaced in the library.
    py1 = next(
        t for t in (await sd.get("/journeys/library", headers=ah)).json() if t["name"] == PY1_NAME
    )
    assert py1["country"] == PY1_COUNTRY

    # The deep clone keeps the country of origin.
    clone = await sd.post(f"/journeys/{py1['id']}/clone", headers=ah, json={})
    assert clone.json()["country"] == PY1_COUNTRY


async def test_all_samples_seeded_groupable_by_country_and_clonable(
    sd: AsyncClient, admin: Agent, db_session: AsyncSession, agent_headers: AuthHeaders
) -> None:
    """Data-driven over the seed's own _SAMPLES spec → auto-covers every
    sample (present and future)."""
    ah = agent_headers(admin)
    await seed_sample_journeys(db_session)
    await seed_sample_journeys(db_session)  # idempotent: no duplicate

    library = (await sd.get("/journeys/library", headers=ah)).json()
    by_name = {t["name"]: t for t in library}
    expected = {name: (country, len(steps)) for name, country, steps in _SAMPLES}

    # Every spec'd sample is present, exactly once, with its country.
    for name, (country, _n) in expected.items():
        assert [t["name"] for t in library].count(name) == 1, name
        assert by_name[name]["country"] == country, name

    # Groupable by country: counts match the spec (e.g. PY ×3, CY ×5).
    for country in {c for c, _ in expected.values()}:
        live = sum(1 for t in library if t["country"] == country)
        spec = sum(1 for c, _ in expected.values() if c == country)
        assert live == spec, country

    # Each sample is clonable: 201, country copied, step count + AND chain kept.
    for name, (country, n_steps) in expected.items():
        clone = await sd.post(f"/journeys/{by_name[name]['id']}/clone", headers=ah, json={})
        assert clone.status_code == 201, clone.text
        assert clone.json()["country"] == country
        detail = (await sd.get(f"/journeys/{clone.json()['id']}", headers=ah)).json()
        ordered = sorted(detail["steps"], key=lambda s: s["position"])
        assert len(ordered) == n_steps, name
        assert ordered[1]["prerequisite_step_ids"] == [ordered[0]["id"]], name
