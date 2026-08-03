"""V4 (solde CRM) — l'import-fiches (créées/liées/ignorées, fill-gap,
sans parcours) et les configs d'agence (journey nullable)."""

from pathlib import Path

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


async def test_company_import_widened_targets_and_typed_coercions(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Audit import P1 — les cibles société élargies (vat_number, country,
    email, phone, website, address, alias registration_number) : rangées
    dans les sections posées, coercitions typées (pays invalide = issue +
    trou, email minusculisé), fixture aux en-têtes Teamleader réels
    (cyrillique compris)."""
    headers = agent_headers(admin)
    csv_text = Path("tests/fixtures/companies_teamleader_synthetic.csv").read_text()
    mapping = {
        "Nom": "name",
        "Numéro de TVA": "vat_number",
        "Pays": "country",
        "Adresse e-mail": "email",
        "Téléphone": "phone",
        "Site web": "website",
        "Numéro d'identification national (Siret)": "registration_number",  # ALIAS
    }
    r = await client.post(
        "/imports/company-profiles/preview",
        headers=headers,
        json={"csv_text": csv_text, "mapping": mapping},
    )
    assert r.status_code == 200, r.text
    preview = r.json()
    assert preview["summary"]["create"] == 3
    first = preview["rows"][0]["person"]
    assert first["name"] == "Домициляция ЕООД"  # cyrillique intact
    assert first["vat_number"] == "BG123456789"
    assert first["country"] == "BG"
    assert first["company_registration_number"] == "204558877"  # l'alias normalisé
    second = preview["rows"][1]["person"]
    assert second["email"] == "hola@iberia.es"  # minusculisé
    third = preview["rows"][2]
    # 'Bulgarie' en toutes lettres ≠ ISO-2 (la règle V1, format seul) →
    # issue + trou ; 'XX' passerait (format-valide, pas de liste blanche).
    assert {"column": "Pays", "code": "invalid_value"} in third["issues"]
    assert "country" not in third["person"]  # trou, la ligne vit

    # L'import réel range les valeurs dans les SECTIONS posées.
    r = await client.post(
        "/imports/company-profiles",
        headers=headers,
        json={"csv_text": csv_text, "mapping": mapping},
    )
    assert r.status_code == 200, r.text
    assert len(r.json()["created"]) == 3
    company_id = r.json()["created"][0]["company_profile_id"]
    detail = (await client.get(f"/company-profiles/{company_id}", headers=headers)).json()
    by_key = {s["key"]: s["references"] for s in detail["sections"]}
    assert "vat_number" in by_key["id_documents"]
    assert "email" in by_key["contact"] and "country" in by_key["contact"]
    assert detail["custom_fields"]["vat_number"] == "BG123456789"
    # Compat PATCH : le sack reste libre, les cibles nommées passent telles
    # quelles (aucune rupture de l'existant).
    r = await client.patch(
        f"/company-profiles/{company_id}",
        headers=headers,
        json={"custom_fields": {"website": "https://nouveau.bg"}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["custom_fields"]["website"] == "https://nouveau.bg"


@pytest.mark.skipif(
    not Path(
        "/Users/alexandre/Desktop/FreelanceProject/nidria/nidria-frontend/.debug-import/Companies-2026-08-03-16-15-09.xlsx"
    ).exists(),
    reason="fichier réel absent (poste local uniquement)",
)
async def test_company_preview_real_file_hooks_widened_targets(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Le fichier Companies RÉEL en preview : TVA, pays, email, téléphone
    TOMBENT dans les cibles élargies — le récap chiffré du rapport."""
    import base64

    headers = agent_headers(admin)
    raw = Path(
        "/Users/alexandre/Desktop/FreelanceProject/nidria/nidria-frontend/.debug-import/Companies-2026-08-03-16-15-09.xlsx"
    ).read_bytes()
    r = await client.post(
        "/imports/company-profiles/preview",
        headers=headers,
        json={
            "file_b64": base64.b64encode(raw).decode(),
            "filename": "Companies.xlsx",
            "mapping": {
                "Nom": "name",
                "Numéro de TVA": "vat_number",
                "Pays": "country",
                "Adresse e-mail": "email",
                "Téléphone": "phone",
                "Site web": "website",
                "Numéro d'identification national (Siret)": "registration_number",
            },
        },
    )
    assert r.status_code == 200, r.text
    preview = r.json()
    assert preview["total_rows"] == 439
    assert preview["summary"]["create"] >= 430
    counts = {"vat_number": 0, "country": 0, "email": 0, "phone": 0}
    body = dict(page_size=500, page=1)
    # une seule page suffit (500 ≥ 439)
    r = await client.post(
        "/imports/company-profiles/preview",
        headers=headers,
        json={
            "file_b64": base64.b64encode(raw).decode(),
            "filename": "Companies.xlsx",
            "mapping": {
                "Nom": "name",
                "Numéro de TVA": "vat_number",
                "Pays": "country",
                "Adresse e-mail": "email",
                "Téléphone": "phone",
            },
            **body,
        },
    )
    for row in r.json()["rows"]:
        for key in counts:
            if row["person"].get(key):
                counts[key] += 1
    print(f"\nRÉCAP CHIFFRÉ Companies réel (439 lignes): {counts}")
    assert counts["vat_number"] > 300  # la TVA tombe massivement
    assert counts["country"] > 300


CONTACT_HEADERS_42 = [
    "Teamleader ID",
    "Prénom",
    "Nom de famille",
    "Rue",
    "Numéro de la rue",
    "Code postal",
    "Ville",
    "adresse postale",
    "Opt-in courriers marketing",
    "Province",
    "Pays",
    "Date de naissance",
    "Genre",
    "Langue",
    "Adresse e-mail",
    "Téléphone",
    "Mobile",
    "Fax",
    "Site web",
    "Numéro de TVA du contact",
    "Numéro d'identification national (Siret)",
    "Actif",
    "Tags",
    "Liste des prix",
    "Nombre de minutes non facturées",
    "Entreprises",
    "Société",
    "Fonction",
    "Sous-fonction",
    "Décideur",
    "Dernière activité",
    "Dernier rendez-vous",
    "Date ajoutée",
    "Dernière modification",
    "Crédits prépayés restants",
    "N° Compte IBAN",
    "Code BIC",
    "Conditions de paiement",
    "Total à facturer",
    "ID externe",
    "Traçage prospects",
    "Taux horaire",
]
COMPANY_HEADERS_54 = [
    "Teamleader ID",
    "Nom",
    "Rue",
    "Numéro de la rue",
    "Code postal",
    "Ville",
    "Pays",
    "Langue",
    "Numéro de TVA",
    "Adresse e-mail facturation",
    "Numéro d'identification national (Siret)",
    "Adresse e-mail",
    "Site web",
    "Type d'entreprise",
    "Téléphone",
    "Fax",
    "Tags",
    "Gestionnaire de compte",
    "Actif",
    "Secteur",
    "Code APE",
    "Client COMPTA",
    "Comptable",
    "Date of VAT Reg",
    "end contrat Dom",
    "NUM IRINA",
    "second Email",
    "TVA",
    "Province",
    "Liste des prix",
    "Conditions de paiement",
    "Total à facturer",
    "ID externe",
    "Nombre de minutes non facturées",
    "Dernière activité",
    "Dernier rendez-vous",
    "Date ajoutée",
    "Dernière modification",
    "Entreprises associées",
    "Crédits prépayés restants",
    "N° Compte IBAN",
    "Code BIC",
    "Notation",
    "Chiffre d'affaires",
    "Marge bén. br.",
    "Bénéfice",
    "Quick ratio",
    "Degré ind. fin.",
    "# Collaborateurs",
    "Valeur ajoutée",
    "Valeur ajout. par collaborateur",
    "ROE",
    "Taux horaire",
    "Opt-in courriers marketing",
]


async def test_suggest_mapping_hits_80_percent_of_mappable_headers(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Lot mapping — LE TÉMOIN CHIFFRÉ sur les en-têtes réels (42+54) :
    ≥80 % des colonnes MAPPABLES auto-suggérées, ZÉRO faux ami suggéré
    (Pays contacts, e-mail facturation, TVA taux), les exclusions tiennent."""
    headers = agent_headers(admin)
    # Les défs person que l'agence de Nico a (preferred_language existe
    # partout via les 21 ; residence_address idem).
    for key in ("preferred_language", "residence_address"):
        db_session.add(
            CustomFieldDefinition(
                agency_id=admin.agency_id, key=key, label=key, field_type="text", scope="person"
            )
        )
    await db_session.commit()

    r = await client.post(
        "/imports/client-profiles/suggest-mapping",
        headers=headers,
        json={"headers": CONTACT_HEADERS_42},
    )
    assert r.status_code == 200, r.text
    person = r.json()["suggestions"]
    # Les colonnes MAPPABLES du fichier Contacts (verdicts du tableau) :
    expected_person = {
        "Prénom": "first_name",
        "Nom de famille": "last_name",
        "adresse postale": "residence_address",
        "Date de naissance": "date_of_birth",
        "Genre": "sex",
        "Langue": "preferred_language",
        "Adresse e-mail": "email",
        "Téléphone": "phone",
        "Société": "employer",
        "Fonction": "profession",
        "Tags": "tags",
    }
    # Lot plafond : TVA contact → tax_id, Siret → company_registration_number
    # (presets du catalogue, déclarés à la volée à l'import).
    expected_person["Numéro de TVA du contact"] = "tax_id"
    expected_person["Numéro d'identification national (Siret)"] = "company_registration_number"
    hit = {h: t for h, t in expected_person.items() if person.get(h) == t}
    ratio = len(hit) / len(expected_person)
    assert ratio >= 0.8, f"personnes: {ratio:.0%} — manquées: {set(expected_person) - set(hit)}"
    # « Pays » : l'ambiguïté SE PROPOSE (deux cibles), rien d'auto-posé.
    assert "Pays" not in person
    assert r.json()["ambiguous"]["Pays"] == ["nationality", "tax_residence_country"]
    # Mobile silencieux (Téléphone direct présent).
    assert "Mobile" not in person

    r = await client.post(
        "/imports/company-profiles/suggest-mapping",
        headers=headers,
        json={"headers": COMPANY_HEADERS_54},
    )
    assert r.status_code == 200, r.text
    company = r.json()["suggestions"]
    expected_company = {
        "Nom": "name",
        "Pays": "country",
        "Numéro de TVA": "vat_number",
        "Adresse e-mail": "email",
        "Site web": "website",
        "Type d'entreprise": "legal_form",
        "Téléphone": "phone",
        "Numéro d'identification national (Siret)": "company_registration_number",
        "Tags": "tags",
    }
    hit_c = {h: t for h, t in expected_company.items() if company.get(h) == t}
    ratio_c = len(hit_c) / len(expected_company)
    assert ratio_c >= 0.8, (
        f"sociétés: {ratio_c:.0%} — manquées: {set(expected_company) - set(hit_c)}"
    )
    for trap in ("Adresse e-mail facturation", "TVA", "Langue", "second Email"):
        assert trap not in company, trap
    print(
        f"\nTÉMOIN: personnes {len(hit)}/{len(expected_person)} ({ratio:.0%}), "
        f"sociétés {len(hit_c)}/{len(expected_company)} ({ratio_c:.0%})"
    )


async def test_tags_import_target(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Point 2 — la cible manquante livrée : `tags` aux deux imports
    (split ,/;, dédup, fill-gap : l'existant gagne)."""
    headers = agent_headers(admin)
    r = await client.post(
        "/imports/client-profiles",
        headers=headers,
        json={
            "csv_text": (
                'Prénom,Nom,Email,Tags\nTag,Guée,tag-import@example.com,"VIP; salon-2026;VIP"\n'
            ),
            "mapping": {
                "Prénom": "first_name",
                "Nom": "last_name",
                "Email": "email",
                "Tags": "tags",
            },
        },
    )
    assert r.status_code == 200, r.text
    listing = (await client.get("/client-profiles?search=tag-import@", headers=headers)).json()
    assert listing["items"][0]["tags"] == ["VIP", "salon-2026"]  # split + dédup
    r = await client.post(
        "/imports/company-profiles",
        headers=headers,
        json={
            "csv_text": 'Nom,Tags\nTagCo,"holding,btp"\n',
            "mapping": {"Nom": "name", "Tags": "tags"},
        },
    )
    assert r.status_code == 200, r.text
    listing = (await client.get("/company-profiles?search=TagCo", headers=headers)).json()
    assert listing["items"][0]["tags"] == ["holding", "btp"]


async def test_catalog_preset_declared_on_the_fly_idempotent(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Lot plafond — mapper un preset NON déclaré le DÉCLARE (la mécanique
    du picker, helper partagé) : déf créée avec le type, le label i18n, le
    scope person et SA section de taxonomie ; idempotent au 2e import ;
    JAMAIS au preview (zéro écriture)."""
    headers = agent_headers(admin)
    body = {
        "csv_text": (
            "Prénom,Nom,Email,Visa,Tags\nSur,Catalogue,catalogue@example.com,Long séjour,VIP\n"
        ),
        "mapping": {
            "Prénom": "first_name",
            "Nom": "last_name",
            "Email": "email",
            "Visa": "visa_type",  # preset du catalogue, NON déclaré
            "Tags": "tags",
        },
    }
    # PREVIEW : la cible passe, RIEN n'est déclaré.
    r = await client.post("/imports/client-profiles/preview", headers=headers, json=body)
    assert r.status_code == 200, r.text
    assert r.json()["rows"][0]["person"]["visa_type"] == "Long séjour"
    n_defs = (
        await db_session.execute(
            text("SELECT count(*) FROM custom_field_definition WHERE key = 'visa_type'")
        )
    ).scalar_one()
    assert n_defs == 0  # zéro écriture au dry-run

    # IMPORT : la déf naît — scope person, section de la taxonomie.
    r = await client.post("/imports/client-profiles", headers=headers, json=body)
    assert r.status_code == 200, r.text
    assert r.json()["tags_applied"] == 1  # le compteur du rapport
    row = (
        await db_session.execute(
            text(
                "SELECT scope, profile_section, field_type FROM custom_field_definition "
                "WHERE key = 'visa_type'"
            )
        )
    ).first()
    assert list(row) == ["person", "id_documents", "select"] or list(row)[:2] == [
        "person",
        "id_documents",
    ]
    # IDEMPOTENT : rejouer ne crée pas de doublon.
    r = await client.post("/imports/client-profiles", headers=headers, json=body)
    assert r.status_code == 200, r.text
    n_defs = (
        await db_session.execute(
            text("SELECT count(*) FROM custom_field_definition WHERE key = 'visa_type'")
        )
    ).scalar_one()
    assert n_defs == 1
    # La valeur est sur la fiche, servie dans SA section.
    listing = (await client.get("/client-profiles?search=catalogue@", headers=headers)).json()
    detail = (
        await client.get(f"/client-profiles/{listing['items'][0]['id']}", headers=headers)
    ).json()
    assert detail["custom_fields"]["visa_type"] == "Long séjour"
    by_key = {s["key"]: s["references"] for s in detail["sections"]}
    assert "visa_type" in by_key["id_documents"]
