"""V4 (solde CRM) — l'import-fiches (créées/liées/ignorées, fill-gap,
sans parcours) et les configs d'agence (journey nullable)."""

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.custom_field import CustomFieldDefinition
from shared.models.rbac import Role
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser

pytestmark = pytest.mark.usefixtures("rbac_baseline")


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


async def test_profile_import_creates_links_ignores(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
) -> None:
    """V4a — dédup email : LIER (fill-gap, jamais dupliquer) ; sans
    email → ignorée ; sans identité → ignorée ; le rapport dit tout.
    AUCUN parcours dans ce chemin (étape séparée optionnelle)."""
    headers = agent_headers(admin)
    db_session.add(
        CustomFieldDefinition(
            agency_id=admin.agency_id,
            key="birth_country",
            label="Pays de naissance",
            field_type="text",
            scope="person",
        )
    )
    await db_session.commit()
    # Une fiche EXISTANTE (sera liée, pas dupliquée) — sans nationalité.
    r = await client.post(
        "/client-profiles",
        headers=headers,
        json={"first_name": "Déjà", "last_name": "Là", "email": "deja@example.com"},
    )
    assert r.status_code == 201, r.text
    existing_id = r.json()["id"]

    csv_text = (
        "Prénom,Nom,Courriel,Nationalité,PaysNaissance\n"
        "Déjà,Là,deja@example.com,BR,BR\n"
        "Nouvelle,Cliente,nouvelle@example.com,AR,AR\n"
        "Sans,Email,,FR,FR\n"
        ",,anonyme@example.com,PT,PT\n"
    )
    mapping = {
        "Prénom": "first_name",
        "Nom": "last_name",
        "Courriel": "email",
        "Nationalité": "nationality",
        "PaysNaissance": "birth_country",
    }
    r = await client.post(
        "/imports/client-profiles",
        headers=headers,
        json={"csv_text": csv_text, "mapping": mapping},
    )
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["total_rows"] == 4
    assert [c["email"] for c in report["created"]] == ["nouvelle@example.com"]
    assert [x["profile_id"] for x in report["linked"]] == [existing_id]
    assert sorted(i["reason"] for i in report["ignored"]) == ["missing_identity", "no_email"]
    # FILL-GAP sur la liée : nationalité comblée, custom person posé.
    detail = (await client.get(f"/client-profiles/{existing_id}", headers=headers)).json()
    assert detail["nationality"] == "BR"
    assert detail["custom_fields"]["birth_country"] == "BR"
    # La créée est une fiche LIBRE (aucun compte, aucun parcours).
    listing = (await client.get("/client-profiles?search=nouvelle@", headers=headers)).json()
    assert listing["total"] == 1
    n_accounts = (
        await db_session.execute(
            text("SELECT count(*) FROM expat_user WHERE email = 'nouvelle@example.com'")
        )
    ).scalar_one()
    assert n_accounts == 0

    # Cible inconnue → 422 nommé.
    r = await client.post(
        "/imports/client-profiles",
        headers=headers,
        json={"csv_text": csv_text, "mapping": {"Courriel": "email", "Nom": "code_postal"}},
    )
    assert r.status_code == 422
    assert r.json()["code"] == "import.unknown_targets"


async def test_agency_level_import_config(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """V4b — une config SANS parcours (niveau agence) : cibles = le
    référentiel person ; hors référentiel → 422 ; homonyme → 409."""
    headers = agent_headers(admin)
    body = {
        "crm_slug": "custom",
        "custom_crm_name": "Mon vieux CRM",
        "name": "Fiches clients",
        "mapping": {"Email": "email", "Prénom": "first_name", "Tel": "phone"},
    }
    r = await client.post("/imports/mappings", headers=headers, json=body)
    assert r.status_code in (200, 201), r.text
    assert r.json()["journey_template_id"] is None
    # Cible hors référentiel person → 422.
    bad = dict(body, name="Mauvaise", mapping={"Email": "email", "X": "montant_facture"})
    r = await client.post("/imports/mappings", headers=headers, json=bad)
    assert r.status_code == 422
    # Homonyme au niveau agence → 409 (NULLS NOT DISTINCT).
    r = await client.post("/imports/mappings", headers=headers, json=body)
    assert r.status_code == 409
    assert r.json()["code"] == "import.mapping_name_taken"


async def test_company_import_creates_links_ignores(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Complément B — l'import sociétés : dédup par dénomination en
    lier-pas-dupliquer (fill-gap presets), sans dénomination → ignorée,
    cible inconnue → 422, rapport identique à l'import personnes."""
    headers = agent_headers(admin)
    # Une société existante SANS forme juridique (sera liée + comblée).
    r = await client.post("/company-profiles", headers=headers, json={"name": "HoldCo"})
    assert r.status_code == 201, r.text
    existing_id = r.json()["id"]

    csv_text = "Société,Forme,SIREN\nholdco,SL,B12345678\nNewCo Iberia,SA,B87654321\n,SARL,X1\n"
    mapping = {"Société": "name", "Forme": "legal_form", "SIREN": "company_registration_number"}
    r = await client.post(
        "/imports/company-profiles",
        headers=headers,
        json={"csv_text": csv_text, "mapping": mapping},
    )
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["total_rows"] == 3
    assert [c["name"] for c in report["created"]] == ["NewCo Iberia"]
    assert [x["company_profile_id"] for x in report["linked"]] == [existing_id]
    assert [i["reason"] for i in report["ignored"]] == ["no_name"]
    # FILL-GAP sur la liée, servie dans SES sections.
    detail = (await client.get(f"/company-profiles/{existing_id}", headers=headers)).json()
    assert detail["custom_fields"]["legal_form"] == "SL"
    assert detail["custom_fields"]["company_registration_number"] == "B12345678"
    # Pas de doublon : l'annuaire société compte 2 (HoldCo + NewCo).
    listing = (await client.get("/company-profiles", headers=headers)).json()
    assert listing["total"] == 2
    # Cible inconnue → 422 nommé.
    r = await client.post(
        "/imports/company-profiles",
        headers=headers,
        json={"csv_text": csv_text, "mapping": {"Société": "name", "Forme": "couleur"}},
    )
    assert r.status_code == 422
    assert r.json()["code"] == "import.unknown_targets"


async def test_address_column_imports_as_full_text_street(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Complément B — ADRESSES IMPORTABLES : une colonne « adresse
    complète » mappée vers une déf address est stockée en texte intégral
    dans `street` (objet valide, AUCUN parsing magique)."""
    headers = agent_headers(admin)
    db_session.add(
        CustomFieldDefinition(
            agency_id=admin.agency_id,
            key="residence_address",
            label="Adresse de résidence",
            field_type="address",
            scope="person",
        )
    )
    await db_session.commit()
    csv_text = (
        "Prénom,Nom,Email,Adresse\n"
        'Addr,Essée,addr@example.com,"12 rue de la Paix, 75002 Paris, France"\n'
    )
    r = await client.post(
        "/imports/client-profiles",
        headers=headers,
        json={
            "csv_text": csv_text,
            "mapping": {
                "Prénom": "first_name",
                "Nom": "last_name",
                "Email": "email",
                "Adresse": "residence_address",
            },
        },
    )
    assert r.status_code == 200, r.text
    assert len(r.json()["created"]) == 1
    detail = (
        await client.get(
            f"/client-profiles/{r.json()['created'][0]['profile_id']}", headers=headers
        )
    ).json()
    # Le texte INTÉGRAL dans street — objet address structurellement valide.
    assert detail["custom_fields"]["residence_address"] == {
        "street": "12 rue de la Paix, 75002 Paris, France"
    }


async def test_bad_cells_never_kill_the_batch(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """LA RÈGLE ABSOLUE (debug Teamleader 03/08) — une cellule mauvaise =
    trou laissé, une ligne mauvaise = ignorée avec raison, le batch ne
    meurt JAMAIS sur une donnée : 'unknown' dans sex (VARCHAR(1) — le 500
    prod), téléphone trop long, date illisible, doublon intra-batch qui
    LIE au lieu de dupliquer."""
    headers = agent_headers(admin)
    csv_text = (
        "Prénom,Nom,Email,Genre,Téléphone,Naissance\n"
        "Kevin,Olivetto,kevin@example.com,unknown,0601020304,1977-04-02\n"
        f"Long,Phone,long@example.com,M,{'9' * 80},pas-une-date\n"
        "Kevin,Olivetto,KEVIN@example.com,,,\n"
        "Sans,Identité,,M,,\n"
    )
    r = await client.post(
        "/imports/client-profiles",
        headers=headers,
        json={
            "csv_text": csv_text,
            "mapping": {
                "Prénom": "first_name",
                "Nom": "last_name",
                "Email": "email",
                "Genre": "sex",
                "Téléphone": "phone",
                "Naissance": "date_of_birth",
            },
        },
    )
    assert r.status_code == 200, r.text  # JAMAIS un 500 sur de la donnée
    rep = r.json()
    assert rep["total_rows"] == 4
    assert [c["email"] for c in rep["created"]] == ["kevin@example.com", "long@example.com"]
    assert len(rep["linked"]) == 1  # le doublon intra-batch LIE (casse ignorée)
    assert [i["reason"] for i in rep["ignored"]] == ["no_email"]
    # Les cellules mauvaises = TROUS, les bonnes ont tenu.
    listing = (await client.get("/client-profiles?search=kevin@", headers=headers)).json()
    detail = (
        await client.get(f"/client-profiles/{listing['items'][0]['id']}", headers=headers)
    ).json()
    assert detail["sex"] is None  # 'unknown' → trou (plus jamais un DataError)
    assert detail["phone"] == "0601020304"
    assert detail["date_of_birth"] == "1977-04-02"
    long_listing = (await client.get("/client-profiles?search=long@", headers=headers)).json()
    long_detail = (
        await client.get(f"/client-profiles/{long_listing['items'][0]['id']}", headers=headers)
    ).json()
    assert long_detail["phone"] is None  # 80 chars → trou (cap 50 du contrat)
    assert long_detail["date_of_birth"] is None  # date illisible → trou


async def test_preview_and_import_render_identical_verdicts(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """LA GARANTIE STRUCTURELLE — une seule fonction d'analyse : même
    fichier → le preview (dry-run, ZÉRO écriture) et l'import réel
    rendent des verdicts IDENTIQUES ligne à ligne."""
    headers = agent_headers(admin)
    # Une fiche existante pour un verdict 'link' en base.
    r = await client.post(
        "/client-profiles",
        headers=headers,
        json={"first_name": "Déjà", "last_name": "Base", "email": "en-base@example.com"},
    )
    assert r.status_code == 201, r.text
    csv_text = (
        "Prénom,Nom,Email,Genre\n"
        "Neuve,Cliente,neuve@example.com,F\n"
        "Déjà,Base,en-base@example.com,M\n"
        "Neuve,Doublon,NEUVE@example.com,\n"
        "Sans,Email,,M\n"
        "Mauvais,Genre,genre@example.com,unknown\n"
    )
    mapping = {"Prénom": "first_name", "Nom": "last_name", "Email": "email", "Genre": "sex"}
    body = {"csv_text": csv_text, "mapping": mapping}

    r = await client.post("/imports/client-profiles/preview", headers=headers, json=body)
    assert r.status_code == 200, r.text
    preview = r.json()
    # ZÉRO écriture au dry-run.
    n_before = (await db_session.execute(text("SELECT count(*) FROM client_profile"))).scalar_one()
    assert preview["summary"] == {
        "create": 2,
        "link": 2,
        "ignore": 1,
        "ignore_reasons": {"no_email": 1},
    }
    statuses = {row["row_index"]: row["status"] for row in preview["rows"]}
    # La cellule mauvaise = ISSUE, la ligne vit (create).
    bad_row = next(row for row in preview["rows"] if row["row_index"] == 5)
    assert bad_row["status"] == "create"
    assert bad_row["issues"] == [{"column": "Genre", "code": "invalid_value"}]
    assert "sex" not in bad_row["person"]  # trou annoncé
    assert preview["rows"][0]["person"]["sex"] == "F"  # normalisé servi

    r = await client.post("/imports/client-profiles", headers=headers, json=body)
    assert r.status_code == 200, r.text
    report = r.json()
    n_after = (await db_session.execute(text("SELECT count(*) FROM client_profile"))).scalar_one()
    assert n_before == n_after - 2  # le preview n'avait RIEN écrit
    # IDENTITÉ ligne à ligne : le rapport réel == les verdicts du preview.
    real_statuses: dict[int, str] = {}
    for outcome in report["created"]:
        real_statuses[outcome["row"]] = "create"
    for outcome in report["linked"]:
        real_statuses[outcome["row"]] = "link"
    for outcome in report["ignored"]:
        real_statuses[outcome["row"]] = "ignore"
    assert real_statuses == statuses


async def test_corrections_flow_through_the_same_mill(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Corrections — appliquées après parse, avant validation : l'email
    vide corrigé fait passer la ligne d'ignore à create ; une correction
    invalide = motivée, jamais un 500."""
    headers = agent_headers(admin)
    csv_text = "Prénom,Nom,Email,Genre\nCorrigée,Cliente,,unknown\n"
    mapping = {"Prénom": "first_name", "Nom": "last_name", "Email": "email", "Genre": "sex"}
    # SANS correction : ignore no_email.
    r = await client.post(
        "/imports/client-profiles/preview",
        headers=headers,
        json={"csv_text": csv_text, "mapping": mapping},
    )
    assert r.json()["rows"][0]["status"] == "ignore"
    # AVEC corrections : email posé (ignore → create) + genre corrigé
    # valide + une correction à cible inconnue (issue motivée, pas de 500).
    body = {
        "csv_text": csv_text,
        "mapping": mapping,
        "corrections": [
            {"row_index": 1, "target": "email", "value": "corrigee@example.com"},
            {"row_index": 1, "target": "sex", "value": "F"},
            {"row_index": 1, "target": "code_postal", "value": "75002"},
        ],
    }
    r = await client.post("/imports/client-profiles/preview", headers=headers, json=body)
    assert r.status_code == 200, r.text
    row = r.json()["rows"][0]
    assert row["status"] == "create"
    assert row["person"]["email"] == "corrigee@example.com"
    assert row["person"]["sex"] == "F"  # la correction passe la moulinette
    assert {"column": "(correction)", "code": "unknown_target"} in row["issues"]
    # L'import réel avec les mêmes corrections écrit le corrigé.
    r = await client.post("/imports/client-profiles", headers=headers, json=body)
    assert r.status_code == 200, r.text
    assert [c["email"] for c in r.json()["created"]] == ["corrigee@example.com"]
    listing = (await client.get("/client-profiles?search=corrigee@", headers=headers)).json()
    detail = (
        await client.get(f"/client-profiles/{listing['items'][0]['id']}", headers=headers)
    ).json()
    assert detail["sex"] == "F"
