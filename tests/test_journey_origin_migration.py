"""Migration proof for b2d8f4a6c0e2 (journey_template.origin, note Eric
26/07) : additive + backfill des seedés identifiables, roundtrip propre sur
un testcontainer dédié. Le backfill est LA partie qui compte : bibliothèque
(is_sample), clone sectoriel offert (agency_id + sector), demo legacy (par
son nom, ici seulement) → 'seed' ; le parcours main-faite reste 'user'."""

import os
import uuid

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from alembic import command

PARENT = "a1c7e9f2b4d8"
THIS = "b2d8f4a6c0e2"


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


def _has_column(engine) -> bool:
    with engine.begin() as c:
        return bool(
            c.execute(
                text(
                    "SELECT 1 FROM information_schema.columns"
                    " WHERE table_name = 'journey_template' AND column_name = 'origin'"
                )
            ).scalar()
        )


def test_origin_roundtrip_and_backfill(alembic_db) -> None:
    cfg, engine = alembic_db

    command.upgrade(cfg, PARENT)
    assert not _has_column(engine)

    # Le décor pré-migration : une agence, ses 4 visages de parcours.
    agency_id = str(uuid.uuid4())
    rows = {
        "library": (None, True, "immigration", "Exemple : Visa longue durée"),
        "gift": (agency_id, False, "immigration", "[Exemple] Immigration"),
        "legacy": (agency_id, False, None, "Exemple : Installation à l'étranger"),
        "handmade": (agency_id, False, None, "Mon parcours à moi"),
    }
    ids = {key: str(uuid.uuid4()) for key in rows}
    with engine.begin() as c:
        c.execute(
            text("INSERT INTO agency (id, name, slug, settings) VALUES (:i, 'A', 'a-mig', '{}')"),
            {"i": agency_id},
        )
        for key, (aid, is_sample, sector, name) in rows.items():
            c.execute(
                text(
                    "INSERT INTO journey_template"
                    " (id, agency_id, is_sample, sector, name, name_i18n)"
                    " VALUES (:i, :a, :s, :sec, :n, '{}')"
                ),
                {"i": ids[key], "a": aid, "s": is_sample, "sec": sector, "n": name},
            )

    command.upgrade(cfg, THIS)
    assert _has_column(engine)
    with engine.begin() as c:
        origins = {
            key: c.execute(
                text("SELECT origin FROM journey_template WHERE id = :i"), {"i": ids[key]}
            ).scalar()
            for key in rows
        }
    assert origins == {
        "library": "seed",
        "gift": "seed",
        "legacy": "seed",
        "handmade": "user",
    }

    command.downgrade(cfg, PARENT)
    assert not _has_column(engine)
    command.upgrade(cfg, THIS)
    assert _has_column(engine)
