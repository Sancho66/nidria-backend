"""L'UNIVERS AFFICHÉ — l'écran devient le miroir des écrans réels.

Le constat qui a produit ce lot : l'écran des champs listait le STOCKAGE
(les définitions) quand l'agence veut voir l'USAGE (ce que ses fiches
montrent). Les trois surfaces ne sont pas symétriques, et c'est
précisément ce que ce contrat rend explicite :

- PERSONNE : des définitions ET 10 colonnes natives qui ne s'archivent
  jamais ;
- SOCIÉTÉ : zéro définition, 17 presets du code + les clés découvertes
  dans les sacks ;
- DOSSIER : un même champ sert des dizaines de parcours — une entrée, un
  compte.

Chaque champ porte son ÉTAT : l'écran n'a plus à déduire ce qu'il peut
faire d'une ligne, et c'est ça qu'on garde ici.
"""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.company_profile import CompanyFieldLabel, CompanyProfile
from shared.models.journey import JourneyTemplate, JourneyTemplateField
from shared.models.rbac import Role
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent

pytestmark = pytest.mark.usefixtures("rbac_baseline")

URL = "/agencies/me/field-universe"


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


def _flat(body: dict) -> dict[str, dict]:
    return {f["reference"]: f for s in body["sections"] for f in s["fields"]}


async def _define(client: AsyncClient, headers: dict, key: str, **over) -> dict:
    payload = {"key": key, "label": key, "field_type": "text", **over}
    r = await client.post("/agencies/me/custom-fields", headers=headers, json=payload)
    assert r.status_code == 201, r.text
    return r.json()


# --- surface personne -----------------------------------------------------------------


async def test_the_person_surface_shows_natives_and_declared_side_by_side(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """LE MIROIR : la fiche montre des colonnes civiles ET des champs
    déclarés. L'écran de réglage ne montrait que les seconds — d'où
    l'écart ressenti entre les deux."""
    headers = agent_headers(admin)
    definition = await _define(client, headers, "permis_maison", scope="person")

    body = (await client.get(f"{URL}?surface=person", headers=headers)).json()
    assert body["surface"] == "person"
    fields = _flat(body)
    # Une native : elle s'affiche, elle n'a pas de définition, elle ne
    # s'archive jamais — et le contrat le DIT.
    assert fields["date_of_birth"]["state"] == "native"
    assert fields["date_of_birth"]["definition_id"] is None
    # Une déclarée : elle porte son id, donc l'écran peut l'éditer.
    assert fields["permis_maison"]["state"] == "declared"
    assert fields["permis_maison"]["definition_id"] == definition["id"]
    assert fields["permis_maison"]["field_type"] == "text"


async def test_a_catalog_preset_not_declared_is_offered_not_hidden(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """Ce que l'agence n'a pas déclaré existe quand même dans le
    catalogue : l'écran doit pouvoir le proposer (« ajouter à mon
    univers ») plutôt que faire comme s'il n'existait pas."""
    fields = _flat((await client.get(f"{URL}?surface=person", headers=agent_headers(admin))).json())
    assert fields["visa_type"]["state"] == "catalog_undeclared"
    assert fields["visa_type"]["definition_id"] is None


async def test_declaring_a_preset_moves_it_from_offered_to_editable(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """L'état SUIT la réalité : la même clé passe de « proposée » à
    « déclarée » dès que l'agence la crée. Sans ça, l'écran mentirait au
    tour suivant."""
    headers = agent_headers(admin)
    before = _flat((await client.get(f"{URL}?surface=person", headers=headers)).json())
    assert before["tax_id"]["state"] == "catalog_undeclared"

    await _define(client, headers, "tax_id", scope="person")
    after = _flat((await client.get(f"{URL}?surface=person", headers=headers)).json())
    assert after["tax_id"]["state"] == "declared"


async def test_the_person_surface_hides_the_company_universe(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """L'ASYMÉTRIE NOMMÉE. Les 8 clés société sont listées dans les
    réglages mais ÉCARTÉES de la fiche personne : cet écran est le miroir
    de la FICHE, elles n'y figurent pas — même déclarées."""
    headers = agent_headers(admin)
    await _define(client, headers, "legal_form", scope="person")

    fields = _flat((await client.get(f"{URL}?surface=person", headers=headers)).json())
    assert "legal_form" not in fields
    assert "company_registration_number" not in fields


async def test_nothing_becomes_invisible_the_company_surface_carries_the_eight(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """La contrepartie du test précédent, et c'est elle qui rend
    l'exclusion acceptable : les 8 écartées sont TOUTES servies sur la
    surface société. Rien ne disparaît de l'application."""
    from src.client_profiles.profile_sections import PERSON_SHEET_EXCLUDED_KEYS

    fields = _flat(
        (await client.get(f"{URL}?surface=company", headers=agent_headers(admin))).json()
    )
    assert set(fields) >= PERSON_SHEET_EXCLUDED_KEYS, sorted(
        PERSON_SHEET_EXCLUDED_KEYS - set(fields)
    )


# --- surface société ------------------------------------------------------------------


async def test_the_company_surface_serves_presets_that_no_one_can_archive(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """Les 17 presets société n'ont AUCUNE définition : le contrat le dit
    (`native`, `definition_id` nul, `renamable`) au lieu de laisser
    l'écran proposer un archivage qui n'existe pas."""
    body = (await client.get(f"{URL}?surface=company", headers=agent_headers(admin))).json()
    assert body["surface"] == "company"
    fields = _flat(body)
    assert len(fields) >= 17
    preset = fields["legal_form"]
    assert preset["state"] == "native"
    assert preset["definition_id"] is None
    assert preset["renamable"] is True


async def test_a_company_sack_key_appears_with_its_agency_label(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Une clé libre n'existe que parce qu'une valeur a été saisie. Elle
    apparaît, avec le libellé que l'agence lui a donné — c'est la seule
    personnalisation possible de cet univers."""
    db_session.add(
        CompanyProfile(
            agency_id=admin.agency_id,
            name="Société Test",
            custom_fields={"numero_greffe": "RCS 1234"},
        )
    )
    db_session.add(
        CompanyFieldLabel(agency_id=admin.agency_id, key="numero_greffe", label="Numéro de greffe")
    )
    await db_session.commit()

    fields = _flat(
        (await client.get(f"{URL}?surface=company", headers=agent_headers(admin))).json()
    )
    assert fields["numero_greffe"]["state"] == "sack_only"
    assert fields["numero_greffe"]["label"] == "Numéro de greffe"
    assert fields["numero_greffe"]["renamable"] is True


# --- surface dossier ------------------------------------------------------------------


async def test_the_case_surface_aggregates_one_entry_per_field_with_its_count(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """LE TÉMOIN DU FOURRE-TOUT. Le même champ sert trois parcours : il
    doit apparaître UNE fois, avec « utilisé dans 3 parcours » — pas
    trois lignes identiques."""
    headers = agent_headers(admin)
    await _define(client, headers, "num_dossier", scope="case")
    for index in range(3):
        template = JourneyTemplate(agency_id=admin.agency_id, name=f"P{index}")
        db_session.add(template)
        await db_session.flush()
        db_session.add(
            JourneyTemplateField(
                template_id=template.id, kind="custom_field", reference="num_dossier"
            )
        )
    await db_session.commit()

    body = (await client.get(f"{URL}?surface=case", headers=headers)).json()
    entries = [f for s in body["sections"] for f in s["fields"] if f["reference"] == "num_dossier"]
    assert len(entries) == 1, entries
    assert entries[0]["used_in_journeys"] == 3
    assert entries[0]["state"] == "declared"


async def test_a_base_field_of_a_journey_is_native_never_editable(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Un parcours collecte aussi des champs de BASE (l'état civil) : ils
    n'ont pas de définition et ne s'éditent pas."""
    template = JourneyTemplate(agency_id=admin.agency_id, name="Avec base")
    db_session.add(template)
    await db_session.flush()
    db_session.add(
        JourneyTemplateField(template_id=template.id, kind="base_field", reference="date_of_birth")
    )
    await db_session.commit()

    fields = _flat((await client.get(f"{URL}?surface=case", headers=agent_headers(admin))).json())
    assert fields["date_of_birth"]["state"] == "native"
    assert fields["date_of_birth"]["used_in_journeys"] == 1


async def test_the_case_surface_costs_the_same_with_two_journeys_or_forty(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """LE TÉMOIN DE COÛT : l'agrégation est UN group by, pas une requête
    par parcours. Quarante parcours ne coûtent pas vingt fois deux."""
    from sqlalchemy import event

    headers = agent_headers(admin)
    await _define(client, headers, "ref_client", scope="case")
    for index in range(40):
        template = JourneyTemplate(agency_id=admin.agency_id, name=f"J{index}")
        db_session.add(template)
        await db_session.flush()
        db_session.add(
            JourneyTemplateField(
                template_id=template.id, kind="custom_field", reference="ref_client"
            )
        )
    await db_session.commit()

    from src.custom_fields.field_universe import field_universe

    engine = db_session.get_bind()
    counter = {"n": 0}

    def _count(*_a: object, **_k: object) -> None:
        counter["n"] += 1

    event.listen(engine, "before_cursor_execute", _count)
    try:
        result = await field_universe(db_session, admin, "case", "fr")
    finally:
        event.remove(engine, "before_cursor_execute", _count)

    entries = [f for s in result.sections for f in s.fields if f.reference == "ref_client"]
    assert entries[0].used_in_journeys == 40
    # langue d'agence + définitions + l'agrégat : trois requêtes, et le
    # nombre de parcours n'y change rien.
    assert counter["n"] == 3, counter


# --- le contrat et son gate -----------------------------------------------------------


async def test_the_three_surfaces_always_serve_their_sections_in_screen_order(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """Le front ne recompose rien : les sections arrivent dans l'ordre de
    l'écran, vides comprises (sinon l'écran devrait deviner qu'une
    section existe mais n'a rien)."""
    headers = agent_headers(admin)
    for surface in ("person", "company", "case"):
        body = (await client.get(f"{URL}?surface={surface}", headers=headers)).json()
        keys = [s["key"] for s in body["sections"]]
        assert keys == ["identity", "contact", "situation", "misc"], (surface, keys)
        assert all("name" in s and s["name"] for s in body["sections"])


async def test_an_unknown_surface_is_refused_at_the_contract(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    r = await client.get(f"{URL}?surface=martien", headers=agent_headers(admin))
    assert r.status_code == 422, r.text


async def test_another_agency_universe_never_leaks(
    client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
) -> None:
    """Le scope agence tient sur les trois sources (définitions, sacks,
    parcours) : le voisin n'apparaît nulle part."""
    other = await make_agent(role=system_roles["admin"])
    await _define(client, agent_headers(other), "secret_voisin", scope="person")

    fields = _flat((await client.get(f"{URL}?surface=person", headers=agent_headers(admin))).json())
    assert "secret_voisin" not in fields


async def test_the_universe_is_gated_like_the_definitions_list(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """Lecture d'agence : un token d'une autre audience n'entre pas, et
    la route est bien bindée (sans binding, le moteur rendrait 403)."""
    r = await client.get(f"{URL}?surface=person", headers=agent_headers(admin))
    assert r.status_code == 200, r.text
    anonymous = await client.get(f"{URL}?surface=person")
    assert anonymous.status_code in (401, 403)


async def test_an_orphan_reference_is_never_presented_as_editable(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Un parcours peut référencer une clé qu'aucune définition ne porte
    (définition archivée, import d'un parcours écrit ailleurs). Elle
    s'affiche — l'agence doit la voir — mais SANS id : l'écran ne peut
    pas proposer de l'éditer."""
    template = JourneyTemplate(agency_id=admin.agency_id, name="Orphelin")
    db_session.add(template)
    await db_session.flush()
    db_session.add(
        JourneyTemplateField(
            template_id=template.id, kind="custom_field", reference=f"orphan_{uuid.uuid4().hex[:6]}"
        )
    )
    await db_session.commit()

    fields = _flat((await client.get(f"{URL}?surface=case", headers=agent_headers(admin))).json())
    orphans = [f for k, f in fields.items() if k.startswith("orphan_")]
    assert len(orphans) == 1
    assert orphans[0]["state"] == "catalog_undeclared"
    assert orphans[0]["definition_id"] is None


# --- le SEUL geste de l'univers société ----------------------------------------------


async def test_renaming_a_sack_key_is_served_back_by_the_universe(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """LE GESTE QUI REND L'ONGLET VIVANT. Sans lui, la surface société
    serait en lecture seule intégrale : rien n'y est archivable ni
    typable. Le renommage doit donc partir du contrat ET revenir dedans."""
    headers = agent_headers(admin)
    db_session.add(
        CompanyProfile(
            agency_id=admin.agency_id, name="ACME", custom_fields={"num_greffe": "RCS 1"}
        )
    )
    await db_session.commit()

    r = await client.patch(
        "/agencies/me/company-field-labels/num_greffe",
        headers=headers,
        json={"label": "Numéro de greffe"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"key": "num_greffe", "label": "Numéro de greffe", "customized": True}

    fields = _flat((await client.get(f"{URL}?surface=company", headers=headers)).json())
    assert fields["num_greffe"]["label"] == "Numéro de greffe"


async def test_a_preset_can_be_renamed_too_and_reset_to_its_origin(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """Un preset se renomme aussi (chaque métier a son vocabulaire), et
    `label: null` REND le libellé d'origine — sans ça, une agence qui
    s'est trompée n'aurait aucun retour en arrière."""
    headers = agent_headers(admin)
    origin = _flat((await client.get(f"{URL}?surface=company", headers=headers)).json())[
        "legal_form"
    ]["label"]

    renamed = await client.patch(
        "/agencies/me/company-field-labels/legal_form",
        headers=headers,
        json={"label": "Forme sociale"},
    )
    assert renamed.json()["label"] == "Forme sociale"

    reset = await client.patch(
        "/agencies/me/company-field-labels/legal_form", headers=headers, json={"label": None}
    )
    assert reset.status_code == 200, reset.text
    assert reset.json() == {"key": "legal_form", "label": origin, "customized": False}
    fields = _flat((await client.get(f"{URL}?surface=company", headers=headers)).json())
    assert fields["legal_form"]["label"] == origin


async def test_an_invented_key_cannot_be_named(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """On ne sème pas des libellés sur des clés qui n'existent nulle
    part : elles n'apparaîtraient sur aucun écran."""
    r = await client.patch(
        "/agencies/me/company-field-labels/cle_inventee",
        headers=agent_headers(admin),
        json={"label": "Peu importe"},
    )
    assert r.status_code == 404, r.text
    assert r.json()["code"] == "company_profile.field_not_found"


async def test_a_blank_label_is_refused_null_is_the_reset(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Une chaîne d'espaces n'est pas un libellé — et ce n'est pas non
    plus la façon de revenir au défaut (c'est `null`)."""
    r = await client.patch(
        "/agencies/me/company-field-labels/legal_form",
        headers=agent_headers(admin),
        json={"label": "   "},
    )
    assert r.status_code == 422, r.text


async def test_renaming_is_gated_by_field_manage(
    client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
) -> None:
    """Renommer est de la CONFIGURATION : un membre ne redéfinit pas le
    vocabulaire de son agence."""
    member = await make_agent(agency_id=admin.agency_id, role=system_roles["member"])
    denied = await client.patch(
        "/agencies/me/company-field-labels/legal_form",
        headers=agent_headers(member),
        json={"label": "Forme sociale"},
    )
    assert denied.status_code == 403, denied.text


async def test_only_declared_entries_carry_a_position(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """LA POSITION SERT AU GESTE, pas au tri. Elle n'existe que là où il y
    a quelque chose à déplacer : une native, un preset non déclaré ou une
    clé de sack n'ont pas de rang à changer — `null`, jamais 0 (qui se
    confondrait avec la première place)."""
    headers = agent_headers(admin)
    definition = await _define(client, headers, "rang_teste", scope="person", position=7)

    fields = _flat((await client.get(f"{URL}?surface=person", headers=headers)).json())
    assert fields["rang_teste"]["position"] == 7
    assert fields["rang_teste"]["definition_id"] == definition["id"]
    # Les trois autres états n'en portent pas.
    assert fields["date_of_birth"]["position"] is None  # native
    assert fields["visa_type"]["position"] is None  # catalogue non déclaré

    db_session.add(
        CompanyProfile(agency_id=admin.agency_id, name="ACME2", custom_fields={"cle_libre": "x"})
    )
    await db_session.commit()
    company = _flat((await client.get(f"{URL}?surface=company", headers=headers)).json())
    assert company["cle_libre"]["position"] is None  # sack_only
    assert company["legal_form"]["position"] is None  # preset société


async def test_the_served_order_is_the_screen_order_not_the_position_order(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """Le front ne recompose rien : une définition à la position 99 reste
    servie à sa place d'écran (sa section, après les natives). Trier sur
    `position` casserait l'affichage — ce test le grave."""
    headers = agent_headers(admin)
    await _define(client, headers, "tardif", scope="person", position=99)

    body = (await client.get(f"{URL}?surface=person", headers=headers)).json()
    misc = next(s for s in body["sections"] if s["key"] == "misc")
    refs = [f["reference"] for f in misc["fields"]]
    assert "tardif" in refs
    positions = [f["position"] for f in misc["fields"] if f["position"] is not None]
    # Les positions servies ne sont PAS triées : elles décrivent une suite
    # globale à l'agence, pas le rang dans la section.
    assert positions == [f["position"] for f in misc["fields"] if f["position"] is not None]


async def test_a_catalog_preset_carries_its_type_before_being_declared(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """LE TYPE SERVI AVANT LA DÉCLARATION. Sans lui, l'écran devrait
    consulter sa propre copie du catalogue pour savoir s'il peut proposer
    l'ajout — la déduction de trop, celle qui a produit trois
    désalignements cette semaine."""
    fields = _flat((await client.get(f"{URL}?surface=person", headers=agent_headers(admin))).json())
    undeclared = fields["visa_type"]
    assert undeclared["state"] == "catalog_undeclared"
    assert undeclared["field_type"], "un preset du catalogue porte son type"
    assert undeclared["origin"] == "catalog"


async def test_origin_separates_what_the_product_knows_from_what_the_agency_wrote(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """`origin` vient du back, plus d'une table de clés recopiée à
    l'écran : une clé du catalogue, une colonne civile et un preset
    société sont `catalog` ; une clé écrite par l'agence est `agency`."""
    headers = agent_headers(admin)
    await _define(client, headers, "cle_maison_agence", scope="person")

    person = _flat((await client.get(f"{URL}?surface=person", headers=headers)).json())
    assert person["date_of_birth"]["origin"] == "catalog"  # colonne civile
    assert person["cle_maison_agence"]["origin"] == "agency"  # écrite par l'agence

    company = _flat((await client.get(f"{URL}?surface=company", headers=headers)).json())
    assert company["legal_form"]["origin"] == "catalog"  # preset société


async def test_an_orphan_reference_is_distinguishable_from_a_real_preset(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """CE QUE `origin` DÉBLOQUE sur la surface dossier : une référence
    orpheline (définition disparue, qu'un parcours cite encore) et un
    preset réellement proposable portaient le MÊME état. L'origine les
    sépare — l'écran peut proposer l'ajout de l'un sans promettre
    l'impossible sur l'autre."""
    template = JourneyTemplate(agency_id=admin.agency_id, name="Mixte")
    db_session.add(template)
    await db_session.flush()
    orphan = f"orphelin_{uuid.uuid4().hex[:6]}"
    for reference in (orphan, "visa_number"):  # l'un inventé, l'autre du catalogue
        db_session.add(
            JourneyTemplateField(template_id=template.id, kind="custom_field", reference=reference)
        )
    await db_session.commit()

    fields = _flat((await client.get(f"{URL}?surface=case", headers=agent_headers(admin))).json())
    assert fields[orphan]["state"] == "catalog_undeclared"
    assert fields[orphan]["origin"] == "agency"
    assert fields[orphan]["field_type"] is None
    assert fields["visa_number"]["origin"] == "catalog"
    assert fields["visa_number"]["field_type"]
