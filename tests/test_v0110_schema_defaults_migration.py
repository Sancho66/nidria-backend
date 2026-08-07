"""D15 — LA CLASSE DE BUG FERMÉE : modèle ≠ migration sur les défauts.

L'incident : `agency_profile_section` est née (p3c7f1a9d5e8) avec
`created_at`/`updated_at` NOT NULL SANS server_default, alors que
`TimestampMixin` est server_default-only — l'ORM n'envoie jamais de
valeur, il compte sur la base. La base de TEST naît des modèles
(`metadata.create_all`) : le défaut y existe, tout est vert. Dev et prod
naissent des MIGRATIONS : le défaut n'y est pas, et le premier
`POST /agencies/me/profile-sections` meurt en NOT NULL violation. Tout
ce qui diverge entre modèle et migration est invisible aux tests et
vivant en prod — ce fichier rend la divergence visible.

Deux preuves, sur un schéma construit DEPUIS LES MIGRATIONS (alembic
upgrade head sur une base vierge, jamais metadata.create_all) :

1. L'INSERT MINIMAL, en SQL BRUT — jamais l'ORM, qui injecte les défauts
   du modèle et masquerait exactement ce bug. On fournit ce qu'un client
   doit fournir (les colonnes sans AUCUN défaut au modèle, plus la PK,
   toujours posée côté client) et on OMET tout ce que le modèle promet en
   server_default : si la base ne le défaute pas, l'INSERT échoue.
2. LA COMPARAISON MODÈLE↔MIGRATION, la garde durable : toute colonne dont
   le modèle porte un server_default doit en porter un en base ; toute
   colonne à défaut Python doit en porter un en base OU figurer dans la
   liste des choix commentés ci-dessous.

CONTRE-ÉPREUVE (07/08) : ce test a été exécuté AVANT la migration
corrective r8b4d0f6a2c8 — ROUGE sur les deux preuves pour
agency_profile_section — puis APRÈS — VERT. Le contrôle a été éprouvé
avant qu'on lui fasse confiance.

Périmètre : les tables créées/modifiées par le train v0.110.0.
"""

import os
import uuid

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from alembic import command

pytestmark = pytest.mark.migration

# Les tables du train v0.110.0, avec l'INSERT MINIMAL de chacune : la PK
# (toujours fournie côté client — voir PYTHON_DEFAULT_OPTOUTS) et les
# colonnes sans aucun défaut au modèle. TOUT LE RESTE est omis exprès :
# c'est le server_default qu'on éprouve.
TRAIN_TABLES: dict[str, dict[str, object]] = {
    "agency_profile_section": {
        "id": lambda: str(uuid.uuid4()),
        "agency_id": "AGENCY",
        "surface": "person",
        "key": "probe_section",
    },
    "company_field_definition": {
        "id": lambda: str(uuid.uuid4()),
        "agency_id": "AGENCY",
        "key": "probe_field",
        "label": "Probe",
    },
    # custom_field_definition : modifiée par q5e1a7c3f9b6 (données seules,
    # aucun DDL) — au périmètre pour la garde n°2 ; l'insert minimal doit
    # fournir scope (le défaut serveur a été retiré exprès, arbitrage du
    # 07/08 : un appelant qui se tait doit échouer bruyamment).
    "custom_field_definition": {
        "id": lambda: str(uuid.uuid4()),
        "agency_id": "AGENCY",
        "key": "probe_custom",
        "label": "Probe",
        "field_type": "text",
        # scope N'A AUCUN défaut, nulle part — l'arbitrage m7a3c9e1b5f4
        # (le silence doit échouer bruyamment) : toujours fourni.
        "scope": "case",
        # profile_section, required, position : défauts SERVEUR — omis,
        # c'est eux qu'on éprouve.
    },
}

# Les colonnes à DÉFAUT PYTHON dont l'absence de server_default est un
# CHOIX, commenté ici (la garde n°2 les saute) :
# - id (uuid4) : la PK est toujours posée côté client/ORM — un écrivain
#   SQL brut la fournit, comme ce test le fait lui-même ;
# (scope, custom_field_definition, n'a pas besoin d'opt-out : il n'a de
# défaut NI au modèle NI en base — l'arbitrage m7a3c9e1b5f4, cohérent des
# deux côtés, est exactement ce que la garde demande.)
PYTHON_DEFAULT_OPTOUTS: frozenset[tuple[str, str]] = frozenset(
    {
        ("agency_profile_section", "id"),
        ("company_field_definition", "id"),
        ("custom_field_definition", "id"),
    }
)


@pytest.fixture(scope="module")
def migrated_db():
    """Une base VIERGE amenée à head PAR LES MIGRATIONS — le schéma que
    dev et prod ont réellement, pas celui que les modèles promettent."""
    from src.core.config import get_settings

    saved = os.environ.get("DATABASE_URL_SYNC")
    with PostgresContainer("postgres:16-alpine") as pg:
        os.environ["DATABASE_URL_SYNC"] = pg.get_connection_url()
        get_settings.cache_clear()
        command.upgrade(Config("alembic.ini"), "head")
        engine = create_engine(pg.get_connection_url())
        try:
            yield engine
        finally:
            engine.dispose()
            if saved is None:
                os.environ.pop("DATABASE_URL_SYNC", None)
            else:
                os.environ["DATABASE_URL_SYNC"] = saved
            get_settings.cache_clear()


def _make_agency(conn) -> str:
    agency_id = str(uuid.uuid4())
    conn.execute(
        text(
            "INSERT INTO agency (id, name, slug, settings, created_at, updated_at)"
            " VALUES (:id, 'Probe', :slug, '{}', now(), now())"
        ),
        {"id": agency_id, "slug": f"probe-{agency_id[:8]}"},
    )
    return agency_id


@pytest.mark.parametrize("table", sorted(TRAIN_TABLES))
def test_minimal_raw_insert_succeeds_on_the_migrated_schema(migrated_db, table: str) -> None:
    """PREUVE 1 — l'INSERT que l'ORM émet réellement (défauts serveur
    omis), rejoué en SQL brut sur le schéma des migrations. Échoue en
    NOT NULL violation si une colonne promise en server_default n'en a
    pas en base — le 500 de prod, reproduit en test."""
    spec = TRAIN_TABLES[table]
    with migrated_db.begin() as conn:
        agency_id = _make_agency(conn)
        values = {
            column: (
                agency_id
                if provided == "AGENCY"
                else provided()
                if callable(provided)
                else provided
            )
            for column, provided in spec.items()
        }
        columns = ", ".join(values)
        placeholders = ", ".join(f":{c}" for c in values)
        row = conn.execute(
            text(
                f"INSERT INTO {table} ({columns}) VALUES ({placeholders})"  # noqa: S608
                " RETURNING created_at, updated_at"
            ),
            values,
        ).one()
        assert row.created_at is not None, f"{table}.created_at sans défaut serveur"
        assert row.updated_at is not None, f"{table}.updated_at sans défaut serveur"


def test_every_model_default_exists_in_the_migrated_schema(migrated_db) -> None:
    """PREUVE 2, LA GARDE DURABLE — le modèle et la migration disent la
    même chose sur chaque défaut. Un server_default du modèle absent de
    la base est EXACTEMENT la classe de l'incident ; un défaut Python
    sans défaut base est toléré seulement s'il est un choix commenté
    (PYTHON_DEFAULT_OPTOUTS)."""
    from shared.models.base import Base

    with migrated_db.begin() as conn:
        db_defaults = {
            (t, c): d
            for t, c, d in conn.execute(
                text(
                    "SELECT table_name, column_name, column_default"
                    " FROM information_schema.columns WHERE table_schema = 'public'"
                )
            )
        }
    problems: list[str] = []
    for table in sorted(TRAIN_TABLES):
        model = Base.metadata.tables[table]
        for column in model.columns:
            key = (table, column.name)
            if column.server_default is not None and db_defaults.get(key) is None:
                problems.append(f"{table}.{column.name}: server_default au modèle, rien en base")
            elif (
                column.default is not None
                and db_defaults.get(key) is None
                and key not in PYTHON_DEFAULT_OPTOUTS
            ):
                problems.append(
                    f"{table}.{column.name}: défaut Python au modèle, rien en base,"
                    " et pas dans les choix commentés"
                )
    assert not problems, "modèle ≠ migration sur les défauts :\n" + "\n".join(problems)
