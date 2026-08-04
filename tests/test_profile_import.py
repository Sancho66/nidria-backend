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
    assert "vat_number" in by_key["identity"]  # fusion id_documents→identity
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


@pytest.mark.skipif(
    not Path(
        "/Users/alexandre/Desktop/FreelanceProject/nidria/nidria-frontend/.debug-import/Contacts-2026-08-03-16-14-51.xlsx"
    ).exists(),
    reason="fichier réel absent (poste local uniquement)",
)
async def test_street_number_pair_real_files(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Micro-lot couple, rejeu RÉEL : les deux fichiers Teamleader passent
    par la charge SUGGÉRÉE (le couple convergent) — les adresses
    s'assemblent AVEC leur numéro, comptes au rapport."""
    import base64

    headers = agent_headers(admin)
    debug_dir = Path(
        "/Users/alexandre/Desktop/FreelanceProject/nidria/nidria-frontend/.debug-import"
    )

    async def replay(entity: str, filename: str, street_base: str) -> dict[str, int]:
        raw = (debug_dir / filename).read_bytes()
        from src.imports.csv_reader import parse_upload

        parsed = parse_upload(filename, raw)
        r = await client.post(
            f"/imports/{entity}/suggest-mapping",
            headers=headers,
            json={"headers": parsed.headers},
        )
        assert r.status_code == 200, r.text
        mapping = r.json()["suggestions"]
        assert mapping["Rue"] == f"{street_base}.street"
        assert mapping["Numéro de la rue"] == f"{street_base}.street"
        # le choix UI : fragments (le couple compris), pas le texte intégral
        mapping.pop("adresse postale", None)
        counts = {"street": 0, "with_number": 0}
        page = 1
        while True:
            r = await client.post(
                f"/imports/{entity}/preview",
                headers=headers,
                json={
                    "file_b64": base64.b64encode(raw).decode(),
                    "filename": filename,
                    "mapping": mapping,
                    "page": page,
                    "page_size": 500,
                },
            )
            assert r.status_code == 200, r.text
            body = r.json()
            for row in body["rows"]:
                street = (row["person"].get(street_base) or {}).get("street")
                if not street:
                    continue
                counts["street"] += 1
                raw_row = parsed.rows[row["row_index"] - 1]
                number = (raw_row.get("Numéro de la rue") or "").strip()
                rue = (raw_row.get("Rue") or "").strip()
                if number and rue:
                    assert street == f"{number} {rue}"  # l'ordre fixe, prouvé
                    counts["with_number"] += 1
            if page * 500 >= body["total_rows"]:
                return counts
            page += 1

    contacts = await replay(
        "client-profiles", "Contacts-2026-08-03-16-14-51.xlsx", "residence_address"
    )
    companies = await replay("company-profiles", "Companies-2026-08-03-16-15-09.xlsx", "address")
    print(f"\nRÉCAP COUPLE Contacts réel: {contacts} | Companies réel: {companies}")
    assert contacts["with_number"] == 83
    assert companies["with_number"] == 360


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
    # Audit catalogue : le NON de « Site web » est renversé — preset website.
    expected_person["Site web"] = "website"
    hit = {h: t for h, t in expected_person.items() if person.get(h) == t}
    ratio = len(hit) / len(expected_person)
    assert ratio >= 0.8, f"personnes: {ratio:.0%} — manquées: {set(expected_person) - set(hit)}"
    # « Pays » : l'ambiguïté SE PROPOSE (deux cibles), rien d'auto-posé.
    assert "Pays" not in person
    assert r.json()["ambiguous"]["Pays"] == [
        "nationality",
        "tax_residence_country",
        "residence_address.country",
    ]
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
        # Audit catalogue : trois re-verdicts — Secteur et Code APE sortent
        # du sack libre, # Collaborateurs sort du pack finance.
        "Secteur": "industry",
        "Code APE": "activity_code",
        "# Collaborateurs": "employee_count",
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


async def test_every_suggested_target_coerces_real_world_values(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """LA RÈGLE STRUCTURELLE (urgence 04/08) — toute cible que le
    suggéreur propose accepte les formats du monde réel de sa colonne :
    le suggest sur les 42 en-têtes réels, puis un preview avec une valeur
    TYPE par colonne suggérée → ZÉRO invalid_value. Un suggéreur qui
    propose une cible incoerçable casse ce test."""
    headers = agent_headers(admin)
    db_session.add(
        CustomFieldDefinition(
            agency_id=admin.agency_id,
            key="residence_address",
            label="residence_address",
            field_type="text",
            scope="person",
        )
    )
    await db_session.commit()
    r = await client.post(
        "/imports/client-profiles/suggest-mapping",
        headers=headers,
        json={"headers": CONTACT_HEADERS_42},
    )
    assert r.status_code == 200, r.text
    suggestions = r.json()["suggestions"]
    # Les valeurs TYPE du monde réel, par en-tête (les traits du fichier).
    real_world = {
        "Prénom": "Kevin",
        "Nom de famille": "Olivetto",
        "Adresse e-mail": "kevin@example.com",
        "Téléphone": "+33 6 00 00 00 01",
        "Date de naissance": "1977-04-02",
        "Genre": "M",
        "Langue": "FR",
        "adresse postale": "12 rue de la Paix, 75002 Paris",
        "Fonction": "Notaire",
        "Société": "MrM Ascenseurs",
        "Tags": "VIP;prospect",
        "Numéro de TVA du contact": "FR12345678901",
        "Numéro d'identification national (Siret)": "84512345678901",
        "Site web": "https://kevin.example.com",
    }
    columns = [c for c in suggestions if c in real_world]
    csv_text = (
        ",".join(f'"{c}"' for c in columns)
        + "\n"
        + ",".join(f'"{real_world[c]}"' for c in columns)
        + "\n"
    )
    r = await client.post(
        "/imports/client-profiles/preview",
        headers=headers,
        json={"csv_text": csv_text, "mapping": {c: suggestions[c] for c in columns}},
    )
    assert r.status_code == 200, r.text
    row = r.json()["rows"][0]
    assert row["issues"] == [], f"cibles incoerçables: {row['issues']}"
    # La langue ISO vise LA COLONNE, normalisée en CODE produit.
    assert row["person"]["preferred_lang"] == "fr"
    assert row["person"]["sex"] == "M"


async def test_language_and_enum_value_normalization(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Urgence 04/08 — les formats du monde réel : ISO et noms complets
    pour la langue (vers l'option de la déf, quelle que soit sa langue de
    déclaration), Homme/Female pour le sexe, Marié/veuve pour l'état
    civil ; l'illisible reste un trou motivé."""
    headers = agent_headers(admin)
    db_session.add(
        CustomFieldDefinition(
            agency_id=admin.agency_id,
            key="preferred_language",
            label="Langue",
            field_type="select",
            options=["French", "English", "Other"],  # déf déclarée EN ANGLAIS
            scope="person",
        )
    )
    await db_session.commit()
    csv_text = (
        "Prénom,Nom,Email,Langue,Genre,EtatCivil\n"
        "A,Un,l1@example.com,fr,Homme,Marié\n"
        "B,Deux,l2@example.com,Français,Female,veuve\n"
        "C,Trois,l3@example.com,klingon,unknown,compliqué\n"
    )
    mapping = {
        "Prénom": "first_name",
        "Nom": "last_name",
        "Email": "email",
        "Langue": "preferred_language",
        "Genre": "sex",
        "EtatCivil": "marital_status",
    }
    r = await client.post(
        "/imports/client-profiles/preview",
        headers=headers,
        json={"csv_text": csv_text, "mapping": mapping},
    )
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    assert rows[0]["person"]["preferred_language"] == "French"  # 'fr' → option EN de la déf
    assert rows[0]["person"]["sex"] == "M"  # Homme
    assert rows[0]["person"]["marital_status"] == "married"  # Marié
    assert rows[1]["person"]["preferred_language"] == "French"  # nom FR → option EN
    assert rows[1]["person"]["sex"] == "F"  # Female
    assert rows[1]["person"]["marital_status"] == "widowed"  # veuve
    third = rows[2]
    assert "preferred_language" not in third["person"]  # klingon → trou
    assert "sex" not in third["person"]
    assert "marital_status" not in third["person"]
    assert len(third["issues"]) == 3  # trois issues motivées, la ligne vit


async def test_address_composition_contract(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Lot composition — le mapping PAR SOUS-CHAMP : l'objet s'assemble
    proprement (cyrillique compris), un sous-champ mauvais = retiré +
    signalé (le reste vit), les deux modes exclusifs par base → 422, le
    texte intégral conservé pour les colonnes complètes."""
    headers = agent_headers(admin)
    csv_text = (
        "Prénom,Nom,Email,Rue,Ville,CP,PaysAdr\n"
        "Compo,Sée,compo@example.com,ул. Витоша 15,София,1000,BG\n"
        "Mauvais,Pays,mauvais-pays@example.com,1 rue A,Paris,75001,Francia\n"
    )
    mapping = {
        "Prénom": "first_name",
        "Nom": "last_name",
        "Email": "email",
        "Rue": "residence_address.street",
        "Ville": "residence_address.city",
        "CP": "residence_address.postal_code",
        "PaysAdr": "residence_address.country",
    }
    r = await client.post(
        "/imports/client-profiles/preview",
        headers=headers,
        json={"csv_text": csv_text, "mapping": mapping},
    )
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    assert rows[0]["person"]["residence_address"] == {
        "street": "ул. Витоша 15",
        "city": "София",
        "postal_code": "1000",
        "country": "BG",
    }
    # 'Francia' ≠ ISO-2 → le sous-champ pays tombe, LE RESTE s'assemble.
    assert rows[1]["person"]["residence_address"] == {
        "street": "1 rue A",
        "city": "Paris",
        "postal_code": "75001",
    }
    assert {"column": "PaysAdr", "code": "invalid_value"} in rows[1]["issues"]

    # EXCLUSIVITÉ : texte intégral + sous-champ sur la même base → 422.
    r = await client.post(
        "/imports/client-profiles/preview",
        headers=headers,
        json={
            "csv_text": csv_text,
            "mapping": {**mapping, "CP": "residence_address"},
        },
    )
    assert r.status_code == 422
    assert r.json()["code"] == "import.address_mode_conflict"

    # L'import réel écrit l'objet assemblé, déclaration à la volée comprise.
    r = await client.post(
        "/imports/client-profiles",
        headers=headers,
        json={"csv_text": csv_text, "mapping": mapping},
    )
    assert r.status_code == 200, r.text
    listing = (await client.get("/client-profiles?search=compo@", headers=headers)).json()
    detail = (
        await client.get(f"/client-profiles/{listing['items'][0]['id']}", headers=headers)
    ).json()
    assert detail["custom_fields"]["residence_address"]["city"] == "София"


async def test_street_number_pair_contract(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Micro-lot couple — rue + numéro, l'exception déclarée : DEUX
    colonnes vers <base>.street s'assemblent en ordre fixe « {numéro}
    {rue} » (le numéro reconnu à son en-tête, pas à l'ordre du mapping),
    une seule vide = l'autre passe seule ; la garde tient (le couple n'est
    pas un collage libre) ; le suggéreur ne propose jamais le numéro seul ;
    la charge suggérée passe le preview (la règle structurelle)."""
    headers = agent_headers(admin)
    csv_text = (
        "Prénom,Nom,Adresse e-mail,Rue,Numéro de la rue,Ville\n"
        "Иван,Петров,ivan.pair@example.com,Екзарх Йосиф,93,София\n"
        "Sans,Numéro,sans-num@example.com,Rue de la Paix,,Paris\n"
        "Sans,Rue,sans-rue@example.com,,12,Lyon\n"
    )
    # Le suggéreur : le couple converge vers le MÊME street.
    r = await client.post(
        "/imports/client-profiles/suggest-mapping",
        headers=headers,
        json={"headers": ["Prénom", "Nom", "Adresse e-mail", "Rue", "Numéro de la rue", "Ville"]},
    )
    assert r.status_code == 200, r.text
    s = r.json()["suggestions"]
    assert s["Rue"] == "residence_address.street"
    assert s["Numéro de la rue"] == "residence_address.street"
    # La charge suggérée passe le preview — la règle structurelle.
    mapping = {**s, "Prénom": "first_name", "Nom": "last_name", "Adresse e-mail": "email"}
    r = await client.post(
        "/imports/client-profiles/preview",
        headers=headers,
        json={"csv_text": csv_text, "mapping": mapping},
    )
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    # Ordre fixe « {numéro} {rue} » — le mapping déclare pourtant Rue avant.
    assert rows[0]["person"]["residence_address"]["street"] == "93 Екзарх Йосиф"
    assert rows[0]["issues"] == []
    # Une seule vide = l'autre passe seule.
    assert rows[1]["person"]["residence_address"]["street"] == "Rue de la Paix"
    assert rows[2]["person"]["residence_address"]["street"] == "12"

    # LA GARDE : deux colonnes vers un sous-champ HORS street → 422.
    r = await client.post(
        "/imports/client-profiles/preview",
        headers=headers,
        json={"csv_text": csv_text, "mapping": {**mapping, "Prénom": "residence_address.city"}},
    )
    assert r.status_code == 422
    assert r.json()["code"] == "import.address_subfield_pair_exceeded"
    # Trois colonnes vers street → 422 (l'exception est un COUPLE).
    r = await client.post(
        "/imports/client-profiles/preview",
        headers=headers,
        json={"csv_text": csv_text, "mapping": {**mapping, "Ville": "residence_address.street"}},
    )
    assert r.status_code == 422
    assert r.json()["code"] == "import.address_subfield_pair_exceeded"

    # Le numéro SEUL (pas de colonne rue) n'est jamais suggéré.
    r = await client.post(
        "/imports/client-profiles/suggest-mapping",
        headers=headers,
        json={"headers": ["Adresse e-mail", "Numéro de la rue", "Ville"]},
    )
    assert "Numéro de la rue" in r.json()["unmatched"]

    # Miroir société : le couple converge vers address.street et s'assemble.
    r = await client.post(
        "/imports/company-profiles/suggest-mapping",
        headers=headers,
        json={"headers": ["Nom de l'entreprise", "Rue", "Numéro de la rue"]},
    )
    assert r.status_code == 200, r.text
    cs = r.json()["suggestions"]
    assert cs["Rue"] == "address.street"
    assert cs["Numéro de la rue"] == "address.street"
    r = await client.post(
        "/imports/company-profiles/preview",
        headers=headers,
        json={
            "csv_text": ("Société,Rue,Numéro de la rue\nАкме ООД,Екзарх Йосиф,93\n"),
            "mapping": {
                "Société": "name",
                "Rue": "address.street",
                "Numéro de la rue": "address.street",
            },
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["rows"][0]["person"]["address"]["street"] == "93 Екзарх Йосиф"


async def test_suggest_offers_address_subfields(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Lot composition — le suggéreur propose les sous-champs pour les
    fragments, la 3e lecture de Pays entre dans l'ambiguë, et « adresse
    postale » complète garde le texte intégral."""
    headers = agent_headers(admin)
    r = await client.post(
        "/imports/client-profiles/suggest-mapping",
        headers=headers,
        json={"headers": CONTACT_HEADERS_42},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    s = body["suggestions"]
    assert s["Rue"] == "residence_address.street"
    assert s["Ville"] == "residence_address.city"
    assert s["Code postal"] == "residence_address.postal_code"
    assert s["adresse postale"] == "residence_address"  # texte intégral conservé
    assert body["ambiguous"]["Pays"] == [
        "nationality",
        "tax_residence_country",
        "residence_address.country",
    ]
    # LE COUPLE (l'exception déclarée) : le numéro rejoint le MÊME street
    # que la rue — deux lignes convergentes, la grammaire du sous-groupe.
    assert s["Numéro de la rue"] == "residence_address.street"


async def test_audit_alias_pack_suggestions(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Audit catalogue — le pack alias : le vocabulaire HubSpot/Pipedrive
    accroche les cibles EXISTANTES (Date of Birth était muet !), Gender
    gagne `sex` sur Salutation (repli), le domaine ne vole pas l'URL
    pleine, et les exclusions EN gravées restent muettes (la frontière)."""
    headers = agent_headers(admin)
    r = await client.post(
        "/imports/client-profiles/suggest-mapping",
        headers=headers,
        json={
            "headers": [
                "First Name",
                "Last Name",
                "Email",
                "Salutation",
                "Gender",
                "Date of Birth",
                "Preferred Language",
                "Company Name",
                "Work Email",
                "Degree",
                "School",
                "Field of Study",
                "Label",
                "LinkedIn URL",
                "Website URL",
                "Industry",
                "Country/Region",
                "Relationship Status",
                "Number of Employees",
                "Annual Revenue",
            ]
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    s = body["suggestions"]
    expected = {
        "Gender": "sex",
        "Date of Birth": "date_of_birth",
        "Preferred Language": "preferred_lang",  # dédup : LA COLONNE
        "Company Name": "employer",
        "Work Email": "secondary_email",
        "Degree": "education_level",
        "School": "last_institution",
        "Field of Study": "field_of_study",
        "Label": "tags",
        "LinkedIn URL": "linkedin_url",
        "Website URL": "website",
        "Industry": "industry",
    }
    for header, target in expected.items():
        assert s.get(header) == target, f"{header}: {s.get(header)} != {target}"
    # Salutation est un REPLI : la colonne Genre directe a pris la cible.
    assert "Salutation" not in s
    # Country/Region : la même ambiguïté à trois lectures que Pays.
    assert body["ambiguous"]["Country/Region"] == [
        "nationality",
        "tax_residence_country",
        "residence_address.country",
    ]
    # Les exclusions gravées (la frontière visible) — jamais suggérées.
    for trap in ("Relationship Status", "Number of Employees", "Annual Revenue"):
        assert trap not in s, trap

    # Civilité SEULE (fichiers FR sans colonne Genre) : le repli la prend.
    r = await client.post(
        "/imports/client-profiles/suggest-mapping",
        headers=headers,
        json={"headers": ["Civilité", "Prénom"]},
    )
    assert r.json()["suggestions"]["Civilité"] == "sex"

    r = await client.post(
        "/imports/company-profiles/suggest-mapping",
        headers=headers,
        json={
            "headers": [
                "Nom",
                "Date de création",
                "Company Domain Name",
                "Website URL",
                "Phone Number",
                "Employee Range",
                "Year Founded",
                "Annual Revenue",
                "Type",
                "Description",
                "LinkedIn Company Page",
                "Fax",
            ]
        },
    )
    assert r.status_code == 200, r.text
    s = r.json()["suggestions"]
    assert s["Date de création"] == "registration_date"  # verdict : ALIAS, pas de preset
    assert s["Phone Number"] == "phone"
    # Le domaine est un REPLI : l'URL pleine gagne quand les deux existent.
    assert s["Website URL"] == "website"
    assert "Company Domain Name" not in s
    for trap in (
        "Employee Range",
        "Year Founded",
        "Annual Revenue",
        "Type",
        "Description",
        "LinkedIn Company Page",
        "Fax",
    ):
        assert trap not in s, trap


async def test_company_number_targets_coerced_on_real_values(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Audit catalogue — effectif/capital coercés en NUMBER à la naissance
    (la règle « suggérable = coerçable ») : « 12 » → 12, « 10 000 € » →
    10000, « 51-200 » (la plage HubSpot) = issue + trou motivé, JAMAIS un
    500 ; industry/activity_code rangés en section situation."""
    headers = agent_headers(admin)
    csv_text = (
        "Nom,Effectif,Capital social,Secteur,Code APE\n"
        "Acme BG,12,10 000 €,Domiciliation,70.22Z\n"
        "Range Corp,51-200,dix mille,Conseil,\n"
    )
    mapping = {
        "Nom": "name",
        "Effectif": "employee_count",
        "Capital social": "share_capital",
        "Secteur": "industry",
        "Code APE": "activity_code",
    }
    r = await client.post(
        "/imports/company-profiles/preview",
        headers=headers,
        json={"csv_text": csv_text, "mapping": mapping},
    )
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    first = rows[0]["person"]
    assert first["employee_count"] == 12  # un NOMBRE, pas la chaîne
    assert first["share_capital"] == 10000  # séparateur de milliers + devise ôtés
    assert first["industry"] == "Domiciliation"
    assert first["activity_code"] == "70.22Z"
    assert rows[0]["issues"] == []
    # La plage et le littéral : deux trous MOTIVÉS, la ligne vit (create).
    second = rows[1]
    assert second["status"] == "create"
    assert {"column": "Effectif", "code": "invalid_value"} in second["issues"]
    assert {"column": "Capital social", "code": "invalid_value"} in second["issues"]
    assert "employee_count" not in second["person"]
    assert "share_capital" not in second["person"]

    # L'import réel range les nombres dans la fiche, section situation.
    r = await client.post(
        "/imports/company-profiles",
        headers=headers,
        json={"csv_text": csv_text, "mapping": mapping},
    )
    assert r.status_code == 200, r.text
    assert len(r.json()["created"]) == 2
    company_id = r.json()["created"][0]["company_profile_id"]
    detail = (await client.get(f"/company-profiles/{company_id}", headers=headers)).json()
    assert detail["custom_fields"]["employee_count"] == 12
    assert detail["custom_fields"]["share_capital"] == 10000
    by_key = {s["key"]: s["references"] for s in detail["sections"]}
    assert "industry" in by_key["situation"]
    assert "employee_count" in by_key["situation"]
    assert "activity_code" in by_key["situation"]


async def test_online_presence_presets_declared_on_the_fly(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Audit catalogue — website/linkedin_url entrent ENTIERS : mappés non
    déclarés, la déf naît à l'import (scope person, section contact de la
    taxonomie, type text) et la fiche les sert en section contact."""
    headers = agent_headers(admin)
    body = {
        "csv_text": (
            "Prénom,Nom,Email,Site web,LinkedIn\n"
            "Enligne,Présence,enligne@example.com,"
            "https://enligne.example.com,https://linkedin.com/in/enligne\n"
        ),
        "mapping": {
            "Prénom": "first_name",
            "Nom": "last_name",
            "Email": "email",
            "Site web": "website",
            "LinkedIn": "linkedin_url",
        },
    }
    r = await client.post("/imports/client-profiles", headers=headers, json=body)
    assert r.status_code == 200, r.text
    for key in ("website", "linkedin_url"):
        row = (
            await db_session.execute(
                text(
                    "SELECT scope, profile_section, field_type FROM custom_field_definition "
                    f"WHERE key = '{key}'"
                )
            )
        ).first()
        assert list(row) == ["person", "contact", "text"], key
    listing = (await client.get("/client-profiles?search=enligne@", headers=headers)).json()
    detail = (
        await client.get(f"/client-profiles/{listing['items'][0]['id']}", headers=headers)
    ).json()
    assert detail["custom_fields"]["website"] == "https://enligne.example.com"
    assert detail["custom_fields"]["linkedin_url"] == "https://linkedin.com/in/enligne"
    by_key = {s["key"]: s["references"] for s in detail["sections"]}
    assert "website" in by_key["contact"]
    assert "linkedin_url" in by_key["contact"]


async def test_create_field_from_grid(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Lot grille — le champ naît de l'import (cœur sans commit du
    picker) : scope person, section misc, kind qui coerce dès la
    naissance (date illisible → trou motivé) ; le preview le PRÉDIT sans
    RIEN écrire ; la dédup lie au second import — UN champ, pas deux."""
    headers = agent_headers(admin)
    body = {
        "csv_text": (
            "Prénom,Nom,Email,NUM IRINA,Fin contrat\n"
            "Grille,Une,grille@example.com,IR-2231,2027-01-15\n"
            "Grille,Deux,grille2@example.com,IR-8890,pas-une-date\n"
        ),
        "mapping": {"Prénom": "first_name", "Nom": "last_name", "Email": "email"},
        "create_fields": [
            {"column": "NUM IRINA", "label": "NUM IRINA", "kind": "text"},
            {"column": "Fin contrat", "label": "End contrat Dom", "kind": "date"},
        ],
    }
    # PREVIEW : prédiction sans écriture.
    r = await client.post("/imports/client-profiles/preview", headers=headers, json=body)
    assert r.status_code == 200, r.text
    preview = r.json()
    assert sorted(preview["fields_created"]) == ["End contrat Dom", "NUM IRINA"]
    assert preview["rows"][0]["person"]["num_irina"] == "IR-2231"
    assert str(preview["rows"][0]["person"]["end_contrat_dom"]).startswith("2027-01-15")
    # kind date → l'illisible fait un TROU motivé, la ligne vit.
    assert {"column": "Fin contrat", "code": "invalid_value"} in preview["rows"][1]["issues"]
    n_defs = (
        await db_session.execute(
            text(
                "SELECT count(*) FROM custom_field_definition "
                "WHERE key IN ('num_irina', 'end_contrat_dom')"
            )
        )
    ).scalar_one()
    assert n_defs == 0  # le dry-run n'a RIEN créé

    # IMPORT : la naissance — scope person, section misc, valeurs posées.
    r = await client.post("/imports/client-profiles", headers=headers, json=body)
    assert r.status_code == 200, r.text
    assert sorted(r.json()["fields_created"]) == ["End contrat Dom", "NUM IRINA"]
    rows = (
        await db_session.execute(
            text(
                "SELECT key, scope, profile_section, field_type FROM custom_field_definition "
                "WHERE key IN ('num_irina', 'end_contrat_dom') ORDER BY key"
            )
        )
    ).all()
    assert [list(r_) for r_ in rows] == [
        ["end_contrat_dom", "person", "misc", "date"],
        ["num_irina", "person", "misc", "text"],
    ]
    listing = (await client.get("/client-profiles?search=grille@", headers=headers)).json()
    detail = (
        await client.get(f"/client-profiles/{listing['items'][0]['id']}", headers=headers)
    ).json()
    assert detail["custom_fields"]["num_irina"] == "IR-2231"

    # DÉDUP : le second import LIE (même label → même champ), zéro né.
    r = await client.post("/imports/client-profiles", headers=headers, json=body)
    assert r.status_code == 200, r.text
    assert r.json()["fields_created"] == []  # rien ne naît deux fois
    n_defs = (
        await db_session.execute(
            text("SELECT count(*) FROM custom_field_definition WHERE key = 'num_irina'")
        )
    ).scalar_one()
    assert n_defs == 1  # UN champ, pas deux

    # SOCIÉTÉ : la clé de sack naît coercée, rangée en misc.
    r = await client.post(
        "/imports/company-profiles",
        headers=headers,
        json={
            "csv_text": "Nom,Note interne\nGrilleCo,42\n",
            "mapping": {"Nom": "name"},
            "create_fields": [
                {"column": "Note interne", "label": "Note interne", "kind": "number"}
            ],
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["fields_created"] == ["Note interne"]
    listing = (await client.get("/company-profiles?search=GrilleCo", headers=headers)).json()
    detail = (
        await client.get(f"/company-profiles/{listing['items'][0]['id']}", headers=headers)
    ).json()
    assert detail["custom_fields"]["note_interne"] == 42  # coercé number
    by_key = {s["key"]: s["references"] for s in detail["sections"]}
    assert "note_interne" in by_key["misc"]


async def test_referential_dedup_migrates_the_three_cases(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Lot dédup — LA fonction de migration sur données seedées : les
    valeurs housing_address migrent vers residence_address (conflit même
    ligne → le survivant gagne, compté), les langues du preset montent
    dans LA COLONNE (normalisées), les défs fusionnent, zéro orpheline."""
    from src.imports.referential_dedup import dedup_referential

    headers = agent_headers(admin)
    # Seed : une fiche avec housing_address + preferred_language au sack,
    # une avec CONFLIT (les deux adresses), les défs des deux presets.
    r = await client.post(
        "/client-profiles",
        headers=headers,
        json={"first_name": "Dédup", "last_name": "Un", "email": "dedup1@example.com"},
    )
    p1 = r.json()["id"]
    r = await client.post(
        "/client-profiles",
        headers=headers,
        json={"first_name": "Dédup", "last_name": "Deux", "email": "dedup2@example.com"},
    )
    p2 = r.json()["id"]
    await db_session.execute(
        text(
            "UPDATE client_profile SET custom_fields = "
            '\'{"housing_address": {"street": "12 rue A"}, '
            '"preferred_language": "Français"}\'::jsonb '
            "WHERE id = :i"
        ),
        {"i": p1},
    )
    await db_session.execute(
        text(
            "UPDATE client_profile SET custom_fields = "
            '\'{"housing_address": {"street": "perdante"}, '
            '"residence_address": {"street": "survivante"}}\'::jsonb WHERE id = :i'
        ),
        {"i": p2},
    )
    for key in ("housing_address", "preferred_language"):
        db_session.add(
            CustomFieldDefinition(
                agency_id=admin.agency_id, key=key, label=key, field_type="text", scope="person"
            )
        )
    db_session.add(
        CustomFieldDefinition(
            agency_id=admin.agency_id,
            key="residence_address",
            label="residence_address",
            field_type="address",
            scope="person",
        )
    )
    await db_session.commit()

    stats = await db_session.run_sync(lambda s_: dedup_referential(s_.connection()))
    await db_session.commit()
    assert stats["client_profile_address_values_moved"] == 1
    assert stats["client_profile_address_conflicts_survivor_kept"] == 1
    assert stats["language_values_moved_from_fiche_sack"] == 1
    assert stats["address_defs_merged"] == 1  # housing meurt (residence existait)
    assert stats["language_defs_deleted"] == 1

    # Zéro orpheline : plus aucune clé morte, les valeurs au bon endroit.
    detail = (await client.get(f"/client-profiles/{p1}", headers=headers)).json()
    assert detail["custom_fields"]["residence_address"] == {"street": "12 rue A"}
    assert "housing_address" not in detail["custom_fields"]
    assert detail["preferred_lang"] == "fr"  # normalisée vers LA COLONNE
    detail = (await client.get(f"/client-profiles/{p2}", headers=headers)).json()
    assert detail["custom_fields"]["residence_address"] == {"street": "survivante"}
    n = (
        await db_session.execute(
            text(
                "SELECT count(*) FROM custom_field_definition "
                "WHERE key IN ('housing_address', 'preferred_language')"
            )
        )
    ).scalar_one()
    assert n == 0
    # IDEMPOTENT : rejouer = zéro mouvement.
    stats2 = await db_session.run_sync(lambda s_: dedup_referential(s_.connection()))
    await db_session.commit()
    assert stats2["client_profile_address_values_moved"] == 0
    assert stats2["language_values_moved_from_fiche_sack"] == 0
