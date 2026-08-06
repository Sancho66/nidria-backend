"""LA PORTÉE DES CHAMPS — choisie à la création, rattrapable après coup.

Le constat qui a produit ce lot : l'API de création n'acceptait AUCUNE
portée, le défaut `case` s'appliquait donc toujours, et un champ créé
depuis les réglages ne pouvait structurellement jamais apparaître sur une
fiche client. L'agence croyait créer un champ « client », elle créait un
champ « dossier », sans que rien ne le dise ni ne permette de le corriger.

Ce fichier tient les deux garanties du lot :

1. La portée demandée est HONORÉE — un champ « personne » apparaît sur la
   fiche, un champ « mission » n'y apparaît pas.
2. La reclassification NE TOUCHE AUCUNE VALEUR. C'est ce qui rend le
   rattrapage sûr sur un annuaire déjà rempli : les valeurs vivent dans
   les sacs JSONB, seule la surface bouge. Testé dans les DEUX sens,
   parce qu'un rattrapage qu'on ne sait pas défaire n'en est pas un.
"""

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.client_profile import ClientProfile
from shared.models.rbac import Role
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent

pytestmark = pytest.mark.usefixtures("rbac_baseline")


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


async def _define(client: AsyncClient, headers: dict[str, str], **overrides: object) -> dict:
    payload = {
        "key": "permit_type",
        "label": "Type de permis",
        "field_type": "text",
        **overrides,
    }
    r = await client.post("/agencies/me/custom-fields", headers=headers, json=payload)
    assert r.status_code == 201, r.text
    return r.json()


async def _sheet_keys(client: AsyncClient, headers: dict[str, str], profile_id: str) -> set[str]:
    """Les clés que la FICHE sert vraiment — l'union de ses sections, ce
    que l'écran affiche et ce que sa recherche peut trouver."""
    r = await client.get(f"/client-profiles/{profile_id}", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    return {ref for section in body["sections"] for ref in section["references"]}


# --- la portée demandée est honorée ---------------------------------------------------


async def test_a_person_field_lands_on_the_client_sheet(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """LE TÉMOIN du lot : demander « décrit la personne » suffit désormais
    à voir le champ sur une fiche. Avant, c'était impossible par l'API."""
    headers = agent_headers(admin)
    definition = await _define(headers=headers, client=client, scope="person")
    assert definition["scope"] == "person"

    created = await client.post(
        "/client-profiles",
        headers=headers,
        json={"first_name": "Ana", "last_name": "Silva", "email": "ana@example.com"},
    )
    assert created.status_code == 201, created.text
    assert "permit_type" in await _sheet_keys(client, headers, created.json()["id"])


async def test_a_case_field_stays_off_the_sheet(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """L'autre branche du choix, tout aussi explicite : « décrit la
    mission » ne pollue pas la fiche client."""
    headers = agent_headers(admin)
    definition = await _define(headers=headers, client=client, scope="case")
    assert definition["scope"] == "case"

    created = await client.post(
        "/client-profiles",
        headers=headers,
        json={"first_name": "Bruno", "last_name": "Costa", "email": "bruno@example.com"},
    )
    assert "permit_type" not in await _sheet_keys(client, headers, created.json()["id"])


async def test_an_absent_scope_keeps_the_historical_default(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """Ce lot OUVRE le choix, il ne déplace pas le défaut : un appelant
    qui se tait obtient exactement ce qu'il obtenait avant."""
    definition = await _define(headers=agent_headers(admin), client=client)
    assert definition["scope"] == "case"


# --- le rattrapage, dans les deux sens, sans perdre une valeur ------------------------


async def test_reclassifying_moves_the_surface_and_never_the_values(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """LES DEUX SENS, sans perdre une valeur. Le scénario suit le chemin
    RÉEL d'une valeur de fiche : elle ne peut être saisie que pendant que
    le champ est « personne » (le PATCH de fiche refuse une clé de portée
    mission — 422, vérifié ici même). On la pose, on bascule en mission,
    on revient : la valeur n'a pas bougé d'un octet."""
    headers = agent_headers(admin)
    definition = await _define(headers=headers, client=client, scope="person")

    created = await client.post(
        "/client-profiles",
        headers=headers,
        json={"first_name": "Carla", "last_name": "Dias", "email": "carla@example.com"},
    )
    profile_id = created.json()["id"]
    patched = await client.patch(
        f"/client-profiles/{profile_id}",
        headers=headers,
        json={"custom_fields": {"permit_type": "Mercosur"}},
    )
    assert patched.status_code == 200, patched.text
    assert "permit_type" in await _sheet_keys(client, headers, profile_id)

    async def stored_value() -> str | None:
        profile = await db_session.get(ClientProfile, profile_id)
        assert profile is not None
        await db_session.refresh(profile)
        return (profile.custom_fields or {}).get("permit_type")

    assert await stored_value() == "Mercosur"

    # person → case : le champ QUITTE la fiche, la valeur RESTE en base.
    to_case = await client.patch(
        f"/agencies/me/custom-fields/{definition['id']}",
        headers=headers,
        json={"scope": "case"},
    )
    assert to_case.status_code == 200, to_case.text
    assert to_case.json()["scope"] == "case"
    assert "permit_type" not in await _sheet_keys(client, headers, profile_id)
    assert await stored_value() == "Mercosur"
    # Et tant qu'il est « mission », la fiche REFUSE la clé — c'est ce qui
    # rend le sens inverse indispensable plutôt que cosmétique.
    refused = await client.patch(
        f"/client-profiles/{profile_id}",
        headers=headers,
        json={"custom_fields": {"permit_type": "Autre"}},
    )
    assert refused.status_code == 422, refused.text

    # case → person : il revient, avec sa valeur intacte.
    back = await client.patch(
        f"/agencies/me/custom-fields/{definition['id']}",
        headers=headers,
        json={"scope": "person"},
    )
    assert back.status_code == 200, back.text
    assert "permit_type" in await _sheet_keys(client, headers, profile_id)
    assert await stored_value() == "Mercosur"


async def test_the_reclassified_value_is_readable_again_on_the_sheet(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """Le rattrapage n'est pas qu'une ligne de plus : la valeur déjà
    saisie se LIT sur la fiche après le retour (le cas Nicolas — des
    données saisies, invisibles le temps d'un mauvais classement)."""
    headers = agent_headers(admin)
    definition = await _define(headers=headers, client=client, scope="person")
    created = await client.post(
        "/client-profiles",
        headers=headers,
        json={"first_name": "Dora", "last_name": "Neves", "email": "dora@example.com"},
    )
    profile_id = created.json()["id"]
    await client.patch(
        f"/client-profiles/{profile_id}",
        headers=headers,
        json={"custom_fields": {"permit_type": "Temporaire"}},
    )
    for scope in ("case", "person"):  # aller-retour complet
        await client.patch(
            f"/agencies/me/custom-fields/{definition['id']}",
            headers=headers,
            json={"scope": scope},
        )
    sheet = (await client.get(f"/client-profiles/{profile_id}", headers=headers)).json()
    # La valeur EST là, et la fiche la sert de nouveau dans ses sections :
    # c'est bien le champ qui revient, pas seulement sa donnée.
    assert sheet["custom_fields"]["permit_type"] == "Temporaire"
    assert "permit_type" in await _sheet_keys(client, headers, profile_id)


async def test_scope_is_still_gated_by_field_manage(
    client: AsyncClient,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """Reclasser reste un geste de configuration : un membre sans
    `field.manage` ne requalifie pas l'univers de son agence."""
    definition = await _define(headers=agent_headers(admin), client=client, scope="case")
    member = await make_agent(agency_id=admin.agency_id, role=system_roles["member"])
    denied = await client.patch(
        f"/agencies/me/custom-fields/{definition['id']}",
        headers=agent_headers(member),
        json={"scope": "person"},
    )
    assert denied.status_code == 403, denied.text


async def test_creating_a_person_field_is_also_gated(
    client: AsyncClient,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """Le nouveau champ du contrat n'ouvre aucune porte : la création
    garde son gate."""
    member = await make_agent(agency_id=admin.agency_id, role=system_roles["member"])
    denied = await client.post(
        "/agencies/me/custom-fields",
        headers=agent_headers(member),
        json={
            "key": "permit_type",
            "label": "Type de permis",
            "field_type": "text",
            "scope": "person",
        },
    )
    assert denied.status_code == 403, denied.text
