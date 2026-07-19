"""Re-génération IA d'un parcours-type (demande Nicolas) : remplacement
TOTAL du contenu depuis un nouveau JSON, UNIQUEMENT si le template n'a
AUCUN dossier (actifs + archivés), garde en transaction ; les pertes
(pièces jointes, traductions) annoncées ; la course fermée."""

import io
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.ai_translation_job import AiTranslationSource
from shared.models.client_case import ClientCase
from shared.models.custom_field import CustomFieldDefinition
from shared.models.rbac import Role
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase

pytestmark = pytest.mark.usefixtures("rbac_baseline")


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


def _payload(name: str, step_ref: str, step_name: str, field_key: str | None = None) -> dict:
    step: dict = {"ref": step_ref, "nom": {"fr": step_name}}
    if field_key:
        step["informations_a_collecter"] = [
            {"cle": field_key, "type": "text", "requis": False, "libelle": {"fr": field_key}}
        ]
    return {
        "version": 1,
        "parcours": {"nom": {"fr": name}, "langue_par_defaut": "fr", "etapes": [step]},
    }


async def _import_new(client: AsyncClient, ah: dict, name: str = "Genese") -> str:
    r = await client.post(
        "/journeys/import", headers=ah, json=_payload(name, "e1", "Etape origine", "champ_a")
    )
    assert r.status_code == 200, r.text
    return r.json()["template_id"]


async def test_regenerate_replaces_content_same_template(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Remplacement complet : même template_id, nouveau nom, nouvelles
    étapes — l'ancienne a disparu."""
    ah = agent_headers(admin)
    tid = await _import_new(client, ah)
    r = await client.post(
        f"/journeys/{tid}/import", headers=ah, json=_payload("Genese v2", "e2", "Etape refaite")
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["template_id"] == tid  # l'identite survit
    detail = (await client.get(f"/journeys/{tid}", headers=ah)).json()
    assert detail["name"] == "Genese v2"
    step_names = [s["name"] for s in detail["steps"]]
    assert step_names == ["Etape refaite"]  # l'origine est partie


async def test_regenerate_409_when_instantiated_active_or_archived(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    ah = agent_headers(admin)
    tid = await _import_new(client, ah, "Occupe")
    case = await make_client_case(agency_id=admin.agency_id)
    await db_session.execute(
        update(ClientCase).where(ClientCase.id == case.id).values(journey_template_id=tid)
    )
    await db_session.commit()
    r = await client.post(
        f"/journeys/{tid}/import", headers=ah, json=_payload("Interdit", "x", "X")
    )
    assert r.status_code == 409
    assert r.json()["code"] == "journey.in_use"
    # dossier ARCHIVE (soft-delete) : bloque PAREIL — l'historique compte
    await db_session.execute(
        update(ClientCase).where(ClientCase.id == case.id).values(deleted_at=datetime.now(UTC))
    )
    await db_session.commit()
    r2 = await client.post(
        f"/journeys/{tid}/import", headers=ah, json=_payload("Interdit", "x", "X")
    )
    assert r2.status_code == 409
    assert r2.json()["code"] == "journey.in_use"


async def test_regenerate_reuses_custom_field_definitions(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """La définition custom existante (même clé, même type) est RÉUTILISÉE,
    jamais dupliquée-suffixée."""
    ah = agent_headers(admin)
    aid = admin.agency_id  # avant tout expire_all (piege greenlet connu)
    tid = await _import_new(client, ah, "Champs")  # cree champ_a
    def_id_before = (
        await db_session.execute(
            select(CustomFieldDefinition.id).where(
                CustomFieldDefinition.agency_id == aid,
                CustomFieldDefinition.key == "champ_a",
            )
        )
    ).scalar_one()
    r = await client.post(
        f"/journeys/{tid}/import",
        headers=ah,
        json=_payload("Champs v2", "e9", "Etape neuve", "champ_a"),
    )
    assert r.status_code == 200, r.text
    db_session.expire_all()
    ids_after = (
        (
            await db_session.execute(
                select(CustomFieldDefinition.id).where(
                    CustomFieldDefinition.agency_id == aid,
                    CustomFieldDefinition.key.like("champ_a%"),
                )
            )
        )
        .scalars()
        .all()
    )
    assert ids_after == [def_id_before]  # une seule, la meme


async def test_regenerate_warns_the_two_losses(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Le rapport (preview compris) annonce les N pièces jointes qui
    tomberont et la perte des traductions IA."""
    ah = agent_headers(admin)
    tid = await _import_new(client, ah, "Pertes")
    detail = (await client.get(f"/journeys/{tid}", headers=ah)).json()
    sid = detail["steps"][0]["id"]
    up = await client.post(
        f"/journeys/{tid}/steps/{sid}/attachments",
        headers=ah,
        files={"file": ("guide.pdf", io.BytesIO(b"%PDF-1.4 guide"), "application/pdf")},
    )
    assert up.status_code in (200, 201), up.text
    db_session.add(
        AiTranslationSource(
            agency_id=admin.agency_id,
            template_id=tid,
            lang="en",
            content_key="template.name",
            source_hash="a",
            output_hash="b",
        )
    )
    await db_session.commit()
    preview = await client.post(
        f"/journeys/{tid}/import?preview=true", headers=ah, json=_payload("Pertes v2", "n", "N")
    )
    assert preview.status_code == 200, preview.text
    codes = {w["code"]: w for w in preview.json()["warnings"]}
    assert codes["import_ai.replace_attachments_lost"]["valeur"] == "1"
    assert "import_ai.replace_translations_lost" in codes
    # et le commit purge les traces de traduction (content_keys morts)
    r = await client.post(
        f"/journeys/{tid}/import", headers=ah, json=_payload("Pertes v2", "n", "N")
    )
    assert r.status_code == 200
    left = (
        (
            await db_session.execute(
                select(AiTranslationSource).where(AiTranslationSource.template_id == tid)
            )
        )
        .scalars()
        .all()
    )
    assert left == []


async def test_regenerate_race_case_born_between_preview_and_commit(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """LA COURSE : le preview passe, un dossier naît, le commit -> 409,
    rien n'est écrit."""
    ah = agent_headers(admin)
    tid = await _import_new(client, ah, "Course")
    preview = await client.post(
        f"/journeys/{tid}/import?preview=true", headers=ah, json=_payload("Course v2", "c", "C")
    )
    assert preview.status_code == 200
    case = await make_client_case(agency_id=admin.agency_id)
    await db_session.execute(
        update(ClientCase).where(ClientCase.id == case.id).values(journey_template_id=tid)
    )
    await db_session.commit()
    commit = await client.post(
        f"/journeys/{tid}/import", headers=ah, json=_payload("Course v2", "c", "C")
    )
    assert commit.status_code == 409
    assert commit.json()["code"] == "journey.in_use"
    detail = (await client.get(f"/journeys/{tid}", headers=ah)).json()
    assert detail["name"] == "Course"  # rien n'a ete ecrit
