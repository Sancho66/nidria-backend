"""LA FICHE SOCIÉTÉ LIT DES DÉFINITIONS (lot du 07/08).

Ce que ce fichier grave, dans l'ordre du lot :

1. les 17 presets se MATÉRIALISENT à la première ouverture, avec leur
   type, leur section et leur libellé ×7 — idempotent ;
2. `resolve_company_sections` LIT `profile_section` : reclasser produit
   quelque chose, archiver retire de la fiche sans perdre la valeur ;
3. une clé de sack naît `text`, JAMAIS d'un type deviné de sa valeur ;
4. les gestes (PATCH, archive, unarchive, masse) passent par la MÊME
   route que la face personne, adressés par `definition_id` ;
5. LA FRONTIÈRE : les deux espaces de clés ne se touchent pas — une même
   clé porte une définition de chaque côté, sans collision ni fuite.
"""

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.company_profile import CompanyFieldDefinition
from shared.models.custom_field import CustomFieldDefinition
from shared.models.rbac import Role
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent

pytestmark = pytest.mark.usefixtures("rbac_baseline")

FIELDS = "/agencies/me/custom-fields"
UNIVERSE = "/agencies/me/field-universe"


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


async def _company(client: AsyncClient, headers: dict[str, str], **body: object) -> dict:
    r = await client.post("/company-profiles", headers=headers, json={"name": "ACME", **body})
    assert r.status_code == 201, r.text
    return r.json()


def _sections(detail: dict) -> dict[str, list[str]]:
    return {s["key"]: s["references"] for s in detail["sections"]}


async def test_opening_a_sheet_materializes_the_seventeen_presets(
    client: AsyncClient, db_session: AsyncSession, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """LA MATÉRIALISATION PARESSEUSE, le pattern de la face personne :
    rien en base tant que personne n'a rien ouvert, tout déclaré au
    premier regard — et rien de plus au second."""
    rows = (
        await db_session.execute(
            select(CompanyFieldDefinition).where(
                CompanyFieldDefinition.agency_id == admin.agency_id
            )
        )
    ).scalars()
    assert list(rows) == []

    headers = agent_headers(admin)
    detail = await _company(client, headers)
    assert detail["sections"], "la fiche sert ses sections"

    rows = list(
        (
            await db_session.execute(
                select(CompanyFieldDefinition).where(
                    CompanyFieldDefinition.agency_id == admin.agency_id
                )
            )
        ).scalars()
    )
    by_key = {row.key: row for row in rows}
    assert len(by_key) == 17, sorted(by_key)
    # Type, section et libellé ×7 viennent du catalogue société.
    assert by_key["registration_date"].field_type == "date"
    assert by_key["registration_date"].profile_section == "identity"
    assert by_key["country"].field_type == "country"  # le catalogue société prime
    assert by_key["employee_count"].field_type == "number"  # hors FIELD_PRESETS
    assert by_key["company_name"].label_i18n["en"], "les 7 langues voyagent"

    await client.get(f"/company-profiles/{detail['id']}", headers=headers)
    again = list(
        (
            await db_session.execute(
                select(CompanyFieldDefinition).where(
                    CompanyFieldDefinition.agency_id == admin.agency_id
                )
            )
        ).scalars()
    )
    assert len(again) == 17, "rouvrir ne redéclare pas"


async def test_the_company_sheet_serves_section_names_in_seven_languages(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """D7, face société : la fiche société sert LA MÊME taxonomie que la
    fiche personne (`COMPANY_PROFILE_SECTIONS` *est* `PROFILE_SECTIONS`),
    donc elle doit servir les mêmes 7 langues. Un contrat qui ne vaudrait
    que d'un côté obligerait l'écran à savoir de quelle face vient la
    section pour choisir sa méthode de résolution."""
    detail = await _company(client, agent_headers(admin))
    by_key = {s["key"]: s for s in detail["sections"]}
    assert by_key["identity"]["name"] == "Identité"  # résolu, langue d'agence
    assert set(by_key["identity"]["name_i18n"]) == {"fr", "en", "es", "ru", "pt", "it", "hu"}
    assert by_key["misc"]["name_i18n"]["en"] == "Miscellaneous"


async def test_the_sheet_reads_profile_section_so_reclassing_shows(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """LE PREMIER CORRECTIF PRÉALABLE : `profile_section` était ignorée,
    donc reclasser un champ société ne pouvait rien produire. La fiche la
    lit — le geste se voit."""
    headers = agent_headers(admin)
    detail = await _company(client, headers)
    assert "legal_form" in _sections(detail)["identity"]

    universe = (await client.get(f"{UNIVERSE}?surface=company", headers=headers)).json()
    entry = next(
        f for s in universe["sections"] for f in s["fields"] if f["reference"] == "legal_form"
    )
    r = await client.patch(
        f"{FIELDS}/{entry['definition_id']}", headers=headers, json={"profile_section": "situation"}
    )
    assert r.status_code == 200, r.text

    after = (await client.get(f"/company-profiles/{detail['id']}", headers=headers)).json()
    sections = _sections(after)
    assert "legal_form" not in sections["identity"]
    assert "legal_form" in sections["situation"]


async def test_archiving_removes_the_field_from_the_sheet_but_never_its_value(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """L'archivage devient VRAI (il ne l'était pas : la fiche retombait
    sur le preset figé). La valeur, elle, reste dans le sac — règle des
    clés orphelines, identique à la face personne. Et la résurrection
    remet le champ en place."""
    headers = agent_headers(admin)
    detail = await _company(client, headers, custom_fields={"legal_form": "SARL"})
    universe = (await client.get(f"{UNIVERSE}?surface=company", headers=headers)).json()
    entry = next(
        f for s in universe["sections"] for f in s["fields"] if f["reference"] == "legal_form"
    )

    r = await client.post(f"{FIELDS}/{entry['definition_id']}/archive", headers=headers)
    assert r.status_code == 200, r.text
    after = (await client.get(f"/company-profiles/{detail['id']}", headers=headers)).json()
    assert "legal_form" not in [ref for refs in _sections(after).values() for ref in refs]
    assert after["custom_fields"]["legal_form"] == "SARL", "la valeur est gardée"

    r = await client.post(f"{FIELDS}/{entry['definition_id']}/unarchive", headers=headers)
    assert r.status_code == 200, r.text
    back = (await client.get(f"/company-profiles/{detail['id']}", headers=headers)).json()
    assert "legal_form" in _sections(back)["identity"]


async def test_a_sack_key_is_declared_as_text_never_a_type_guessed_from_its_value(
    client: AsyncClient, db_session: AsyncSession, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """LA RÈGLE DU LOT : « 1234 » ne devient pas un `number`. Le deviner
    ferait échouer la coercition du prochain import sur « 01234 » — la
    règle « suggérable = coerçable » y passerait."""
    headers = agent_headers(admin)
    await _company(client, headers, custom_fields={"numero_greffe": "1234"})

    row = (
        await db_session.execute(
            select(CompanyFieldDefinition).where(
                CompanyFieldDefinition.agency_id == admin.agency_id,
                CompanyFieldDefinition.key == "numero_greffe",
            )
        )
    ).scalar_one()
    assert row.field_type == "text"
    assert row.profile_section == "misc"


async def test_the_two_key_spaces_never_collide(
    client: AsyncClient, db_session: AsyncSession, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """LA RAISON D'ÊTRE DE LA TABLE DÉDIÉE. `company_name` existe déjà en
    définition PERSONNE dans 3 agences de prod (contrainte `(agency_id,
    key)` UNIQUE). Les deux faces portent chacune la leur, et la surface
    personne ne voit jamais la société — ni l'inverse."""
    headers = agent_headers(admin)
    r = await client.post(
        FIELDS,
        headers=headers,
        json={
            "key": "company_name",
            "label": "Société du client",
            "field_type": "text",
            "scope": "person",
        },
    )
    assert r.status_code == 201, r.text
    person_id = r.json()["id"]

    await _company(client, headers)  # matérialise la face société

    company_row = (
        await db_session.execute(
            select(CompanyFieldDefinition).where(
                CompanyFieldDefinition.agency_id == admin.agency_id,
                CompanyFieldDefinition.key == "company_name",
            )
        )
    ).scalar_one()
    person_row = (
        await db_session.execute(
            select(CustomFieldDefinition).where(CustomFieldDefinition.id == person_id)
        )
    ).scalar_one()
    assert company_row.id != person_row.id
    assert company_row.label != person_row.label

    # La liste des définitions, par face : chacune chez soi.
    company_side = (await client.get(f"{FIELDS}?surface=company", headers=headers)).json()
    keys = {d["key"]: d for d in company_side}
    assert keys["company_name"]["id"] == str(company_row.id)
    assert keys["company_name"]["scope"] == "company"
    person_side = (await client.get(f"{FIELDS}?surface=person", headers=headers)).json()
    assert {d["id"] for d in person_side} == {str(person_id)}

    # L'univers PERSONNE n'a pas bougé : les clés société en restent
    # exclues (invariant de la demande design A).
    universe = (await client.get(f"{UNIVERSE}?surface=person", headers=headers)).json()
    assert "company_name" not in {f["reference"] for s in universe["sections"] for f in s["fields"]}


async def test_bulk_moves_company_fields_and_counts_their_values(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """Les gestes de MASSE valent aussi de ce côté — c'est ce que la
    sélection promet. Le dry-run annonce les conséquences (les valeurs
    concernées) avant d'écrire, comme en face personne."""
    headers = agent_headers(admin)
    detail = await _company(client, headers, custom_fields={"legal_form": "SARL"})
    universe = (await client.get(f"{UNIVERSE}?surface=company", headers=headers)).json()
    ids = {f["reference"]: f["definition_id"] for s in universe["sections"] for f in s["fields"]}

    r = await client.post(
        f"{FIELDS}/bulk",
        headers=headers,
        json={
            "ids": [ids["legal_form"], ids["vat_number"]],
            "action": "section",
            "profile_section": "misc",
            "dry_run": True,
        },
    )
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["eligible"] == 2
    assert report["applied"] == 0
    assert report["with_values"] == 1  # seul legal_form porte une valeur
    assert report["values_count"] == 1

    r = await client.post(
        f"{FIELDS}/bulk",
        headers=headers,
        json={
            "ids": [ids["legal_form"], ids["vat_number"]],
            "action": "section",
            "profile_section": "misc",
        },
    )
    assert r.json()["applied"] == 2
    after = _sections(
        (await client.get(f"/company-profiles/{detail['id']}", headers=headers)).json()
    )
    assert "legal_form" in after["misc"]
    assert "vat_number" in after["misc"]


async def test_a_company_field_refuses_the_attributes_that_do_not_apply(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """Une définition société EST sa face : lui proposer un `scope`
    reviendrait à offrir de la déménager vers la fiche personne, où sa clé
    est peut-être déjà prise. Refus nommé, jamais un silence."""
    headers = agent_headers(admin)
    await _company(client, headers)
    universe = (await client.get(f"{UNIVERSE}?surface=company", headers=headers)).json()
    entry = next(
        f for s in universe["sections"] for f in s["fields"] if f["reference"] == "legal_form"
    )
    r = await client.patch(
        f"{FIELDS}/{entry['definition_id']}", headers=headers, json={"scope": "person"}
    )
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "company_field.unsupported_attribute"


# --- D9 : CRÉER un champ de fiche société --------------------------------------------


@pytest_asyncio.fixture
async def member(admin: Agent, make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    """Même agence, rôle member : case.edit mais PAS field.manage."""
    return await make_agent(agency_id=admin.agency_id, role=system_roles["member"])


def _company_field(**overrides: object) -> dict:
    """Le corps d'une création SOCIÉTÉ sur LA route des définitions : la
    face n'est qu'une portée de plus. Pas de `key` (dérivée du libellé),
    et une section explicite (requise de ce côté)."""
    return {"field_type": "text", "scope": "company", "profile_section": "misc", **overrides}


async def test_creating_a_company_field_derives_its_key_and_lands_last_in_its_section(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """LE GESTE QUI MANQUAIT À CETTE FACE. Jusqu'ici un champ société ne
    naissait que du catalogue ou d'une colonne baptisée à la grille
    d'import : une agence qui voulait « Numéro de greffe » n'avait que le
    détour d'un fichier.

    La clé est DÉRIVÉE du libellé — l'appelant ne la pose pas — et le
    champ naît DÉCLARÉ, à la fin de sa section : il porte donc son
    `definition_id` dès sa première seconde, et les quatre gestes
    (renommer, reclasser, archiver, ranger) valent sur lui immédiatement.
    """
    headers = agent_headers(admin)
    detail = await _company(client, headers, custom_fields={"legal_form": "SARL"})

    r = await client.post(
        FIELDS,
        headers=headers,
        json=_company_field(label="Numéro de greffe", profile_section="situation"),
    )
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["key"] == "numero_de_greffe", "la clé vient du libellé, accents retirés"
    assert created["field_type"] == "text"
    assert created["profile_section"] == "situation"
    assert created["scope"] == "company", "la face est servie en sortie"
    assert created["label"] == "Numéro de greffe"
    # Même mécanique i18n que la face personne, puisque c'est la même
    # porte : le libellé seul s'ancre dans la langue de l'agence — aucune
    # traduction inventée dans les six autres.
    assert created["label_i18n"] == {"fr": "Numéro de greffe"}
    assert created["archived_at"] is None
    assert created["options"] is None and created["required"] is False

    # EN FIN DE SECTION : après les presets `situation`, jamais devant.
    after = _sections(
        (await client.get(f"/company-profiles/{detail['id']}", headers=headers)).json()
    )
    assert after["situation"][-1] == "numero_de_greffe"
    assert "share_capital" in after["situation"], "les presets de la section restent devant"

    # DÉCLARÉ, donc adressable : l'univers le sert avec son id, et le
    # PATCH de définition (la route de la face personne) le reclasse.
    universe = (await client.get(f"{UNIVERSE}?surface=company", headers=headers)).json()
    entry = next(
        f for s in universe["sections"] for f in s["fields"] if f["reference"] == "numero_de_greffe"
    )
    assert entry["state"] == "declared"
    assert entry["origin"] == "agency", "l'agence l'a écrit, pas le catalogue"
    assert entry["definition_id"] == created["id"]
    moved = await client.patch(
        f"{FIELDS}/{created['id']}", headers=headers, json={"profile_section": "misc"}
    )
    assert moved.status_code == 200, moved.text


async def test_creating_a_company_field_refuses_a_preset_key(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """COLLISION AVEC LE CATALOGUE — refus NOMMÉ, distinct de l'autre :
    la clé appartient au produit, elle est déjà dans l'univers de
    l'agence (ou y entrera au premier écran ouvert). Le recours est de
    renommer ce champ-là, pas d'en créer un second qui porterait la même
    clé sur la même fiche.

    Et le refus vaut AVANT toute matérialisation : une agence qui n'a
    jamais ouvert un écran société n'a aucune ligne en base — comparer
    aux seules définitions existantes l'aurait laissée passer."""
    headers = agent_headers(admin)
    r = await client.post(
        FIELDS,
        headers=headers,
        json=_company_field(label="VAT number", profile_section="identity"),
    )
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["code"] == "company_field.key_reserved"
    assert body["params"]["key"] == "vat_number"
    assert body["params"]["label"], "le refus NOMME le preset en place"


async def test_creating_a_company_field_refuses_a_key_the_agency_already_holds(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """COLLISION AVEC L'AGENCE — l'autre refus, l'autre recours : la
    définition en place est NOMMÉE (id + libellé + état d'archivage) pour
    que l'écran propose de la renommer ou de la ressusciter.

    Un champ ARCHIVÉ compte comme pris : la contrainte `(agency_id, key)`
    couvre les archivés, et créer par-dessus adopterait ses valeurs en
    silence."""
    headers = agent_headers(admin)
    first = await client.post(
        FIELDS,
        headers=headers,
        json=_company_field(label="Numéro de greffe"),
    )
    assert first.status_code == 201, first.text

    # Même clé, libellé écrit autrement : c'est le slug qui décide.
    again = await client.post(
        FIELDS,
        headers=headers,
        json=_company_field(
            label="  NUMÉRO DE GREFFE  ", field_type="number", profile_section="identity"
        ),
    )
    assert again.status_code == 409, again.text
    body = again.json()
    assert body["code"] == "company_field.key_exists"
    assert body["params"]["field_id"] == first.json()["id"]
    assert body["params"]["label"] == "Numéro de greffe"
    assert body["params"]["archived"] is False

    # Archivé, toujours pris — et le refus le dit, pour que l'écran
    # propose la résurrection plutôt qu'un doublon impossible.
    archived = await client.post(f"{FIELDS}/{first.json()['id']}/archive", headers=headers)
    assert archived.status_code == 200, archived.text
    third = await client.post(
        FIELDS,
        headers=headers,
        json=_company_field(label="Numéro de greffe"),
    )
    assert third.status_code == 409, third.text
    assert third.json()["params"]["archived"] is True


async def test_the_collision_check_sees_a_sack_key_no_screen_has_declared_yet(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """LA COLLISION INVISIBLE. Une clé qui ne vit encore que dans le sac
    d'une société (import, saisie libre) n'a pas de définition tant que
    personne n'a ouvert l'écran des Réglages. Créer par-dessus ferait
    naître un champ « neuf » déjà porteur des valeurs de toutes ces
    sociétés — la création balaie donc les sacs de l'agence avant de
    comparer."""
    headers = agent_headers(admin)
    await _company(client, headers, custom_fields={"numero_de_greffe": "1234"})

    r = await client.post(
        FIELDS,
        headers=headers,
        json=_company_field(label="Numéro de greffe", profile_section="identity"),
    )
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "company_field.key_exists"


@pytest.mark.parametrize(
    ("label", "key"),
    [
        ("  Numéro de GREFFE  ", "numero_de_greffe"),  # accents, casse, bords
        ("Chiffre d'affaires", "chiffre_d_affaires"),  # apostrophe → séparateur
        ("2e adresse", "f_2e_adresse"),  # une clé ne commence pas par un chiffre
    ],
)
async def test_the_derived_key_follows_the_one_slug_rule_of_the_repo(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders, label: str, key: str
) -> None:
    """LA MÊME MOULINETTE QUE LA GRILLE D'IMPORT (`slugify_field_label`).
    Deux fabriques de clés pour la même table auraient divergé au premier
    caractère accentué : « Numéro de greffe » saisi ici et importé demain
    doivent se retrouver, pas coexister en double."""
    r = await client.post(
        FIELDS,
        headers=agent_headers(admin),
        json=_company_field(label=label),
    )
    assert r.status_code == 201, r.text
    assert r.json()["key"] == key


async def test_creating_a_company_field_refuses_an_unknown_section(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """La section est une clé de section DE L'AGENCE (les sections sont
    une donnée, pas une liste figée) : une clé inconnue est refusée
    nommément, avec la liste disponible. Pas de repli silencieux en
    « Divers » — le champ naîtrait ailleurs que là où l'agence l'a
    déposé."""
    r = await client.post(
        FIELDS,
        headers=agent_headers(admin),
        json=_company_field(label="Numéro de greffe", profile_section="inventee"),
    )
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["code"] == "profile_section.unknown"
    assert "misc" in body["params"]["available"]


async def test_creating_a_company_field_refuses_what_this_face_does_not_carry(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """LA PORTE EST PARTAGÉE, PAS LE CONTRAT. Une portée `company` sur la
    route des définitions accepte trois choses de moins, et le refus est
    NOMMÉ dans les trois cas — jamais un silence qui accepterait pour ne
    rien en faire (même code que le PATCH société) :

    - `key` : elle est DÉRIVÉE du libellé. L'accepter ouvrirait une
      seconde fabrique de clés sur la même table ;
    - `options` / `required` : la table société n'a pas ces colonnes ;
    - la section : requise ici, sans berceau implicite.
    """
    headers = agent_headers(admin)
    sent = await client.post(
        FIELDS, headers=headers, json=_company_field(label="Greffe", key="autre_cle")
    )
    assert sent.status_code == 422, sent.text
    assert sent.json()["code"] == "company_field.unsupported_attribute"
    assert sent.json()["params"]["attribute"] == "key"

    demanding = await client.post(
        FIELDS, headers=headers, json=_company_field(label="Greffe", required=True)
    )
    assert demanding.status_code == 422, demanding.text
    assert demanding.json()["params"]["attribute"] == "required"

    homeless = await client.post(
        FIELDS,
        headers=headers,
        json={"label": "Greffe", "field_type": "text", "scope": "company"},
    )
    assert homeless.status_code == 422, homeless.text
    assert homeless.json()["code"] == "company_field.section_required"

    # Une liste de choix sans colonne `options` ne peut pas exister : le
    # contrat le dit (422 de validation), il ne le fait pas découvrir.
    choices = await client.post(
        FIELDS,
        headers=headers,
        json=_company_field(label="Greffe", field_type="select", options=["a", "b"]),
    )
    assert choices.status_code == 422, choices.text


async def test_creating_a_company_field_is_gated_by_field_manage(
    client: AsyncClient, member: Agent, agent_headers: AuthHeaders
) -> None:
    """Dessiner une fiche est de la CONFIGURATION : un agent qui travaille
    des dossiers (case.edit, sans field.manage) ne redessine pas la fiche
    société — même porte que le renommage et que la face personne."""
    r = await client.post(
        FIELDS,
        headers=agent_headers(member),
        json=_company_field(label="Numéro de greffe"),
    )
    assert r.status_code == 403, r.text
