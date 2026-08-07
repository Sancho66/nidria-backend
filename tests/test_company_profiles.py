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
    # L'invariant d'exhaustivité re-vérifié : QUATRE sections société
    # (id_documents a fusionné dans l'identité légale).
    assert [x["key"] for x in company["sections"]] == ["identity", "contact", "situation", "misc"]
    assert "legal_form" in by_key["identity"]
    assert "company_registration_number" in by_key["identity"]  # 4 sections
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


async def test_delete_company_rules(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
) -> None:
    """Suppression société — des rôles OK (ils se dissolvent), un dossier
    lié → 409 avec le compte."""
    headers = agent_headers(admin)
    r = await client.post("/company-profiles", headers=headers, json={"name": "DeletableCo"})
    company_id = r.json()["id"]
    r = await client.post(
        "/client-profiles",
        headers=headers,
        json={"first_name": "Rôle", "last_name": "Dissous", "email": "role-dissous@example.com"},
    )
    person_id = r.json()["id"]
    r = await client.post(
        f"/company-profiles/{company_id}/roles",
        headers=headers,
        json={"client_profile_id": person_id, "role": "manager"},
    )
    assert r.status_code == 201, r.text
    r = await client.delete(f"/company-profiles/{company_id}", headers=headers)
    assert r.status_code == 204  # les rôles n'ont pas protégé
    # Un dossier lié protège.
    r = await client.post("/company-profiles", headers=headers, json={"name": "ProtectedCo"})
    protected_id = r.json()["id"]
    r = await client.post(
        "/cases",
        headers=headers,
        json={"first_name": "Doss", "last_name": "Lié", "email": "doss-lie@example.com"},
    )
    case_id = r.json()["id"]
    r = await client.patch(
        f"/cases/{case_id}", headers=headers, json={"company_profile_id": protected_id}
    )
    assert r.status_code == 200, r.text
    r = await client.delete(f"/company-profiles/{protected_id}", headers=headers)
    assert r.status_code == 409
    assert r.json()["code"] == "company_profile.has_cases"
    assert r.json()["params"]["cases_count"] == 1


async def test_company_directory_filters_sorts_and_cost(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
) -> None:
    """Annuaire Sociétés au niveau Personnes — filtres (tags,
    has_active_case, has_people), tris SQL (name/created/last_activity),
    last_activity_at servi, l'entité vues « companies », et le coût
    CONSTANT au témoin (le pattern des personnes)."""
    from sqlalchemy import event

    from src.company_profiles.company_profiles_manager import CompanyProfilesManager

    headers = agent_headers(admin)
    # Alpha : taguée, avec personne + dossier vivant. Beta : nue.
    r = await client.post(
        "/company-profiles", headers=headers, json={"name": "Alpha SL", "tags": ["vip"]}
    )
    alpha_id = r.json()["id"]
    r = await client.post("/company-profiles", headers=headers, json={"name": "Beta GmbH"})
    assert r.status_code == 201, r.text
    r = await client.post(
        "/client-profiles",
        headers=headers,
        json={"first_name": "Rôle", "last_name": "Alpha", "email": "role-alpha@example.com"},
    )
    r = await client.post(
        f"/company-profiles/{alpha_id}/roles",
        headers=headers,
        json={"client_profile_id": r.json()["id"], "role": "manager"},
    )
    assert r.status_code == 201, r.text
    r = await client.post(
        "/cases",
        headers=headers,
        json={"first_name": "Doss", "last_name": "Alpha", "email": "doss-alpha@example.com"},
    )
    case_id = r.json()["id"]
    r = await client.patch(
        f"/cases/{case_id}", headers=headers, json={"company_profile_id": alpha_id}
    )
    assert r.status_code == 200, r.text

    # FILTRES.
    for query, expected in (
        ("tags=vip", ["Alpha SL"]),
        ("has_active_case=true", ["Alpha SL"]),
        ("has_active_case=false", ["Beta GmbH"]),
        ("has_people=true", ["Alpha SL"]),
        ("has_people=false", ["Beta GmbH"]),
    ):
        listing = (await client.get(f"/company-profiles?{query}", headers=headers)).json()
        assert [i["name"] for i in listing["items"]] == expected, query
    # TRIS + le champ d'activité servi.
    listing = (
        await client.get("/company-profiles?sort_by=last_activity&sort_order=desc", headers=headers)
    ).json()
    assert [i["name"] for i in listing["items"]] == ["Alpha SL", "Beta GmbH"]
    assert listing["items"][0]["last_activity_at"] is not None
    listing = (
        await client.get("/company-profiles?sort_by=created_at&sort_order=desc", headers=headers)
    ).json()
    assert [i["name"] for i in listing["items"]] == ["Beta GmbH", "Alpha SL"]

    # L'ENTITÉ VUES « companies ».
    r = await client.post(
        "/views", headers=headers, json={"name": "Mes sociétés", "entity": "companies"}
    )
    assert r.status_code == 201, r.text
    assert r.json()["entity"] == "companies"

    # TÉMOIN : coût constant, filtres + tri activité compris.
    engine = db_session.get_bind()
    counter = {"n": 0}

    def _count(*_args: object, **_kwargs: object) -> None:
        counter["n"] += 1

    event.listen(engine, "before_cursor_execute", _count)
    try:
        await CompanyProfilesManager(db_session).list_companies(
            admin,
            search=None,
            tags=["vip"],
            has_active_case=True,
            sort_by="last_activity",
            sort_order="desc",
            page=1,
            page_size=20,
        )
    finally:
        event.remove(engine, "before_cursor_execute", _count)
    # 5 = liste + count + compteurs rôles + compteurs dossiers + activité.
    assert counter["n"] <= 5, f"company directory ran {counter['n']} queries"


async def test_legacy_profile_section_never_breaks_either_sheet(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Incident 04/08 (fausse piste, invariant gravé) — une section
    HÉRITÉE en base (`id_documents`, morte à la fusion h8c2) ne fait
    jamais tomber une fiche : la déf résiduelle retombe en Divers côté
    personne, et la fiche société (dont la taxonomie ne lit AUCUNE déf)
    sert ses 4 sections quoi qu'il arrive — clé libre du sack comprise."""
    from shared.models.custom_field import CustomFieldDefinition

    headers = agent_headers(admin)
    db_session.add(
        CustomFieldDefinition(
            agency_id=admin.agency_id,
            key="vieux_papier",
            label="Vieux papier",
            field_type="text",
            scope="person",
            profile_section="id_documents",  # la section MORTE, telle quelle
        )
    )
    await db_session.commit()

    # Face PERSONNE : 200, la résiduelle tombe en Divers (jamais perdue).
    r = await client.post(
        "/client-profiles",
        headers=headers,
        json={"first_name": "Legacy", "last_name": "Section", "email": "legacy-sec@example.com"},
    )
    assert r.status_code == 201, r.text
    detail = (await client.get(f"/client-profiles/{r.json()['id']}", headers=headers)).json()
    by_key = {s["key"]: s["references"] for s in detail["sections"]}
    assert "id_documents" not in by_key
    assert "vieux_papier" in by_key["misc"]

    # Face SOCIÉTÉ : 200, 4 sections, la clé libre du sack en Divers.
    r = await client.post(
        "/company-profiles",
        headers=headers,
        json={"name": "Legacy Section SA", "custom_fields": {"vieux_papier": "RC-1988"}},
    )
    assert r.status_code == 201, r.text
    detail = (await client.get(f"/company-profiles/{r.json()['id']}", headers=headers)).json()
    assert [s["key"] for s in detail["sections"]] == ["identity", "contact", "situation", "misc"]
    assert "vieux_papier" in {s["key"]: s["references"] for s in detail["sections"]}["misc"]
    assert detail["custom_fields"]["vieux_papier"] == "RC-1988"
    # `field_labels` PORTE DÉSORMAIS TOUTES LES CLÉS VIVANTES (lot du
    # 07/08) : la matérialisation déclare les 17 presets AVEC leur libellé
    # de catalogue, donc la distinction « baptisée par l'agence / d'origine »
    # n'existe plus — il n'y a plus qu'un libellé servi par clé.
    labels = detail["field_labels"]
    assert labels["company_name"] == "Raison sociale"
    assert labels["vieux_papier"] == "Vieux papier"  # clé de sack, humanisée
    assert set(labels) >= {s for sec in detail["sections"] for s in sec["references"]}
