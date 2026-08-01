"""V2b/V2c (solde CRM) — fiches société : CRUD, dédup souple, rôles
personne↔société, plan de valeurs sur la taxonomie, lien dossier."""

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.rbac import Role
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser

pytestmark = pytest.mark.usefixtures("rbac_baseline")


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


async def test_company_crud_soft_dedup_and_sections(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    r = await client.post(
        "/company-profiles",
        headers=headers,
        json={"name": "Nidria Iberia SL", "custom_fields": {"legal_form": "SL"}},
    )
    assert r.status_code == 201, r.text
    company = r.json()
    company_id = company["id"]
    # Le plan de valeurs sur la taxonomie posée : les presets company
    # mappés (legal_form → identity), une clé libre → misc, 5 sections.
    by_key = {s["key"]: s["references"] for s in company["sections"]}
    assert "legal_form" in by_key["identity"]
    assert "company_registration_number" in by_key["id_documents"]
    r = await client.patch(
        f"/company-profiles/{company_id}",
        headers=headers,
        json={"custom_fields": {"cle_libre": "x"}, "tags": ["holding"]},
    )
    assert r.status_code == 200, r.text
    by_key = {s["key"]: s["references"] for s in r.json()["sections"]}
    assert "cle_libre" in by_key["misc"]

    # DÉDUP SOUPLE : homonyme → 409 avec référence ; allow_duplicate passe.
    r = await client.post("/company-profiles", headers=headers, json={"name": "nidria iberia sl"})
    assert r.status_code == 409
    assert r.json()["code"] == "company_profile.name_taken"
    assert r.json()["params"]["company_profile_id"] == company_id
    r = await client.post(
        "/company-profiles",
        headers=headers,
        json={"name": "nidria iberia sl", "allow_duplicate": True},
    )
    assert r.status_code == 201, r.text

    # Liste + compteurs ; cross-agence : rien.
    listing = (await client.get("/company-profiles?search=iberia", headers=headers)).json()
    assert listing["total"] == 2
    other = await make_agent(role=system_roles["admin"])
    assert (
        await client.get("/company-profiles?search=iberia", headers=agent_headers(other))
    ).json()["total"] == 0
    r = await client.get(f"/company-profiles/{company_id}", headers=agent_headers(other))
    assert r.status_code == 404


async def test_company_roles_and_case_link(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
) -> None:
    headers = agent_headers(admin)
    # Une personne (fiche) + une société.
    r = await client.post(
        "/client-profiles",
        headers=headers,
        json={"first_name": "Gérante", "last_name": "Sociale", "email": "gerante@example.com"},
    )
    assert r.status_code == 201, r.text
    person_id = r.json()["id"]
    r = await client.post("/company-profiles", headers=headers, json={"name": "HoldCo"})
    assert r.status_code == 201, r.text
    company_id = r.json()["id"]

    # RÔLES : ajout (vocabulaire canonique), doublon 409, identité servie.
    r = await client.post(
        f"/company-profiles/{company_id}/roles",
        headers=headers,
        json={"client_profile_id": person_id, "role": "manager"},
    )
    assert r.status_code == 201, r.text
    roles = r.json()["roles"]
    assert roles[0]["role"] == "manager"
    assert roles[0]["email"] == "gerante@example.com"  # identité coalescée
    r = await client.post(
        f"/company-profiles/{company_id}/roles",
        headers=headers,
        json={"client_profile_id": person_id, "role": "manager"},
    )
    assert r.status_code == 409
    assert r.json()["code"] == "company_profile.role_exists"
    r = await client.post(
        f"/company-profiles/{company_id}/roles",
        headers=headers,
        json={"client_profile_id": person_id, "role": "fondateur"},
    )
    assert r.status_code == 422  # hors vocabulaire canonique

    # LIEN DOSSIER↔SOCIÉTÉ (V2c) : PATCH du dossier, garde d'agence.
    r = await client.post(
        "/cases",
        headers=headers,
        json={"first_name": "Doss", "last_name": "Ier", "email": "dossier-soc@example.com"},
    )
    assert r.status_code == 201, r.text
    case_id = r.json()["id"]
    r = await client.patch(
        f"/cases/{case_id}", headers=headers, json={"company_profile_id": company_id}
    )
    assert r.status_code == 200, r.text
    assert r.json()["company_profile_id"] == company_id
    # La fiche société voit son dossier.
    detail = (await client.get(f"/company-profiles/{company_id}", headers=headers)).json()
    assert [c["id"] for c in detail["cases"]] == [case_id]
    # Société d'une AUTRE agence → 404 non-révélateur.
    other = await make_agent(role=system_roles["admin"])
    r = await client.post(
        "/company-profiles", headers=agent_headers(other), json={"name": "Etrangère"}
    )
    foreign_id = r.json()["id"]
    r = await client.patch(
        f"/cases/{case_id}", headers=headers, json={"company_profile_id": foreign_id}
    )
    assert r.status_code == 404
    # Délier : null explicite.
    r = await client.patch(f"/cases/{case_id}", headers=headers, json={"company_profile_id": None})
    assert r.status_code == 200, r.text
    assert r.json()["company_profile_id"] is None
    # Rôle : suppression.
    detail = (await client.get(f"/company-profiles/{company_id}", headers=headers)).json()
    role_id = detail["roles"][0]["id"]
    r = await client.delete(f"/company-profiles/{company_id}/roles/{role_id}", headers=headers)
    assert r.status_code == 204
