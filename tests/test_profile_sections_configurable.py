"""LES SECTIONS DE FICHE DEVIENNENT CONFIGURABLES PAR AGENCE.

Ce fichier grave, dans l'ordre du lot :

1. **L'écran est identique le lendemain** : les 4 sections d'origine,
   leurs clés inchangées, leurs champs à la même place.
2. **Le repli catalogue** : tant que l'agence n'a pas renommé, le libellé
   vient du produit et suit ses corrections de traduction.
3. **Les gestes** : créer, renommer, réordonner — et les TROIS refus de
   suppression (« Divers », section portant des champs, section portant
   des colonnes civiles).
4. **La validation par tenant** : une section créée par l'agence est
   acceptée au PATCH et à la masse (le `Literal` gravé la refusait) ; une
   clé inventée reste refusée, nommément.
5. **Les invariants** : union des sections == univers de complétude, un
   champ dans exactement une section, aucune section orpheline.
6. **D7** (`name_i18n`) et **la création de champ société**.
"""

import pytest
import pytest_asyncio
from httpx import AsyncClient

from shared.models.agent import Agent
from shared.models.rbac import Role
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent

pytestmark = pytest.mark.usefixtures("rbac_baseline")

SECTIONS = "/agencies/me/profile-sections"
FIELDS = "/agencies/me/custom-fields"
UNIVERSE = "/agencies/me/field-universe"


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


async def _sections(client: AsyncClient, headers: dict[str, str], surface: str = "person") -> list:
    r = await client.get(f"{SECTIONS}?surface={surface}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def _profile(client: AsyncClient, headers: dict[str, str], email: str) -> dict:
    r = await client.post(
        "/cases",
        headers=headers,
        json={"first_name": "Sec", "last_name": "Tion", "email": email},
    )
    assert r.status_code == 201, r.text
    listing = (await client.get(f"/client-profiles?search={email}", headers=headers)).json()
    return (
        await client.get(f"/client-profiles/{listing['items'][0]['id']}", headers=headers)
    ).json()


async def test_the_four_sections_are_served_unchanged_with_catalog_labels(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """L'ÉCRAN EST IDENTIQUE LE LENDEMAIN : mêmes 4 clés, même ordre, et
    les libellés viennent du CATALOGUE tant que l'agence n'a rien renommé
    — pas d'un blob gelé en base qui ne suivrait plus les corrections."""
    headers = agent_headers(admin)
    sections = await _sections(client, headers)
    assert [s["key"] for s in sections] == ["identity", "contact", "situation", "misc"]
    assert [s["name"] for s in sections] == ["Identité", "Contact", "Situation", "Divers"]
    # Le repli, prouvé : rien de personnalisé, et pourtant les 7 langues.
    assert all(s["customized"] is False for s in sections)
    assert sections[0]["name_i18n"]["hu"] == "Személyazonosság"
    # « Divers » s'annonce indéracinable AU CONTRAT, l'écran ne le déduit pas.
    assert sections[-1]["deletable"] is False
    assert all(s["deletable"] for s in sections[:-1])


async def test_the_sheet_serves_the_agency_sections_and_d7_blob(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """La fiche sert les sections de l'agence — celle qu'elle vient de
    créer comprise — et chacune porte son `name_i18n` (D7)."""
    headers = agent_headers(admin)
    r = await client.post(
        SECTIONS,
        headers=headers,
        json={
            "surface": "person",
            "key": "fiscalite",
            "label_i18n": {"fr": "Fiscalité", "en": "Tax"},
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["customized"] is True

    detail = await _profile(client, headers, "d7@example.com")
    by_key = {s["key"]: s for s in detail["sections"]}
    assert "fiscalite" in by_key, "la section créée est servie par la fiche"
    assert by_key["fiscalite"]["name"] == "Fiscalité"
    assert by_key["fiscalite"]["name_i18n"] == {"fr": "Fiscalité", "en": "Tax"}
    # Les sections d'origine gardent le repli catalogue, ×7.
    assert by_key["identity"]["name_i18n"]["ru"] == "Личные данные"


async def test_renaming_touches_the_label_never_the_key(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """Renommer « Identité » en « État civil » ne déplace AUCUN champ :
    la clé est ce que portent les définitions. Et `label_i18n = {}` rend
    le libellé d'origine — l'annulation, sans toucher aux champs."""
    headers = agent_headers(admin)
    identity = (await _sections(client, headers))[0]
    before = await _profile(client, headers, "rename@example.com")
    refs_before = {s["key"]: s["references"] for s in before["sections"]}["identity"]

    r = await client.patch(
        f"{SECTIONS}/{identity['id']}",
        headers=headers,
        json={"label_i18n": {"fr": "État civil"}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["key"] == "identity", "la clé ne bouge jamais"
    assert r.json()["name"] == "État civil"
    assert r.json()["customized"] is True

    after = await _profile(client, headers, "rename2@example.com")
    by_key = {s["key"]: s for s in after["sections"]}
    assert by_key["identity"]["name"] == "État civil"
    assert by_key["identity"]["references"] == refs_before, "aucun champ n'a bougé"

    # L'annulation : le repli catalogue reprend la main.
    r = await client.patch(f"{SECTIONS}/{identity['id']}", headers=headers, json={"label_i18n": {}})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Identité"
    assert r.json()["customized"] is False


async def test_reordering_is_served_in_the_new_order(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """L'ordre servi est celui de l'agence — la fiche ne recompose rien."""
    headers = agent_headers(admin)
    sections = await _sections(client, headers)
    situation = next(s for s in sections if s["key"] == "situation")
    r = await client.patch(f"{SECTIONS}/{situation['id']}", headers=headers, json={"position": -1})
    assert r.status_code == 200, r.text

    detail = await _profile(client, headers, "order@example.com")
    assert [s["key"] for s in detail["sections"]][0] == "situation"


async def test_the_three_refusals_of_deletion(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """LES TROIS REFUS, chacun nommé :

    1. « Divers » ne se supprime jamais — c'est le berceau ;
    2. une section qui porte des COLONNES CIVILES non plus (elles ne sont
       pas déplaçables, backlog D5) — donc Identité/Contact/Situation
       tiennent tant qu'elles les portent ;
    3. une section qui porte un CHAMP déclaré non plus, tant qu'il y est.
    """
    headers = agent_headers(admin)
    sections = await _sections(client, headers)
    by_key = {s["key"]: s for s in sections}

    r = await client.delete(f"{SECTIONS}/{by_key['misc']['id']}", headers=headers)
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "profile_section.misc_immutable"

    r = await client.delete(f"{SECTIONS}/{by_key['identity']['id']}", headers=headers)
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["code"] == "profile_section.not_empty"
    assert body["params"]["native_columns"] > 0, "les colonnes civiles comptent"

    # Une section neuve est vide : elle part. Avec un champ dedans, non.
    r = await client.post(
        SECTIONS,
        headers=headers,
        json={"surface": "person", "key": "jetable", "label_i18n": {"fr": "Jetable"}},
    )
    created = r.json()
    r = await client.post(
        FIELDS,
        headers=headers,
        json={
            "key": "un_champ",
            "label": "Un champ",
            "field_type": "text",
            "scope": "person",
            "profile_section": "jetable",
        },
    )
    assert r.status_code == 201, r.text
    field_id = r.json()["id"]

    r = await client.delete(f"{SECTIONS}/{created['id']}", headers=headers)
    assert r.status_code == 422, r.text
    assert r.json()["params"]["fields"] == 1

    # Déplacé ailleurs, la section part enfin — le message ne mentait pas.
    r = await client.patch(
        f"{FIELDS}/{field_id}", headers=headers, json={"profile_section": "misc"}
    )
    assert r.status_code == 200, r.text
    r = await client.delete(f"{SECTIONS}/{created['id']}", headers=headers)
    assert r.status_code == 204, r.text


async def test_an_agency_section_is_accepted_where_the_literal_refused_it(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """LE BLOCANT n°1 DU CONSTAT : les deux `Literal` gravaient 4 clés,
    donc une section créée par l'agence était refusée en 422 par son
    propre back. Le PATCH et la MASSE l'acceptent désormais — et une clé
    inventée reste refusée, en nommant les sections disponibles."""
    headers = agent_headers(admin)
    await client.post(
        SECTIONS,
        headers=headers,
        json={"surface": "person", "key": "patrimoine", "label_i18n": {"fr": "Patrimoine"}},
    )
    r = await client.post(
        FIELDS,
        headers=headers,
        json={"key": "un_bien", "label": "Un bien", "field_type": "text", "scope": "person"},
    )
    field_id = r.json()["id"]

    r = await client.patch(
        f"{FIELDS}/{field_id}", headers=headers, json={"profile_section": "patrimoine"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["profile_section"] == "patrimoine"

    r = await client.post(
        f"{FIELDS}/bulk",
        headers=headers,
        json={"ids": [field_id], "action": "section", "profile_section": "patrimoine"},
    )
    assert r.status_code == 200, r.text

    r = await client.patch(
        f"{FIELDS}/{field_id}", headers=headers, json={"profile_section": "inventee"}
    )
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["code"] == "profile_section.unknown"
    assert "patrimoine" in body["params"]["available"]


async def test_the_invariants_hold_with_a_configurable_taxonomy(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """LES INVARIANTS, re-prouvés sur une taxonomie devenue variable :

    - l'union des références servies == l'univers de complétude ;
    - un champ vit dans EXACTEMENT une section ;
    - aucune section orpheline : toute section servie par la fiche existe
      chez l'agence, et réciproquement.
    """
    headers = agent_headers(admin)
    await client.post(
        SECTIONS,
        headers=headers,
        json={"surface": "person", "key": "douane", "label_i18n": {"fr": "Douane"}},
    )
    await client.post(
        FIELDS,
        headers=headers,
        json={
            "key": "num_douane",
            "label": "N° douane",
            "field_type": "text",
            "scope": "person",
            "profile_section": "douane",
        },
    )
    detail = await _profile(client, headers, "invariants@example.com")

    served = [ref for s in detail["sections"] for ref in s["references"]]
    assert len(served) == len(set(served)), "un champ, exactement une section"
    universe = set(detail["completeness"]["filled"]) | set(detail["completeness"]["missing"])
    assert set(served) == universe, sorted(set(served) ^ universe)

    agency_keys = {s["key"] for s in await _sections(client, headers)}
    assert {s["key"] for s in detail["sections"]} == agency_keys, "aucune section orpheline"
    assert "num_douane" in {s["key"]: s["references"] for s in detail["sections"]}["douane"]


async def test_creating_a_company_field_lands_on_the_company_face(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """POINT 6 — l'écran société avait le bouton « Créer un champ
    personnalisé », pas la route. La même route sert les trois faces ; le
    champ naît dans l'espace de clés SOCIÉTÉ, donc sans collision avec la
    face personne."""
    headers = agent_headers(admin)
    r = await client.post(
        SECTIONS,
        headers=headers,
        json={"surface": "company", "key": "juridique", "label_i18n": {"fr": "Juridique"}},
    )
    assert r.status_code == 201, r.text

    # LA PORTE UNIFIÉE : la création société passe par la route des
    # définitions, avec `scope: "company"`. La route dédiée
    # `/agencies/me/company-fields` a existé un temps puis a été retirée —
    # une seule porte au contrat. Ses règles propres tiennent : la clé est
    # DÉRIVÉE du libellé (l'appelant ne la pose pas, et la poser est
    # refusé), la section est REQUISE (pas de berceau implicite de ce
    # côté).
    r = await client.post(
        FIELDS,
        headers=headers,
        json={
            "label": "Greffe",
            "field_type": "text",
            "scope": "company",
            "profile_section": "juridique",
        },
    )
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["scope"] == "company"
    assert created["profile_section"] == "juridique"
    assert created["key"] == "greffe", "la clé est dérivée du libellé"

    # Il vit côté société, et NULLE PART côté personne.
    company_side = (await client.get(f"{FIELDS}?surface=company", headers=headers)).json()
    assert "greffe" in {d["key"] for d in company_side}
    person_side = (await client.get(f"{FIELDS}?surface=person", headers=headers)).json()
    assert "greffe" not in {d["key"] for d in person_side}

    # Et la fiche société le range dans la section demandée.
    r = await client.post("/company-profiles", headers=headers, json={"name": "ACME"})
    detail = r.json()
    by_key = {s["key"]: s["references"] for s in detail["sections"]}
    assert "greffe" in by_key["juridique"]


async def test_sections_are_scoped_to_their_agency_and_gated(
    client: AsyncClient,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """Scopage agence et partage des droits : lire suffit à un membre,
    configurer demande `field.manage`, et l'agence d'à côté ne voit rien."""
    headers = agent_headers(admin)
    await client.post(
        SECTIONS,
        headers=headers,
        json={"surface": "person", "key": "chez_moi", "label_i18n": {"fr": "Chez moi"}},
    )
    other = await make_agent(role=system_roles["admin"])  # sa propre agence
    keys = {s["key"] for s in await _sections(client, agent_headers(other))}
    assert "chez_moi" not in keys

    member = await make_agent(agency_id=admin.agency_id, role=system_roles["member"])
    r = await client.get(SECTIONS, headers=agent_headers(member))
    assert r.status_code == 200, "lire les sections est du travail de dossier"
    r = await client.post(
        SECTIONS,
        headers=agent_headers(member),
        json={"surface": "person", "key": "interdit", "label_i18n": {"fr": "Interdit"}},
    )
    assert r.status_code == 403, "configurer demande field.manage"
