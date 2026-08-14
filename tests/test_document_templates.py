"""Bibliothèque de modèles de documents (méga-lot 29/07) — CRUD + gardes.

Le cycle sonde : POST (PDF chez nous + template provider sans champs,
external_id = notre UUID) → builder-token (JWT court signé avec la clé
API) → builder-sync (constat fields_configured/roles_count) → l'exigence
signable référence le modèle → suppression refusée tant qu'une définition
ou une ligne pendante le pointe (409 nommé, références listées)."""

import pytest
import pytest_asyncio
from httpx import AsyncClient
from jose import jwt
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.rbac import Role
from src.core.config import get_settings
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser
from tests.plugins.signature_plugin import SOURCE_PDF, FakeProvider
from tests.test_signatures import _document_template, _signable_case

pytestmark = pytest.mark.usefixtures("rbac_baseline", "signatures_enabled")


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


# --- CRUD -----------------------------------------------------------------------------


async def test_create_list_get_rename(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders, fake_provider: FakeProvider
) -> None:
    headers = agent_headers(admin)
    r = await client.post(
        "/document-templates",
        headers=headers,
        data={"name": "Mandat"},
        files={"file": ("mandat.pdf", SOURCE_PDF, "application/pdf")},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "Mandat"
    assert body["filename"] == "mandat.pdf"
    assert body["provider"] == "docuseal"
    # Naît SANS zones : le builder n'a pas encore parlé.
    assert body["fields_configured"] is False
    assert body["roles_count"] == 0
    # Le template provider est né du PDF, lié par external_id = notre UUID.
    call = fake_provider.create_template_calls[0]
    assert call["external_id"] == body["id"]
    assert fake_provider.templates[call["ref"]]["pdf"] == SOURCE_PDF

    # Né BROUILLON (lot 14/08) : la bibliothèque ne le montre pas encore.
    # `get` et les routes du builder, elles, le servent — sans quoi on ne
    # pourrait pas poser les zones du modèle qu'on vient de créer.
    assert body["state"] == "draft"
    assert (await client.get("/document-templates", headers=headers)).json() == []
    await client.post(f"/document-templates/{body['id']}/builder-sync", headers=headers)
    listed = (await client.get("/document-templates", headers=headers)).json()
    assert [t["id"] for t in listed] == [body["id"]]
    got = (await client.get(f"/document-templates/{body['id']}", headers=headers)).json()
    assert got["id"] == body["id"]
    renamed = await client.patch(
        f"/document-templates/{body['id']}", headers=headers, json={"name": "Mandat v2"}
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Mandat v2"


async def test_create_refuses_non_pdf(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders, fake_provider: FakeProvider
) -> None:
    r = await client.post(
        "/document-templates",
        headers=agent_headers(admin),
        data={"name": "Doc"},
        files={"file": ("doc.docx", b"not a pdf", "application/msword")},
    )
    assert r.status_code == 422
    assert r.json()["code"] == "document_template.not_pdf"
    assert fake_provider.create_template_calls == []


async def test_flag_off_conflicts_and_permission_enforced(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
    fake_provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = agent_headers(admin)
    # Sous-interrupteur agence OFF → 409 nommé (flag EFFECTIF).
    agency = await db_session.get(Agency, admin.agency_id)
    assert agency is not None
    agency.settings = {**(agency.settings or {}), "signatures_enabled": False}
    await db_session.commit()
    r = await client.get("/document-templates", headers=headers)
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "signatures.disabled"
    agency.settings = {**(agency.settings or {}), "signatures_enabled": True}
    await db_session.commit()
    # journey.configure absent (viewer) → 403 par la matrice.
    viewer = await make_agent(agency_id=admin.agency_id, role=system_roles["viewer"])
    r = await client.get("/document-templates", headers=agent_headers(viewer))
    assert r.status_code == 403, r.text


async def test_cross_agency_isolation(
    client: AsyncClient,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
    fake_provider: FakeProvider,
) -> None:
    template = await _document_template(client, agent_headers(admin))
    other_admin = await make_agent(role=system_roles["admin"])
    r = await client.get(
        f"/document-templates/{template['id']}", headers=agent_headers(other_admin)
    )
    assert r.status_code == 404
    assert (
        await client.get("/document-templates", headers=agent_headers(other_admin))
    ).json() == []


# --- builder-token + builder-sync -----------------------------------------------------


async def test_builder_token_short_lived_and_scoped(
    client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
    fake_provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Le jeton du builder : HS256 signé avec LA CLÉ API, user_email = le
    compte provider, external_id = l'UUID du modèle (la liaison constatée
    à la sonde — template_id est ignoré), expiration courte."""
    headers = agent_headers(admin)
    template = await _document_template(client, headers, synced=False)
    monkeypatch.setenv("DOCUSEAL_API_KEY", "sk-test-builder")
    monkeypatch.setenv("DOCUSEAL_ACCOUNT_EMAIL", "compte@agence.test")
    get_settings.cache_clear()
    r = await client.post(f"/document-templates/{template['id']}/builder-token", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provider"] == "docuseal"
    claims = jwt.decode(body["token"], "sk-test-builder", algorithms=["HS256"])
    assert claims["user_email"] == "compte@agence.test"
    assert claims["external_id"] == template["id"]
    import time

    assert claims["exp"] - time.time() <= 10 * 60 + 5


async def test_builder_token_requires_provider_config(
    client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
    fake_provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = agent_headers(admin)
    template = await _document_template(client, headers, synced=False)
    # setenv "" (pas delenv) : hermétique au .env local, que pydantic-settings
    # fusionne avec l'environnement — leçon du lot packs.
    monkeypatch.setenv("DOCUSEAL_ACCOUNT_EMAIL", "")
    get_settings.cache_clear()
    r = await client.post(f"/document-templates/{template['id']}/builder-token", headers=headers)
    assert r.status_code == 502, r.text
    assert r.json()["code"] == "signatures.provider_unconfigured"


async def test_builder_sync_materializes_the_provider_state(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders, fake_provider: FakeProvider
) -> None:
    headers = agent_headers(admin)
    template = await _document_template(client, headers, synced=False)
    assert template["fields_configured"] is False
    fake_provider.default_roles = ["Signataire 1", "Signataire 2", "Signataire 3"]
    r = await client.post(f"/document-templates/{template['id']}/builder-sync", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["fields_configured"] is True
    assert r.json()["roles_count"] == 3


async def test_builder_sync_locks_until_every_role_has_its_signature_zone(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders, fake_provider: FakeProvider
) -> None:
    """Verrou (mini-lot 30/07) : configuré = CHAQUE rôle porte sa zone
    signature. 2 rôles, une seule zone → PAS configuré (le 2e signataire
    n'aurait rien à signer) ; la 2e zone posée → configuré."""
    headers = agent_headers(admin)
    template = await _document_template(client, headers, synced=False)
    fake_provider.default_roles = ["Signataire 1", "Signataire 2"]
    fake_provider.signature_roles = ["Signataire 1"]  # builder sauvegardé incomplet
    r = await client.post(f"/document-templates/{template['id']}/builder-sync", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["fields_configured"] is False
    assert r.json()["roles_count"] == 2
    fake_provider.signature_roles = None  # chaque rôle couvert
    r = await client.post(f"/document-templates/{template['id']}/builder-sync", headers=headers)
    assert r.json()["fields_configured"] is True


# --- suppression : refusée tant que référencé -----------------------------------------


async def test_delete_unreferenced_archives_provider_side(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders, fake_provider: FakeProvider
) -> None:
    headers = agent_headers(admin)
    template = await _document_template(client, headers)
    r = await client.delete(f"/document-templates/{template['id']}", headers=headers)
    assert r.status_code == 204, r.text
    assert fake_provider.archive_calls == [fake_provider.create_template_calls[0]["ref"]]
    assert (await client.get("/document-templates", headers=headers)).json() == []


async def test_delete_refused_when_a_definition_references_it(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders, fake_provider: FakeProvider
) -> None:
    headers = agent_headers(admin)
    journey = (await client.post("/journeys", headers=headers, json={"name": "Visa"})).json()
    step = (
        await client.post(
            f"/journeys/{journey['id']}/steps", headers=headers, json={"name": "Contrat"}
        )
    ).json()
    template = await _document_template(client, headers, name="Statuts")
    r = await client.post(
        f"/journeys/{journey['id']}/steps/{step['id']}/requirements",
        headers=headers,
        json={
            "kind": "document",
            "reference": "Statuts",
            "scope": "principal",
            "signature_required": True,
            "document_template_id": template["id"],
        },
    )
    assert r.status_code == 201, r.text
    r = await client.delete(f"/document-templates/{template['id']}", headers=headers)
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["code"] == "document_template.in_use"
    assert body["params"]["references"] == [
        {"journey": "Visa", "step": "Contrat", "reference": "Statuts"}
    ]
    assert fake_provider.archive_calls == []


async def test_delete_refused_while_a_pending_row_references_it(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    fake_provider: FakeProvider,
    give_credits,
) -> None:
    """Même définition supprimée, un dossier EN VOL (lignes pendantes
    snapshotées) retient le modèle — 409 avec pending_rows."""
    await give_credits(admin.agency_id, 10)
    headers = agent_headers(admin)
    case, _pid = await _signable_case(
        client,
        db_session,
        admin,
        headers,
        make_client_case,
        make_expat_user,
        email_prefix="del",
    )
    case_id = case.id
    template_id = (await client.get("/document-templates", headers=headers)).json()[0]["id"]
    r = await client.delete(f"/document-templates/{template_id}", headers=headers)
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "document_template.in_use"
    assert r.json()["params"]["pending_rows"] == 2  # principal + membre
    assert str(case_id)  # le dossier vit toujours


# --- l'état de vie : brouillon invisible, promu à la première zone ---------------------


async def test_a_fresh_template_is_a_draft_the_agency_never_sees(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders, fake_provider: FakeProvider
) -> None:
    """LE TÉMOIN DU LOT (14/08).

    La modale « Nouveau modèle » créait le modèle avant que l'agence n'ait
    posé la moindre zone, et fermer le builder le laissait dans sa
    bibliothèque. On ne PEUT PAS ne rien créer : le builder embeddé du
    provider refuse de s'ouvrir sans un document déjà matérialisé chez lui
    (son jeton exige `template_id` ou `document_urls`) — le geste ne peut pas
    précéder ce qui le rend possible. On peut ne rien MONTRER.

    Les quatre sorties du builder (croix, Échap, clic hors modale, ONGLET
    FERMÉ) sont toutes le même cas vu d'ici : plus aucun appel n'arrive.
    C'est précisément pourquoi la garantie ne peut pas vivre dans le
    navigateur — une suppression au démontage manquerait l'onglet fermé.
    Elle vit ici : rien n'a été promu, donc rien ne se voit."""
    headers = agent_headers(admin)
    r = await client.post(
        "/document-templates",
        headers=headers,
        data={"name": "Mandat abandonné"},
        files={"file": ("mandat.pdf", SOURCE_PDF, "application/pdf")},
    )
    assert r.status_code == 201, r.text
    assert r.json()["state"] == "draft"
    # La bibliothèque est vide : l'agence a fermé, il ne s'est rien passé.
    assert (await client.get("/document-templates", headers=headers)).json() == []


async def test_a_builder_closed_without_a_single_zone_leaves_nothing_visible(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders, fake_provider: FakeProvider
) -> None:
    """Le builder autosauve : un sync peut arriver SANS qu'aucune zone n'ait
    été posée (l'agence a ouvert, regardé, refermé). Zéro zone constatée =
    toujours un brouillon, toujours invisible."""
    headers = agent_headers(admin)
    template = await _document_template(client, headers, synced=False)
    fake_provider.signature_roles = []  # builder ouvert, rien posé
    r = await client.post(f"/document-templates/{template['id']}/builder-sync", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "draft"
    assert r.json()["fields_configured"] is False
    assert (await client.get("/document-templates", headers=headers)).json() == []


async def test_the_first_zone_promotes_even_when_the_lock_is_not_satisfied(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders, fake_provider: FakeProvider
) -> None:
    """LE POINT QUI NE DOIT JAMAIS RÉGRESSER : la promotion se joue sur « au
    moins une zone », JAMAIS sur `fields_configured`.

    Deux signataires, une seule signature posée : le verrou d'envoi n'est pas
    satisfait (`fields_configured` faux, l'ambre « Zones à configurer » dans
    la liste) — et pourtant le modèle DOIT entrer dans la bibliothèque. C'est
    un modèle que l'agence a commencé ; l'y cacher entre deux séances de
    travail lui ferait croire qu'elle a tout perdu, et le balayage
    l'emporterait pour de bon au bout de 24 h."""
    headers = agent_headers(admin)
    template = await _document_template(client, headers, synced=False)
    fake_provider.default_roles = ["Signataire 1", "Signataire 2"]
    fake_provider.signature_roles = ["Signataire 1"]  # une seule zone posée
    r = await client.post(f"/document-templates/{template['id']}/builder-sync", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["fields_configured"] is False  # le verrou d'envoi tient
    assert body["state"] == "active"  # mais le modèle EXISTE pour l'agence
    listed = (await client.get("/document-templates", headers=headers)).json()
    assert [t["id"] for t in listed] == [template["id"]]


async def test_a_promoted_template_never_falls_back_to_draft(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders, fake_provider: FakeProvider
) -> None:
    """Sens unique. Une agence qui retire toutes ses zones a un modèle actif
    MAL CONFIGURÉ (l'ambre le dit, l'envoi le refuse) — pas un abandon. Le
    faire retomber en brouillon le rendrait invisible et le condamnerait au
    balayage : on effacerait son travail sur un geste d'édition."""
    headers = agent_headers(admin)
    template = await _document_template(client, headers, synced=True)
    assert template["state"] == "active"
    fake_provider.signature_roles = []  # toutes les zones retirées
    r = await client.post(f"/document-templates/{template['id']}/builder-sync", headers=headers)
    assert r.json()["fields_configured"] is False
    assert r.json()["state"] == "active"
    listed = (await client.get("/document-templates", headers=headers)).json()
    assert [t["id"] for t in listed] == [template["id"]]
