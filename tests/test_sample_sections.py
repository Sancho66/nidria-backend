"""Samples × sections d'Informations (phase B).

Covers: coverage (every sample is mapped, every mapped key exists);
(a) a cloned sample carries the sections + fields AND the agency's
custom_field_definition rows are materialized (labels 6 languages);
(b) re-seed ×2: zero duplicate, labels refreshed in place; (c) an
agency's own template is never touched; (d) a step of the CLONE can
require a seeded catalogue field (membership + active definition);
(e) the library listing renders after seeding — and the clone's detail
resolves every catalogue field (nothing orphaned)."""

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.custom_field import CustomFieldDefinition
from shared.models.journey import JourneySection, JourneyTemplate, JourneyTemplateField
from shared.models.rbac import Role
from src.core.i18n import SUPPORTED_LANGUAGES
from src.journeys.field_catalog import FIELD_PRESETS, SECTION_TYPES, field_kind
from src.journeys.sample_seed import _SAMPLE_SECTIONS, _SAMPLES, PY1_NAME, seed_sample_journeys
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent

pytestmark = [pytest.mark.usefixtures("rbac_baseline"), pytest.mark.seed]


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


def test_mapping_covers_all_samples() -> None:
    names = {name for name, _c, _s in _SAMPLES}
    assert set(_SAMPLE_SECTIONS) == names  # every sample mapped, no stray key
    for name, keys in _SAMPLE_SECTIONS.items():
        assert 4 <= len(keys) <= 6, name
        for key in keys:
            assert key in SECTION_TYPES, (name, key)


async def _section_counts(db: AsyncSession) -> tuple[int, int]:
    sections = (
        await db.execute(
            select(func.count())
            .select_from(JourneySection)
            .join(JourneyTemplate, JourneyTemplate.id == JourneySection.template_id)
            .where(JourneyTemplate.is_sample.is_(True))
        )
    ).scalar_one()
    fields = (
        await db.execute(
            select(func.count())
            .select_from(JourneyTemplateField)
            .join(JourneyTemplate, JourneyTemplate.id == JourneyTemplateField.template_id)
            .where(JourneyTemplate.is_sample.is_(True))
        )
    ).scalar_one()
    return sections, fields


async def test_reseed_twice_no_duplicate_and_refresh_in_place(db_session: AsyncSession) -> None:
    await seed_sample_journeys(db_session)
    sections, fields = await _section_counts(db_session)
    assert sections > 300 and fields > 1500  # 77 samples × 4-6 sections × their fields

    # Spot-check PY1: 5 sections in mapping order, seed_key anchored, i18n 6 keys.
    template = (
        await db_session.execute(select(JourneyTemplate).where(JourneyTemplate.name == PY1_NAME))
    ).scalar_one()
    rows = list(
        (
            await db_session.execute(
                select(JourneySection)
                .where(JourneySection.template_id == template.id)
                .order_by(JourneySection.position)
            )
        ).scalars()
    )
    assert [s.seed_key for s in rows] == list(_SAMPLE_SECTIONS[PY1_NAME])
    assert rows[0].name == "Identité"
    assert set(rows[0].name_i18n) == set(SUPPORTED_LANGUAGES)

    # Drift + re-seed: refreshed in place, never duplicated.
    first_id = rows[0].id
    rows[0].name = "Old label"
    await db_session.commit()
    await seed_sample_journeys(db_session)
    db_session.expire_all()
    assert await _section_counts(db_session) == (sections, fields)
    refreshed = await db_session.get(JourneySection, first_id)
    assert refreshed is not None and refreshed.name == "Identité"

    await seed_sample_journeys(db_session)  # ×2
    assert await _section_counts(db_session) == (sections, fields)


async def test_agency_template_never_touched(
    db_session: AsyncSession, admin: Agent, client: AsyncClient, agent_headers: AuthHeaders
) -> None:
    created = await client.post(
        "/journeys", headers=agent_headers(admin), json={"name": "Parcours maison"}
    )
    template_id = created.json()["id"]
    await seed_sample_journeys(db_session)
    sections = (
        await db_session.execute(
            select(func.count())
            .select_from(JourneySection)
            .where(JourneySection.template_id == template_id)
        )
    ).scalar_one()
    fields = (
        await db_session.execute(
            select(func.count())
            .select_from(JourneyTemplateField)
            .where(JourneyTemplateField.template_id == template_id)
        )
    ).scalar_one()
    assert (sections, fields) == (0, 0)


async def test_clone_carries_sections_and_materializes_definitions(
    db_session: AsyncSession, admin: Agent, client: AsyncClient, agent_headers: AuthHeaders
) -> None:
    await seed_sample_journeys(db_session)
    headers = agent_headers(admin)
    sample = (
        await db_session.execute(select(JourneyTemplate).where(JourneyTemplate.name == PY1_NAME))
    ).scalar_one()

    cloned = await client.post(f"/journeys/{sample.id}/clone", headers=headers)
    assert cloned.status_code == 201, cloned.text
    clone_id = cloned.json()["id"]

    # (a) sections + fields cloned…
    detail = await client.get(f"/journeys/{clone_id}", headers=headers)
    assert detail.status_code == 200, detail.text
    body = detail.json()
    section_names = [s["name"] for s in body["sections"]]
    assert "Identité" in section_names and "Contact" in section_names
    all_fields = [f for s in body["sections"] for f in s["fields"]] + body["fields"]
    assert len(all_fields) >= 20

    # …and (e) every catalogue field resolves — materialized, never orphaned.
    for field in all_fields:
        if field["kind"] == "custom_field":
            assert field["is_archived"] is False, field["reference"]
            assert field["label"], field["reference"]

    # The agency's definitions were materialized with the 6-language blob.
    expected = {
        key
        for section_key in _SAMPLE_SECTIONS[PY1_NAME]
        for key in SECTION_TYPES[section_key].field_keys
        if field_kind(key) == "custom_field"
    }
    definitions = {
        d.key: d
        for d in (
            await db_session.execute(
                select(CustomFieldDefinition).where(
                    CustomFieldDefinition.agency_id == admin.agency_id
                )
            )
        ).scalars()
    }
    assert expected <= set(definitions)
    probe = definitions[next(iter(expected))]
    assert set(probe.label_i18n) == set(SUPPORTED_LANGUAGES)
    assert probe.archived_at is None
    select_probe = next(
        (d for d in definitions.values() if FIELD_PRESETS[d.key].options is not None), None
    )
    if select_probe is not None:
        assert select_probe.options  # options landed in the agency language

    # Re-cloning is definition-idempotent (no duplicate key crash).
    again = await client.post(f"/journeys/{sample.id}/clone", headers=headers)
    assert again.status_code == 201, again.text

    # (d) a step of the CLONE can require a seeded catalogue field.
    step = await client.post(
        f"/journeys/{clone_id}/steps", headers=headers, json={"name": "Vérif visa"}
    )
    assert step.status_code == 201, step.text
    requirement = await client.post(
        f"/journeys/{clone_id}/steps/{step.json()['id']}/requirements",
        headers=headers,
        json={"kind": "custom_field", "reference": "visa_number", "scope": "principal"},
    )
    assert requirement.status_code == 201, requirement.text


async def test_library_listing_renders_after_seeding(
    db_session: AsyncSession, admin: Agent, client: AsyncClient, agent_headers: AuthHeaders
) -> None:
    await seed_sample_journeys(db_session)
    listing = await client.get("/journeys/library", headers=agent_headers(admin))
    assert listing.status_code == 200
    assert len(listing.json()) == 77
