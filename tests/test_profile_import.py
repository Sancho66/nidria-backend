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
    email mais AVEC identité → CRÉÉE (lot email optionnel) ; sans identité
    → ignorée ; le rapport dit tout, ventilé avec/sans email.
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
    # « Sans Email » (identité complète, pas d'email) est CRÉÉE — plus
    # jamais ignorée ; la ventilation le dit à l'agence.
    assert [c["email"] for c in report["created"]] == ["nouvelle@example.com", None]
    assert report["created_with_email"] == 1
    assert report["created_without_email"] == 1
    assert [x["profile_id"] for x in report["linked"]] == [existing_id]
    assert [i["reason"] for i in report["ignored"]] == ["missing_identity"]
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
    # « Sans Identité » porte en fait une identité complète (prénom+nom) :
    # sans email, elle CRÉE désormais au lieu d'être ignorée.
    assert [c["email"] for c in rep["created"]] == [
        "kevin@example.com",
        "long@example.com",
        None,
    ]
    assert len(rep["linked"]) == 1  # le doublon intra-batch LIE (casse ignorée)
    assert rep["ignored"] == []
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
        "create": 3,  # « Sans Email » crée désormais (identité complète)
        "link": 2,
        "ignore": 0,
        "ignore_reasons": {},
        "create_with_email": 2,
        "create_without_email": 1,
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
    assert n_before == n_after - 3  # le preview n'avait RIEN écrit
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
    """Corrections — appliquées après parse, avant validation : l'identité
    manquante corrigée fait passer la ligne d'ignore à create (et l'email
    posé au passage la range dans les créées AVEC email) ; une correction
    invalide = motivée, jamais un 500."""
    headers = agent_headers(admin)
    # Sans NOM ni email il ne reste rien : missing_identity (le motif
    # no_email est mort — une identité complète suffit désormais à créer).
    csv_text = "Prénom,Nom,Email,Genre\nCorrigée,,,unknown\n"
    mapping = {"Prénom": "first_name", "Nom": "last_name", "Email": "email", "Genre": "sex"}
    # SANS correction : ignore missing_identity.
    r = await client.post(
        "/imports/client-profiles/preview",
        headers=headers,
        json={"csv_text": csv_text, "mapping": mapping},
    )
    assert r.json()["rows"][0]["status"] == "ignore"
    assert r.json()["rows"][0]["reason"] == "missing_identity"
    # AVEC corrections : nom + email posés (ignore → create) + genre corrigé
    # valide + une correction à cible inconnue (issue motivée, pas de 500).
    body = {
        "csv_text": csv_text,
        "mapping": mapping,
        "corrections": [
            {"row_index": 1, "target": "last_name", "value": "Cliente"},
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
        # LE SUGGÉREUR A DÉJÀ TRANCHÉ : les fragments gagnent, la colonne
        # texte intégral n'est PAS suggérée (elle reste libre, l'agence
        # peut basculer). Plus aucun arbitrage manuel avant le preview —
        # avant le correctif, cette ligne faisait un `pop` sans quoi tout
        # l'import partait en 422 `import.address_mode_conflict`.
        assert "adresse postale" not in mapping
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
    # partout via les 21 ; residence_address idem). Une déf DÉCLARÉE porte
    # le type de son preset — `residence_address` est une ADRESSE : c'est
    # ce type qui ouvre ses sous-champs, à la suggestion comme à l'import
    # (source unique `import_targets` ; une déf déclarée `text` fermerait
    # la composition des DEUX côtés, cohéremment).
    for key, kind in (("preferred_language", "text"), ("residence_address", "address")):
        db_session.add(
            CustomFieldDefinition(
                agency_id=admin.agency_id, key=key, label=key, field_type=kind, scope="person"
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
    # Lot plafond : TVA contact → tax_id (le NIF de la personne).
    expected_person["Numéro de TVA du contact"] = "tax_id"
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
    # LA MAISON DU MOBILE (correctif a) : « Téléphone » tient `phone`, le
    # repli descend d'un cran au lieu de se taire — 235 contacts du fichier
    # réel n'avaient AUCUN numéro à cause de ce silence.
    assert person["Mobile"] == "secondary_phone"
    # Re-verdict 03/08 (demande design A) : le Siret d'un CONTACT est une
    # donnée société — l'univers société a quitté la fiche personne, la
    # colonne n'est plus suggérée (l'import sociétés la porte toujours).
    assert "Numéro d'identification national (Siret)" not in person
    assert "Numéro d'identification national (Siret)" not in r.json()["ambiguous"]

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
    assert list(row) == ["person", "identity", "select"] or list(row)[:2] == [
        "person",
        "identity",  # fusion id_documents → identity (taxonomie à 4)
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
    assert "visa_type" in by_key["identity"]  # fusion id_documents → identity


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
    # La RUE seule passe seule (une rue sans numéro reste une adresse).
    assert rows[1]["person"]["residence_address"]["street"] == "Rue de la Paix"
    assert rows[1]["issues"] == []
    # LE NUMÉRO SEUL, LIGNE À LIGNE (correctif b) : la garde « jamais un
    # numéro seul » ne vivait qu'au niveau COLONNE — une LIGNE sans nom de
    # rue produisait street="12". C'est un trou motivé, plus un street ;
    # le reste de l'adresse (la ville) vit.
    assert "street" not in rows[2]["person"]["residence_address"]
    assert rows[2]["person"]["residence_address"]["city"] == "Lyon"
    assert rows[2]["issues"] == [
        {"column": "Numéro de la rue + Rue", "code": "street_number_orphan"}
    ]

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
    fragments et la 3e lecture de Pays entre dans l'ambiguë. « adresse
    postale » NE prend PAS le texte intégral : ses fragments existent, et
    les deux modes d'une même base sont exclusifs à l'import — le
    suggéreur tranche pour les morceaux (cf. la garantie structurelle)."""
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
    assert "adresse postale" not in s  # écartée : ses fragments gagnent
    assert "adresse postale" in body["unmatched"]  # …mais laissée LIBRE
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


async def test_company_theme_not_a_person_import_target(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Demande design A (03/08) — l'univers société a quitté la fiche
    personne : ses clés ne sont plus des cibles d'import PERSONNE (422
    nommé, même DÉCLARÉES chez l'agence), et le suggéreur ne les propose
    plus (jamais une combinaison que l'import rejette)."""
    headers = agent_headers(admin)
    db_session.add(
        CustomFieldDefinition(
            agency_id=admin.agency_id,
            key="legal_form",
            label="Forme juridique",
            field_type="text",
            scope="person",
            profile_section="situation",
        )
    )
    await db_session.commit()
    r = await client.post(
        "/imports/client-profiles/preview",
        headers=headers,
        json={
            "csv_text": "Email,Forme\na@example.com,SARL\n",
            "mapping": {"Email": "email", "Forme": "legal_form"},
        },
    )
    assert r.status_code == 422
    assert r.json()["code"] == "import.unknown_targets"
    # La déf déclarée « Forme juridique » reste muette côté personne —
    # ni suggestion directe, ni vocabulaire dynamique.
    r = await client.post(
        "/imports/client-profiles/suggest-mapping",
        headers=headers,
        json={"headers": ["Forme juridique", "Adresse e-mail"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["suggestions"] == {"Adresse e-mail": "email"}
    assert "Forme juridique" in r.json()["unmatched"]


async def test_company_sack_labels_survive_import_creation(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Demande design A (03/08) — le label choisi à la création depuis la
    grille SOCIÉTÉ ne se perd plus : gravé en table de labels d'AGENCE
    (une vérité par clé, le kind de naissance voyage), servi sur la fiche
    (field_labels), re-suggéré au ré-import, dédup lier-pas-dupliquer,
    coercition par le kind de naissance en mapping direct. Le preview
    n'écrit RIEN."""
    headers = agent_headers(admin)
    body = {
        "csv_text": "Nom,Note du juriste\nLabelCo,42\n",
        "mapping": {"Nom": "name"},
        "create_fields": [
            {"column": "Note du juriste", "label": "Note du juriste", "kind": "number"}
        ],
    }
    # PREVIEW : zéro écriture — pas de label gravé.
    r = await client.post("/imports/company-profiles/preview", headers=headers, json=body)
    assert r.status_code == 200, r.text
    n_labels = (
        await db_session.execute(text("SELECT count(*) FROM company_field_definition"))
    ).scalar_one()
    assert n_labels == 0

    # IMPORT : le label naît au niveau AGENCE, kind de naissance gravé.
    r = await client.post("/imports/company-profiles", headers=headers, json=body)
    assert r.status_code == 200, r.text
    assert r.json()["fields_created"] == ["Note du juriste"]
    row = (
        await db_session.execute(
            text("SELECT key, label, field_type FROM company_field_definition WHERE key = :k"),
            {"k": "note_du_juriste"},
        )
    ).one()
    assert list(row) == ["note_du_juriste", "Note du juriste", "number"]
    # La fiche société SERT le label (le front n'affiche plus la clé nue).
    listing = (await client.get("/company-profiles?search=LabelCo", headers=headers)).json()
    detail = (
        await client.get(f"/company-profiles/{listing['items'][0]['id']}", headers=headers)
    ).json()
    # `field_labels` porte TOUTES les clés vivantes depuis le lot du 07/08
    # (les 17 presets se matérialisent avec leur libellé de catalogue) : ce
    # qui compte ici est que la clé BAPTISÉE à la grille garde son nom.
    assert detail["field_labels"]["note_du_juriste"] == "Note du juriste"
    assert detail["custom_fields"]["note_du_juriste"] == 42

    # RÉ-IMPORT : la colonne baptisée se RE-SUGGÈRE vers SA clé.
    r = await client.post(
        "/imports/company-profiles/suggest-mapping",
        headers=headers,
        json={"headers": ["Note du juriste"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["suggestions"] == {"Note du juriste": "note_du_juriste"}

    # DÉDUP : le même label re-créé LIE — rien ne naît deux fois, et le
    # kind de NAISSANCE coerce (le payload dit text, la table dit number).
    r = await client.post(
        "/imports/company-profiles",
        headers=headers,
        json={
            "csv_text": "Nom,Note du juriste\nLabelCo Bis,17\n",
            "mapping": {"Nom": "name"},
            "create_fields": [
                {"column": "Note du juriste", "label": "Note du juriste", "kind": "text"}
            ],
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["fields_created"] == []
    # LA DÉDUP SE COMPTE SUR LA CLÉ, pas sur la table : depuis le lot du
    # 07/08 l'import matérialise aussi les 17 presets société (il en offre
    # les cibles, elles doivent exister en définitions). Compter toute la
    # table mesurerait la matérialisation, pas ce qui est en jeu ici — que
    # la clé BAPTISÉE ne naisse pas une seconde fois.
    n_labels = (
        await db_session.execute(
            text("SELECT count(*) FROM company_field_definition WHERE key = :k"),
            {"k": "note_du_juriste"},
        )
    ).scalar_one()
    assert n_labels == 1
    listing = (await client.get("/company-profiles?search=LabelCo Bis", headers=headers)).json()
    detail = (
        await client.get(f"/company-profiles/{listing['items'][0]['id']}", headers=headers)
    ).json()
    assert detail["custom_fields"]["note_du_juriste"] == 17  # number, pas "17"

    # MAPPING DIRECT vers la clé : coercition par le kind de naissance —
    # l'illisible fait un TROU motivé, la ligne vit (règle absolue).
    r = await client.post(
        "/imports/company-profiles/preview",
        headers=headers,
        json={
            "csv_text": "Nom,Note du juriste\nLabelCo Ter,pas-un-nombre\n",
            "mapping": {"Nom": "name", "Note du juriste": "note_du_juriste"},
        },
    )
    assert r.status_code == 200, r.text
    first = r.json()["rows"][0]
    assert {"column": "Note du juriste", "code": "invalid_value"} in first["issues"]
    assert "note_du_juriste" not in first["person"]


# --- LOT EMAIL OPTIONNEL (parité avec la création manuelle) -------------------------------


async def test_email_optional_creates_and_dedups_by_identity_within_the_batch(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """L'email n'est plus obligatoire : identité seule = fiche CRÉÉE.
    La dédup suit la frontière — email présent, la clé d'email ; email
    absent, l'identité normalisée DANS LE BATCH SEULEMENT, JAMAIS contre
    la base (deux homonymes d'une agence peuvent être deux personnes).
    Le rapport ventile « avec email · sans email »."""
    headers = agent_headers(admin)
    # Un HOMONYME déjà en base, sans email : l'import ne doit PAS le
    # rejoindre — on ne fusionne pas des homonymes en silence.
    r = await client.post(
        "/client-profiles",
        headers=headers,
        json={"first_name": "Jean", "last_name": "Martin"},
    )
    assert r.status_code == 201, r.text
    homonym_id = r.json()["id"]

    csv_text = (
        "Prénom,Nom,Email,Téléphone\n"
        "Jean,Martin,,0601020304\n"  # homonyme de la base → CRÉE quand même
        "JEAN,  martin ,,0700000000\n"  # même identité normalisée → LIE
        "Gil,Dieu,,0611223344\n"  # identité seule → CRÉE
        "Avec,Email,avec@example.com,\n"  # la clé d'email, inchangée
        ",,,0699999999\n"  # ni nom ni email → il ne reste RIEN
    )
    mapping = {
        "Prénom": "first_name",
        "Nom": "last_name",
        "Email": "email",
        "Téléphone": "phone",
    }
    body = {"csv_text": csv_text, "mapping": mapping}

    preview = (
        await client.post("/imports/client-profiles/preview", headers=headers, json=body)
    ).json()
    assert preview["summary"] == {
        "create": 3,
        "link": 1,
        "ignore": 1,
        "ignore_reasons": {"missing_identity": 1},
        "create_with_email": 1,
        "create_without_email": 2,
    }

    r = await client.post("/imports/client-profiles", headers=headers, json=body)
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["created_with_email"] == 1
    assert report["created_without_email"] == 2
    assert [c["email"] for c in report["created"]] == [None, None, "avec@example.com"]
    # Le doublon d'identité a LIÉ la fiche née à la ligne 1 (pas dupliqué),
    # et sûrement PAS l'homonyme de la base.
    assert len(report["linked"]) == 1
    linked_id = report["linked"][0]["profile_id"]
    assert linked_id == report["created"][0]["profile_id"]
    assert linked_id != homonym_id
    assert [i["reason"] for i in report["ignored"]] == ["missing_identity"]

    # L'homonyme de la base est resté INTOUCHÉ (pas de fusion silencieuse).
    detail = (await client.get(f"/client-profiles/{homonym_id}", headers=headers)).json()
    assert detail["phone"] is None
    # Deux « Jean Martin » cohabitent : celui de la base, celui de l'import.
    listing = (await client.get("/client-profiles?search=Martin", headers=headers)).json()
    assert listing["total"] == 2
    # Le fill-gap du doublon intra-batch a tenu : 1re valeur gardée.
    imported = (await client.get(f"/client-profiles/{linked_id}", headers=headers)).json()
    assert imported["phone"] == "0601020304"
    assert not imported["email"]  # la fiche reste sans email (colonne NULL)
    n_null = (
        await db_session.execute(text("SELECT count(*) FROM client_profile WHERE email IS NULL"))
    ).scalar_one()
    assert n_null == 3  # l'homonyme de base + les 2 créées sans email


async def test_values_of_the_dropped_row_reach_the_sibling_profile(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Correctif c — une ligne ignorée pour `missing_identity` dont
    l'email crée une fiche PAR UNE AUTRE ligne n'emporte plus ses valeurs
    dans la tombe (2 téléphones perdus sur le fichier Teamleader réel) :
    report en FILL-ONLY, la ligne reste ignorée mais dit où c'est parti."""
    headers = agent_headers(admin)
    # Le cas réel `distri-24h@outlook.fr` : ligne 1 sans prénom MAIS avec
    # le téléphone, ligne 2 complète et muette sur le numéro.
    csv_text = (
        "Prénom,Nom,Email,Téléphone,Fonction\n"
        ",K,distri@example.com,+33 6 20 51 64 85,Gérant\n"
        "Karim,Rekad,distri@example.com,,\n"
    )
    body = {
        "csv_text": csv_text,
        "mapping": {
            "Prénom": "first_name",
            "Nom": "last_name",
            "Email": "email",
            "Téléphone": "phone",
            "Fonction": "profession",
        },
    }
    r = await client.post("/imports/client-profiles", headers=headers, json=body)
    assert r.status_code == 200, r.text
    report = r.json()
    assert [c["email"] for c in report["created"]] == ["distri@example.com"]
    created_id = report["created"][0]["profile_id"]
    # La ligne 1 reste IGNORÉE (elle n'a créé aucune fiche)…
    assert [i["reason"] for i in report["ignored"]] == ["missing_identity"]
    # …mais son `profile_id` dit où sa donnée est allée, et le rapport compte.
    assert report["ignored"][0]["profile_id"] == created_id
    assert report["values_salvaged"] == 1
    detail = (await client.get(f"/client-profiles/{created_id}", headers=headers)).json()
    assert detail["phone"] == "+33 6 20 51 64 85"  # sauvé de la ligne jetée
    assert detail["profession"] == "Gérant"
    assert detail["first_name"] == "Karim"  # l'identité vient de la ligne 2


async def test_mobile_lands_in_secondary_phone_end_to_end(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Correctif a — le repli descend d'un cran : `Téléphone` tient
    `phone`, `Mobile` prend `secondary_phone` au lieu de se taire. Le
    preset entre ENTIER : suggéré, coercé, déclaré à la volée, posé."""
    headers = agent_headers(admin)
    r = await client.post(
        "/imports/client-profiles/suggest-mapping",
        headers=headers,
        json={"headers": ["Prénom", "Nom", "Adresse e-mail", "Téléphone", "Mobile"]},
    )
    assert r.status_code == 200, r.text
    s = r.json()["suggestions"]
    assert s["Téléphone"] == "phone"
    assert s["Mobile"] == "secondary_phone"
    # SANS colonne Téléphone, le 1er cran est libre : Mobile reprend `phone`.
    r = await client.post(
        "/imports/client-profiles/suggest-mapping",
        headers=headers,
        json={"headers": ["Prénom", "Nom", "Adresse e-mail", "Mobile"]},
    )
    assert r.json()["suggestions"]["Mobile"] == "phone"

    # La charge suggérée passe l'import réel — la règle structurelle.
    r = await client.post(
        "/imports/client-profiles",
        headers=headers,
        json={
            "csv_text": (
                "Prénom,Nom,Adresse e-mail,Téléphone,Mobile\n"
                "Deux,Numéros,deux@example.com,0102030405,+33 6 11 22 33 44\n"
            ),
            "mapping": {**s, "Prénom": "first_name", "Nom": "last_name"},
        },
    )
    assert r.status_code == 200, r.text
    profile_id = r.json()["created"][0]["profile_id"]
    detail = (await client.get(f"/client-profiles/{profile_id}", headers=headers)).json()
    assert detail["phone"] == "0102030405"
    assert detail["custom_fields"]["secondary_phone"] == "+33 6 11 22 33 44"


async def test_agency_config_accepts_the_whole_import_universe(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """LE BUG SIGNALÉ + SA GARDE STRUCTURELLE — `POST /imports/mappings`
    refusait `residence_address.street` : une agence composait une adresse
    dans le wizard sans pouvoir ENREGISTRER la correspondance. La cause
    était la DUPLICATION de la règle (trois copies). Preuve que la
    duplication ne peut plus revenir : l'univers est calculé par LA
    fonction partagée (`import_targets.person_targets`) et envoyé EN
    ENTIER à la config — toute cible que l'import accepte s'enregistre."""
    from src.imports.import_targets import person_targets

    headers = agent_headers(admin)
    # Une agence réelle : un custom libre + une base adresse DÉCLARÉE
    # (l'univers doit porter ses sous-champs, pas seulement ceux du
    # catalogue).
    for key, kind in (("num_dossier", "text"), ("adresse_bureau", "address")):
        db_session.add(
            CustomFieldDefinition(
                agency_id=admin.agency_id,
                key=key,
                label=key,
                field_type=kind,
                scope="person",
            )
        )
    await db_session.commit()

    # LA CIBLE DU BUG, nommée : elle s'enregistre maintenant.
    r = await client.post(
        "/imports/mappings",
        headers=headers,
        json={
            "crm_slug": "custom",
            "custom_crm_name": "Mon vieux CRM",
            "name": "Adresse composée",
            "mapping": {
                "Email": "email",
                "Rue": "residence_address.street",
                "Ville": "residence_address.city",
                "CP": "residence_address.postal_code",
                "Pays": "residence_address.country",
                "Bureau rue": "adresse_bureau.street",
                "Langue": "preferred_lang",
                "Étiquettes": "tags",
                "Passeport exp.": "passport_expiry",  # preset du catalogue NON déclaré
            },
        },
    )
    assert r.status_code in (200, 201), r.text

    # L'ÉGALITÉ DES UNIVERS : tout ce que l'import accepte, la config
    # l'enregistre — la liste vient de la source, elle ne peut pas dater.
    universe = sorted((await person_targets(db_session, admin.agency_id)).valid)
    assert "residence_address.street" in universe
    assert "adresse_bureau.postal_code" in universe  # base DÉCLARÉE, pas catalogue
    r = await client.post(
        "/imports/mappings",
        headers=headers,
        json={
            "crm_slug": "custom",
            "custom_crm_name": "Mon vieux CRM",
            "name": "Univers entier",
            "mapping": {f"col{i}": target for i, target in enumerate(universe)},
        },
    )
    assert r.status_code in (200, 201), (
        f"la config refuse des cibles que l'import accepte : {r.text}"
    )

    # LE TROISIÈME CÔTÉ DU TRIANGLE : le SUGGÉREUR puise au même univers —
    # il ne peut donc proposer ni offrir en ambiguïté une cible que
    # l'import refuserait (la plainte du front : « il propose une
    # correspondance que son propre import rejette »).
    r = await client.post(
        "/imports/client-profiles/suggest-mapping",
        headers=headers,
        json={"headers": CONTACT_HEADERS_42},
    )
    assert r.status_code == 200, r.text
    offered = set(r.json()["suggestions"].values()) | {
        t for options in r.json()["ambiguous"].values() for t in options
    }
    assert offered <= set(universe), f"suggérées hors univers : {offered - set(universe)}"

    # ET LA FRONTIÈRE TIENT : hors univers → 422 nommé, dans les deux
    # sens (une clé inventée, et l'univers société écarté de la personne).
    for bad_target in ("montant_facture", "legal_form", "residence_address.province"):
        r = await client.post(
            "/imports/mappings",
            headers=headers,
            json={
                "crm_slug": "custom",
                "custom_crm_name": "Mon vieux CRM",
                "name": f"Refus {bad_target}",
                "mapping": {"Email": "email", "X": bad_target},
            },
        )
        assert r.status_code == 422, bad_target
        assert r.json()["code"] == "import.unknown_targets"
        assert bad_target in r.json()["params"]["targets"]


async def test_company_import_and_suggest_share_one_universe(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Le miroir société : suggéreur et import lisent LA MÊME source
    (`company_targets`) — le suggéreur ne peut donc pas proposer une
    combinaison que l'import rejette. Constat nommé au rapport : il
    n'existe PAS de config société au contrat (une config est personne,
    `journey_template_id` NULL) — la face société n'a rien à aligner."""
    from src.imports.import_targets import company_targets

    headers = agent_headers(admin)
    universe = (await company_targets(db_session, admin.agency_id)).valid
    # La composition d'adresse société est DANS l'univers, aux deux bases.
    assert {"address.street", "headquarters_address.city"} <= universe

    # Tout l'univers passe l'import société — SAUF les deux bases en
    # texte intégral, volontairement EXCLUSIVES de leurs sous-champs
    # (422 `import.address_mode_conflict`, la règle du lot adresse) : on
    # mappe donc la composition, pas les deux modes à la fois.
    mappable = sorted(universe - {"address", "headquarters_address"})
    mapping = {f"col{i}": target for i, target in enumerate(mappable)}
    columns = list(mapping)
    header_line = ",".join(columns)
    value_line = ",".join("UniversCo" if mapping[c] == "name" else "" for c in columns)
    r = await client.post(
        "/imports/company-profiles/preview",
        headers=headers,
        json={"csv_text": f"{header_line}\n{value_line}\n", "mapping": mapping},
    )
    assert r.status_code == 200, f"l'import société refuse une cible de son univers : {r.text}"
    # Et les deux bases en texte intégral passent SEULES (l'autre mode).
    r = await client.post(
        "/imports/company-profiles/preview",
        headers=headers,
        json={
            "csv_text": "Nom,Adresse,Siège\nUniversCo,12 rue A,3 av B\n",
            "mapping": {"Nom": "name", "Adresse": "address", "Siège": "headquarters_address"},
        },
    )
    assert r.status_code == 200, r.text

    # Et le suggéreur ne rend QUE des cibles de cet univers.
    r = await client.post(
        "/imports/company-profiles/suggest-mapping",
        headers=headers,
        json={"headers": ["Nom", "Rue", "Ville", "Code postal", "Numéro de TVA", "Site web"]},
    )
    assert r.status_code == 200, r.text
    suggested = set(r.json()["suggestions"].values())
    assert suggested <= universe, f"le suggéreur propose hors univers : {suggested - universe}"


# --- LA GARANTIE STRUCTURELLE : le suggéreur ne propose jamais un 422 -----------------------


async def test_suggested_charge_always_passes_the_preview(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """LA RÈGLE STRUCTURELLE, étendue : ce que suggest-mapping propose
    DOIT passer le preview — pas seulement se coercer, PASSER, sans un
    seul 422. Le contre-exemple qui a fait ce test : sur les 42 en-têtes
    Teamleader réels, le suggéreur prenait `Rue`/`Code postal`/`Ville`
    (sous-champs) ET `adresse postale` (texte intégral) de la MÊME base —
    or l'import refuse les deux modes ensemble. Tout import de la
    suggestion brute mourait en `import.address_mode_conflict`.

    Le suggéreur tranche désormais lui-même : LES MORCEAUX GAGNENT, la
    colonne texte repart libre. Vérifié sur les DEUX fichiers réels."""
    headers = agent_headers(admin)

    async def suggest_then_preview(entity: str, file_headers: list[str]) -> dict:
        r = await client.post(
            f"/imports/{entity}/suggest-mapping", headers=headers, json={"headers": file_headers}
        )
        assert r.status_code == 200, r.text
        payload = r.json()
        mapping = payload["suggestions"]
        # Une ligne synthétique : seule la CHARGE compte ici, pas les valeurs.
        csv_text = ",".join(file_headers) + "\n" + ",".join("x" for _ in file_headers) + "\n"
        r = await client.post(
            f"/imports/{entity}/preview",
            headers=headers,
            json={"csv_text": csv_text, "mapping": mapping},
        )
        # LE CŒUR : 200, jamais un 422 — la charge suggérée est importable
        # TELLE QUELLE, sans arbitrage manuel de l'agence.
        assert r.status_code == 200, f"{entity}: {r.text}"
        return payload

    person = await suggest_then_preview("client-profiles", CONTACT_HEADERS_42)
    company = await suggest_then_preview("company-profiles", COMPANY_HEADERS_54)

    # Le verdict d'arbitrage, nommé : les fragments gagnent…
    assert person["suggestions"]["Rue"] == "residence_address.street"
    assert person["suggestions"]["Code postal"] == "residence_address.postal_code"
    assert person["suggestions"]["Ville"] == "residence_address.city"
    # …et la colonne texte intégral n'est PAS suggérée — mais reste LIBRE
    # (l'agence peut basculer sur le mode intégral à la main).
    assert "adresse postale" not in person["suggestions"]
    assert "adresse postale" in person["unmatched"]
    # L'ambiguïté « Pays » garde son option d'adresse : le mode en jeu est
    # celui des sous-champs, `.country` est cohérent avec lui.
    assert "residence_address.country" in person["ambiguous"]["Pays"]
    # Côté société le fichier réel n'a pas de colonne texte intégral :
    # rien à arbitrer, les fragments passent comme avant.
    assert company["suggestions"]["Rue"] == "address.street"
    assert "address" not in company["suggestions"].values()


async def test_suggester_arbitrates_whatever_the_column_order(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """L'arbitrage est une POST-PASSE, pas un effet de l'ordre du fichier :
    que la colonne texte intégral arrive avant ou après ses fragments, ce
    sont les fragments qui gagnent. Et une base SANS fragment garde son
    mode texte — on n'a pas tué le mode intégral, on l'a désambiguïsé."""
    headers = agent_headers(admin)

    async def suggest(file_headers: list[str]) -> dict:
        r = await client.post(
            "/imports/client-profiles/suggest-mapping",
            headers=headers,
            json={"headers": file_headers},
        )
        assert r.status_code == 200, r.text
        return r.json()

    # Le texte intégral EN PREMIER — il perd quand même.
    first = await suggest(["Prénom", "Nom", "Adresse e-mail", "Adresse", "Rue", "Ville"])
    assert first["suggestions"]["Rue"] == "residence_address.street"
    assert first["suggestions"]["Ville"] == "residence_address.city"
    assert "Adresse" not in first["suggestions"]
    assert "Adresse" in first["unmatched"]

    # Le texte intégral EN DERNIER — même verdict.
    last = await suggest(["Prénom", "Nom", "Adresse e-mail", "Rue", "Ville", "Adresse"])
    assert last["suggestions"]["Rue"] == "residence_address.street"
    assert "Adresse" not in last["suggestions"]

    # SEUL, sans aucun fragment : le mode texte intégral vit toujours.
    alone = await suggest(["Prénom", "Nom", "Adresse e-mail", "Adresse"])
    assert alone["suggestions"]["Adresse"] == "residence_address"


# --- LE FILL-GAP GRANULAIRE SUR LES ADRESSES ----------------------------------------------


async def test_address_fill_gap_is_per_subfield_whatever_the_pass_order(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """LE FILL-GAP DESCEND AU SOUS-CHAMP. Avant, il comparait l'OBJET :
    une adresse ne portant que `{country}` était « non vide », donc elle
    bloquait l'arrivée de la rue, de la ville et du code postal — l'ordre
    des passes d'import devenait signifiant (constat du ré-import Nico :
    1234 objets « pays seul » tuaient la passe texte intégral).

    Désormais chaque sous-champ VIDE se remplit, chaque sous-champ REMPLI
    est protégé — et l'ordre des passes n'a plus d'importance."""
    headers = agent_headers(admin)
    csv = "Prénom,Nom,Email,Rue,Ville,CP,Pays,Adresse\n"
    row = "Ordre,Passes,ordre@example.com,{rue},{ville},{cp},{pays},{adr}\n"
    base_mapping = {"Prénom": "first_name", "Nom": "last_name", "Email": "email"}

    # colonne → (cible, clé de la cellule)
    cols = {
        "Rue": ("residence_address.street", "rue"),
        "Ville": ("residence_address.city", "ville"),
        "CP": ("residence_address.postal_code", "cp"),
        "Pays": ("residence_address.country", "pays"),
    }

    async def run(**cells: str) -> dict:
        filled = {k: cells.get(k, "") for k in ("rue", "ville", "cp", "pays", "adr")}
        # Seules les colonnes PORTEUSES sont mappées (une base ne se mappe
        # pas en texte intégral ET en sous-champs — l'exclusivité tient).
        mapping = dict(base_mapping)
        if filled["adr"]:
            mapping["Adresse"] = "residence_address"
        else:
            mapping |= {col: target for col, (target, key) in cols.items() if filled[key]}
        r = await client.post(
            "/imports/client-profiles",
            headers=headers,
            json={"csv_text": csv + row.format(**filled), "mapping": mapping},
        )
        assert r.status_code == 200, r.text
        listing = (await client.get("/client-profiles?search=ordre@", headers=headers)).json()
        detail = (
            await client.get(f"/client-profiles/{listing['items'][0]['id']}", headers=headers)
        ).json()
        return detail["custom_fields"].get("residence_address", {})

    # PASSE 1 — le sous-champ le plus PAUVRE en premier (le pire cas).
    after_country = await run(pays="FR")
    assert after_country == {"country": "FR"}
    # PASSE 2 — le texte intégral arrive APRÈS : il n'est plus bloqué.
    after_text = await run(adr="12 rue des Lilas - 75011 Paris - France")
    assert after_text["country"] == "FR"  # l'existant est intact…
    assert after_text["street"] == "12 rue des Lilas - 75011 Paris - France"  # trou comblé
    # PASSE 3 — un sous-champ DÉJÀ POSÉ n'est JAMAIS écrasé, les autres
    # trous se comblent dans le même geste.
    after_more = await run(rue="99 avenue Ignorée", ville="Paris", cp="75011", pays="BE")
    assert after_more["street"] == "12 rue des Lilas - 75011 Paris - France"  # protégé
    assert after_more["country"] == "FR"  # protégé
    assert after_more["city"] == "Paris"  # trou comblé
    assert after_more["postal_code"] == "75011"  # trou comblé


async def test_scalar_fill_gap_still_wins_for_the_existing_value(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """La granularité ne concerne QUE les objets : sur un scalaire, la
    règle d'avant tient mot pour mot — l'existant gagne, l'import ne
    comble que le vide."""
    headers = agent_headers(admin)
    mapping = {
        "Prénom": "first_name",
        "Nom": "last_name",
        "Email": "email",
        "Tel": "phone",
        "Site": "website",
    }
    first = (
        "Prénom,Nom,Email,Tel,Site\nScalaire,Test,scal@example.com,0101010101,https://a.example\n"
    )
    r = await client.post(
        "/imports/client-profiles", headers=headers, json={"csv_text": first, "mapping": mapping}
    )
    assert r.status_code == 200, r.text
    second = (
        "Prénom,Nom,Email,Tel,Site\nScalaire,Test,scal@example.com,0202020202,https://b.example\n"
    )
    r = await client.post(
        "/imports/client-profiles", headers=headers, json={"csv_text": second, "mapping": mapping}
    )
    assert r.status_code == 200, r.text
    assert len(r.json()["linked"]) == 1  # lié, pas dupliqué
    listing = (await client.get("/client-profiles?search=scal@", headers=headers)).json()
    detail = (
        await client.get(f"/client-profiles/{listing['items'][0]['id']}", headers=headers)
    ).json()
    assert detail["phone"] == "0101010101"  # colonne civile : l'existant gagne
    assert detail["custom_fields"]["website"] == "https://a.example"  # sack scalaire : idem
