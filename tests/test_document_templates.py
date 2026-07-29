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
