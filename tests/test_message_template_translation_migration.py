"""Preuve de migration pour z2c7d4e0f6a8 (rail de traduction élargi) :
additive, CHECK « exactement une cible » réellement APPLIQUÉ (on insère,
on ne suppose pas), roundtrip propre sur un testcontainer dédié."""

import os
import uuid

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from alembic import command

PARENT = "y1b6c3d9e5f2"
THIS = "z2c7d4e0f6a8"


@pytest.fixture(scope="module")
def alembic_db():
    from src.core.config import get_settings

    saved = os.environ.get("DATABASE_URL_SYNC")
    with PostgresContainer("postgres:16-alpine") as pg:
        os.environ["DATABASE_URL_SYNC"] = pg.get_connection_url()
        get_settings.cache_clear()
        cfg = Config("alembic.ini")
        engine = create_engine(pg.get_connection_url())
        try:
            yield cfg, engine
        finally:
            engine.dispose()
            if saved is None:
                os.environ.pop("DATABASE_URL_SYNC", None)
            else:
                os.environ["DATABASE_URL_SYNC"] = saved
            get_settings.cache_clear()


def _column(engine, table: str, column: str) -> bool:
    with engine.begin() as c:
        return bool(
            c.execute(
                text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = :t AND column_name = :c"
                ),
                {"t": table, "c": column},
            ).scalar()
        )


def _nullable(engine, table: str, column: str) -> bool:
    with engine.begin() as c:
        return (
            c.execute(
                text(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_name = :t AND column_name = :c"
                ),
                {"t": table, "c": column},
            ).scalar()
            == "YES"
        )


def _seed_graph(engine) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """agence + parcours + modèle de message — le minimum pour viser les FK."""
    agency, journey, message = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO agency (id, name, slug, settings, created_at, updated_at) "
                "VALUES (:id, 'A', :slug, '{}', now(), now())"
            ),
            {"id": agency, "slug": f"mig-{agency.hex[:8]}"},
        )
        c.execute(
            text(
                "INSERT INTO journey_template (id, agency_id, name, created_at, updated_at) "
                "VALUES (:id, :agency, 'T', now(), now())"
            ),
            {"id": journey, "agency": agency},
        )
        c.execute(
            text(
                "INSERT INTO message_template (id, agency_id, name, body, created_at, updated_at) "
                "VALUES (:id, :agency, 'M', 'Bonjour {client_name}', now(), now())"
            ),
            {"id": message, "agency": agency},
        )
    return agency, journey, message


def test_translation_rail_roundtrip(alembic_db) -> None:
    cfg, engine = alembic_db
    command.upgrade(cfg, PARENT)
    assert not _column(engine, "message_template", "body_i18n")
    assert not _column(engine, "ai_translation_job", "message_template_id")
    assert not _nullable(engine, "ai_translation_job", "template_id")

    command.upgrade(cfg, THIS)
    assert _column(engine, "message_template", "body_i18n")
    for table in ("ai_translation_job", "ai_translation_source"):
        assert _column(engine, table, "message_template_id")
        assert _nullable(engine, table, "template_id")

    # Le CHECK, APPLIQUÉ et pas supposé : une ligne parcours passe, une ligne
    # message passe, une ligne SANS cible est refusée, une ligne aux DEUX
    # cibles aussi.
    agency, journey, message = _seed_graph(engine)

    def insert_job(template, message_template) -> None:
        with engine.begin() as c:
            c.execute(
                text(
                    "INSERT INTO ai_translation_job (id, agency_id, template_id, "
                    "message_template_id, status, langs, progress_done, progress_total, "
                    "translated_keys, points_charged, failed_keys, created_at, updated_at) "
                    "VALUES (:id, :agency, :t, :m, 'pending', '[]', 0, 0, 0, 0, '[]', "
                    "now(), now())"
                ),
                {"id": uuid.uuid4(), "agency": agency, "t": template, "m": message_template},
            )

    insert_job(journey, None)
    insert_job(None, message)
    for bad in ((None, None), (journey, message)):
        with pytest.raises(Exception, match="ck_ai_translation_job_one_target"):
            insert_job(*bad)

    # L'unique PARTIEL sur les lignes message : le doublon exact est refusé,
    # et il n'aveugle PAS les lignes parcours (message_template_id NULL).
    def insert_source(template, message_template, key="template.body", lang="en") -> None:
        with engine.begin() as c:
            c.execute(
                text(
                    "INSERT INTO ai_translation_source (id, agency_id, template_id, "
                    "message_template_id, content_key, lang, source_hash, output_hash, "
                    "created_at, updated_at) VALUES (:id, :agency, :t, :m, :k, :lang, "
                    "'s', 'o', now(), now())"
                ),
                {
                    "id": uuid.uuid4(),
                    "agency": agency,
                    "t": template,
                    "m": message_template,
                    "k": key,
                    "lang": lang,
                },
            )

    insert_source(None, message)
    with pytest.raises(Exception, match="uq_ai_translation_source_message_key"):
        insert_source(None, message)
    insert_source(journey, None)
    insert_source(journey, None, lang="es")  # les lignes parcours gardent LEUR unique

    # Réversibilité : les lignes message sont retirées (le downgrade ramène
    # l'état d'avant le rail), les lignes parcours survivent, template_id
    # redevient NOT NULL.
    command.downgrade(cfg, PARENT)
    assert not _column(engine, "message_template", "body_i18n")
    assert not _column(engine, "ai_translation_job", "message_template_id")
    assert not _nullable(engine, "ai_translation_job", "template_id")
    with engine.begin() as c:
        jobs = c.execute(text("SELECT count(*) FROM ai_translation_job")).scalar()
        sources = c.execute(text("SELECT count(*) FROM ai_translation_source")).scalar()
    assert jobs == 1 and sources == 2  # les lignes parcours, intactes

    command.upgrade(cfg, THIS)  # le roundtrip se rejoue sans heurt
    assert _column(engine, "message_template", "body_i18n")
