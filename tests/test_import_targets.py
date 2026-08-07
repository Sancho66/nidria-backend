"""`GET /imports/targets` — l'univers des cibles SERVI.

Le front tenait un miroir de cet univers (`importTargets.ts`) : la liste
des presets acceptés, leurs sections, les clés écartées. Un miroir date,
et quand il date l'agence voit une cible que l'import refuse (mur en
422) ou ne voit pas une cible que le back accepte (colonne perdue sans
un mot). Cet endpoint le remplace : la source unique (`import_targets`)
s'habille et se sert.

Ce qui est gravé ici :
  - l'ÉGALITÉ servi == accepté (personne au jeton près, société au
    détail des alias de compat près, et l'écart est nommé) ;
  - LA GARDE DE PARITÉ, devenue triviale : sur les 96 en-têtes réels
    Teamleader, tout ce que le suggéreur propose est servi — plus
    aucun parseur, les deux côtés lisent la même fonction ;
  - les FLAGS que le combobox affiche (obligatoire, sous-champ de
    quelle base, « sera ajouté », clé baptisée) ;
  - les libellés ×7 et leur résolution dans la langue de la requête.
"""

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.company_profile import CompanyFieldDefinition
from shared.models.custom_field import CustomFieldDefinition
from shared.models.rbac import Role
from src.core.i18n import SUPPORTED_LANGUAGES
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.expat_plugin import MakeExpatUser
from tests.test_profile_import import COMPANY_HEADERS_54, CONTACT_HEADERS_42

pytestmark = pytest.mark.usefixtures("rbac_baseline")


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


async def _targets(
    client: AsyncClient, headers: dict[str, str], entity: str, **params: str
) -> dict:
    response = await client.get(
        "/imports/targets", headers=headers, params={"entity": entity, **params}
    )
    assert response.status_code == 200, response.text
    return response.json()


# ─── L'égalité servi == accepté ───────────────────────────────────────────


async def test_person_targets_served_are_exactly_the_import_universe(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """LE CONTRAT : ce que la liste offre EST ce que l'import accepte,
    au jeton près. Pas un sous-ensemble prudent (une cible atteignable
    et non offerte est une colonne perdue), pas un sur-ensemble (une
    cible offerte et refusée est un mur en 422)."""
    from src.imports.import_targets import person_targets

    # Une agence réelle : une clé libre, et une base adresse DÉCLARÉE
    # (ses 4 morceaux doivent être servis comme ceux du catalogue).
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

    body = await _targets(client, agent_headers(admin), "person")
    served = {t["key"] for t in body["targets"]}
    universe = (await person_targets(db_session, admin.agency_id)).valid
    assert served == universe, (
        f"servi seul : {sorted(served - universe)} / accepté seul : {sorted(universe - served)}"
    )
    # Aucun doublon : une cible, une entrée.
    assert len(served) == len(body["targets"])


async def test_company_targets_served_are_the_universe_minus_compat_aliases(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """LE SEUL ÉCART ASSUMÉ, et il est nommé : les alias de compat
    restent ACCEPTÉS sans être OFFERTS (offrir `registration_number` à
    côté de `company_registration_number`, ce serait deux options pour
    un champ). Gravé pour qu'il ne grandisse pas en douce."""
    from src.client_profiles.profile_sections import COMPANY_TARGET_ALIASES
    from src.imports.import_targets import company_targets

    body = await _targets(client, agent_headers(admin), "company")
    served = {t["key"] for t in body["targets"]}
    universe = (await company_targets(db_session, admin.agency_id)).valid
    assert served == universe - set(COMPANY_TARGET_ALIASES)
    assert set(COMPANY_TARGET_ALIASES) <= universe  # toujours acceptés


# ─── LA GARDE DE PARITÉ, devenue triviale ─────────────────────────────────


@pytest.mark.parametrize(
    ("entity", "suggest_path", "headers_96"),
    [
        ("person", "/imports/client-profiles/suggest-mapping", CONTACT_HEADERS_42),
        ("company", "/imports/company-profiles/suggest-mapping", COMPANY_HEADERS_54),
    ],
)
async def test_every_suggested_target_is_served(
    client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
    entity: str,
    suggest_path: str,
    headers_96: list[str],
) -> None:
    """LA LOI : tout ce que le suggéreur PROPOSE, la liste l'OFFRE.

    Elle tenait jusqu'ici dans un script front de 600 lignes qui
    parsait les tables python du back et rejouait le matching — un
    témoin qui pouvait dater comme le miroir qu'il surveillait. Les
    deux côtés lisent maintenant `import_targets` : le témoin se
    résume à comparer deux réponses HTTP sur les 96 en-têtes réels.
    """
    auth = agent_headers(admin)
    served = {t["key"] for t in (await _targets(client, auth, entity))["targets"]}
    response = await client.post(suggest_path, headers=auth, json={"headers": headers_96})
    assert response.status_code == 200, response.text
    body = response.json()
    proposed = set(body["suggestions"].values()) | {
        target for options in body["ambiguous"].values() for target in options
    }
    assert proposed, "le suggéreur ne propose rien sur 96 en-têtes réels : parse cassé ?"
    assert proposed <= served, f"suggérées mais non servies : {sorted(proposed - served)}"


# ─── Les flags que le combobox affiche ────────────────────────────────────


async def test_person_flags_required_will_create_named_and_subfields(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Les quatre mentions de la grille, chacune sur un cas réel."""
    db_session.add_all(
        [
            # Un preset DÉCLARÉ (donc pas « sera ajouté »), traduit par
            # l'agence en français seulement.
            CustomFieldDefinition(
                agency_id=admin.agency_id,
                key="whatsapp",
                label="WhatsApp pro",
                label_i18n={"fr": "WhatsApp pro"},
                field_type="text",
                scope="person",
                profile_section="contact",
            ),
            # Une clé BAPTISÉE par l'agence — hors vocabulaire produit.
            CustomFieldDefinition(
                agency_id=admin.agency_id,
                key="num_dossier",
                label="N° de dossier",
                field_type="text",
                scope="person",
            ),
        ]
    )
    await db_session.commit()

    body = await _targets(client, agent_headers(admin), "person")
    by_key = {t["key"]: t for t in body["targets"]}

    # OBLIGATOIRE : le trio d'identité, et lui seul.
    assert {k for k, t in by_key.items() if t["required"]} == {
        "first_name",
        "last_name",
        "email",
    }

    # « SERA AJOUTÉ » : un preset du catalogue non déclaré — l'import
    # crée sa définition en chemin, la liste le dit avant d'agir.
    assert by_key["passport_expiry"]["will_create"] is True
    assert by_key["passport_expiry"]["agency_named"] is False
    # …et le même preset DÉCLARÉ ne le dit plus.
    assert by_key["whatsapp"]["will_create"] is False
    assert by_key["whatsapp"]["label"] == "WhatsApp pro"

    # CLÉ BAPTISÉE : le produit ne connaît pas la clé, donc pas de ×7.
    named = by_key["num_dossier"]
    assert named["agency_named"] is True
    assert named["label"] == "N° de dossier"
    assert named["label_i18n"] == {}
    # Une colonne civile native n'est baptisée par personne.
    assert by_key["phone"]["agency_named"] is False

    # SOUS-CHAMP DE QUELLE BASE : la base porte le type `address`, ses 4
    # morceaux la nomment, restent DANS sa section et la ferment —
    # groupés, dans l'ordre de lecture d'une enveloppe.
    base = by_key["residence_address"]
    assert base["field_type"] == "address"
    assert base["address_base"] is None
    contact = [t["key"] for t in body["targets"] if t["section"] == base["section"]]
    assert contact[-4:] == [
        "residence_address.street",
        "residence_address.city",
        "residence_address.postal_code",
        "residence_address.country",
    ]
    assert contact.index("residence_address") < contact.index("residence_address.street")
    street = by_key["residence_address.street"]
    assert street["address_base"] == "residence_address"
    assert street["address_subfield"] == "street"
    assert street["section"] == base["section"]
    # La section n'est pas coupée en deux : ses cibles sont contiguës
    # (sinon le combobox afficherait deux fois le même titre de groupe).
    keys = [t["key"] for t in body["targets"]]
    first = keys.index(contact[0])
    assert keys[first : first + len(contact)] == contact


async def test_declared_definition_decides_its_section(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """La section d'une clé DÉCLARÉE vient de sa définition (le toggle
    de la fiche l'édite), pas de la table de migration : le combobox et
    la fiche rangent le même champ au même endroit."""
    db_session.add(
        CustomFieldDefinition(
            agency_id=admin.agency_id,
            key="job_title",  # table de migration : 'situation'
            label="Poste",
            field_type="text",
            scope="person",
            profile_section="identity",  # …l'agence l'a rangé ailleurs
        )
    )
    await db_session.commit()

    body = await _targets(client, agent_headers(admin), "person")
    by_key = {t["key"]: t for t in body["targets"]}
    assert by_key["job_title"]["section"] == "identity"
    # Un preset NON déclaré garde, lui, la section de la table.
    assert by_key["industry"]["section"] == "situation"


async def test_company_flags_name_named_keys_and_address_bases(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Face société : la dénomination est la clé de dédup (obligatoire),
    les clés baptisées portent leur label ET leur kind de naissance, et
    les deux bases adresse ouvrent leurs morceaux."""
    db_session.add(
        CompanyFieldDefinition(
            agency_id=admin.agency_id,
            key="chiffre_affaires",
            label="Chiffre d'affaires",
            field_type="number",
        )
    )
    await db_session.commit()

    body = await _targets(client, agent_headers(admin), "company")
    by_key = {t["key"]: t for t in body["targets"]}

    assert {k for k, t in by_key.items() if t["required"]} == {"name"}
    named = by_key["chiffre_affaires"]
    assert named["agency_named"] is True
    assert named["label"] == "Chiffre d'affaires"
    assert named["field_type"] == "number"  # le kind de la naissance voyage
    assert named["section"] == "misc"

    # Rien n'est « sera ajouté » côté société : une valeur tombe dans le
    # sac JSONB, il n'y a pas de définition à créer.
    assert not [t for t in body["targets"] if t["will_create"]]

    for base in ("address", "headquarters_address"):
        assert by_key[base]["field_type"] == "address"
        assert by_key[f"{base}.city"]["address_base"] == base


# ─── Les libellés ×7 et leur résolution ───────────────────────────────────


async def test_labels_are_served_in_seven_languages(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """La mécanique du fix traductions : le back livre les 7 langues,
    aucune cible du vocabulaire PRODUIT n'en manque une. Seules les clés
    baptisées par une agence n'ont qu'une langue — celle où elles ont
    été écrites (on ne traduit pas ce qu'on ne connaît pas)."""
    for entity in ("person", "company"):
        body = await _targets(client, agent_headers(admin), entity)
        assert {s["key"] for s in body["sections"]} == {
            "identity",
            "contact",
            "situation",
            "misc",
        }
        for section in body["sections"]:
            assert set(section["label_i18n"]) == set(SUPPORTED_LANGUAGES), section["key"]
        for target in body["targets"]:
            assert target["label"], target["key"]
            if target["agency_named"]:
                continue
            assert set(target["label_i18n"]) == set(SUPPORTED_LANGUAGES), target["key"]
        # Toute cible tombe dans une section servie.
        served_sections = {s["key"] for s in body["sections"]}
        assert {t["section"] for t in body["targets"]} <= served_sections


async def test_label_follows_the_request_language(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """`label` est le chemin court : déjà résolu pour la langue de la
    requête. `label_i18n` reste servi entier — une bascule de langue à
    l'écran ne redemande rien."""
    auth = agent_headers(admin)
    fr = {t["key"]: t for t in (await _targets(client, auth, "person"))["targets"]}
    ru = {t["key"]: t for t in (await _targets(client, auth, "person", lang="ru"))["targets"]}
    assert fr["date_of_birth"]["label"] == "Date de naissance"
    assert ru["date_of_birth"]["label"] == "Дата рождения"
    assert ru["date_of_birth"]["label_i18n"]["fr"] == "Date de naissance"
    sections_ru = {
        s["key"]: s["label"]
        for s in (await _targets(client, auth, "person", lang="ru"))["sections"]
    }
    assert sections_ru["identity"] == "Личные данные"


# ─── Le contrat HTTP : audience, permission, entité, cache ────────────────


async def test_missing_token_401(client: AsyncClient) -> None:
    response = await client.get("/imports/targets", params={"entity": "person"})
    assert response.status_code == 401


async def test_agent_without_permission_403(
    client: AsyncClient, agent: Agent, agent_headers: AuthHeaders
) -> None:
    response = await client.get(
        "/imports/targets", headers=agent_headers(agent), params={"entity": "person"}
    )
    assert response.status_code == 403


async def test_expat_token_is_not_an_agent_token(
    client: AsyncClient,
    make_expat_user: MakeExpatUser,
    expat_headers: AuthHeaders,
) -> None:
    expat = await make_expat_user()
    response = await client.get(
        "/imports/targets", headers=expat_headers(expat), params={"entity": "person"}
    )
    assert response.status_code == 401


async def test_unknown_entity_422(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    response = await client.get(
        "/imports/targets", headers=agent_headers(admin), params={"entity": "dossier"}
    )
    assert response.status_code == 422


async def test_default_entity_is_person(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    response = await client.get("/imports/targets", headers=agent_headers(admin))
    assert response.status_code == 200
    assert response.json()["entity"] == "person"


async def test_response_is_privately_cacheable(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """Le menu se relit à chaque montage du wizard : une demi-minute de
    cache PRIVÉ (jamais partagé — l'univers est celui d'UNE agence)."""
    response = await client.get(
        "/imports/targets", headers=agent_headers(admin), params={"entity": "person"}
    )
    assert response.headers["cache-control"] == "private, max-age=30"


async def test_universe_is_scoped_to_the_agency(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """Une clé déclarée chez A n'est jamais une cible chez B."""
    db_session.add(
        CustomFieldDefinition(
            agency_id=admin.agency_id,
            key="num_dossier",
            label="N° de dossier",
            field_type="text",
            scope="person",
        )
    )
    await db_session.commit()
    other = await make_agent(role=system_roles["admin"])
    assert other.agency_id != admin.agency_id

    mine = await _targets(client, agent_headers(admin), "person")
    theirs = await _targets(client, agent_headers(other), "person")
    assert "num_dossier" in {t["key"] for t in mine["targets"]}
    assert "num_dossier" not in {t["key"] for t in theirs["targets"]}
