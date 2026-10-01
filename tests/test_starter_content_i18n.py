"""Starter content in the agency's language (01/10). The sector journeys every
new agency receives and their demo dossiers existed in French only, and the
library samples had no Hungarian. Covers: the translation tables are complete
and parallel to the FR seeds; an English and a Hungarian agency receive their
sector journey (name, steps, notes, requested documents) and demo dossier in
their language, with select values that ARE options of their fields; a French
agency is unchanged."""

import re
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.case_person import CasePerson
from shared.models.client_case import ClientCase
from shared.models.custom_field import CustomFieldDefinition
from shared.models.expat_user import ExpatUser
from shared.models.journey import JourneyTemplate, JourneyTemplateStep
from shared.models.rbac import Role
from shared.models.step_requirement import StepRequirement
from src.journeys.field_catalog import FIELD_PRESETS
from src.journeys.sample_seed import _SAMPLES
from src.journeys.sample_seed_hu import _SAMPLE_I18N_HU
from src.journeys.sector_seed import SECTOR_TEMPLATES
from src.journeys.sector_seed_i18n import (
    DEMO_I18N,
    EXAMPLE_PREFIX_I18N,
    SECTOR_I18N,
)
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent

LANGS = ["en", "es", "ru", "pt", "it", "hu"]
DASH = re.compile("[–—]")

# --- the tables are complete and parallel to the FR seeds ------------------------------


def test_every_library_sample_has_its_hungarian() -> None:
    for name, _country, steps in _SAMPLES:
        entry = _SAMPLE_I18N_HU.get(name)
        assert entry, name
        assert entry["name"]["hu"]  # type: ignore[index]
        tr_steps = entry["steps"]
        assert isinstance(tr_steps, list) and len(tr_steps) == len(steps), name
        for (step_name, _days, note, _role, _docs), (hu_name, hu_note) in zip(
            steps, tr_steps, strict=True
        ):
            assert hu_name.get("hu"), (name, step_name)
            if note:
                assert hu_note.get("hu"), (name, step_name)
            assert not DASH.search(hu_name.get("hu", "") + hu_note.get("hu", ""))


def test_every_sector_journey_speaks_the_six_other_languages() -> None:
    assert set(EXAMPLE_PREFIX_I18N) == set(LANGS)
    assert set(SECTOR_I18N) == set(SECTOR_TEMPLATES)
    for sector, (_name, steps) in SECTOR_TEMPLATES.items():
        tr = SECTOR_I18N[sector]
        assert all(tr["name"][lang] for lang in LANGS), sector
        assert len(tr["steps"]) == len(steps), sector
        for (fr_name, _days, note, _doers, docs), st in zip(steps, tr["steps"], strict=True):
            for lang in LANGS:
                assert st["name"][lang], (sector, fr_name, lang)
                if note:
                    assert st["note"][lang], (sector, fr_name, lang)
                labels = [d[lang] for d in st["docs"]]
                assert len(labels) == len(docs) and all(labels), (sector, fr_name, lang)
                assert len(set(labels)) == len(labels)  # unique per step (DB constraint)
                assert all(len(label) <= 100 for label in labels)
        for lang in LANGS:
            assert DEMO_I18N[sector]["last_name"][lang], (sector, lang)


# --- an agency receives its starter content in its language ----------------------------


async def _create(
    client: AsyncClient,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
    *,
    language: str,
    sector: str,
) -> uuid.UUID:
    superadmin = await make_agent(role=system_roles["superadmin"])
    slug = f"starter-{language}-{uuid.uuid4().hex[:6]}"
    created = await client.post(
        "/agencies",
        headers=agent_headers(superadmin),
        json={
            "name": f"Starter {language}",
            "slug": slug,
            "admin_email": f"admin@{slug}.example.com",
            "admin_first_name": "Ana",
            "admin_last_name": "Boss",
            "default_language": language,
            "sectors": [sector],
        },
    )
    assert created.status_code == 201, created.text
    return uuid.UUID(created.json()["agency"]["id"])


async def _clone(db: AsyncSession, agency_id: uuid.UUID) -> JourneyTemplate:
    return (
        await db.execute(select(JourneyTemplate).where(JourneyTemplate.agency_id == agency_id))
    ).scalar_one()


@pytest.mark.usefixtures("rbac_baseline", "sector_templates")
@pytest.mark.parametrize("language", ["en", "hu"])
async def test_new_agency_gets_its_sector_journey_in_its_language(
    client: AsyncClient,
    db_session: AsyncSession,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
    language: str,
) -> None:
    sector = "real_estate"
    agency_id = await _create(
        client, make_agent, system_roles, agent_headers, language=language, sector=sector
    )
    tr = SECTOR_I18N[sector]
    fr_name, fr_steps = SECTOR_TEMPLATES[sector]

    clone = await _clone(db_session, agency_id)
    assert clone.name == f"{EXAMPLE_PREFIX_I18N[language]} {tr['name'][language]}"
    steps = (
        (
            await db_session.execute(
                select(JourneyTemplateStep)
                .where(JourneyTemplateStep.template_id == clone.id)
                .order_by(JourneyTemplateStep.position)
            )
        )
        .scalars()
        .all()
    )
    assert [s.name for s in steps] == [st["name"][language] for st in tr["steps"]]
    for step, st, (_n, _d, note, _doers, _docs) in zip(steps, tr["steps"], fr_steps, strict=True):
        if note:
            assert step.content_note == st["note"][language]
        assert step.name_i18n["fr"]  # the blob is kept: every language still resolves
        refs = (
            (
                await db_session.execute(
                    select(StepRequirement.reference)
                    .where(StepRequirement.step_id == step.id, StepRequirement.kind == "document")
                    .order_by(StepRequirement.position)
                )
            )
            .scalars()
            .all()
        )
        assert list(refs) == [d[language] for d in st["docs"]]

    # The demo dossier: labels in the agency language, select values that ARE
    # options of the agency's own field definitions (materialized in its language).
    case = (
        await db_session.execute(
            select(ClientCase).where(ClientCase.agency_id == agency_id, ClientCase.is_demo)
        )
    ).scalar_one()
    expat = await db_session.get(ExpatUser, case.principal_expat_user_id)
    assert expat is not None
    assert expat.first_name == EXAMPLE_PREFIX_I18N[language]
    assert expat.last_name == DEMO_I18N[sector]["last_name"][language]
    person = (
        await db_session.execute(select(CasePerson).where(CasePerson.case_id == case.id))
    ).scalar_one()
    assert person.nationality == "PT"
    definitions = {
        d.key: d
        for d in (
            await db_session.execute(
                select(CustomFieldDefinition).where(CustomFieldDefinition.agency_id == agency_id)
            )
        ).scalars()
    }
    deal_type = person.custom_fields["property_deal_type"]
    assert deal_type in (definitions["property_deal_type"].options or [])
    assert (
        deal_type
        == FIELD_PRESETS["property_deal_type"].options[language][  # type: ignore[index]
            FIELD_PRESETS["property_deal_type"].options["fr"].index("Vente")  # type: ignore[index]
        ]
    )


@pytest.mark.usefixtures("rbac_baseline", "sector_templates")
async def test_french_agency_keeps_its_french_starter_content(
    client: AsyncClient,
    db_session: AsyncSession,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    agency_id = await _create(
        client, make_agent, system_roles, agent_headers, language="fr", sector="real_estate"
    )
    clone = await _clone(db_session, agency_id)
    fr_name, fr_steps = SECTOR_TEMPLATES["real_estate"]
    assert clone.name == f"[Exemple] {fr_name}"
    case = (
        await db_session.execute(
            select(ClientCase).where(ClientCase.agency_id == agency_id, ClientCase.is_demo)
        )
    ).scalar_one()
    person = (
        await db_session.execute(select(CasePerson).where(CasePerson.case_id == case.id))
    ).scalar_one()
    assert person.custom_fields["property_deal_type"] == "Vente"
    assert case.source == "Dossier d'exemple" and case.tags == ["exemple"]
