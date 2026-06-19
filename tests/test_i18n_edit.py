"""BLOC 2bis — i18n EDIT surface: write {fr,en,es} blobs, read them RAW on the
edit detail, and read the RESOLVED value on display (?lang=). The two surfaces
are distinct: editor = raw blob; display = resolved. Plus agency.default_language
read/write. The scalar FR stays in sync with the blob (never desynchronized)."""

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.journey import JourneyTemplateStep
from shared.models.rbac import Role
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase


@pytest.fixture
def ic(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


async def test_write_i18n_blob_read_raw_and_resolved_are_coherent(
    ic: AsyncClient, admin: Agent, db_session: AsyncSession, agent_headers: AuthHeaders
) -> None:
    ah = agent_headers(admin)
    tid = (await ic.post("/journeys", headers=ah, json={"name": "Parcours"})).json()["id"]
    sid = (await ic.post(f"/journeys/{tid}/steps", headers=ah, json={"name": "Étape"})).json()["id"]

    # WRITE the 3-language blobs on the step (name + content_note).
    r = await ic.patch(
        f"/journeys/{tid}/steps/{sid}",
        headers=ah,
        json={
            "name_i18n": {"fr": "Dépôt du dossier", "en": "File submission", "es": "Presentación"},
            "content_note_i18n": {"fr": "Note FR", "en": "Note EN"},  # es absent → FR fallback
        },
    )
    assert r.status_code == 200, r.text

    # RAW read on the EDIT detail: the full blobs come back as-is.
    detail = (await ic.get(f"/journeys/{tid}", headers=ah)).json()
    step = next(s for s in detail["steps"] if s["id"] == sid)
    assert step["name_i18n"] == {
        "fr": "Dépôt du dossier",
        "en": "File submission",
        "es": "Presentación",
    }
    assert step["content_note_i18n"] == {"fr": "Note FR", "en": "Note EN"}
    # The resolved value (no ?lang) = the agency default (fr).
    assert step["name"] == "Dépôt du dossier"

    # The scalar FR column stays in sync with the blob (seed anchor / fallback).
    row = await db_session.get(JourneyTemplateStep, __import__("uuid").UUID(sid))
    assert row is not None and row.name == "Dépôt du dossier"

    # RESOLVED read on the EDIT detail with ?lang=en → English.
    detail_en = (await ic.get(f"/journeys/{tid}?lang=en", headers=ah)).json()
    step_en = next(s for s in detail_en["steps"] if s["id"] == sid)
    assert step_en["name"] == "File submission"
    # content_note es absent → ?lang=es falls back to FR.
    detail_es = (await ic.get(f"/journeys/{tid}?lang=es", headers=ah)).json()
    step_es = next(s for s in detail_es["steps"] if s["id"] == sid)
    assert step_es["content_note"] == "Note FR"


async def test_display_timeline_resolves_edited_blob(
    ic: AsyncClient,
    admin: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """The DISPLAY surface (case timeline) resolves the blob written via the
    editor — same source, coherent."""
    ah = agent_headers(admin)
    tid = (await ic.post("/journeys", headers=ah, json={"name": "P"})).json()["id"]
    sid = (await ic.post(f"/journeys/{tid}/steps", headers=ah, json={"name": "X"})).json()["id"]
    await ic.patch(
        f"/journeys/{tid}/steps/{sid}",
        headers=ah,
        json={"name_i18n": {"fr": "Étape FR", "en": "Step EN", "es": "Paso ES"}},
    )
    case = await make_client_case(agency_id=admin.agency_id)
    await ic.post(f"/cases/{case.id}/journey", headers=ah, json={"journey_template_id": tid})

    en = (await ic.get(f"/cases/{case.id}/steps?lang=en", headers=ah)).json()
    assert en[0]["name"] == "Step EN"
    es = (await ic.get(f"/cases/{case.id}/steps?lang=es", headers=ah)).json()
    assert es[0]["name"] == "Paso ES"


async def test_agency_default_language_read_write(
    ic: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    ah = agent_headers(admin)
    # Default is "fr".
    assert (await ic.get("/agencies/me", headers=ah)).json()["default_language"] == "fr"
    # Write "en".
    r = await ic.patch("/agencies/me", headers=ah, json={"default_language": "en"})
    assert r.status_code == 200, r.text
    assert r.json()["default_language"] == "en"
    # Invalid value rejected (422).
    bad = await ic.patch("/agencies/me", headers=ah, json={"default_language": "de"})
    assert bad.status_code == 422

    # With the agency default now "en": the scalar follows the default at write
    # time, and a requested language ABSENT from the blob falls back to the
    # agency default (en), not to fr.
    tid = (await ic.post("/journeys", headers=ah, json={"name": "P"})).json()["id"]
    sid = (await ic.post(f"/journeys/{tid}/steps", headers=ah, json={"name": "X"})).json()["id"]
    await ic.patch(
        f"/journeys/{tid}/steps/{sid}",
        headers=ah,
        json={"name_i18n": {"fr": "FR", "en": "EN"}},  # no es
    )
    # ?lang=es absent → falls back to the agency default (en), not fr.
    detail_es = (await ic.get(f"/journeys/{tid}?lang=es", headers=ah)).json()
    step_es = next(s for s in detail_es["steps"] if s["id"] == sid)
    assert step_es["name"] == "EN"
    # The scalar followed the agency default at write time.
    detail_fr = (await ic.get(f"/journeys/{tid}?lang=fr", headers=ah)).json()
    step_fr = next(s for s in detail_fr["steps"] if s["id"] == sid)
    assert step_fr["name"] == "FR" and step_fr["name_i18n"] == {"fr": "FR", "en": "EN"}
