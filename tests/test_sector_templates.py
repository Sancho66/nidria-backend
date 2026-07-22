"""Multi-sector library (Lot 3): 7 GLOBAL sector templates + copy-at-creation.

Covers: (a) the seed shape — 7 global templates, agent/expat participants
ONLY (résolution A: no external on a global template), low-bound delays,
docs as requirements, provider NAMED in content_note; (b) copy-at-creation
clones the checked sectors into the agency (one/two journeys), demo case
rides the first; (c) the clone is INDEPENDENT of the template (edit isolation);
(d) the legacy expat example is no longer seeded; (e) a sector with no library
template → 0 journeys, no error; (f) THE invariant — creating an agency touches
NO pre-existing agency (bit-for-bit)."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.client_case import ClientCase
from shared.models.custom_field import CustomFieldDefinition
from shared.models.journey import (
    JourneySection,
    JourneyStepParticipant,
    JourneyTemplate,
    JourneyTemplateField,
    JourneyTemplateStep,
)
from shared.models.rbac import Role
from shared.models.step_requirement import StepRequirement
from src.agencies.demo_case_seed import DEMO_JOURNEY_NAME
from src.journeys.field_catalog import SECTION_TYPES
from src.journeys.sector_seed import SECTOR_SECTIONS
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.journey_plugin import MakeJourneyTemplate

pytestmark = pytest.mark.usefixtures("rbac_baseline", "sector_templates")


async def _create_agency(
    client: AsyncClient,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
    *,
    slug: str,
    sectors: list[str],
) -> Agency:
    superadmin = await make_agent(role=system_roles["superadmin"])
    created = await client.post(
        "/agencies",
        headers=agent_headers(superadmin),
        json={
            "name": slug.title(),
            "slug": slug,
            "admin_email": f"admin@{slug}.example.com",
            "admin_first_name": "Ana",
            "admin_last_name": "Boss",
            "sectors": sectors,
        },
    )
    assert created.status_code == 201, created.text
    return created.json()["agency"]


async def _agency_journeys(db: AsyncSession, agency_id: uuid.UUID) -> list[JourneyTemplate]:
    return list(
        (
            await db.execute(
                select(JourneyTemplate)
                .where(JourneyTemplate.agency_id == agency_id)
                .order_by(JourneyTemplate.created_at)
            )
        ).scalars()
    )


async def _global_template(db: AsyncSession, sector: str) -> JourneyTemplate:
    return (
        await db.execute(
            select(JourneyTemplate).where(
                JourneyTemplate.agency_id.is_(None),
                JourneyTemplate.is_sample.is_(True),
                JourneyTemplate.sector == sector,
            )
        )
    ).scalar_one()


# --- (a) the seed shape ---------------------------------------------------------------------


async def test_seed_creates_seven_global_sector_templates(db_session: AsyncSession) -> None:
    globals_ = list(
        (
            await db_session.execute(
                select(JourneyTemplate).where(
                    JourneyTemplate.agency_id.is_(None),
                    JourneyTemplate.is_sample.is_(True),
                    JourneyTemplate.sector.is_not(None),
                )
            )
        ).scalars()
    )
    assert {t.sector for t in globals_} == {
        "legal",
        "accounting",
        "real_estate",
        "wealth",
        "hr_mobility",
        "immigration",
        "consulting",
    }


async def test_seed_is_idempotent(db_session: AsyncSession) -> None:
    from src.journeys.sector_seed import seed_sector_templates

    before = (
        (
            await db_session.execute(
                select(JourneyTemplate).where(JourneyTemplate.sector.is_not(None))
            )
        )
        .scalars()
        .all()
    )
    await seed_sector_templates(db_session)  # a second boot
    after = (
        (
            await db_session.execute(
                select(JourneyTemplate).where(JourneyTemplate.sector.is_not(None))
            )
        )
        .scalars()
        .all()
    )
    assert {t.id for t in before} == {t.id for t in after}  # no duplicate, same rows


async def test_global_templates_carry_only_agent_and_expat_participants(
    db_session: AsyncSession,
) -> None:
    """Résolution A: a GLOBAL template NEVER carries an external participant
    (the polymorphic CHECK is untouched) — only agent / expat."""
    rows = list(
        (
            await db_session.execute(
                select(JourneyStepParticipant.type)
                .join(
                    JourneyTemplateStep,
                    JourneyTemplateStep.id == JourneyStepParticipant.step_id,
                )
                .join(JourneyTemplate, JourneyTemplate.id == JourneyTemplateStep.template_id)
                .where(JourneyTemplate.sector.is_not(None))
            )
        ).scalars()
    )
    assert rows  # there ARE participants
    assert set(rows) <= {"agent", "expat"}
    assert "external" not in rows


async def test_delays_are_low_bound_and_provider_is_named_in_content_note(
    db_session: AsyncSession,
) -> None:
    legal = await _global_template(db_session, "legal")
    steps = list(
        (
            await db_session.execute(
                select(JourneyTemplateStep)
                .where(JourneyTemplateStep.template_id == legal.id)
                .order_by(JourneyTemplateStep.position)
            )
        ).scalars()
    )
    # 7 steps; range 15-30 → 15, 120-300 → 120, "variable" → None.
    assert len(steps) == 7
    assert steps[2].estimated_days == 15  # "Rédaction et signification…" (15-30)
    assert steps[3].estimated_days == 120  # "Mise en état" (120-300)
    assert steps[4].estimated_days is None  # "Audience de plaidoirie" (variable)

    # Provider NAMED in content_note (sector-neutral wording) + NO participant.
    jugement = steps[5]  # "Jugement et notification" — provider-only step
    assert "autorité judiciaire compétente" in jugement.content_note
    doers = (
        (
            await db_session.execute(
                select(JourneyStepParticipant).where(JourneyStepParticipant.step_id == jugement.id)
            )
        )
        .scalars()
        .all()
    )
    assert doers == []  # provider-only ⇒ no participant, only the named note


async def test_docs_land_as_step_requirements(db_session: AsyncSession) -> None:
    legal = await _global_template(db_session, "legal")
    first_step = (
        await db_session.execute(
            select(JourneyTemplateStep)
            .where(JourneyTemplateStep.template_id == legal.id)
            .order_by(JourneyTemplateStep.position)
            .limit(1)
        )
    ).scalar_one()
    reqs = list(
        (
            await db_session.execute(
                select(StepRequirement).where(StepRequirement.step_id == first_step.id)
            )
        ).scalars()
    )
    assert {r.reference for r in reqs} == {
        "Pièces du litige",
        "Justificatif d'identité",
        "Convention d'honoraires",
    }
    assert all(r.kind == "document" and r.scope == "principal" for r in reqs)


# --- (b) copy-at-creation -------------------------------------------------------------------


async def test_one_sector_clones_one_journey(
    client: AsyncClient,
    db_session: AsyncSession,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    agency = await _create_agency(
        client, make_agent, system_roles, agent_headers, slug="realtor", sectors=["real_estate"]
    )
    agency_id = uuid.UUID(agency["id"])
    journeys = await _agency_journeys(db_session, agency_id)
    assert len(journeys) == 1
    clone = journeys[0]
    assert clone.name == "[Exemple] Vente d'un bien"
    assert clone.sector == "real_estate"  # provenance kept
    assert clone.is_sample is False
    # Independent copy: a different row from the global template.
    global_re = await _global_template(db_session, "real_estate")
    assert clone.id != global_re.id
    n_steps = (
        (
            await db_session.execute(
                select(JourneyTemplateStep).where(JourneyTemplateStep.template_id == clone.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(n_steps) == 7  # real_estate has 7 steps


async def test_two_sectors_clone_two_journeys_and_demo_rides_the_first(
    client: AsyncClient,
    db_session: AsyncSession,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    agency = await _create_agency(
        client,
        make_agent,
        system_roles,
        agent_headers,
        slug="multi",
        sectors=["legal", "accounting"],
    )
    agency_id = uuid.UUID(agency["id"])
    journeys = await _agency_journeys(db_session, agency_id)
    assert {j.name for j in journeys} == {
        "[Exemple] Contentieux civil",
        "[Exemple] Établissement des comptes annuels",
    }
    # The demo case rides the FIRST checked sector (legal → "Contentieux civil").
    demo = (
        await db_session.execute(
            select(ClientCase).where(
                ClientCase.agency_id == agency_id, ClientCase.is_demo.is_(True)
            )
        )
    ).scalar_one()
    demo_journey = await db_session.get(JourneyTemplate, demo.journey_template_id)
    assert demo_journey is not None and demo_journey.name == "[Exemple] Contentieux civil"


# --- (c) the clone is INDEPENDENT of the template -------------------------------------------


async def test_editing_the_clone_never_touches_the_global_template(
    client: AsyncClient,
    db_session: AsyncSession,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    agency = await _create_agency(
        client, make_agent, system_roles, agent_headers, slug="isolated", sectors=["real_estate"]
    )
    agency_id = uuid.UUID(agency["id"])
    clone = (await _agency_journeys(db_session, agency_id))[0]
    clone_step = (
        await db_session.execute(
            select(JourneyTemplateStep)
            .where(JourneyTemplateStep.template_id == clone.id)
            .order_by(JourneyTemplateStep.position)
            .limit(1)
        )
    ).scalar_one()

    clone_step.name = "ÉTAPE MODIFIÉE PAR L'AGENCE"
    await db_session.commit()

    global_re = await _global_template(db_session, "real_estate")
    global_first = (
        await db_session.execute(
            select(JourneyTemplateStep)
            .where(JourneyTemplateStep.template_id == global_re.id)
            .order_by(JourneyTemplateStep.position)
            .limit(1)
        )
    ).scalar_one()
    assert global_first.name == "Estimation et signature du mandat"  # untouched


# --- (d) the legacy expat example is gone ---------------------------------------------------


async def test_legacy_expat_example_is_no_longer_seeded(
    client: AsyncClient,
    db_session: AsyncSession,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    agency = await _create_agency(
        client, make_agent, system_roles, agent_headers, slug="no-legacy", sectors=["consulting"]
    )
    agency_id = uuid.UUID(agency["id"])
    names = {j.name for j in await _agency_journeys(db_session, agency_id)}
    assert DEMO_JOURNEY_NAME not in names
    assert names == {"[Exemple] Mission de conseil"}


# --- (e) a sector with no library template → 0 journeys, no error ---------------------------


async def test_sector_without_library_template_yields_zero_journeys_no_error(
    client: AsyncClient,
    db_session: AsyncSession,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    # Remove the consulting library template, then create a consulting agency.
    consulting = await _global_template(db_session, "consulting")
    await db_session.delete(consulting)
    await db_session.commit()

    agency = await _create_agency(
        client, make_agent, system_roles, agent_headers, slug="orphan", sectors=["consulting"]
    )
    agency_id = uuid.UUID(agency["id"])
    assert await _agency_journeys(db_session, agency_id) == []  # 0 journeys, no crash (201)
    demo = (
        await db_session.execute(
            select(ClientCase).where(
                ClientCase.agency_id == agency_id, ClientCase.is_demo.is_(True)
            )
        )
    ).scalar_one_or_none()
    assert demo is None  # no journey to ride ⇒ no demo case, but creation succeeded


# --- (f) THE invariant: creating an agency touches NO pre-existing agency -------------------


async def test_creating_an_agency_leaves_a_preexisting_agency_bit_for_bit(
    client: AsyncClient,
    db_session: AsyncSession,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    first = await _create_agency(
        client, make_agent, system_roles, agent_headers, slug="incumbent", sectors=["legal"]
    )
    first_id = uuid.UUID(first["id"])

    def _snapshot_key(t: JourneyTemplate) -> tuple:
        return (t.id, t.name, t.sector, t.updated_at)

    before_journeys = {_snapshot_key(t) for t in await _agency_journeys(db_session, first_id)}
    before_steps = {
        (s.id, s.name, s.updated_at)
        for s in (
            await db_session.execute(
                select(JourneyTemplateStep)
                .join(JourneyTemplate, JourneyTemplate.id == JourneyTemplateStep.template_id)
                .where(JourneyTemplate.agency_id == first_id)
            )
        ).scalars()
    }
    before_case = (
        await db_session.execute(
            select(ClientCase.id, ClientCase.updated_at).where(ClientCase.agency_id == first_id)
        )
    ).all()

    # A DIFFERENT agency is created afterwards.
    await _create_agency(
        client, make_agent, system_roles, agent_headers, slug="newcomer", sectors=["accounting"]
    )

    db_session.expire_all()
    after_journeys = {_snapshot_key(t) for t in await _agency_journeys(db_session, first_id)}
    after_steps = {
        (s.id, s.name, s.updated_at)
        for s in (
            await db_session.execute(
                select(JourneyTemplateStep)
                .join(JourneyTemplate, JourneyTemplate.id == JourneyTemplateStep.template_id)
                .where(JourneyTemplate.agency_id == first_id)
            )
        ).scalars()
    }
    after_case = (
        await db_session.execute(
            select(ClientCase.id, ClientCase.updated_at).where(ClientCase.agency_id == first_id)
        )
    ).all()

    assert after_journeys == before_journeys  # same ids, names, no updated_at bump
    assert after_steps == before_steps
    assert after_case == before_case


# --- (g) sections (the sector field pack) on the templates ---------------------------------


async def _sections_of(db: AsyncSession, template_id: uuid.UUID) -> list[JourneySection]:
    return list(
        (
            await db.execute(
                select(JourneySection)
                .where(JourneySection.template_id == template_id)
                .order_by(JourneySection.position)
            )
        ).scalars()
    )


async def test_global_sector_templates_carry_their_mapped_sections(
    db_session: AsyncSession,
) -> None:
    for sector, section_keys in SECTOR_SECTIONS.items():
        tpl = await _global_template(db_session, sector)
        sections = await _sections_of(db_session, tpl.id)
        assert [s.seed_key for s in sections] == list(section_keys), sector
        assert sections, f"{sector}: sections must not be empty"
        # every field_key of every section is materialized as a template field.
        fields = {
            f.reference
            for f in (
                await db_session.execute(
                    select(JourneyTemplateField).where(JourneyTemplateField.template_id == tpl.id)
                )
            ).scalars()
        }
        for key in section_keys:
            for field_key in SECTION_TYPES[key].field_keys:
                assert field_key in fields, f"{sector}/{key}/{field_key}"


async def test_real_estate_clone_has_populated_section_independent_of_template(
    client: AsyncClient,
    db_session: AsyncSession,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    agency = await _create_agency(
        client, make_agent, system_roles, agent_headers, slug="re-sections", sectors=["real_estate"]
    )
    agency_id = uuid.UUID(agency["id"])
    clone = (await _agency_journeys(db_session, agency_id))[0]
    assert clone.name == "[Exemple] Vente d'un bien"

    # The clone carries identity + real_estate_deal sections, populated.
    sections = await _sections_of(db_session, clone.id)
    assert [s.seed_key for s in sections] == ["identity", "real_estate_deal"]
    deal = next(s for s in sections if s.seed_key == "real_estate_deal")
    assert deal.name == "Transaction immobilière"

    clone_fields = {
        f.reference
        for f in (
            await db_session.execute(
                select(JourneyTemplateField).where(JourneyTemplateField.template_id == clone.id)
            )
        ).scalars()
    }
    # the sector-specific catalog fields are present (a step could require them).
    assert {"property_deal_type", "property_price", "transaction_stage"} <= clone_fields

    # Independent copy: the clone's sections are DIFFERENT rows from the template's.
    global_re = await _global_template(db_session, "real_estate")
    global_section_ids = {s.id for s in await _sections_of(db_session, global_re.id)}
    assert {s.id for s in sections}.isdisjoint(global_section_ids)

    # The agency's custom_field_definition rows were materialized (else the
    # Informations tab renders orphaned) — a sector-specific custom key exists.
    defs = {
        d.key
        for d in (
            await db_session.execute(
                select(CustomFieldDefinition).where(CustomFieldDefinition.agency_id == agency_id)
            )
        ).scalars()
    }
    assert "property_deal_type" in defs


# --- (h) the "[Exemple]" prefix on the global templates ------------------------------------


async def test_global_templates_are_prefixed_example(db_session: AsyncSession) -> None:
    for sector in SECTOR_SECTIONS:
        tpl = await _global_template(db_session, sector)
        assert tpl.name.startswith("[Exemple] "), sector
        assert tpl.name_i18n["fr"] == tpl.name  # blob stays in sync
    real_estate = await _global_template(db_session, "real_estate")
    assert real_estate.name == "[Exemple] Vente d'un bien"


async def test_reconcile_does_not_stack_the_prefix(db_session: AsyncSession) -> None:
    from src.journeys.sector_seed import seed_sector_templates

    await seed_sector_templates(db_session)  # 2nd boot
    await seed_sector_templates(db_session)  # 3rd boot
    tpl = await _global_template(db_session, "real_estate")
    assert tpl.name == "[Exemple] Vente d'un bien"  # single prefix, never stacked
    assert tpl.name.count("[Exemple]") == 1


async def test_reconcile_never_renames_an_agency_template(
    db_session: AsyncSession,
    make_journey_template: MakeJourneyTemplate,
) -> None:
    from src.journeys.sector_seed import seed_sector_templates

    # An agency's OWN journey named exactly like a sector base name (agency_id
    # set → out of the reconcile's reach, which filters agency_id IS NULL).
    owned = await make_journey_template(name="Vente d'un bien", sector="real_estate")
    await seed_sector_templates(db_session)  # reconcile the GLOBAL library
    await db_session.refresh(owned)
    assert owned.name == "Vente d'un bien"  # untouched, never prefixed
