"""LE DÉFAUT SILENCIEUX EST MORT — et ce fichier le garde mort.

Trois régressions en trois semaines, toutes du même motif : un appelant
crée une définition sans dire sa portée, le défaut « mission » s'applique
sans bruit, et personne ne le découvre avant qu'une agence signale des
champs invisibles sur ses fiches. Les trois coupables — le seed du
dossier démo, l'import de parcours, le script de seed — passaient à côté
d'une classification qui existait pourtant.

Deux garanties, et la seconde est la seule qui tienne dans le temps :

1. LA CLASSIFICATION EST LA MÊME PARTOUT : une clé du catalogue naît
   « personne » quel que soit le chemin qui la matérialise.
2. OUBLIER LA PORTÉE NE PASSE PLUS EN SILENCE : sans défaut de colonne,
   l'insert ÉCHOUE. Le prochain appelant distrait sera arrêté par la
   première passe de tests, pas par un client six semaines plus tard.
"""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.custom_field import CustomFieldDefinition
from shared.models.rbac import Role
from src.client_profiles.profile_sections import (
    PRESET_PROFILE_SECTION,
    catalog_classification,
)
from tests.plugins.agent_plugin import MakeAgent

pytestmark = pytest.mark.usefixtures("rbac_baseline")


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


# --- la règle, écrite une seule fois --------------------------------------------------


def test_a_catalog_person_key_is_classified_person_with_its_section() -> None:
    """`tax_id` est un trait de personne : la règle le dit, et c'est
    d'elle que dépendent les cinq appelants."""
    assert catalog_classification("tax_id") == ("person", PRESET_PROFILE_SECTION["tax_id"])
    assert catalog_classification("birth_country") == ("person", "identity")


def test_a_key_outside_the_catalog_stays_a_case_field() -> None:
    """Une clé inventée par une agence n'est pas requalifiée d'office :
    elle reste « mission »/« divers », et c'est l'agence qui tranche."""
    assert catalog_classification("truc_maison_de_lagence") == ("case", "misc")


def test_every_catalog_person_key_lands_in_a_real_sheet_section() -> None:
    """Aucune clé « personne » ne peut atterrir dans une section que la
    fiche ne sait pas afficher — sinon elle serait classée bien, et
    invisible quand même."""
    known = {"identity", "contact", "situation", "misc"}
    for key in PRESET_PROFILE_SECTION:
        scope, section = catalog_classification(key)
        assert scope == "person", key
        assert section in known, (key, section)


# --- le silence est mort --------------------------------------------------------------


async def test_forgetting_the_scope_fails_loudly_instead_of_defaulting(
    db_session: AsyncSession, admin: Agent
) -> None:
    """LE TÉMOIN DE L'ARBITRAGE. Avant, cette insertion passait et posait
    'case' en silence : c'est exactement ce qui a produit les 22 champs
    invisibles de QuinnAshford. Elle doit désormais ÉCHOUER."""
    db_session.add(
        CustomFieldDefinition(
            agency_id=admin.agency_id,
            key=f"oubli_{uuid.uuid4().hex[:6]}",
            label="Portée oubliée",
            field_type="text",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_the_same_insert_passes_the_moment_the_scope_is_said(
    db_session: AsyncSession, admin: Agent
) -> None:
    """Le contraire du précédent : dire la portée suffit, il n'y a rien
    d'autre à savoir. La contrainte guide, elle ne punit pas."""
    key = f"dit_{uuid.uuid4().hex[:6]}"
    scope, section = catalog_classification(key)
    db_session.add(
        CustomFieldDefinition(
            agency_id=admin.agency_id,
            key=key,
            label="Portée dite",
            field_type="text",
            scope=scope,
            profile_section=section,
        )
    )
    await db_session.flush()
    assert scope == "case"  # clé hors catalogue : le comportement d'avant, mais VOULU


async def test_the_demo_seed_classifies_its_catalog_fields(
    db_session: AsyncSession, admin: Agent
) -> None:
    """LE TROU DU CONSTAT, refermé : le seed du dossier démo matérialise
    des presets du catalogue ; ils doivent naître « personne », donc
    visibles sur les fiches de l'agence dès sa création."""
    import inspect

    from src.agencies.demo_case_seed import _materialize_field_definitions

    # Le seed passe par LA règle partagée — pas par une copie qui
    # dériverait (c'est la copie qui a produit le trou : trois exemplaires
    # de la règle, deux appelants qui l'ignoraient).
    source = inspect.getsource(_materialize_field_definitions)
    assert "catalog_classification(key)" in source
    assert "scope=scope" in source and "profile_section=section" in source
    # Et la règle elle-même dit bien « personne » pour ces clés-là.
    for key in ("tax_id", "risk_profile", "birth_country"):
        assert catalog_classification(key)[0] == "person", key
