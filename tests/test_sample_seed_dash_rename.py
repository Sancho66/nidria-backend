"""Dash purge × seed reconciliation (2026-07-05).

Covers: (b) re-seed ×2 after the rename → no duplicate, names updated in
place (including a prod-shaped row still carrying the legacy em-dash
name); (c) an agency's OWN template with an em-dash name is NEVER
touched (user data belongs to the agency)."""

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.journey import JourneyTemplate, JourneyTemplateStep
from src.journeys.sample_seed import PY1_NAME, seed_sample_journeys
from tests.plugins.agency_plugin import MakeAgency

pytestmark = pytest.mark.usefixtures("rbac_baseline")


async def _sample_count(db: AsyncSession) -> int:
    stmt = (
        select(func.count())
        .select_from(JourneyTemplate)
        .where(JourneyTemplate.agency_id.is_(None), JourneyTemplate.is_sample.is_(True))
    )
    return (await db.execute(stmt)).scalar_one()


async def test_reseed_twice_no_duplicate_and_legacy_renamed_in_place(
    db_session: AsyncSession,
) -> None:
    await seed_sample_journeys(db_session)
    count = await _sample_count(db_session)
    assert count > 0

    # No seeded name carries a dash anymore (scalar AND i18n variants).
    templates = list(
        (
            await db_session.execute(
                select(JourneyTemplate).where(JourneyTemplate.is_sample.is_(True))
            )
        ).scalars()
    )
    for template in templates:
        assert "—" not in template.name and "–" not in template.name, template.name
        for value in (template.name_i18n or {}).values():
            assert "—" not in value and "–" not in value, value
    step_texts = (
        await db_session.execute(
            select(JourneyTemplateStep.name, JourneyTemplateStep.content_note).where(
                JourneyTemplateStep.template_id.in_([t.id for t in templates])
            )
        )
    ).all()
    for name, note in step_texts:
        assert "—" not in name and "–" not in name
        assert note is None or ("—" not in note and "–" not in note)

    # Simulate the PROD state: a sample still carrying the legacy em-dash
    # name (seeded before the purge). The re-seed must RENAME it in place,
    # never duplicate it.
    sample = next(t for t in templates if t.name == PY1_NAME)
    legacy_name = PY1_NAME.replace(" : ", " — ", 1)
    assert legacy_name != PY1_NAME
    sample.name = legacy_name
    sample.name_i18n = {**sample.name_i18n, "fr": legacy_name}
    await db_session.commit()
    original_id = sample.id

    await seed_sample_journeys(db_session)
    db_session.expire_all()
    assert await _sample_count(db_session) == count  # zero duplicates
    renamed = await db_session.get(JourneyTemplate, original_id)
    assert renamed is not None
    assert renamed.name == PY1_NAME  # updated IN PLACE, same row
    assert renamed.name_i18n["fr"] == PY1_NAME

    # And a second full re-seed stays a strict no-op on counts.
    await seed_sample_journeys(db_session)
    assert await _sample_count(db_session) == count


async def test_agency_template_with_dash_is_never_touched(
    db_session: AsyncSession, make_agency: MakeAgency
) -> None:
    agency = await make_agency()
    custom = JourneyTemplate(agency_id=agency.id, name="Mon parcours — personnalisé")
    db_session.add(custom)
    await db_session.commit()
    custom_id = custom.id

    await seed_sample_journeys(db_session)
    db_session.expire_all()
    kept = await db_session.get(JourneyTemplate, custom_id)
    assert kept is not None
    assert kept.name == "Mon parcours — personnalisé"  # user data, untouched
